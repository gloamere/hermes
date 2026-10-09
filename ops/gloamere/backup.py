#!/usr/bin/python3
"""Encrypted Hermes and workspace snapshots with bounded GitHub asset retention."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import profile_archive
from common import load_config, run, save_json, use_proxy
from retention import TAG_PATTERN, plan_retention

BASE = pathlib.Path("/var/lib/hermes-backup")
EXCLUDE_DIRECTORIES = {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git", "tmp"}
VERIFIED_MARKER = "<!-- hermes-backup-verified -->"


def digest(path: pathlib.Path) -> str:
    """Hash archives and downloaded assets without loading them into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def workspace_files(root: pathlib.Path, *, max_file_bytes: int, max_total_bytes: int) -> tuple[list, int]:
    """Select workspace content; preserve symlinks, never follow targets outside it."""
    selected = []
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        current = pathlib.Path(directory)
        names[:] = sorted(name for name in names if name not in EXCLUDE_DIRECTORIES)
        for name in sorted(files + [name for name in names if (current / name).is_symlink()]):
            path = current / name
            if not path.is_symlink() and not path.is_file():
                continue
            size = 0 if path.is_symlink() else path.stat().st_size
            if size > max_file_bytes:
                raise RuntimeError("Workspace file exceeds the configured backup limit")
            total += size
            if total > max_total_bytes:
                raise RuntimeError("Workspace exceeds the configured backup limit")
            selected.append(path)
    return selected, total


def archive_workspace(bundle: pathlib.Path, config: dict) -> dict:
    """Compress selected working files; oversize data fails visibly instead of being omitted."""
    root = pathlib.Path(config["workspace"]).resolve(strict=True)
    paths, total = workspace_files(root, max_file_bytes=config["workspace_max_file_bytes"],
                                   max_total_bytes=config["workspace_max_bytes"])
    output = bundle / "workspace.tar.gz"
    with tarfile.open(output, "w:gz", dereference=False) as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(root), recursive=False)
    with tarfile.open(output, "r:gz") as archive:
        if len(archive.getmembers()) != len(paths):
            raise RuntimeError("Workspace archive validation failed")
    information = {"path": str(root), "files": len(paths), "input_bytes": total,
                   "excluded_directories": sorted(EXCLUDE_DIRECTORIES), "archive_sha256": digest(output)}
    (bundle / "workspace-manifest.json").write_text(json.dumps(information, ensure_ascii=False, indent=2) + "\n")
    return information


def github(config: dict, arguments: list[str]) -> bytes:
    """Use the authenticated GitHub CLI, keeping credentials outside the repository."""
    return run(["gh", *arguments], timeout=1200)


def release(config: dict, tag: str) -> dict:
    """Retrieve authoritative asset sizes and digests from GitHub."""
    return json.loads(github(config, ["api", f"repos/{config['repository_slug']}/releases/tags/{tag}"]))


def verify_release(config: dict, manifest: dict, stage: pathlib.Path) -> None:
    """Require remote assets to match the encrypted archive before pruning older snapshots."""
    assets = {asset["name"]: asset for asset in release(config, manifest["release_tag"])["assets"]}
    for item in manifest["files"]:
        asset = assets.get(item["name"])
        if not asset or asset["size"] != item["bytes"]:
            raise RuntimeError("Published backup asset is missing or has the wrong size")
        remote_digest = asset.get("digest")
        if remote_digest:
            if remote_digest != "sha256:" + item["sha256"]:
                raise RuntimeError("Published backup asset digest mismatch")
        else:
            downloaded = stage / ("verify-" + item["name"])
            github(config, ["release", "download", manifest["release_tag"], "--repo", config["repository_slug"],
                            "--pattern", item["name"], "--output", str(downloaded)])
            if digest(downloaded) != item["sha256"]:
                raise RuntimeError("Downloaded backup asset digest mismatch")


