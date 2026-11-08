import asyncio
import logging
import re
import secrets
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.base import AuthProvider
from app.auth.internal import InternalAuthProvider, hash_password
from app.config import settings
from app.models.directory_settings import DirectorySettings
from app.models.user import User, UserRole
from app.schemas.directory import DirectorySettingsUpdate
from app.schemas.user import UserCreate
from app.security.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret

logger = logging.getLogger("aifactory.auth.ldap")


class DirectoryConfigurationError(ValueError):
    pass


class DirectoryConnectionError(RuntimeError):
    pass


class DirectoryUnavailableError(RuntimeError):
    """The directory could not be queried (network, TLS or service-bind failure)."""


class DirectoryLoginRejected(RuntimeError):
    """The directory recognised the user but refused the login for a known reason."""


# Active Directory reports the reason of a failed simple bind as an
# "AcceptSecurityContext error, data <code>" sub-code.
_AD_BIND_REASONS = {
    "530": "Bu saatte oturum açma izniniz yok.",
    "531": "Bu bilgisayardan oturum açma izniniz yok.",
    "532": "Kurumsal dizin parolanızın süresi dolmuş. Parolanızı yenileyip tekrar deneyin.",
    "533": "Kurumsal dizin hesabınız devre dışı.",
    "701": "Kurumsal dizin hesabınızın süresi dolmuş.",
    "773": "Kurumsal dizin parolanızı değiştirmeniz gerekiyor. Parolanızı yenileyip tekrar deneyin.",
    "775": "Kurumsal dizin hesabınız kilitlendi. Kilit açıldıktan sonra yeni parolanızla deneyin.",
}
_AD_DATA_CODE = re.compile(r"\bdata\s+([0-9a-fA-F]{3,4})\b")


def ad_bind_reason(message: str) -> tuple[str, str | None]:
    """Return the AD sub-code and user-facing reason embedded in a bind error."""
    match = _AD_DATA_CODE.search(message or "")
    if not match:
        return "", None
    code = match.group(1).lower()
    return code, _AD_BIND_REASONS.get(code)


def _safe_unbind(connection) -> None:
    try:
        connection.unbind()
    except Exception:  # noqa: BLE001 - closing a broken socket must not mask the bind error
        logger.debug("Ignoring LDAP unbind failure", exc_info=True)


@dataclass(frozen=True)
class DirectoryConfig:
    server_host: str
    server_port: int
    use_ssl: bool
    validate_tls: bool
    ca_cert_file: str
    connect_timeout_seconds: int
    bind_dn: str
    bind_password: str
    user_base_dn: str
    user_filter: str
    username_attribute: str
    email_attribute: str
    display_name_attribute: str
    team_attribute: str
    directorate_attribute: str
    group_membership_attribute: str
    required_group_dn: str
    admin_group_dn: str
    nested_group_search: bool
    organization_unit_attribute: str = ""
    manager_attribute: str = "manager"
    title_attribute: str = "title"
    unit_head_titles: str = "MÜDÜR"
    division_head_titles: str = "GENEL MÜDÜR"


@dataclass(frozen=True)
class DirectoryPerson:
    """The organization attributes of one directory entry in a manager chain."""

    dn: str
    team: str
    title: str
    directorate: str
    manager_dn: str


# A real AD chain is a handful of levels; the cap only guards broken data.
MAX_MANAGER_CHAIN_DEPTH = 12


def fold_directory_text(value: str | None) -> str:
    """Normalize a title or unit name for matching, tolerant of Turkish i forms.

    Python's casefold maps "İ" to "i" plus a combining dot and "I" to "i", so
    "MÜDÜR YARDIMCISI" and "Müdür Yardımcısı" would differ. All i variants fold
    to a plain "i" instead.
    """
    text = " ".join((value or "").split())
    for variant in ("İ", "I", "ı"):
        text = text.replace(variant, "i")
    return text.casefold().replace("\u0307", "")


_TITLE_QUALIFIER = re.compile(r"\([^)]*\)")


