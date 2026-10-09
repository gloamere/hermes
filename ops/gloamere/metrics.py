"""Linux resource, thermal, SMART, and service probes."""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def netdata_value(chart: str, *, group: str = "average", after: int = -60) -> float | None:
    """Read a one-point aggregate from the local Netdata database."""
    query = urllib.parse.urlencode({"chart": chart, "after": after, "points": 1,
                                   "group": group, "format": "json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:19999/api/v1/data?" + query, timeout=8) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not data.get("data"):
        return None
    values = [value for label, value in zip(data["labels"][1:], data["data"][0][1:])
              if label != "idle" and isinstance(value, (int, float))]
    return sum(values) if values else None


def temperatures(root: Path = Path("/sys/class/hwmon")) -> dict:
    """Read CPU and NVMe temperatures in Celsius from kernel hwmon sensors."""
    readings = {"cpu": [], "ssd": []}
    for device in root.glob("hwmon*"):
        name = (device / "name").read_text().strip()
        category = {"coretemp": "cpu", "k10temp": "cpu", "nvme": "ssd"}.get(name)
        if category:
            for value in device.glob("temp*_input"):
                readings[category].append(float(value.read_text()) / 1000)
    return {name: max(values) if values else None for name, values in readings.items()}


def smart(device: str) -> dict:
    """Probe NVMe health without requesting unsupported self-test logs."""
    result = subprocess.run(["smartctl", "-H", "-A", "-j", device], capture_output=True,
                            timeout=20, check=False)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {"readable": False}
    health = data.get("nvme_smart_health_information_log", {})
    return {"readable": bool(health), "passed": data.get("smart_status", {}).get("passed"),
            "critical_warning": health.get("critical_warning"),
            "wear_percent": health.get("percentage_used"), "media_errors": health.get("media_errors"),
            "available_spare": health.get("available_spare")}


def services(names: list[str]) -> dict:
    """Get systemd health and restart counters for the watched services."""
    statuses = {}
    for name in names:
        result = subprocess.run(["systemctl", "show", name, "--property=ActiveState,SubState,NRestarts"],
                                capture_output=True, text=True, timeout=10, check=False)
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        statuses[name] = {"active": properties.get("ActiveState") == "active",
                          "state": properties.get("ActiveState", "unknown"),
                          "restarts": int(properties.get("NRestarts", "0"))}
    return statuses


def snapshot(config: dict) -> dict:
    """Collect a small resource sample; Netdata owns full performance history."""
    memory = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) * 1024
    disk = shutil.disk_usage(config["workspace"])
    thermal = temperatures()
    return {"cpu_percent": netdata_value("system.cpu"),
            "load": Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3],
            "memory_used": memory["MemTotal"] - memory["MemAvailable"],
            "memory_total": memory["MemTotal"],
            "memory_percent": 100 * (1 - memory["MemAvailable"] / memory["MemTotal"]),
            "disk_used": disk.used, "disk_total": disk.total,
            "disk_percent": 100 * disk.used / disk.total,
            "cpu_temperature": thermal["cpu"], "ssd_temperature": thermal["ssd"],
            "uptime_seconds": float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]),
            "services": services(config["services"]), "smart": smart(config["nvme_device"])}
