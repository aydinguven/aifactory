"""Read-only NVIDIA GPU discovery for worker capability reporting.

AI Factory never installs or changes the host NVIDIA stack. The bootstrap script
validates it before enrollment; this module keeps reporting the same runtime
state so administrators can see drift after installation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
NVIDIA_VENDOR_ID = "0x10de"


def nvidia_hardware_present(
    devices_root: Path = Path("/sys/bus/pci/devices"),
) -> bool:
    """Return whether sysfs exposes an NVIDIA display/3D PCI device."""
    try:
        vendor_files = devices_root.glob("*/vendor")
    except OSError:
        return False
    for vendor_file in vendor_files:
        try:
            vendor = vendor_file.read_text(encoding="ascii").strip().lower()
            device_class = (vendor_file.parent / "class").read_text(
                encoding="ascii"
            ).strip().lower()
        except OSError:
            continue
        if vendor == NVIDIA_VENDOR_ID and device_class.startswith("0x03"):
            return True
    return False


def _run(command: list[str], runner: CommandRunner) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _number(value: str, *, integer: bool = False) -> int | float:
    value = value.strip()
    try:
        return int(float(value)) if integer else round(float(value), 1)
    except (TypeError, ValueError):
        return 0


def parse_nvidia_smi_csv(output: str) -> list[dict]:
    """Parse the stable no-header query output used by discovery."""
    devices: list[dict] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 9:
            continue
        index, device_id, model, total, used, util, temp, driver, mig_mode = parts
        devices.append(
            {
                "vendor": "nvidia",
                "kind": "physical",
                "index": int(_number(index, integer=True)),
                "id": device_id,
                "model": model,
                "memory_mb": int(_number(total, integer=True)),
                "memory_used_mb": int(_number(used, integer=True)),
                "utilization_percent": float(_number(util)),
                "temperature_c": float(_number(temp)),
                "driver_version": driver,
                "mig_enabled": mig_mode.lower() == "enabled",
            }
        )
    return devices


def parse_nvidia_smi_list(output: str) -> list[dict]:
    """Parse MIG instances from nvidia-smi -L output."""
    current_gpu_id = ""
    instances: list[dict] = []
    gpu_pattern = re.compile(r"^GPU\s+\d+:.*\(UUID:\s*(GPU-[^)]+)\)\s*$")
    mig_pattern = re.compile(
        r"^\s+MIG\s+(.+?)\s+Device\s+(\d+):\s*\(UUID:\s*(MIG-[^)]+)\)\s*$"
    )
    for line in output.splitlines():
        gpu_match = gpu_pattern.match(line)
        if gpu_match:
            current_gpu_id = gpu_match.group(1)
            continue
        mig_match = mig_pattern.match(line)
        if not mig_match:
            continue
        profile, device_index, device_id = mig_match.groups()
        memory_match = re.search(r"(\d+)gb", profile.lower())
        instances.append(
            {
                "vendor": "nvidia",
                "kind": "mig",
                "index": int(device_index),
                "id": device_id,
                "parent_id": current_gpu_id,
                "model": "NVIDIA MIG",
                "profile": profile,
                "memory_mb": int(memory_match.group(1)) * 1024 if memory_match else 0,
                "memory_used_mb": 0,
                "utilization_percent": 0.0,
                "temperature_c": 0.0,
                "mig_enabled": True,
            }
        )
    return instances


def parse_cdi_devices(output: str) -> set[str]:
    """Extract fully-qualified NVIDIA CDI device names from command output."""
    return {
        match.group(0).rstrip(",")
        for match in re.finditer(r"nvidia\.com/gpu=[A-Za-z0-9_.:/-]+", output)
    }


def _version_line(output: str) -> str:
    return next((line.strip() for line in output.splitlines() if line.strip()), "")[:160]


def discover_nvidia_capabilities(
    *,
    runner: CommandRunner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    devices_root: Path = Path("/sys/bus/pci/devices"),
) -> dict:
    """Return a JSON-safe accelerator/runtime capability snapshot."""
    hardware_present = nvidia_hardware_present(devices_root)
    smi_path = which("nvidia-smi")
    if not hardware_present and not smi_path:
        return {
            "accelerators": [],
            "accelerator_runtime": {
                "nvidia": {
                    "status": "not_detected",
                    "message": "NVIDIA GPU algılanmadı.",
                    "cdi_ready": False,
                }
            },
        }
    if not smi_path:
        return _error_snapshot("NVIDIA GPU algılandı ancak nvidia-smi bulunamadı.")

    query = _run(
        [
            smi_path,
            "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version,mig.mode.current",
            "--format=csv,noheader,nounits",
        ],
        runner,
    )
    if query.returncode != 0:
        detail = _version_line(query.stderr or query.stdout)
        return _error_snapshot(
            "NVIDIA sürücüsü GPU sorgusuna yanıt vermedi."
            + (f" {detail}" if detail else "")
        )
    physical = parse_nvidia_smi_csv(query.stdout)
    if not physical:
        return _error_snapshot("nvidia-smi kullanılabilir GPU döndürmedi.")

    listed = _run([smi_path, "-L"], runner)
    mig_devices = parse_nvidia_smi_list(listed.stdout) if listed.returncode == 0 else []

    toolkit_path = which("nvidia-ctk")
    toolkit_version = ""
    cdi_devices: set[str] = set()
    runtime_error = ""
    if not toolkit_path:
        runtime_error = "NVIDIA Container Toolkit (nvidia-ctk) bulunamadı."
    else:
        version = _run([toolkit_path, "--version"], runner)
        toolkit_version = _version_line(version.stdout or version.stderr)
        cdi = _run([toolkit_path, "cdi", "list"], runner)
        if cdi.returncode == 0:
            cdi_devices = parse_cdi_devices(f"{cdi.stdout}\n{cdi.stderr}")
        if not cdi_devices:
            runtime_error = "NVIDIA CDI aygıtı bulunamadı; CDI yapılandırmasını kontrol edin."

    accelerators = [*physical, *mig_devices]
    physical_by_id = {device["id"]: device for device in physical}
    for device in accelerators:
        candidates = [
            f"nvidia.com/gpu={device['id']}",
            f"nvidia.com/gpu={device['index']}",
        ]
        cdi_name = next((name for name in candidates if name in cdi_devices), "")
        device["cdi_name"] = cdi_name
        device["healthy"] = bool(cdi_name)
        device["allocatable"] = bool(cdi_name) and not (
            device["kind"] == "physical" and device.get("mig_enabled")
        )
        if device["kind"] == "mig":
            parent = physical_by_id.get(str(device.get("parent_id") or ""))
            if parent:
                device["model"] = str(parent.get("model") or "NVIDIA MIG")
                device["driver_version"] = str(parent.get("driver_version") or "")

    driver_version = str(physical[0].get("driver_version") or "")
    cdi_ready = any(device.get("allocatable") for device in accelerators)
    if not runtime_error and not cdi_ready:
        runtime_error = "GPU bulundu ancak allocatable bir CDI aygıtı yok."
    status = "ready" if not runtime_error and cdi_ready else "error"
    return {
        "accelerators": accelerators,
        "accelerator_runtime": {
            "nvidia": {
                "status": status,
                "message": runtime_error or f"{len(physical)} NVIDIA GPU kullanıma hazır.",
                "driver_version": driver_version,
                "toolkit_version": toolkit_version,
                "cdi_ready": cdi_ready,
                "physical_device_count": len(physical),
                "mig_device_count": len(mig_devices),
            }
        },
    }


def _error_snapshot(message: str) -> dict:
    return {
        "accelerators": [],
        "accelerator_runtime": {
            "nvidia": {
                "status": "error",
                "message": message[:500],
                "cdi_ready": False,
            }
        },
    }