def publish_archive(stage: pathlib.Path, bundle: pathlib.Path, config: dict, validation: dict,
                    deployment: dict, workspace: dict, now: datetime) -> dict:
    """Upload only age ciphertext; Git contains a small daily manifest."""
    plain = stage / "payload.tar"
    with tarfile.open(plain, "w") as archive:
        archive.add(bundle, arcname=".", recursive=True)
    encrypted = stage / "hermes.tar.age"
    run(["age", "--encrypt", "-R", config["recipient_file"], "-o", str(encrypted), str(plain)])
    if encrypted.stat().st_size > config["snapshot_max_bytes"]:
        raise RuntimeError("Encrypted snapshot exceeds its size limit")
    parts = []
    with encrypted.open("rb") as stream:
        while block := stream.read(config["chunk_bytes"]):
            target = stage / f"hermes.tar.age.part-{len(parts):04d}"
            target.write_bytes(block)
            parts.append({"name": target.name, "bytes": len(block), "sha256": digest(target)})
    tag = "hermes-backup-" + now.strftime("%Y-%m-%dT%H%M%S")
    if not TAG_PATTERN.fullmatch(tag):
        raise RuntimeError("Invalid backup release tag")
    repository = pathlib.Path(config["repository"])
    commit = run(["git", "rev-parse", "HEAD"], cwd=repository).decode().strip()
    github(config, ["release", "create", tag, "--repo", config["repository_slug"], "--target", commit,
                    "--title", "Hermes 加密备份 " + now.strftime("%Y-%m-%d %H:%M"),
                    "--notes", "age 加密的 Hermes、工作目录与主机配置。恢复步骤见 RESTORE.md。",
                    "--latest=false", *[str(stage / part["name"]) for part in parts]])
    manifest = {"format": 2, "created_at": now.isoformat(), "release_tag": tag,
                "encryption": "age X25519", "archive": "tar: Hermes ZIP, workspace.tar.gz, host settings",
                "code_sha": deployment["code_sha"], "version": deployment["version"],
                "validation": validation, "workspace": workspace, "files": parts,
                "total_bytes": sum(item["bytes"] for item in parts)}
    verify_release(config, manifest, stage)
    github(config, ["release", "edit", tag, "--repo", config["repository_slug"],
                    "--notes", VERIFIED_MARKER + "\nage 加密备份；全部附件已校验，恢复步骤见 RESTORE.md。"])
    return manifest


def list_releases(config: dict) -> list[dict]:
    """Enumerate all pages so aging backups cannot escape pruning."""
    pages = json.loads(github(config, ["api", "--paginate", "--slurp",
                                      f"repos/{config['repository_slug']}/releases?per_page=100"]))
    return [item for page in pages for item in page]


def prune(config: dict, now: datetime) -> dict:
    """Delete only generated backup releases and their corresponding expired manifests."""
    releases = list_releases(config)
    verified = [item for item in releases if VERIFIED_MARKER in (item.get("body") or "")]
    retained, removed = plan_retention(verified, now, daily_days=config["daily_keep_days"],
                                       monthly_months=config["monthly_keep_months"], max_bytes=config["remote_max_bytes"])
    if not retained:
        raise RuntimeError("Refusing to prune without a verified restore point")
    removed.extend(item for item in releases if TAG_PATTERN.fullmatch(item["tag_name"])
                   and VERIFIED_MARKER not in (item.get("body") or ""))
    removed_tags = {item["tag_name"] for item in removed}
    for item in removed:
        github(config, ["release", "delete", item["tag_name"], "--repo", config["repository_slug"],
                        "--yes", "--cleanup-tag"])
    repository = pathlib.Path(config["repository"]).resolve()
    for path in repository.glob("????-??/????-??-??/manifest.json"):
        if not path.resolve().is_relative_to(repository):
            raise RuntimeError("Backup manifest escapes its repository")
        manifest = json.loads(path.read_text())
        if manifest.get("format") == 2 and manifest.get("release_tag") in removed_tags:
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
    return {"retained_snapshots": len(retained), "removed_snapshots": len(removed),
            "remote_bytes": sum(asset["size"] for item in retained for asset in item.get("assets", []))}


def clean_stale_staging(now: float) -> None:
    """Remove crash leftovers under the locked staging root, never working directories."""
    root = (BASE / "staging").resolve(strict=True)
    for path in root.glob("run.*"):
        if path.is_symlink() or not path.is_dir():
            continue
        if not path.resolve().is_relative_to(root):
            raise RuntimeError("Staging cleanup escapes its root")
        if now - path.stat().st_mtime > 48 * 3600:
            shutil.rmtree(path)