def fold_title(value: str | None) -> str:
    """Normalize a job title for matching, ignoring qualifiers in parentheses.

    An acting head is titled e.g. "MÜDÜR (TEDVİR)" or "MÜDÜR (V)" in AD and
    must match the configured "MÜDÜR".
    """
    return fold_directory_text(_TITLE_QUALIFIER.sub(" ", value or ""))


def parse_unit_head_titles(raw: str | None) -> frozenset[str]:
    """Comma-separated unit-head titles from the settings, normalized."""
    keys = (fold_title(part) for part in (raw or "").split(","))
    return frozenset(key for key in keys if key)


# Why a manager chain ended without a müdürlük.
CHAIN_UNIT = "unit"  # found a unit head
CHAIN_DIVISION_HEAD = "division_head"  # reached the Genel Müdür first
CHAIN_NO_MANAGER = "no_manager"  # the person has no manager in AD
CHAIN_NO_HEAD = "no_head"  # the chain tops out without a unit head
CHAIN_MISSING_MANAGER = "missing_manager"  # a manager DN is not readable
CHAIN_OTHER_DIVISION = "other_division"  # a manager sits in another division
CHAIN_LOOP = "loop"  # a manager loop or the depth cap


@dataclass(frozen=True)
class ChainResult:
    unit: str
    reason: str
    head: DirectoryPerson | None = None


def walk_manager_chain(
    person: DirectoryPerson,
    lookup: Callable[[str], DirectoryPerson | None],
    head_titles: frozenset[str],
    division_head_titles: frozenset[str] = frozenset(),
) -> ChainResult:
    """Find the nearest unit head in ``person``'s manager chain.

    The person counts as their own head (a MÜDÜR's müdürlük is their own team).
    The walk stops without a unit at a division head (the team then belongs
    directly to the Genel Müdürlük), a missing manager, a manager loop, the
    depth cap, or a manager in another directorate, so a chain never climbs
    past the Genel Müdürlük into another organization. A blank ``division`` on
    either side is not a different directorate.
    """
    if not head_titles:
        return ChainResult("", CHAIN_NO_HEAD)
    directorate = fold_directory_text(person.directorate)
    seen: set[str] = set()
    current = person
    for _ in range(MAX_MANAGER_CHAIN_DEPTH + 1):
        title = fold_title(current.title)
        if title in head_titles:
            return ChainResult(current.team, CHAIN_UNIT, current)
        if title in division_head_titles:
            return ChainResult("", CHAIN_DIVISION_HEAD, current)
        seen.add(current.dn.strip().casefold())
        manager_dn = current.manager_dn.strip()
        if not manager_dn:
            return ChainResult("", CHAIN_NO_MANAGER if current is person else CHAIN_NO_HEAD)
        if manager_dn.casefold() in seen:
            return ChainResult("", CHAIN_LOOP)
        manager = lookup(manager_dn)
        if manager is None:
            return ChainResult("", CHAIN_MISSING_MANAGER)
        # A blank division in AD does not end the chain; only a different one does.
        manager_directorate = fold_directory_text(manager.directorate)
        if directorate and manager_directorate and manager_directorate != directorate:
            return ChainResult("", CHAIN_OTHER_DIVISION)
        directorate = directorate or manager_directorate
        current = manager
    return ChainResult("", CHAIN_LOOP)


def derive_organization_unit(
    person: DirectoryPerson,
    lookup: Callable[[str], DirectoryPerson | None],
    head_titles: frozenset[str],
) -> str:
    """The team of the nearest unit head in ``person``'s manager chain, or ""."""
    return walk_manager_chain(person, lookup, head_titles).unit


@dataclass(frozen=True)
class DirectoryIdentity:
    username: str
    email: str
    full_name: str
    team: str
    directorate: str
    user_dn: str
    groups: tuple[str, ...]
    is_admin: bool
    organization_unit: str = ""
    managed_unit: str = ""


def encrypt_directory_secret(secret: str) -> str:
    return encrypt_secret(secret)


def decrypt_directory_secret(token: str) -> str:
    try:
        return decrypt_secret(token)
    except SecretDecryptionError as exc:
        raise DirectoryConfigurationError(
            "Kayıtlı bind parolası çözülemedi. Parolayı yeniden kaydedin."
        ) from exc


