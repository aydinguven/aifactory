"""Opt-in operating-system package upgrade before a queued platform update.

The root-owned queued updater runs this when the admin asked for it
("OS paketlerini de yükselt (dnf)"). It uses only the standard library: the
updater imports it with the system Python.

dnf runs in a transient systemd unit, not as a child of the updater:
aifactory-update.service sets NoNewPrivileges, which blocks the SELinux domain
transition RPM scriptlets need. The transient unit also lets an interrupted
updater leave the RPM transaction to finish instead of killing it halfway.

The upgrade runs before the platform installer. A failed dnf transaction is
rolled back by RPM itself, and the platform update is then not applied, so the
host keeps a consistent package set and platform version. The host is never
rebooted; whether it needs one is reported to the admin.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Callable

OS_UPGRADE_UNIT = "aifactory-os-upgrade"
# --refresh: worker hosts may have used stale metadata for weeks. Air-gapped
# hosts point at Satellite/Foreman or local file:// repositories (AIRGAP.md),
# which --refresh re-reads the same way.
DNF_UPGRADE_COMMAND = ("dnf", "-y", "upgrade", "--refresh")
NEEDS_RESTARTING_COMMAND = ("dnf", "-q", "needs-restarting", "-r")


def upgrade_command() -> list[str]:
    return [
        "systemd-run",
        f"--unit={OS_UPGRADE_UNIT}",
        "--description=AI Factory OS package upgrade",
        "--wait",
        "--pipe",
        "--quiet",
        "--collect",
        *DNF_UPGRADE_COMMAND,
    ]


def reboot_required(run: Callable | None = None) -> bool | None:
    """True/False from ``dnf needs-restarting -r``; None when it cannot tell."""
    run = run or subprocess.run
    try:
        result = run(
            list(NEEDS_RESTARTING_COMMAND),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # needs-restarting -r: 0 = no reboot needed, 1 = reboot needed.
    return {0: False, 1: True}.get(result.returncode)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_os_upgrade(log, progress, *, run: Callable | None = None) -> dict:
    """Run ``dnf -y upgrade --refresh`` and describe the result.

    ``log`` is the updater's live output file; ``progress`` its
    ProgressReporter. The returned dict is stored in status.json as
    ``os_upgrade_result`` and shown on the worker inventory.
    """
    run = run or subprocess.run
    started_at = _now()
    progress.os_packages()
    log.write(f"==> OS paketleri yükseltiliyor: {' '.join(DNF_UPGRADE_COMMAND)}\n")
    log.flush()
    try:
        returncode = run(
            upgrade_command(), text=True, stdout=log, stderr=subprocess.STDOUT
        ).returncode
        error = ""
    except OSError as exc:
        returncode = None
        error = str(exc)
    log.flush()
    if returncode != 0:
        reason = f"dnf çıkış kodu {returncode}" if returncode is not None else error
        message = (
            f"OS paket yükseltmesi başarısız ({reason}); platform güncellemesi "
            "uygulanmadı. Worker'ın DNF depolarını (Satellite/Foreman veya yerel "
            "depo) kontrol edip yeniden deneyin ya da OS seçeneği olmadan güncelleyin."
        )
        log.write(f"==> {message}\n")
        return {
            "requested": True,
            "state": "failed",
            "return_code": returncode,
            "message": message,
            "started_at": started_at,
            "finished_at": _now(),
        }
    reboot = reboot_required(run)
    if reboot is True:
        message = (
            "OS paketleri yükseltildi. Yeni çekirdek veya temel kütüphanelerin "
            "devreye girmesi için worker'ı yeniden başlatın (otomatik yeniden "
            "başlatılmaz)."
        )
    elif reboot is False:
        message = "OS paketleri yükseltildi; yeniden başlatma gerekmiyor."
    else:
        message = (
            "OS paketleri yükseltildi; yeniden başlatma ihtiyacı belirlenemedi "
            "('dnf needs-restarting' kullanılamıyor)."
        )
    log.write(f"==> {message}\n")
    log.flush()
    return {
        "requested": True,
        "state": "succeeded",
        "return_code": 0,
        "reboot_required": reboot,
        "message": message,
        "started_at": started_at,
        "finished_at": _now(),
    }
