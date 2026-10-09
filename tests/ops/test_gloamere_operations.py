"""Behavior contracts for bounded backups, symlink handling, and host notifications."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tarfile
import urllib.error

import pytest


@pytest.fixture
def ops(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "ops" / "gloamere"))
    import backup
    import common
    import monitor
    import retention
    return SimpleNamespace(backup=backup, common=common, monitor=monitor, retention=retention)


def snapshot(day, size=100):
    return {"tag_name": "hermes-backup-" + day.strftime("%Y-%m-%dT%H%M%S"), "assets": [{"size": size}]}


def test_retention_preserves_recent_days_and_last_snapshot_of_each_month(ops):
    now = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
    recent = [snapshot(now - timedelta(days=i)) for i in range(90)]
    monthly = [snapshot(datetime(2026, month, 20, 9, tzinfo=timezone.utc)) for month in range(1, 8)]
    unrelated = {"tag_name": "manual-archive", "assets": [{"size": 100}]}
    kept, removed = ops.retention.plan_retention(recent + monthly + [unrelated], now,
                                               daily_days=30, monthly_months=12, max_bytes=100000)
    tags = {item["tag_name"] for item in kept}
    assert all(item["tag_name"] in tags for item in recent[:30])
    assert len([item for item in kept if "2026-07" in item["tag_name"]]) == 1
    assert unrelated not in kept + removed
    assert len([item for item in kept if "2026-01" in item["tag_name"]]) == 1


def test_quota_drops_oldest_but_never_the_last_restore_point(ops):
    now = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
    releases = [snapshot(now - timedelta(days=i)) for i in range(4)]
    kept, removed = ops.retention.plan_retention(releases, now, daily_days=30,
                                               monthly_months=12, max_bytes=200)
    assert kept == releases[:2]
    assert removed == releases[2:]
    with pytest.raises(ValueError):
        ops.retention.plan_retention(releases, now, daily_days=30, monthly_months=0, max_bytes=99)


def test_workspace_archive_preserves_files_and_links_without_following_external_targets(ops, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "project.py").write_text("print('hello')")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "cache").write_bytes(b"unused")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not part of workspace")
    (root / "linked-directory").symlink_to(outside, target_is_directory=True)
    (root / "linked-file").symlink_to(outside / "private.txt")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    info = ops.backup.archive_workspace(bundle, {"workspace": str(root), "workspace_max_file_bytes": 1024,
                                                "workspace_max_bytes": 1024})
    with tarfile.open(bundle / "workspace.tar.gz") as archive:
        assert set(archive.getnames()) == {"project.py", "linked-directory", "linked-file"}
        assert archive.getmember("linked-directory").issym()
        assert archive.extractfile("project.py").read() == b"print('hello')"
    assert info["files"] == 3


def test_oversize_workspace_fails_instead_of_publishing_incomplete_data(ops, tmp_path):
    (tmp_path / "data.bin").write_bytes(b"x" * 100)
    with pytest.raises(RuntimeError, match="file exceeds"):
        ops.backup.workspace_files(tmp_path, max_file_bytes=99, max_total_bytes=1024)
    with pytest.raises(RuntimeError, match="Workspace exceeds"):
        ops.backup.workspace_files(tmp_path, max_file_bytes=1024, max_total_bytes=99)


def test_remote_digest_mismatch_stops_validation(ops, monkeypatch, tmp_path):
    monkeypatch.setattr(ops.backup, "release", lambda *_: {"assets": [
        {"name": "archive.age", "size": 10, "digest": "sha256:bad"}]})
    with pytest.raises(RuntimeError, match="digest mismatch"):
        ops.backup.verify_release({}, {"release_tag": "tag", "files": [
            {"name": "archive.age", "bytes": 10, "sha256": "expected"}]}, tmp_path)


def test_incomplete_upload_cannot_replace_verified_restore_point(ops, monkeypatch, tmp_path):
    now = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
    complete = snapshot(now - timedelta(hours=1))
    complete["body"] = ops.backup.VERIFIED_MARKER
    incomplete = snapshot(now)
    incomplete["body"] = "upload interrupted"
    monkeypatch.setattr(ops.backup, "list_releases", lambda _: [incomplete, complete])
    deleted = []
    monkeypatch.setattr(ops.backup, "github", lambda _config, args: deleted.append(args[2]))
    config = {"repository": str(tmp_path), "repository_slug": "owner/private-backups",
              "daily_keep_days": 30, "monthly_keep_months": 12, "remote_max_bytes": 1000}
    result = ops.backup.prune(config, now)
    assert deleted == [incomplete["tag_name"]]
    assert result["retained_snapshots"] == 1


def test_sustained_resource_alert_is_delayed_deduplicated_and_reports_recovery(ops):
    faults = {"cpu_percent": "CPU busy"}
    state, messages = ops.monitor.reconcile_alerts({}, faults, 1000)
    assert not messages
    state, messages = ops.monitor.reconcile_alerts(state, faults, 1600)
    assert len(messages) == 1
    state, messages = ops.monitor.reconcile_alerts(state, faults, 1700)
    assert not messages
    state, messages = ops.monitor.reconcile_alerts(state, {}, 1800)
    assert len(messages) == 1 and "已恢复" in messages[0]
    assert not state


def test_service_fault_is_immediate_and_repeats_only_after_cooldown(ops):
    faults = {"service:hermes-gateway.service": "stopped"}
    state, messages = ops.monitor.reconcile_alerts({}, faults, 1000)
    assert len(messages) == 1
    state, messages = ops.monitor.reconcile_alerts(state, faults, 1100)
    assert not messages
    state, messages = ops.monitor.reconcile_alerts(state, faults, 1000 + 21600)
    assert len(messages) == 1


def test_telegram_transport_error_does_not_expose_bot_credentials(ops, monkeypatch, tmp_path):
    token = "123456:synthetic_test_credential"
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN='" + token + "'\n")
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("https://api.telegram.org/bot" + token)
    monkeypatch.setattr(ops.common.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=fail))
    with pytest.raises(RuntimeError) as result:
        ops.common.notify({"telegram_env": str(env), "chat_id": 1, "proxy": "http://127.0.0.1:7890"}, "test")
    assert token not in str(result.value)
