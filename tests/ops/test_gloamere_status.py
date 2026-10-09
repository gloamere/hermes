"""Live plugin discovery/dispatch and read-only resource/backup query contracts."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
from unittest.mock import AsyncMock, Mock

import pytest
import hermes_yaml as yaml


@pytest.fixture
def query(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "ops/gloamere"))
    import status_query
    return status_query


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    from hermes_cli import plugins
    home = tmp_path / "home"
    target = home / "plugins/gloamere-status"
    shutil.copytree(Path(__file__).parents[2] / "ops/gloamere/gloamere-status", target)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["gloamere-status"]},
        "platforms": {"telegram": {"extra": {"command_menu": {
            "max_commands": 62, "priority": ["server", "backups"], "priority_mode": "prepend"}}}},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    loaded = manager._plugins["gloamere-status"]
    assert loaded.enabled and not loaded.error
    yield manager, loaded.module
    manager.unload()


def test_registered_commands_are_visible_in_telegram_menu(plugin):
    from hermes_cli.commands_platforms import telegram_menu_commands
    menu, _hidden = telegram_menu_commands(max_commands=62)
    descriptions = dict(menu)
    assert "温度" in descriptions["server"]
    assert "备份" in descriptions["backups"]
    assert [name for name, _description in menu[:2]] == ["server", "backups"]
    assert "status" in descriptions  # Existing Hermes session status remains available.


@pytest.mark.asyncio
async def test_gateway_dispatch_uses_plugin_result_without_agent_turn(plugin, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.session import SessionSource
    manager, module = plugin
    mock_query = AsyncMock(return_value="服务器状态：CPU 12%，固态 55℃")
    monkeypatch.setattr(module, "query", mock_query)
    class Runner(GatewayInboundMixin):
        _draining = False
        config = GatewayConfig()
        def _hm_quick_commands(self):
            return {}
        def _session_key_for_source(self, _source):
            return "agent:main:telegram:fixture"
        def _session_env_scope(self, _context):
            return nullcontext()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="123")
    event = MessageEvent(text="/server@fixture_bot", source=source)
    handled, result, _command = await Runner()._hm_dispatch_quick_and_plugin_commands(event, source, "server")
    assert handled and result == "服务器状态：CPU 12%，固态 55℃"
    mock_query.assert_awaited_once_with("server", "")
    assert manager._plugin_commands["backups"]["handler"] is module.backups


@pytest.mark.asyncio
async def test_arguments_cannot_invoke_additional_commands(plugin, monkeypatch):
    _manager, module = plugin
    spawn = AsyncMock()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    result = await module.server("; touch /tmp/should-not-run")
    assert "不需要参数" in result
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_helper_failure_does_not_expose_privileged_error(plugin, monkeypatch):
    _manager, module = plugin
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", b"private-token-placeholder")))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await module.backups("")
    assert "查询失败" in result
    assert "private-token-placeholder" not in result


@pytest.mark.asyncio
async def test_timed_out_helper_is_killed_and_reaped(plugin, monkeypatch):
    _manager, module = plugin
    class Process:
        returncode = None
        killed = False
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            return self.returncode
        async def communicate(self):
            import asyncio
            if not self.killed:
                await asyncio.Event().wait()
            return b"", b""
    process = Process()
    monkeypatch.setattr(module, "QUERY_TIMEOUT", 0.01)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await module.server("")
    assert "超时" in result and process.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, None, "truthy"])
async def test_private_query_fails_closed_before_reading_host_data(plugin, monkeypatch, authorized):
    _manager, module = plugin
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123, is_bot=False),
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_message=SimpleNamespace(text="/server", message_id=42, message_thread_id=None))
    adapter = SimpleNamespace(_is_sender_authorized=Mock(return_value=authorized), send=AsyncMock())
    probe = AsyncMock(return_value="sensitive host status")
    monkeypatch.setattr(module, "query", probe)
    await module.telegram_command(update, SimpleNamespace(args=[]), adapter)
    probe.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_query_replies_without_touching_active_agent_or_stream(plugin, monkeypatch):
    _manager, module = plugin
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123, is_bot=False),
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_message=SimpleNamespace(text="/backups@fixture_bot", message_id=42, message_thread_id=7))
    running = {"agent:main:telegram:123": object()}
    adapter = SimpleNamespace(_is_sender_authorized=Mock(return_value=True), send=AsyncMock(),
                              _active_sessions=running, _pending_messages={})
    probe = AsyncMock(return_value="备份状态：成功")
    monkeypatch.setattr(module, "query", probe)
    await module.telegram_command(update, SimpleNamespace(args=[]), adapter)
    adapter._is_sender_authorized.assert_called_once_with("123", "dm", "123", thread_id="7")
    probe.assert_awaited_once_with("backups", "")
    adapter.send.assert_awaited_once_with("123", "备份状态：成功", reply_to="42",
                                        metadata={"thread_id": "7", "_interim_send": True})
    assert adapter._active_sessions is running and not adapter._pending_messages


@pytest.mark.asyncio
async def test_native_private_handler_does_not_respond_in_groups(plugin, monkeypatch):
    _manager, module = plugin
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123, is_bot=False),
        effective_chat=SimpleNamespace(id=-123, type="group"), effective_message=SimpleNamespace(text="/server"))
    probe = AsyncMock()
    monkeypatch.setattr(module, "query", probe)
    await module.telegram_command(update, SimpleNamespace(args=[]), SimpleNamespace(send=AsyncMock()))
    probe.assert_not_awaited()


def test_counter_deltas_exclude_idle_and_handle_counter_resets(query):
    before = {"cpu": [100, 0, 0, 500, 0, 0, 0, 0], "network": {"eth0": (1000, 2000)}, "disk": (1000, 2000, 0)}
    after = {"cpu": [125, 0, 0, 575, 0, 0, 0, 0], "network": {"eth0": (1100, 10)}, "disk": (1200, 2400, 100)}
    sample = query.rates(before, after, 2)
    assert sample["cpu"] == 25
    assert sample["network"]["eth0"] == (50, 0)
    assert sample["disk"] == (100, 200, 50)


def test_resource_report_preserves_zero_temperature_and_unhealthy_ssd(query):
    sample = {"cpu": 25, "cores": 4, "load": (0.5, 0.4, 0.3), "memory": (1024, 4096),
              "swap": (0, 0), "storage": {}, "disk": None, "network": {}, "uptime": 7200,
              "temperatures": {"cpu": 0, "ssd": None}, "services": {"hermes-gateway.service": {"ActiveState": "failed"}},
              "smart": {"smart_status": {"passed": True}, "nvme_smart_health_information_log": {
                  "critical_warning": 0, "media_errors": 1, "percentage_used": 7}}}
    result = query.server_report(sample, datetime.now(timezone.utc))
    assert "CPU 温度：0.0℃" in result
    assert "固态温度：未读到传感器" in result
    assert "固态健康：异常" in result
    assert "hermes-gateway：failed" in result


@pytest.mark.parametrize("state,expected", [({}, "尚无成功记录"), ({"status": "failed"}, "失败"),
    ({"status": "success", "finished_at": "invalid"}, "时间无效"), ({"_invalid": True}, "损坏")])
def test_missing_failed_or_corrupt_backup_is_never_reported_as_success(query, state, expected):
    assert expected in query.backup_health(state, datetime.now(timezone.utc))


def test_stale_and_future_success_timestamps_are_flagged(query):
    now = datetime.now(timezone.utc)
    stale = {"status": "success", "finished_at": (now - timedelta(hours=37)).isoformat()}
    future = {"status": "success", "finished_at": (now + timedelta(hours=1)).isoformat()}
    assert "超过 36 小时" in query.backup_health(stale, now)
    assert "未来" in query.backup_health(future, now)


def test_empty_enabled_restore_features_show_no_restore_points(query, tmp_path):
    lines = query.local_backup_lines(tmp_path, {"checkpoints": {"enabled": True},
        "curator": {"enabled": True, "backup": {"enabled": True, "keep": 2}}})
    result = "\n".join(lines)
    assert "代码检查点：开启，尚无记录" in result
    assert "Curator 技能恢复点：开启，尚无记录" in result


def test_backup_report_is_read_only_and_survives_corrupt_status(query, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("checkpoints:\n  enabled: true\n", encoding="utf-8")
    state = tmp_path / "status.json"
    state.write_text("broken json", encoding="utf-8")
    manifest = tmp_path / "repo/2026-10/2026-10-09/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"format": 2, "created_at": "2026-10-09T10:00:00+08:00",
        "total_bytes": 2048, "workspace": {"files": 3, "input_bytes": 600}}), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(query, "unit_properties", lambda *_: {})
    result = query.backups_report({"backup_status": str(state)}, {
        "hermes_home": str(home), "repository": str(tmp_path / "repo"), "daily_keep_days": 30,
        "monthly_keep_months": 12, "remote_max_bytes": 10000, "workspace_max_bytes": 10000,
        "workspace_max_file_bytes": 1000}, datetime.now(timezone.utc))
    assert "状态记录损坏" in result
    assert "已纳入该包，3 项" in result
    assert "本次查询不访问 GitHub" in result
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