def config_from_record(record: DirectorySettings) -> DirectoryConfig:
    return DirectoryConfig(
        server_host=record.server_host,
        server_port=record.server_port,
        use_ssl=record.use_ssl,
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        connect_timeout_seconds=record.connect_timeout_seconds,
        bind_dn=record.bind_dn,
        bind_password=decrypt_directory_secret(record.encrypted_bind_password),
        user_base_dn=record.user_base_dn,
        user_filter=record.user_filter,
        username_attribute=record.username_attribute,
        email_attribute=record.email_attribute,
        display_name_attribute=record.display_name_attribute,
        team_attribute=record.team_attribute,
        directorate_attribute=record.directorate_attribute,
        organization_unit_attribute=record.organization_unit_attribute,
        manager_attribute=record.manager_attribute,
        title_attribute=record.title_attribute,
        unit_head_titles=record.unit_head_titles,
        division_head_titles=record.division_head_titles,
        group_membership_attribute=record.group_membership_attribute,
        required_group_dn=record.required_group_dn,
        admin_group_dn=record.admin_group_dn,
        nested_group_search=record.nested_group_search,
    )


def config_from_update(
    update: DirectorySettingsUpdate,
    saved_record: DirectorySettings | None = None,
) -> DirectoryConfig:
    bind_password = update.bind_password or ""
    if not bind_password and saved_record:
        bind_password = decrypt_directory_secret(saved_record.encrypted_bind_password)
    return DirectoryConfig(
        server_host=update.server_host,
        server_port=update.server_port,
        use_ssl=update.use_ssl,
        validate_tls=update.validate_tls,
        ca_cert_file=update.ca_cert_file,
        connect_timeout_seconds=update.connect_timeout_seconds,
        bind_dn=update.bind_dn,
        bind_password=bind_password,
        user_base_dn=update.user_base_dn,
        user_filter=update.user_filter,
        username_attribute=update.username_attribute,
        email_attribute=update.email_attribute,
        display_name_attribute=update.display_name_attribute,
        team_attribute=update.team_attribute,
        directorate_attribute=update.directorate_attribute,
        organization_unit_attribute=update.organization_unit_attribute,
        manager_attribute=update.manager_attribute,
        title_attribute=update.title_attribute,
        unit_head_titles=update.unit_head_titles,
        division_head_titles=update.division_head_titles,
        group_membership_attribute=update.group_membership_attribute,
        required_group_dn=update.required_group_dn,
        admin_group_dn=update.admin_group_dn,
        nested_group_search=update.nested_group_search,
    )


def validate_directory_config(config: DirectoryConfig) -> None:
    missing = []
    if not config.server_host:
        missing.append("sunucu")
    if not config.bind_dn:
        missing.append("bind DN")
    if not config.bind_password:
        missing.append("bind parolası")
    if not config.user_base_dn:
        missing.append("kullanıcı base DN")
    if missing:
        raise DirectoryConfigurationError(
            "Eksik LDAP ayarı: " + ", ".join(missing) + "."
        )


def _ldap3():
    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError as exc:
        raise DirectoryConnectionError(
            "LDAP desteği yüklü değil; ldap3 paketini kurun."
        ) from exc
    return ldap3, LDAPException, escape_filter_chars


def _server_for(config: DirectoryConfig):
    ldap3, _, _ = _ldap3()
    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED if config.validate_tls else ssl.CERT_NONE,
        ca_certs_file=config.ca_cert_file or None,
    )
    return ldap3.Server(
        config.server_host,
        port=config.server_port,
        use_ssl=config.use_ssl,
        tls=tls,
        connect_timeout=config.connect_timeout_seconds,
        get_info=ldap3.NONE,
    )


def _bound_connection(config: DirectoryConfig, user: str, password: str):
    ldap3, LDAPException, _ = _ldap3()
    try:
        connection = ldap3.Connection(
            _server_for(config),
            user=user,
            password=password,
            receive_timeout=config.connect_timeout_seconds,
            raise_exceptions=True,
        )
    except (LDAPException, OSError, ValueError) as exc:
        raise DirectoryConnectionError(str(exc)) from exc
    try:
        connection.open()
        connection.bind()
        return connection
    except (LDAPException, OSError) as exc:
        _safe_unbind(connection)
        raise DirectoryConnectionError(str(exc)) from exc


