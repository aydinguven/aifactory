"""Validate TLS material and ask the privileged ingress helper to apply it."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.config import settings


MAX_CERTIFICATE_BYTES = 256 * 1024
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_AGENT_CA_BYTES = 256 * 1024
CERTIFICATE_PATTERN = re.compile(
    br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


class IngressConfigurationError(ValueError):
    """The requested HTTPS configuration or uploaded TLS material is invalid."""


class IngressApplyError(RuntimeError):
    """The privileged ingress helper could not apply a valid configuration."""


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    not_after: str
    sha256: str


def normalize_https_hostname(value: str) -> str:
    hostname = value.strip().lower().rstrip(".")
    if not hostname or len(hostname) > 253:
        raise IngressConfigurationError("HTTPS hostname geçerli değil.")
    labels = hostname.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise IngressConfigurationError(
            "HTTPS hostname tam bir DNS adı olmalıdır (ör. aifactory.example.com)."
        )
    return hostname


def _dns_name_matches(pattern: str, hostname: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    hostname = hostname.lower().rstrip(".")
    if pattern == hostname:
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return hostname.endswith(f".{suffix}") and hostname.count(".") == suffix.count(".") + 1
    return False


def validate_certificate_pair(
    certificate_pem: bytes,
    private_key_pem: bytes,
    hostname: str,
) -> CertificateInfo:
    """Validate size, validity, SAN coverage, server usage, and key matching."""
    hostname = normalize_https_hostname(hostname)
    if not certificate_pem or len(certificate_pem) > MAX_CERTIFICATE_BYTES:
        raise IngressConfigurationError("Sertifika dosyası boş veya 256 KB sınırını aşıyor.")
    if not private_key_pem or len(private_key_pem) > MAX_PRIVATE_KEY_BYTES:
        raise IngressConfigurationError("Private key dosyası boş veya 64 KB sınırını aşıyor.")

    certificate_blocks = CERTIFICATE_PATTERN.findall(certificate_pem)
    if not certificate_blocks:
        raise IngressConfigurationError("Sertifika PEM formatında olmalıdır.")
    if CERTIFICATE_PATTERN.sub(b"", certificate_pem).strip():
        raise IngressConfigurationError(
            "Sertifika dosyası yalnızca PEM sertifika blokları içermelidir."
        )
    try:
        certificates = [
            x509.load_pem_x509_certificate(block) for block in certificate_blocks
        ]
    except ValueError as exc:
        raise IngressConfigurationError("Sertifika PEM içeriği okunamadı.") from exc
    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    except TypeError as exc:
        raise IngressConfigurationError(
            "Şifreli private key desteklenmiyor; şifresiz PEM yükleyin."
        ) from exc
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise IngressConfigurationError("Private key PEM içeriği okunamadı.") from exc

    now = datetime.now(timezone.utc)
    if any(
        now < certificate.not_valid_before_utc
        or now > certificate.not_valid_after_utc
        for certificate in certificates
    ):
        raise IngressConfigurationError(
            "Sertifika zincirinde henüz geçerli olmayan veya süresi dolmuş sertifika var."
        )
    leaf = certificates[0]
    try:
        dns_names = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound as exc:
        raise IngressConfigurationError(
            "Sertifika Subject Alternative Name (SAN) içermelidir."
        ) from exc
    if not any(_dns_name_matches(name, hostname) for name in dns_names):
        raise IngressConfigurationError(
            f"Sertifika SAN alanı {hostname} hostname'ini kapsamıyor."
        )
    try:
        extended_usage = leaf.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
        if ExtendedKeyUsageOID.SERVER_AUTH not in extended_usage:
            raise IngressConfigurationError(
                "Sertifika TLS Web Server Authentication amacı taşımıyor."
            )
    except x509.ExtensionNotFound:
        pass

    certificate_key = leaf.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    uploaded_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if certificate_key != uploaded_key:
        raise IngressConfigurationError("Sertifika ve private key birbiriyle eşleşmiyor.")

    return CertificateInfo(
        subject=leaf.subject.rfc4514_string()[:1024],
        not_after=leaf.not_valid_after_utc.isoformat(),
        sha256=leaf.fingerprint(hashes.SHA256()).hex(),
    )


def certificate_public_key_pin(certificate_pem: bytes) -> str:
    """Return the curl-compatible SHA-256 SPKI pin for the leaf certificate."""
    certificate_blocks = CERTIFICATE_PATTERN.findall(certificate_pem)
    if not certificate_blocks:
        raise IngressConfigurationError("Sertifika PEM formatında olmalıdır.")
    try:
        leaf = x509.load_pem_x509_certificate(certificate_blocks[0])
    except ValueError as exc:
        raise IngressConfigurationError("Sertifika PEM içeriği okunamadı.") from exc
    public_key = leaf.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(public_key).digest()
    return "sha256//" + base64.b64encode(digest).decode("ascii")


def validate_agent_ca_bundle(
    certificate_pem: bytes,
    label: str = "Worker CA bundle",
) -> str:
    """Validate a public CA-only PEM bundle and return its content digest."""
    if not certificate_pem or len(certificate_pem) > MAX_AGENT_CA_BYTES:
        raise IngressConfigurationError(
            f"{label} dosyası boş veya 256 KB sınırını aşıyor."
        )
    certificate_blocks = CERTIFICATE_PATTERN.findall(certificate_pem)
    if not certificate_blocks or CERTIFICATE_PATTERN.sub(b"", certificate_pem).strip():
        raise IngressConfigurationError(
            f"{label} yalnızca PEM sertifika blokları içermelidir."
        )
    try:
        certificates = [
            x509.load_pem_x509_certificate(block) for block in certificate_blocks
        ]
    except ValueError as exc:
        raise IngressConfigurationError(
            f"{label} PEM içeriği okunamadı."
        ) from exc

    now = datetime.now(timezone.utc)
    for certificate in certificates:
        if now < certificate.not_valid_before_utc or now > certificate.not_valid_after_utc:
            raise IngressConfigurationError(
                f"{label} süresi dolmuş veya henüz geçerli olmayan sertifika içeriyor."
            )
        try:
            constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
        except x509.ExtensionNotFound as exc:
            raise IngressConfigurationError(
                f"{label} içindeki her sertifika CA Basic Constraints taşımalıdır."
            ) from exc
        if not constraints.ca:
            raise IngressConfigurationError(
                f"{label} yalnızca kök ve ara CA sertifikaları içerebilir."
            )
        try:
            key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            key_usage = None
        if key_usage is not None and not key_usage.key_cert_sign:
            raise IngressConfigurationError(
                f"{label} sertifikası sertifika imzalama yetkisi taşımıyor."
            )

    normalized = certificate_pem.strip() + b"\n"
    return hashlib.sha256(normalized).hexdigest()


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class IngressManager:
    def __init__(self, staging_root: Path | None = None, helper: Path | None = None):
        self.staging_root = staging_root or Path(settings.INGRESS_STAGING_ROOT)
        self.helper = helper or Path(settings.INGRESS_APPLY_COMMAND)
        self.certificate_path = self.staging_root / "certificate.pem"
        self.private_key_path = self.staging_root / "private-key.pem"
        self.agent_ca_path = self.staging_root / "agent-ca.pem"
        self.desired_path = self.staging_root / "desired.json"
        self.request_path = self.staging_root / "apply.request"
        self.result_path = self.staging_root / "apply-result.json"
        self._lock = asyncio.Lock()

    async def _run_helper(self, request_id: str) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            if not self.helper.is_file():
                raise IngressApplyError(
                    "HTTPS uygulama yardımcısı kurulu değil. "
                    "Sunucuda deploy/install_ingress.sh scriptini çalıştırın."
                )
            try:
                process = await asyncio.create_subprocess_exec(
                    str(self.helper),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as exc:
                raise IngressApplyError(f"HTTPS yardımcısı başlatılamadı: {exc}") from exc
            output, _ = await process.communicate()
            message = output.decode("utf-8", errors="replace").strip()
            if process.returncode:
                raise IngressApplyError(
                    message
                    or f"HTTPS yardımcısı {process.returncode} koduyla başarısız oldu."
                )
            return

        _atomic_write(self.request_path, f"{request_id}\n".encode("ascii"), 0o600)
        for _ in range(200):
            await asyncio.sleep(0.1)
            try:
                result = json.loads(self.result_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                continue
            if result.get("request_id") != request_id:
                continue
            if result.get("success") is True:
                return
            raise IngressApplyError(
                str(result.get("message") or "Nginx yapılandırması uygulanamadı.")
            )
        raise IngressApplyError(
            "Nginx uygulama servisi yanıt vermedi. "
            "aifactory-ingress.path ve aifactory-ingress.service durumunu kontrol edin."
        )

    async def apply(
        self,
        *,
        https_enabled: bool,
        hostname: str,
        http_fallback_enabled: bool,
        certificate_pem: bytes | None = None,
        private_key_pem: bytes | None = None,
        agent_ca_pem: bytes | None = None,
    ) -> CertificateInfo | None:
        async with self._lock:
            hostname = normalize_https_hostname(hostname)
            if (certificate_pem is None) != (private_key_pem is None):
                raise IngressConfigurationError(
                    "Sertifika ve private key birlikte yüklenmelidir."
                )

            paths = (
                self.certificate_path,
                self.private_key_path,
                self.agent_ca_path,
                self.desired_path,
            )
            previous = {
                path: path.read_bytes() if path.is_file() else None for path in paths
            }
            info = None
            try:
                if agent_ca_pem is not None:
                    validate_agent_ca_bundle(agent_ca_pem)
                    _atomic_write(
                        self.agent_ca_path,
                        agent_ca_pem.strip() + b"\n",
                        0o600,
                    )
                elif self.agent_ca_path.is_file():
                    validate_agent_ca_bundle(self.agent_ca_path.read_bytes())

                if certificate_pem is not None and private_key_pem is not None:
                    info = validate_certificate_pair(
                        certificate_pem, private_key_pem, hostname
                    )
                    _atomic_write(
                        self.certificate_path, certificate_pem.strip() + b"\n", 0o600
                    )
                    _atomic_write(
                        self.private_key_path, private_key_pem.strip() + b"\n", 0o600
                    )
                elif self.certificate_path.is_file() and self.private_key_path.is_file():
                    info = validate_certificate_pair(
                        self.certificate_path.read_bytes(),
                        self.private_key_path.read_bytes(),
                        hostname,
                    )
                elif https_enabled:
                    raise IngressConfigurationError(
                        "HTTPS'i etkinleştirmek için sertifika ve private key yükleyin."
                    )

                request_id = str(uuid.uuid4())
                desired = {
                    "https_enabled": https_enabled,
                    "hostname": hostname,
                    "http_fallback_enabled": http_fallback_enabled,
                    "request_id": request_id,
                }
                _atomic_write(
                    self.desired_path,
                    (json.dumps(desired, sort_keys=True) + "\n").encode("utf-8"),
                    0o600,
                )
                await self._run_helper(request_id)
                return info
            except Exception:
                for path, content in previous.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, content, 0o600)
                raise


ingress_manager = IngressManager()
