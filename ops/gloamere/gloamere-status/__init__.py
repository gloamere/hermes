"""Hermes chat commands backed by the installed, read-only host query helper."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)
HELPER = "/usr/local/lib/hermes-ops/status_query.py"
QUERY_TIMEOUT = 22


async def query(action: str, raw_args: str) -> str:
    if raw_args.strip():
        return f"用法：/{action}（不需要参数）"
    try:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/python3", HELPER, action,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.exception("Unable to start host status helper")
        return "查询组件无法启动，请检查服务器安装。"
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=QUERY_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            logger.warning("Host status helper did not finish cleanup: %s", action)
        if isinstance(error, asyncio.CancelledError):
            raise
        logger.warning("Host status query timed out: %s", action)
        return "状态查询超时，请稍后重试。"
    if process.returncode:
        # stderr may contain privileged paths; keep operational details in host logs.
        logger.warning("Host status helper failed: %s, exit %s", action, process.returncode)
        return "状态查询失败，请检查组件、sudo 权限或服务器状态记录。"
    result = stdout.decode("utf-8", errors="replace").strip()
    return result[:3900] or "暂无可用状态数据。"


async def server(raw_args: str) -> str:
    return await query("server", raw_args)


async def backups(raw_args: str) -> str:
    return await query("backups", raw_args)


async def telegram_command(update, context, adapter) -> None:
    """Read-only private-chat queries stay outside the running agent's task queue."""
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    if user is None or chat is None or message is None or chat.type != "private" or user.is_bot:
        return
    authorize = getattr(adapter, "_is_sender_authorized", None)
    thread_id = getattr(message, "message_thread_id", None)
    thread = str(thread_id) if thread_id is not None else None
    # The adapter's gateway-installed callback owns the actual allowlist/pairing policy.
    # Missing callback and unknown decisions fail closed; do not read a second allowlist.
    if authorize is None or authorize(str(user.id), "dm", str(chat.id), thread_id=thread) is not True:
        return
    action = message.text.split()[0].split("@", 1)[0].removeprefix("/")
    if action not in {"server", "backups"}:
        return
    text = await query(action, " ".join(context.args or []))
    metadata = {"_interim_send": True}
    if thread is not None:
        metadata["thread_id"] = thread
    await adapter.send(str(chat.id), text, reply_to=str(message.message_id), metadata=metadata)


def wire_telegram(application, adapter) -> None:
    from telegram.ext import CommandHandler, filters

    async def handle(update, context):
        await telegram_command(update, context, adapter)

    # Same first-match group as the core catch-all; only these private-chat commands match.
    application.add_handler(CommandHandler(["server", "backups"], handle,
                                           filters=filters.ChatType.PRIVATE, block=False))


def register(ctx) -> None:
    ctx.register_command("server", server, description="查询服务器 CPU、内存、磁盘、网络和温度")
    ctx.register_command("backups", backups, description="查询每日备份、工作目录和本地恢复点状态")
    ctx.register_platform_handler("telegram", wire_telegram)