def test_directory_configuration(config: DirectoryConfig) -> tuple[str, int]:
    """Bind and perform a minimal base search without exposing credentials."""
    validate_directory_config(config)
    ldap3, LDAPException, _ = _ldap3()
    started = time.monotonic()
    connection = _bound_connection(config, config.bind_dn, config.bind_password)
    try:
        connection.search(
            search_base=config.user_base_dn,
            search_filter="(objectClass=*)",
            search_scope=ldap3.SUBTREE,
            attributes=[config.username_attribute],
            size_limit=1,
        )
    except LDAPException as exc:
        raise DirectoryConnectionError(
            f"Bind başarılı ancak kullanıcı tabanı aranamadı: {exc}"
        ) from exc
    finally:
        connection.unbind()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return "Bind ve kullanıcı tabanı araması başarılı.", elapsed_ms


def _entry_values(entry, attribute: str) -> list[str]:
    if not attribute or attribute not in entry.entry_attributes:
        return []
    values = entry[attribute].values
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        return [str(values)]
    return [str(value) for value in values]


def _entry_value(entry, attribute: str) -> str:
    values = _entry_values(entry, attribute)
    return values[0].strip() if values else ""


def _detects_unit_heads(config: DirectoryConfig) -> bool:
    return bool(config.title_attribute and parse_unit_head_titles(config.unit_head_titles))


def _derives_organization_unit(config: DirectoryConfig) -> bool:
    return bool(
        not config.organization_unit_attribute
        and config.manager_attribute
        and _detects_unit_heads(config)
    )


def _person_from_entry(entry, config: DirectoryConfig) -> DirectoryPerson:
    return DirectoryPerson(
        dn=entry.entry_dn,
        team=_entry_value(entry, config.team_attribute),
        title=_entry_value(entry, config.title_attribute),
        directorate=_entry_value(entry, config.directorate_attribute),
        manager_dn=_entry_value(entry, config.manager_attribute),
    )


def _manager_chain_lookup(connection, config: DirectoryConfig):
    """Return a DN -> DirectoryPerson lookup over the service connection."""
    ldap3, LDAPException, _ = _ldap3()
    attributes = [
        attribute
        for attribute in (
            config.team_attribute,
            config.title_attribute,
            config.directorate_attribute,
            config.manager_attribute,
        )
        if attribute
    ]

    def lookup(dn: str) -> DirectoryPerson | None:
        try:
            connection.search(
                search_base=dn,
                search_filter="(objectClass=*)",
                search_scope=ldap3.BASE,
                attributes=attributes,
                size_limit=1,
            )
        except LDAPException as exc:
            # A manager outside the bind account's view or a stale DN only
            # leaves the müdürlük empty; it must not fail the login.
            logger.info("Manager lookup for %s failed: %s", dn, exc)
            return None
        if not connection.entries:
            return None
        return _person_from_entry(connection.entries[0], config)

    return lookup


