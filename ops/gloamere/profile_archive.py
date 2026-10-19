#!/usr/bin/python3
"""Use the official Hermes backup, encrypt it, and publish one snapshot per local day."""
import fcntl
import hashlib
import json
import os
import pathlib
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = pathlib.Path("/var/lib/hermes-backup")
CONFIG = pathlib.Path("/etc/hermes-backup/config.json")
HOST_FILES = (
    "/etc/hermes-ops/config.json",
    "/etc/netdata/netdata.conf",
    "/etc/netdata/python.d.conf",
    "/etc/netdata/.opt-out-from-anonymous-statistics",
    "/etc/netdata/cloud.conf",
    "/etc/systemd/system/hermes-monitor.service",
    "/etc/systemd/system/hermes-monitor.timer",
    "/etc/systemd/system/hermes-report.service",
    "/etc/systemd/system/hermes-report.timer",
    "/etc/systemd/system/hermes-alert@.service",
    "/etc/systemd/system/hermes-gateway.service.d/alerts.conf",
    "/etc/systemd/system/mihomo.service.d/alerts.conf",
    "/etc/systemd/system/hermes-backup.service.d/alerts.conf",
    "/etc/logrotate.d/hermes-ops",
    "/etc/mihomo/config.yaml",
    "/etc/mihomo/subscription.yaml",
    "/etc/systemd/system/mihomo.service",
    "/etc/systemd/system/hermes-gateway.service",
    "/etc/systemd/system/hermes-dashboard.service",
    "/etc/systemd/system/hermes-gateway.service.d/proxy.conf",
    "/etc/systemd/system/hermes-gateway.service.d/desktop.conf",
    "/etc/systemd/system/hermes-backup.service",
    "/etc/systemd/system/hermes-backup.timer",
    "/etc/sudoers.d/90-hermes-gloamere",
    "/etc/netplan/50-cloud-init.yaml",
    "/etc/environment",
    "/etc/profile.d/hermes-proxy.sh",
    "/etc/hermes-backup/config.json",
    "/etc/hermes-backup/recipients.txt",
    "/usr/local/bin/hermes",
    "/usr/local/sbin/hermes-backup",
    "/home/Gloamere/.config/gh/hosts.yml",
    "/root/.config/gh/hosts.yml",
    "/home/Gloamere/.gitconfig",
    "/root/.gitconfig",
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args, *, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=1800, check=False)
    with (BASE / "logs/last-run.log").open("ab") as log:
        log.write(("COMMAND: " + " ".join(map(str, args)) + "\n").encode())
        log.write(result.stdout)
        log.write(result.stderr)
    if result.returncode:
        raise RuntimeError(f"{pathlib.Path(args[0]).name} failed (exit {result.returncode}); see private last-run.log")
    return result.stdout


def save_status(data):
    target = BASE / "status.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(target)


def verify_published_day(day_dir):
    manifest = json.loads((day_dir / "manifest.json").read_text())
    for part in manifest["files"]:
        name = part["name"]
        if pathlib.Path(name).name != name:
            raise RuntimeError("Invalid backup part path")
        path = day_dir / name
        if path.stat().st_size != part["bytes"] or digest(path) != part["sha256"]:
            raise RuntimeError(f"Backup part failed hash check: {name}")
    return manifest


def archive_profile(stage, config):
    account = pwd.getpwnam(config["user"])
    os.chown(stage, 0, account.pw_gid)
    stage.chmod(0o710)
    profile_dir = stage / "bundle"
    profile_dir.mkdir(mode=0o700)
    os.chown(profile_dir, account.pw_uid, account.pw_gid)
    hermes_zip = profile_dir / "hermes.zip"
    run([
        "sudo", "-H", "-u", config["user"], "env",
        "HERMES_HOME=" + config["hermes_home"],
        "HTTP_PROXY=" + config["proxy"], "HTTPS_PROXY=" + config["proxy"],
        "/usr/local/bin/hermes", "backup", "-o", str(hermes_zip), "--keep", "0",
    ])
    with zipfile.ZipFile(hermes_zip) as archive:
        names = archive.namelist()
        damaged = archive.testzip()
        if damaged:
            raise RuntimeError("Official backup ZIP failed CRC validation")
        members = {}
        for required in ("config.yaml", ".env", "auth.json", "state.db"):
            matches = [name for name in names if name == required or name.endswith("/" + required)]
            if len(matches) != 1:
                raise RuntimeError(f"Missing or ambiguous critical backup member: {required}")
            members[required] = matches[0]
        db_file = stage / "validated-state.db"
        with archive.open(members["state.db"]) as source, db_file.open("wb") as output:
            shutil.copyfileobj(source, output)
        with closing(sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)) as database:
            check = database.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError("State DB snapshot failed integrity check")
            sessions = database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            messages = database.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return profile_dir, {"zip_members": len(names), "sqlite_integrity": "ok", "sessions": sessions, "messages": messages}


def collect_host_and_source(bundle, config):
    host_dir = bundle / "host"
    host_dir.mkdir(mode=0o700)
    copied = []
    for item in HOST_FILES:
        source = pathlib.Path(item)
        if source.is_file():
            target = host_dir / source.relative_to("/")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, target)
            copied.append(item)
    deployed_ops = pathlib.Path("/usr/local/lib/hermes-ops")
    if deployed_ops.is_dir():
        shutil.copytree(deployed_ops, host_dir / "usr/local/lib/hermes-ops", ignore=shutil.ignore_patterns("__pycache__"))
    git = ["sudo", "-H", "-u", config["user"], "git", "-C", config["source"]]
    sha = run([*git, "rev-parse", "HEAD"]).decode().strip()
    version = run([*git, "describe", "--tags", "--always", "HEAD"]).decode().strip()
    patch = run([*git, "diff", "--binary", "HEAD"])
    (bundle / "source-changes.patch").write_bytes(patch)
    untracked = [p for p in run([*git, "ls-files", "--others", "--exclude-standard", "-z"]).decode().split("\0") if p]
    if untracked:
        with tarfile.open(bundle / "source-untracked.tar", "w") as archive:
            source_root = pathlib.Path(config["source"]).resolve()
            for name in untracked:
                path = (source_root / name).resolve()
                if not path.is_relative_to(source_root):
                    raise RuntimeError("Untracked source escapes source checkout")
                if path.is_file():
                    archive.add(path, arcname=name, recursive=False)
    deployment = {
        "source_repository": "https://github.com/gloamere/hermes",
        "upstream_repository": "https://github.com/NousResearch/hermes-agent",
        "code_sha": sha,
        "version": version,
        "hermes_home": config["hermes_home"],
        "source_path": config["source"],
        "os_user": config["user"],
        "uid": pwd.getpwnam(config["user"]).pw_uid,
        "host_files": copied,
        "tracked_patch_bytes": len(patch),
        "untracked_source_files": len(untracked),
    }
    (bundle / "deployment.json").write_text(json.dumps(deployment, ensure_ascii=False, indent=2) + "\n")
    shutil.copy2(pathlib.Path(config["repository"]) / "RESTORE.md", bundle / "RESTORE.md")
    return deployment
