"""Read-only host diagnostics for Hermes slash commands; no model or WAN calls."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# The host helper uses Ubuntu's Python; the PM test interpreter ships hermes_yaml.
try:
    import hermes_yaml as yaml
except ImportError:
    import yaml

from common import load_config
from metrics import temperatures

logger = logging.getLogger(__name__)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def command(arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Status probe unavailable: %s", arguments[0], exc_info=True)
        return None
    # smartctl uses a bitmask exit status even when it returns useful health data.
    return result.stdout if result.returncode == 0 or arguments[0] == "smartctl" else None


def unit_properties(unit: str, properties: str) -> dict:
    text = command(["systemctl", "show", unit, "--property=" + properties]) or ""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def size(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "未知"


def timestamp(value: str | None, timezone: str) -> str:
    if not value:
        return "暂无记录"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return "时间记录无时区"
        return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "时间记录无效"


def counters(device: str, proc: Path = Path("/proc"), network: Path = Path("/sys/class/net")) -> dict:
    cpu = [int(value) for value in (proc / "stat").read_text(encoding="utf-8").splitlines()[0].split()[1:9]]
    interfaces = {}
    for line in (proc / "net/dev").read_text(encoding="utf-8").splitlines()[2:]:
        name, data = line.split(":", 1)
        name = name.strip()
        if (network / name / "device").exists():
            values = data.split()
            interfaces[name] = (int(values[0]), int(values[8]))
    disk = None
    for line in (proc / "diskstats").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields[2] == Path(device).name:
            disk = (int(fields[5]) * 512, int(fields[9]) * 512, int(fields[12]))
    return {"cpu": cpu, "network": interfaces, "disk": disk}


def rates(before: dict, after: dict, elapsed: float) -> dict:
    total = sum(after["cpu"]) - sum(before["cpu"])
    idle = sum(after["cpu"][3:5]) - sum(before["cpu"][3:5])
    cpu = max(0.0, min(100.0, 100 * (total - idle) / total)) if total > 0 else None
    network = {name: tuple(max(0, new[i] - before["network"][name][i]) / elapsed for i in (0, 1))
               for name, new in after["network"].items() if name in before["network"]}
    disk = None
    if before["disk"] is not None and after["disk"] is not None:
        disk = tuple(max(0, after["disk"][i] - before["disk"][i]) / elapsed for i in range(3))
    return {"cpu": cpu, "network": network, "disk": disk}


def resource_sample(config: dict) -> dict:
    before = counters(config["nvme_device"])
    start = time.monotonic()
    time.sleep(1)
    after = counters(config["nvme_device"])
    result = rates(before, after, time.monotonic() - start)
    memory = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) * 1024
    result["memory"] = (memory["MemTotal"] - memory["MemAvailable"], memory["MemTotal"])
    result["swap"] = (memory["SwapTotal"] - memory["SwapFree"], memory["SwapTotal"])
    result["storage"] = {"/": shutil.disk_usage("/")}
    # Report each filesystem once: du-like directory size is not filesystem usage.
    if Path(config["workspace"]).stat().st_dev != Path("/").stat().st_dev:
        result["storage"][config["workspace"]] = shutil.disk_usage(config["workspace"])
    result["load"] = os.getloadavg()
    result["cores"] = os.cpu_count()
    result["uptime"] = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    try:
        result["temperatures"] = temperatures()
    except (OSError, ValueError):
        logger.warning("Temperature probe unavailable", exc_info=True)
        result["temperatures"] = {"cpu": None, "ssd": None}
    smart_text = command(["smartctl", "-H", "-A", "-j", config["nvme_device"]])
    try:
        result["smart"] = json.loads(smart_text) if smart_text else {}
    except ValueError:
        logger.warning("SMART response is invalid", exc_info=True)
        result["smart"] = {}
    result["services"] = {name: unit_properties(name, "ActiveState,SubState,NRestarts")
                          for name in config["services"]}
    return result


def server_report(sample: dict, now: datetime) -> str:
    cpu = f"{sample['cpu']:.1f}%" if sample["cpu"] is not None else "未知"
    used, total = sample["memory"]
    swap_used, swap_total = sample["swap"]
    hours = int(sample["uptime"] // 3600)
    lines = [f"服务器状态 · {socket.gethostname()}", now.strftime("%Y-%m-%d %H:%M:%S %Z"),
             f"CPU：{cpu}（约 1 秒采样，{sample['cores']} 核）",
             "负载（1/5/15 分钟）：" + " / ".join(f"{value:.2f}" for value in sample["load"]),
             f"内存：{size(used)} / {size(total)}（{100 * used / total:.1f}%）",
             f"交换空间：{size(swap_used)} / {size(swap_total)}" if swap_total else "交换空间：未启用"]
    for path, disk in sample["storage"].items():
        label = "磁盘 /" if path == "/" else "工作目录所在分区"
        lines.append(f"{label}：{size(disk.used)} / {size(disk.total)}"
                     f"（{100 * disk.used / disk.total:.1f}%），可用 {size(disk.free)}")
    if sample["disk"] is not None:
        read, write, busy = sample["disk"]
        lines.append(f"固态 I/O：读 {size(read)}/s，写 {size(write)}/s，忙碌 {min(100, busy / 10):.1f}%")
    for name, (receive, transmit) in sample["network"].items():
        lines.append(f"网卡 {name}：下行 {size(receive)}/s，上行 {size(transmit)}/s")
    if not sample["network"]:
        lines.append("物理网卡流量：暂无可用数据")
    for label, key in (("CPU 温度", "cpu"), ("固态温度", "ssd")):
        value = sample["temperatures"][key]
        lines.append(f"{label}：{value:.1f}℃" if value is not None else f"{label}：未读到传感器")
    smart = sample["smart"]
    passed = smart.get("smart_status", {}).get("passed")
    health = smart.get("nvme_smart_health_information_log", {})
    if passed is None or not health:
        lines.append("固态健康：无法读取")
    else:
        healthy = passed and health.get("critical_warning") == 0 and health.get("media_errors") == 0
        lines.append("固态健康：" + ("正常" if healthy else "异常") +
                     f"，寿命消耗 {health.get('percentage_used', '未知')}%，介质错误 {health.get('media_errors', '未知')}")
    for name, properties in sample["services"].items():
        state = "运行中" if properties.get("ActiveState") == "active" else properties.get("ActiveState", "未知")
        lines.append(f"{name.removesuffix('.service')}：{state}，自动重启 {properties.get('NRestarts', '未知')} 次")
    lines.append(f"开机时长：{hours // 24} 天 {hours % 24} 小时")
    return "\n".join(lines)


def inventory(paths) -> str:
    entries = [path for path in paths if not path.is_symlink() and path.exists()]
    if not entries:
        return "尚无记录"
    latest = max(path.stat().st_mtime for path in entries)
    date = datetime.fromtimestamp(latest, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    return f"{len(entries)} 份，最近 {date}"


def latest_manifest(config: dict) -> dict:
    repository = Path(config["repository"]).resolve()
    paths = sorted(repository.glob("????-??/????-??-??/manifest.json"), reverse=True)
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(repository):
            continue
        try:
            data = read_json(path)
        except (OSError, ValueError):
            logger.warning("Backup manifest unavailable", exc_info=True)
            continue
        if data.get("format") == 2:
            return data
    return {}


def backup_health(state: dict, now: datetime) -> str:
    if state.get("_invalid"):
        return "状态记录损坏，无法确认"
    status = state.get("status")
    if status != "success":
        return {"failed": "最近一次失败", "running": "正在备份"}.get(status, "尚无成功记录")
    try:
        finished = datetime.fromisoformat(state["finished_at"])
        age = (now - finished).total_seconds()
    except (KeyError, TypeError, ValueError):
        return "成功，但完成时间无效"
    if age < -300:
        return "成功，但完成时间在未来，请检查时钟"
    return "成功（已超过 36 小时，需检查）" if age > 36 * 3600 else "成功"


def cloud_backup_lines(config: dict, state: dict, manifest: dict, now: datetime) -> list[str]:
    validation = manifest.get("validation", {})
    workspace = manifest.get("workspace", {})
    lines = ["每日完整备份：" + backup_health(state, now),
             "最近任务结束：" + timestamp(state.get("finished_at") or state.get("failed_at"),
                                        config.get("timezone", "Asia/Shanghai"))]
    if manifest:
        lines += ["最近已校验附件：" + timestamp(manifest.get("created_at"), config.get("timezone", "Asia/Shanghai")),
                  f"加密包：{size(manifest.get('total_bytes', 0))}，版本 {manifest.get('version', '未知')}",
                  "Hermes 数据：SQLite " + ("校验通过" if validation.get("sqlite_integrity") == "ok" else "未确认") + "，"
                  f"{validation.get('sessions', '未知')} 个会话 / {validation.get('messages', '未知')} 条消息",
                  f"工作目录：已纳入该包，{workspace.get('files', '未知')} 项，原始大小 {size(workspace.get('input_bytes', 0))}"]
    else:
        lines.append("完整包 / 工作目录：尚未找到已校验清单")
    if state.get("status") == "success":
        lines.append(f"上次确认云端留存：{state.get('retained_snapshots', '未知')} 份，"
                     f"{size(state.get('remote_bytes', 0))}；清理 {state.get('removed_snapshots', '未知')} 份")
    lines += [f"保留：每日 {config['daily_keep_days']} 天 + 每月 {config['monthly_keep_months']} 个月，"
              f"总上限 {size(config['remote_max_bytes'])}",
              f"工作目录上限：总计 {size(config['workspace_max_bytes'])}，单文件 {size(config['workspace_max_file_bytes'])}"]
    return lines


def local_backup_lines(home: Path, config: dict) -> list[str]:
    checkpoints = config.get("checkpoints", {})
    curator = config.get("curator", {})
    updates = config.get("updates", {})
    checkpoint_base = home / "checkpoints"
    store = checkpoint_base / "store"
    count = command(["git", "--git-dir=" + str(store), "rev-list", "--all", "--count"]) if (store / "HEAD").exists() else None
    checkpoint_count = count.strip() if count and count.strip().isdigit() else None
    checkpoint_status = f"{checkpoint_count} 个检查点" if checkpoint_count is not None else (
        "尚无记录" if not checkpoint_base.exists() else "目录存在，数量无法读取")
    backup = curator.get("backup", {})
    curator_on = curator.get("enabled", False) and backup.get("enabled", True)
    config_files = (home / "backups/config").glob("*")
    return ["", "本地恢复点", "配置备份：" + inventory(path for path in config_files if path.is_file()),
            f"升级前完整包：{inventory((home / 'backups').glob('pre-update-*.zip'))}；"
            f"策略 {updates.get('pre_update_backup', '未知')}，保留 {updates.get('backup_keep', '未知')} 份",
            "其他手动完整包：" + inventory(path for path in (home / "backups").glob("*.zip")
                                         if not path.name.startswith("pre-update-")),
            "快速状态快照：" + inventory(path for path in (home / "state-snapshots").glob("*") if path.is_dir()),
            f"代码检查点：{'开启' if checkpoints.get('enabled', False) else '关闭'}，{checkpoint_status}；"
            f"保留 {checkpoints.get('retention_days', 7)} 天 / 上限 {checkpoints.get('max_total_size_mb', 500)} MiB",
            f"Curator 技能恢复点：{'开启' if curator_on else '关闭'}，"
            f"{inventory((home / 'skills/.curator_backups').glob('*/manifest.json'))}；保留 {backup.get('keep', 3)} 份"]


def backups_report(ops_config: dict, backup_config: dict, now: datetime) -> str:
    try:
        state = read_json(Path(ops_config["backup_status"]))
    except (OSError, ValueError):
        logger.warning("Backup status record unavailable", exc_info=True)
        state = {"_invalid": True}
    manifest = latest_manifest(backup_config)
    timer = unit_properties("hermes-backup.timer", "ActiveState,NextElapseUSecRealtime,UnitFileState")
    job = unit_properties("hermes-backup.service", "ActiveState,SubState,Result")
    home = Path(backup_config["hermes_home"])
    profile = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    lines = ["备份状态", now.strftime("%Y-%m-%d %H:%M:%S %Z")]
    if job.get("ActiveState") == "activating":
        lines.append("当前任务：正在执行；下面显示最近完成的记录")
    lines += cloud_backup_lines(backup_config, state, manifest, now)
    active = timer.get("ActiveState") == "active" and timer.get("UnitFileState") == "enabled"
    next_run = timer.get("NextElapseUSecRealtime") or "暂无计划"
    if len(next_run.split()) == 4:
        next_run = next_run.partition(" ")[2].replace("CST", "北京时间")
    lines += ["每日定时器：" + ("已开启" if active else "未运行或未设为自启"),
              "下次备份：" + next_run,
              "任务退出状态：" + {"success": "成功", "exit-code": "失败", "timeout": "超时"}.get(
                  job.get("Result"), job.get("Result", "未知"))]
    lines += local_backup_lines(home, profile)
    lines += ["", "云端信息来自最近备份任务的校验记录，本次查询不访问 GitHub。",
              "工作目录与 Hermes 同包；本地恢复点数量不代表已完成恢复校验。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("server", "backups"))
    args = parser.parse_args()
    try:
        config = load_config("/etc/hermes-ops/config.json")
        now = datetime.now(ZoneInfo(config.get("timezone", "Asia/Shanghai")))
        if args.action == "server":
            report = server_report(resource_sample(config), now)
        else:
            report = backups_report(config, load_config("/etc/hermes-backup/config.json"), now)
        print(report)
        return 0
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        logger.exception("Host status query failed")
        print("状态查询失败：配置或状态记录无法读取，请检查服务器日志。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