def _dn_equal(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _is_group_member(
    connection,
    config: DirectoryConfig,
    user_dn: str,
    direct_groups: list[str],
    target_group_dn: str,
) -> bool:
    if not target_group_dn:
        return False
    if any(_dn_equal(group, target_group_dn) for group in direct_groups):
        return True
    if not config.nested_group_search:
        return False

    ldap3, LDAPException, escape_filter_chars = _ldap3()
    try:
        # Active Directory's matching-rule-in-chain supports transitive membership.
        connection.search(
            search_base=target_group_dn,
            search_filter=(
                "(member:1.2.840.113556.1.4.1941:="
                f"{escape_filter_chars(user_dn)})"
            ),
            search_scope=ldap3.BASE,
            attributes=["distinguishedName"],
            size_limit=1,
        )
        return bool(connection.entries)
    except LDAPException:
        logger.info("Nested LDAP group lookup failed for %s", target_group_dn)
        return False


def authenticate_directory_user(
    config: DirectoryConfig, username: str, password: str
) -> DirectoryIdentity | None:
    validate_directory_config(config)
    if not username.strip() or not password:
        return None

    ldap3, LDAPException, escape_filter_chars = _ldap3()
    try:
        service_connection = _bound_connection(
            config, config.bind_dn, config.bind_password
        )
    except DirectoryConnectionError as exc:
        code, _ = ad_bind_reason(str(exc))
        logger.error(
            "LDAP service bind as %s failed (AD code %s): %s. If this is a "
            "personal account whose password changed, update the bind password "
            "in Admin > Directory; every login retries the stale password and "
            "can lock the account.",
            config.bind_dn,
            code or "-",
            exc,
        )
        raise DirectoryUnavailableError(str(exc)) from exc
    try:
        search_filter = config.user_filter.replace(
            "{username}", escape_filter_chars(username.strip())
        )
        attributes = list(
            dict.fromkeys(
                attribute
                for attribute in [
                    config.username_attribute,
                    config.email_attribute,
                    config.display_name_attribute,
                    config.team_attribute,
                    config.directorate_attribute,
                    config.organization_unit_attribute,
                    config.group_membership_attribute,
                    config.title_attribute if _detects_unit_heads(config) else "",
                    config.manager_attribute if _derives_organization_unit(config) else "",
                ]
                if attribute
            )
        )
        service_connection.search(
            search_base=config.user_base_dn,
            search_filter=search_filter,
            search_scope=ldap3.SUBTREE,
            attributes=attributes,
            size_limit=2,
        )
        if len(service_connection.entries) != 1:
            return None

        entry = service_connection.entries[0]
        user_dn = entry.entry_dn
        directory_username = _entry_value(entry, config.username_attribute) or username.strip()
        email = _entry_value(entry, config.email_attribute)
        full_name = _entry_value(entry, config.display_name_attribute) or directory_username
        team = _entry_value(entry, config.team_attribute)
        directorate = _entry_value(entry, config.directorate_attribute)
        organization_unit = _entry_value(entry, config.organization_unit_attribute)
        groups = _entry_values(entry, config.group_membership_attribute)
        head_titles = parse_unit_head_titles(config.unit_head_titles)
        if _derives_organization_unit(config):
            organization_unit = derive_organization_unit(
                _person_from_entry(entry, config),
                _manager_chain_lookup(service_connection, config),
                head_titles,
            )
        # A unit head manages their own müdürlük.
        is_unit_head = _detects_unit_heads(config) and (
            fold_title(_entry_value(entry, config.title_attribute)) in head_titles
        )
        managed_unit = organization_unit if is_unit_head else ""

        if config.required_group_dn and not _is_group_member(
            service_connection,
            config,
            user_dn,
            groups,
            config.required_group_dn,
        ):
            return None

        is_admin = _is_group_member(
            service_connection,
            config,
            user_dn,
            groups,
            config.admin_group_dn,
        )
    except (LDAPException, OSError) as exc:
        logger.error("LDAP user lookup failed: %s", exc)
        raise DirectoryUnavailableError(str(exc)) from exc
    finally:
        _safe_unbind(service_connection)

    try:
        user_connection = _bound_connection(config, user_dn, password)
    except DirectoryConnectionError as exc:
        code, reason = ad_bind_reason(str(exc))
        logger.info(
            "LDAP bind rejected for %s (AD code %s)", user_dn, code or "-"
        )
        if reason:
            raise DirectoryLoginRejected(reason) from exc
        return None
    _safe_unbind(user_connection)

    return DirectoryIdentity(
        username=directory_username,
        email=email,
        full_name=full_name,
        team=team,
        directorate=directorate,
        organization_unit=organization_unit,
        managed_unit=managed_unit,
        user_dn=user_dn,
        groups=tuple(groups),
        is_admin=is_admin,
    )


def _fit_identity_to_columns(identity: DirectoryIdentity) -> DirectoryIdentity:
    """Clip directory attributes to the user columns; PostgreSQL rejects overflow."""
    columns = User.__table__.columns

    def clip(value: str, column: str) -> str:
        return (value or "")[: columns[column].type.length]

    return replace(
        identity,
        username=clip(identity.username, "username"),
        email=clip(identity.email, "email"),
        full_name=clip(identity.full_name, "full_name"),
        team=clip(identity.team, "team"),
        directorate=clip(identity.directorate, "directorate"),
        organization_unit=clip(identity.organization_unit, "organization_unit"),
        managed_unit=clip(identity.managed_unit, "managed_unit"),
    )


async def _with_team_unit_fallback(
    db: AsyncSession, identity: DirectoryIdentity
) -> DirectoryIdentity:
    """Use the team's müdürlük from the last bulk sync when the own chain gave none."""
    if identity.organization_unit or not identity.team.strip():
        return identity
    from app.models.directory_team import DirectoryTeam
    from app.quotas import team_key

    row = (
        await db.execute(
            select(DirectoryTeam).where(DirectoryTeam.team_key == team_key(identity.team))
        )
    ).scalar_one_or_none()
    if row is None or not row.organization_unit:
        return identity
    return replace(identity, organization_unit=row.organization_unit)


class HybridAuthProvider(AuthProvider):
    """Internal auth fallback plus runtime-configured LDAP authentication."""

    def __init__(self) -> None:
        self.internal = InternalAuthProvider()

    @property
    def provider_name(self) -> str:
        return "hybrid"

    async def authenticate(
        self, username: str, password: str, db: AsyncSession
    ) -> User | None:
        local_user = await self.internal.authenticate(username, password, db)
        if local_user:
            return local_user

        record = await db.get(DirectorySettings, 1)
        if not record or not record.enabled:
            return None

        try:
            config = config_from_record(record)
            identity = await asyncio.to_thread(
                authenticate_directory_user, config, username, password
            )
        except DirectoryLoginRejected:
            raise
        except (
            DirectoryConfigurationError,
            DirectoryConnectionError,
            DirectoryUnavailableError,
        ) as exc:
            logger.error("LDAP authentication unavailable: %s", exc)
            raise DirectoryUnavailableError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never turn a login into a bare 500
            logger.exception("Unexpected LDAP authentication failure")
            raise DirectoryUnavailableError(str(exc)) from exc
        if not identity:
            return None
        identity = await _with_team_unit_fallback(db, identity)
        identity = _fit_identity_to_columns(identity)

        existing = (
            await db.execute(
                select(User).where(User.username == identity.username)
            )
        ).scalar_one_or_none()
        if existing and existing.auth_source != "active_directory":
            logger.warning(
                "LDAP login rejected because username %s belongs to a local account",
                identity.username,
            )
            return None

        role = UserRole.ADMIN if identity.is_admin else UserRole.USER
        if existing:
            existing.full_name = identity.full_name
            directory_email = identity.email.strip().lower()
            if directory_email and directory_email != existing.email:
                email_owner = (
                    await db.execute(
                        select(User).where(
                            User.email == directory_email,
                            User.id != existing.id,
                        )
                    )
                ).scalar_one_or_none()
                if not email_owner:
                    existing.email = directory_email
            existing.team = identity.team
            existing.directorate = identity.directorate
            existing.organization_unit = identity.organization_unit
            existing.managed_unit = identity.managed_unit
            existing.role = role
            existing.is_active = True
            user = existing
        else:
            email = identity.email.strip().lower()
            if not email:
                email = f"{identity.username.lower()}@directory.local"
            email_owner = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if email_owner:
                email = f"{identity.username.lower()}@directory.local"
            user = User(
                username=identity.username,
                email=email,
                hashed_password=hash_password(secrets.token_urlsafe(48)),
                full_name=identity.full_name,
                team=identity.team,
                directorate=identity.directorate,
                organization_unit=identity.organization_unit,
                managed_unit=identity.managed_unit,
                role=role,
                auth_source="active_directory",
                is_active=True,
            )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    async def create_user(
        self, user_in: UserCreate, db: AsyncSession, is_admin: bool = False
    ) -> User:
        return await self.internal.create_user(user_in, db, is_admin=is_admin)

    async def change_password(
        self, user: User, new_password: str, db: AsyncSession
    ) -> bool:
        if user.auth_source != "internal":
            return False
        return await self.internal.change_password(user, new_password, db)
