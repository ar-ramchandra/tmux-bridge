"""
Platform-aware system info — detected once at import, cached for runtime.
Works on Linux, macOS, and Raspberry Pi. No Windows support.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time

import psutil

# ── Platform detection (runs once at import) ──

PLATFORM = platform.system().lower()  # 'linux', 'darwin', 'freebsd'


def _detect_cpu_model() -> str:
    if PLATFORM == "linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
    elif PLATFORM == "darwin":
        try:
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                text=True, stderr=subprocess.DEVNULL, timeout=2,
            ).strip()
        except Exception:
            pass
    # Fallback — works everywhere but gives generic names
    return platform.processor() or platform.uname().processor or "unknown"


def _detect_gpu() -> str | None:
    if PLATFORM == "linux":
        try:
            out = subprocess.check_output(
                ["lspci"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            )
            for line in out.splitlines():
                if "VGA" in line or "3D controller" in line:
                    return line.split(":", 2)[-1].strip()
        except Exception:
            pass
    elif PLATFORM == "darwin":
        try:
            out = subprocess.check_output(
                ["system_profiler", "SPDisplaysDataType"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Chipset Model:") or line.startswith("Chip Model:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return None


def _detect_os_pretty() -> str:
    if PLATFORM == "linux":
        try:
            return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except Exception:
            pass
    elif PLATFORM == "darwin":
        ver = platform.mac_ver()[0]
        return f"macOS {ver}" if ver else "macOS"
    return platform.system()


def _detect_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


# ── Cached at import time ──

CPU_MODEL: str = _detect_cpu_model()
GPU_NAME: str | None = _detect_gpu()
OS_PRETTY: str = _detect_os_pretty()
HAS_NVIDIA: bool = _detect_nvidia()

# Partitions to skip (expand as needed)
_SKIP_MOUNTS = ("/snap", "/boot/efi", "/private/var/vm")


def get_specs() -> dict:
    """Full system specs. Static values are cached, dynamic values (load, uptime,
    disk usage) are fresh each call."""
    uname = platform.uname()
    mem = psutil.virtual_memory()
    cpu_freq = psutil.cpu_freq()

    try:
        load1, load5, load15 = os.getloadavg()
        load_str = f"{load1:.1f} / {load5:.1f} / {load15:.1f}"
    except OSError:
        load_str = "N/A"

    uptime_s = time.time() - psutil.boot_time()
    days, rem = divmod(int(uptime_s), 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    uptime_str = (f"{days}d " if days else "") + f"{hours}h {mins}m"

    disks = []
    for part in psutil.disk_partitions():
        if not part.fstype:
            continue
        if any(part.mountpoint.startswith(skip) for skip in _SKIP_MOUNTS):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "device": part.device,
                "fs": part.fstype,
                "total_gb": round(u.total / (1024**3), 1),
                "used_gb": round(u.used / (1024**3), 1),
                "percent": u.percent,
            })
        except Exception:
            pass

    return {
        "hostname": uname.node,
        "os": OS_PRETTY,
        "arch": uname.machine,
        "kernel": uname.release,
        "cpu_model": CPU_MODEL,
        "cpu_cores": psutil.cpu_count(logical=False),
        "cpu_threads": psutil.cpu_count(logical=True),
        "cpu_freq_mhz": round(cpu_freq.current) if cpu_freq else None,
        "memory_gb": round(mem.total / (1024**3), 1),
        "swap_gb": round(psutil.swap_memory().total / (1024**3), 1),
        "disks": disks,
        "gpu": GPU_NAME,
        "load": load_str,
        "uptime": uptime_str,
    }
