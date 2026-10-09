#!/usr/bin/python3
"""Deduplicated server alerts and a daily Telegram report; no LLM calls."""
from __future__ import annotations

import argparse
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from common import load_config, notify, save_json
from metrics import snapshot


def conditions(sample: dict, config: dict, now: float) -> dict[str, str]:
    """Select concrete faults, tolerating the first installation's missing backup."""
    result = {}
    labels = {"cpu_percent": "CPU 使用率", "memory_percent": "内存使用率",
              "disk_percent": "磁盘使用率", "cpu_temperature": "CPU 温度", "ssd_temperature": "固态温度"}
    for key, limit in config["thresholds"].items():
        value = sample.get(key)
        if value is not None and value >= limit:
            result[key] = f"{labels.get(key, key)}：{value:.1f}，阈值 {limit}"
    for name, state in sample["services"].items():
        if not state["active"]:
            result["service:" + name] = f"{name} 状态：{state['state']}"
    health = sample["smart"]
    if not health.get("readable"):
        result["smart"] = "无法读取固态 SMART 健康状态"
    elif health.get("passed") is False or health.get("critical_warning"):
            result["smart"] = "固态 SMART 健康告警"
    backup = Path(config["backup_status"])
    if backup.exists():
        status = json.loads(backup.read_text(encoding="utf-8"))
        if status.get("status") != "success":
            result["backup"] = "最近一次备份失败，请检查 hermes-backup 日志"
        elif status.get("finished_at"):
            finished = datetime.fromisoformat(status["finished_at"]).timestamp()
            if now - finished > 36 * 3600:
                result["backup"] = "已超过 36 小时没有成功备份"
    return result


def reconcile_alerts(previous: dict, faults: dict, now: float, *, cooldown: int = 21600,
                     resource_delay: int = 600) -> tuple[dict, list[str]]:
    """Delay sustained resource load, send once per fault, and report recovery once."""
    active = {}
    messages = []
    for key, detail in faults.items():
        item = dict(previous.get(key, {"first_seen": now, "last_sent": 0}))
        delay = resource_delay if key in {"cpu_percent", "memory_percent"} else 0
        if now - item["first_seen"] >= delay and (not item["last_sent"] or now - item["last_sent"] >= cooldown):
            messages.append("⚠️ 服务器异常：" + detail)
            item["last_sent"] = now
        active[key] = item
    for key, item in previous.items():
        if key not in faults and item.get("last_sent"):
            messages.append("✅ 服务器已恢复：" + key)
    return active, messages


def report(sample: dict, config: dict) -> str:
    """Render a compact server report without exposing account credentials."""
    local = datetime.now(ZoneInfo(config["timezone"])).strftime("%m-%d %H:%M")
    gib = 1024 ** 3
    number = lambda value, suffix: "暂不可读" if value is None else f"{value:.1f}{suffix}"
    lines = [f"🖥️ 服务器简报 {local}",
             f"CPU {number(sample['cpu_percent'], '%')} · 负载 {' / '.join(sample['load'])}",
             f"内存 {sample['memory_used']/gib:.1f}/{sample['memory_total']/gib:.1f} GB",
             f"磁盘 {sample['disk_used']/gib:.1f}/{sample['disk_total']/gib:.1f} GB ({sample['disk_percent']:.1f}%)",
             f"温度：CPU {number(sample['cpu_temperature'], '℃')} · 固态 {number(sample['ssd_temperature'], '℃')}",
             f"固态健康：{'正常' if sample['smart'].get('passed') else '需检查'} · 寿命已用 {sample['smart'].get('wear_percent', '?')}%",
             "服务：" + " · ".join(name.removesuffix(".service") + (" ✓" if value["active"] else " ✗")
                                   for name, value in sample["services"].items()),
             f"连续运行 {sample['uptime_seconds']/86400:.1f} 天"]
    backup = Path(config["backup_status"])
    if backup.exists():
        status = json.loads(backup.read_text(encoding="utf-8"))
        lines.append(f"备份：{status.get('status', '?')} · {status.get('date', '?')}")
    return "\n".join(lines)


def execute(action: str, config: dict, unit: str = "") -> None:
    """Persist notification state only after all required messages are delivered."""
    state_file = Path(config["state_directory"]) / "monitor.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    now = time.time()
    if action == "failure":
        key = "unit:" + unit
        sent = state.setdefault("unit_failures", {}).get(key, 0)
        if now - sent >= config["alert_cooldown_seconds"]:
            notify(config, f"⚠️ 服务器任务失败：{unit}\n请检查 journalctl -u {unit}")
            state["unit_failures"][key] = now
    else:
        sample = snapshot(config)
        state["latest"] = sample
        state["sample_at"] = now
        if action == "report":
            day = datetime.now(ZoneInfo(config["timezone"])).date().isoformat()
            if state.get("reported_date") != day:
                notify(config, report(sample, config))
                state["reported_date"] = day
        else:
            current = conditions(sample, config, now)
            alerts, messages = reconcile_alerts(state.get("alerts", {}), current, now,
                                               cooldown=config["alert_cooldown_seconds"])
            for message in messages:
                notify(config, message)
            state["alerts"] = alerts
        print(json.dumps(sample, ensure_ascii=False))
    save_json(state_file, state)


def main() -> None:
    """Serialize checker, reporter, and failure-hook updates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "report", "failure"])
    parser.add_argument("--unit", default="")
    parser.add_argument("--config", default="/etc/hermes-ops/config.json")
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    directory = Path(config["state_directory"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        execute(arguments.action, config, arguments.unit)


if __name__ == "__main__":
    main()
