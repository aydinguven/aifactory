import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.ingress_settings import (
    IngressApplyError,
    IngressConfigurationError,
    IngressManager,
    normalize_https_hostname,
    validate_certificate_pair,
)
from deploy.apply_ingress import render_nginx_config


HOSTNAME = "aifactory.tcmb.gov.tr"


def certificate_pair(hostname: str = HOSTNAME) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, hostname)]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_certificate_pair_validation_checks_hostname_and_key_match():
    certificate, private_key = certificate_pair()
    info = validate_certificate_pair(certificate, private_key, HOSTNAME)
    assert info.subject == f"CN={HOSTNAME}"
    assert len(info.sha256) == 64

    _, other_key = certificate_pair()
    with pytest.raises(IngressConfigurationError, match="eşleşmiyor"):
        validate_certificate_pair(certificate, other_key, HOSTNAME)
    with pytest.raises(IngressConfigurationError, match="SAN"):
        validate_certificate_pair(certificate, private_key, "other.tcmb.gov.tr")
    with pytest.raises(IngressConfigurationError, match="yalnızca PEM"):
        validate_certificate_pair(
            certificate + b"unexpected-data",
            private_key,
            HOSTNAME,
        )


def test_https_hostname_is_normalized_and_rejects_config_injection():
    assert normalize_https_hostname(" AIFACTORY.TCMB.GOV.TR. ") == HOSTNAME
    for unsafe in ("localhost", "host name.example", "x;include.example", "*.tcmb.gov.tr"):
        with pytest.raises(IngressConfigurationError):
            normalize_https_hostname(unsafe)


@pytest.mark.asyncio
async def test_ingress_manager_stages_only_fixed_files_and_rolls_back(tmp_path, monkeypatch):
    certificate, private_key = certificate_pair()
    manager = IngressManager(
        staging_root=tmp_path / "ingress",
        helper=tmp_path / "aifactory-apply-ingress",
    )
    manager.helper.write_text("# helper", encoding="utf-8")
    applied_request_ids = []

    async def successful_helper(request_id: str):
        applied_request_ids.append(request_id)

    monkeypatch.setattr(manager, "_run_helper", successful_helper)
    info = await manager.apply(
        https_enabled=True,
        hostname=HOSTNAME,
        http_fallback_enabled=True,
        certificate_pem=certificate,
        private_key_pem=private_key,
    )
    assert info is not None
    assert applied_request_ids
    assert manager.certificate_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert manager.private_key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    desired = manager.desired_path.read_text(encoding="utf-8")
    assert HOSTNAME in desired
    assert "PRIVATE KEY" not in desired

    previous_desired = manager.desired_path.read_bytes()

    async def failed_helper(_request_id: str):
        raise IngressApplyError("nginx -t failed")

    monkeypatch.setattr(manager, "_run_helper", failed_helper)
    with pytest.raises(IngressApplyError, match="nginx -t failed"):
        await manager.apply(
            https_enabled=False,
            hostname=HOSTNAME,
            http_fallback_enabled=True,
        )
    assert manager.desired_path.read_bytes() == previous_desired


def test_nginx_config_preserves_http_fallback_and_workspace_websockets():
    fallback = render_nginx_config(HOSTNAME, True, True)
    assert "listen 80;" in fallback
    assert "listen 443 ssl;" in fallback
    assert fallback.count("proxy_pass http://127.0.0.1:8000;") == 2
    assert "proxy_set_header Upgrade $http_upgrade;" in fallback
    assert "proxy_set_header Connection $aifactory_connection_upgrade;" in fallback
    assert "map $http_upgrade $aifactory_connection_upgrade" in fallback
    assert "proxy_read_timeout 86400s;" in fallback
    assert "proxy_buffering off;" in fallback
    assert "Strict-Transport-Security" not in fallback

    redirect = render_nginx_config(HOSTNAME, True, False)
    assert f"return 308 https://{HOSTNAME}$request_uri;" in redirect
    assert redirect.count("proxy_pass http://127.0.0.1:8000;") == 1

    http_only = render_nginx_config(HOSTNAME, False, True)
    assert "listen 80;" in http_only
    assert "listen 443 ssl;" not in http_only
    assert "proxy_pass http://127.0.0.1:8000;" in http_only


@pytest.mark.asyncio
async def test_non_root_controller_uses_watcher_without_helper_binary(
    tmp_path, monkeypatch
):
    manager = IngressManager(
        staging_root=tmp_path / "ingress",
        helper=tmp_path / "missing-aifactory-apply-ingress",
    )
    manager.staging_root.mkdir()
    request_id = str(uuid.uuid4())
    manager.result_path.write_text(
        json.dumps({"request_id": request_id, "success": True}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)

    await manager._run_helper(request_id)

    assert manager.request_path.read_text(encoding="ascii") == f"{request_id}\n"