def push_manifests(config: dict, now: datetime) -> str:
    """Commit only monthly backup folders and verify remote parity."""
    repository = pathlib.Path(config["repository"])
    run(["git", "add", "--all", "--", ":(glob)????-??/**"], cwd=repository)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repository, check=False, timeout=60)
    if changed.returncode == 1:
        run(["git", "commit", "-m", "每日加密备份与保留清理 " + now.strftime("%Y-%m-%d")], cwd=repository)
    elif changed.returncode:
        raise RuntimeError("Unable to inspect backup manifest changes")
    run(["git", "push", "origin", "HEAD:main"], cwd=repository, timeout=180)
    local = run(["git", "rev-parse", "HEAD"], cwd=repository).decode().strip()
    remote = run(["git", "ls-remote", "origin", "refs/heads/main"], cwd=repository).decode().split()[0]
    if local != remote:
        raise RuntimeError("Backup manifest push has not reached remote main")
    return local


def backup(config: dict, *, force: bool = False) -> dict:
    """Publish and validate a fresh snapshot before any retention deletion."""
    use_proxy(config)
    os.environ["GH_CONFIG_DIR"] = "/root/.config/gh"
    if not json.loads(github(config, ["api", f"repos/{config['repository_slug']}"]))["private"]:
        raise RuntimeError("Backup repository must be private")
    repository = pathlib.Path(config["repository"])
    run(["git", "pull", "--ff-only", "origin", "main"], cwd=repository, timeout=180)
    if run(["git", "status", "--porcelain"], cwd=repository).strip():
        raise RuntimeError("Backup repository contains uncommitted changes")
    now = datetime.now(ZoneInfo(config["timezone"]))
    clean_stale_staging(now.timestamp())
    target = repository / now.strftime("%Y-%m") / now.strftime("%Y-%m-%d") / "manifest.json"
    existing = json.loads(target.read_text()) if target.exists() else {}
    (BASE / "logs").mkdir(exist_ok=True)
    (BASE / "logs/last-run.log").write_bytes(b"")
    with tempfile.TemporaryDirectory(prefix="run.", dir=BASE / "staging") as directory:
        stage = pathlib.Path(directory)
        if existing.get("format") == 2 and not force:
            manifest = existing
            verify_release(config, manifest, stage)
        else:
            bundle, validation = profile_archive.archive_profile(stage, config)
            deployment = profile_archive.collect_host_and_source(bundle, config)
            workspace = archive_workspace(bundle, config)
            manifest = publish_archive(stage, bundle, config, validation, deployment, workspace, now)
            save_json(target, manifest)
        commit = push_manifests(config, now)
        retention = prune(config, now)
        commit = push_manifests(config, now)
    result = {"status": "success", "date": now.date().isoformat(),
              "finished_at": datetime.now(ZoneInfo(config["timezone"])).isoformat(),
              "repository": config["repository_slug"], "release_tag": manifest["release_tag"],
              "commit": commit, "bytes": manifest["total_bytes"], "sqlite_integrity": "ok",
              "workspace_files": manifest["workspace"]["files"], **retention}
    save_json(BASE / "status.json", result)
    return result


def main() -> None:
    """Lock the complete archive/upload/prune transaction; surface failures to systemd."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/hermes-backup/config.json")
    parser.add_argument("--force", action="store_true", help="Refresh today's snapshot after configuration changes")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo to include host configuration")
    os.umask(0o077)
    with pathlib.Path("/run/lock/hermes-backup.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(2) from None
        try:
            result = backup(load_config(arguments.config), force=arguments.force)
            print(json.dumps(result, ensure_ascii=False))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, sqlite3.Error, zipfile.BadZipFile) as error:
            save_json(BASE / "status.json", {"status": "failed", "error_type": type(error).__name__,
                                           "failed_at": datetime.now().astimezone().isoformat()})
            print(f"Backup failed ({type(error).__name__}): {error}", file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
