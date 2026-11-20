"""CLI entrypoint: iMessage daemon, local REPL (--cli), invite/user admin, --self-test, and --doctor."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Iterator

from .bridge_imessage import CliBridge, InboundMessage, MacIMessageBridge, MessageDeliveryError
from .config import AgentConfig
from .dreamina_client import DreaminaClient
from .harnesses_client import AGYClient, CodexClient, DSHClient
from .human_gate import HumanGate
from .logging_utils import RedactedLogFormatter
from .orchestrator import HyperTextOrchestrator, proactive_cron_loop
from .sandbox_browser import BrowserSandbox
from .selftest import run_self_test
from .store_sqlite import StateStore
from .workbuddy_client import WorkBuddyClient
from .xiaona_client import XiaoNaClient

logger = logging.getLogger("hypertext.cli")
_CLI_VERSION = "0.6.4"
INTERRUPTION_NOTICE_ATTEMPTS = 3
INTERRUPTION_NOTICE_RETRY_SEC = 30


@contextlib.contextmanager
def acquire_daemon_lock(state_dir: Path) -> Iterator[int | None]:
    """Acquire non-blocking exclusive `flock` on `state_dir/daemon.lock` (`FD_CLOEXEC`).

    Yields the open lock file descriptor when acquired, or `None` if another live
    daemon instance already holds the lock (leaving `daemon.pid` untouched).
    """
    from .daemon_control import lease_lock

    state_dir = Path(state_dir).expanduser()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / "daemon.lock"
    pid_path = state_dir / "daemon.pid"

    with lease_lock(lock_path) as fd:
        if fd is None:
            existing_pid = "unknown"
            with contextlib.suppress(OSError, ValueError):
                existing_pid = str(int(pid_path.read_text(encoding="ascii")))
            msg = (
                f"ℹ️ HyperText daemon already running (PID={existing_pid}, "
                f"lock={lock_path}); exiting duplicate instance cleanly."
            )
            logger.info("%s", msg)
            print(msg)
            yield None
            return

        pid_tmp = state_dir / f".daemon.pid.{os.getpid()}"
        try:
            pid_tmp.write_text(f"{os.getpid()}\n", encoding="ascii")
            pid_tmp.replace(pid_path)
            yield fd
        finally:
            pid_tmp.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(RedactedLogFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    ))
    # The browser's own progress observer still receives its records.
    handler.addFilter(lambda record: not (
        record.name == "browser_use" or record.name.startswith("browser_use.")
    ))
    logging.basicConfig(level=level, handlers=[handler])


def _check_pkg_version(dist_name: str) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _check_fts5_support() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x);")
        con.close()
        return True
    except sqlite3.Error:
        return False


def _format_key_status(status: str, clean_key: str) -> str:
    if status == "configured" and clean_key:
        return f"[OK] 已配置有效密钥 (长度={len(clean_key)} 字符，内容已脱敏隐藏)"
    if status == "placeholder":
        return "[WARN] 未生效（当前仍为 .env 模板占位符，请替换为真实 sk-... 密钥）"
    return "[WARN] 未配置（空值）"


def run_doctor(config: AgentConfig, env_file: str | Path | None = None) -> int:
    """Inspect runtime environment, dependencies, redacted .env status, and macOS permissions."""
    print("============================================================")
    print(f"🩺 HyperText v{_CLI_VERSION} — 环境与权限诊断 (--doctor)")
    print("============================================================")

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)
    print(
        f"{'[OK]' if py_ok else '[FAIL]'} Python 运行时: {py_ver} ({sys.executable})"
    )

    fts_ok = _check_fts5_support()
    print(
        f"{'[OK]' if fts_ok else '[FAIL]'} SQLite 引擎: {sqlite3.sqlite_version} "
        f"(FTS5 混合检索支持: {'已启用' if fts_ok else '缺失'})"
    )

    for pkg in ("openai", "browser-use"):
        ver = _check_pkg_version(pkg)
        if ver:
            print(f"[OK] 依赖包 `{pkg}`: v{ver}")
        else:
            print(
                f"[WARN] 依赖包 `{pkg}`: 当前解释器未安装（生产运行请使用项目 `.venv/bin/python3`）"
            )

    env_display = (
        str(config.loaded_env_file)
        if config.loaded_env_file
        else "未找到 .env（使用默认环境变量）"
    )
    print(f"[INFO] 配置文件路径: {env_display}")
    print(
        f"[INFO] 模型路由模式: PROVIDER_MODE={config.provider_mode} | "
        f"DeepSeek模型={config.deepseek_model} (effort={config.deepseek_reasoning_effort}, "
        f"web_tools={config.enable_deepseek_web_tools})"
    )
    print(
        f"       DEEPSEEK_API_KEY 状态: {_format_key_status(config.deepseek_key_status, config.deepseek_api_key)}"
    )
    print(
        f"       OPENAI_API_KEY   状态: {_format_key_status(config.openai_key_status, config.openai_api_key)}"
    )

    if config.allowed_senders:
        print(
            f"[OK] ALLOWED_SENDERS 管理员白名单: 已配置 {len(config.allowed_senders)} 个受信任发送者"
        )
    else:
        print(
            "[WARN] ALLOWED_SENDERS 管理员白名单: 当前为空（上线前请配置管理员手机号或 Apple ID）"
        )

    print(
        f"[INFO] 仅限 iMessage 真手机号门禁 (IMESSAGE_ONLY_SERVICE): {config.imessage_only_service} "
        "(拦截 SMS/RCS、1069/955/400 服务号、企业 RCS 机器人与群聊)"
    )
    print(
        f"[INFO] 同号自聊模式 (ALLOW_SELF_CHAT): {config.allow_self_chat} "
        f"(触发前缀: `{config.self_chat_prefix}`, 4层防回环保护已启用)"
    )
    print(
        f"[INFO] 多用户内测邀请码 (ENABLE_INVITE_CODES): {config.enable_invite_codes} "
        f"(静态种子码={len(config.invite_codes)} 个, 默认日配额: 消息={config.default_member_daily_msg_limit}/天, "
        f"浏览器={config.default_member_daily_browser_limit}/天)"
    )
    print(
        f"[INFO] 双速秒回确认 (ENABLE_INSTANT_ACK): {config.enable_instant_ack} "
        f"(延迟阈值={config.fast_ack_delay_sec}s, 兜底安抚间隔={config.heartbeat_interval_sec}s, "
        f"单轮上限={config.max_heartbeat_per_turn}次)"
    )
    print(
        f"[INFO] 浏览器沙箱并发池 (BROWSER_MAX_CONCURRENCY): {config.browser_max_concurrency} "
        "(按发送者 SHA-256 独立 Profile 隔离)"
    )
    readiness = XiaoNaClient(config).check_readiness()
    print(
        f"[INFO] XiaoNa Relay (显式唤醒通道): URL configured={readiness['url_configured']} | "
        f"Key configured={readiness['key_configured']} (hot-reload ready)"
    )
    wb_ready = WorkBuddyClient(config).check_readiness()
    print(
        f"[INFO] WorkBuddy CLI (本机显式通道): CLI found={wb_ready['cli_found']} | "
        f"timeout={wb_ready['timeout_sec']}s"
    )
    dsh_ready = DSHClient(config).check_readiness()
    print(
        f"[INFO] DSH CLI (本机显式通道): CLI found={dsh_ready['cli_found']} | "
        f"timeout={dsh_ready['timeout_sec']}s"
    )
    cdx_ready = CodexClient(config).check_readiness()
    print(
        f"[INFO] Codex CLI (本机显式通道): CLI found={cdx_ready['cli_found']} | "
        f"timeout={cdx_ready['timeout_sec']}s"
    )
    agy_ready = AGYClient(config).check_readiness()
    print(
        f"[INFO] AGY CLI (本机显式通道): CLI found={agy_ready['cli_found']} | "
        f"timeout={agy_ready['timeout_sec']}s"
    )
    dm_ready = DreaminaClient(config).check_readiness()
    print(
        f"[INFO] Dreamina Canvas CLI (即梦显式通道): CLI found={dm_ready['cli_found']} | "
        f"timeout={dm_ready['timeout_sec']}s"
    )

    chat_db = config.chat_db_path
    try:
        if not chat_db.exists():
            print(f"[WARN] iMessage 数据库: 未找到 `{chat_db}`（请确认已登录 Messages.app）")
        else:
            uri = f"file:{chat_db}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=3.0) as conn:
                row = conn.execute("SELECT COUNT(*) FROM message").fetchone()
                total_msgs = int(row[0]) if row else 0
            print(
                f"[OK] Full Disk Access (chat.db 只读校验): 可正常读取 `{chat_db}` (共 {total_msgs} 条历史记录)"
            )
    except (PermissionError, sqlite3.OperationalError, OSError) as exc:
        print(
            f"[WARN] Full Disk Access (chat.db 只读校验): 暂无读取权限 ({exc})。\n"
            "       👉 请在 macOS「系统设置 -> 隐私与安全性 -> 完全磁盘访问权限」中勾选项目 `.venv/bin/python3` 或终端应用。"
        )

    state_dir = config.state_dir
    try:
        if state_dir.exists():
            writable = state_dir.is_dir()
            print(
                f"{'[OK]' if writable else '[WARN]'} 状态存储目录: `{state_dir}` 已存在"
            )
        else:
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            print(f"[OK] 状态存储目录: 已初始化 `{state_dir}`")
    except OSError as exc:
        print(
            f"[WARN] 状态存储目录 `{state_dir}` 当前受限或尚未创建 ({exc})；在宿主机运行时将自动创建。"
        )

    print("------------------------------------------------------------")
    if not py_ok or not fts_ok:
        print("❌ 基础 Python / SQLite FTS5 环境不满足最低要求。")
        return 1
    print("✅ 诊断完成（未输出任何敏感密钥明文）。")
    return 0


def run_create_invite(
    config: AgentConfig,
    code: str | None = None,
    max_uses: int = 1,
    note: str = "",
) -> int:
    """Create a new invite code in StateStore and display activation instructions."""
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = StateStore(config.sqlite_db_path)
    try:
        created_code = store.create_invite_code(
            code=code,
            max_uses=max_uses,
            daily_msg_limit=config.default_member_daily_msg_limit,
            daily_browser_limit=config.default_member_daily_browser_limit,
            note=note,
        )
        print("============================================================")
        print("🎟️  已生成 HyperText 内测邀请码")
        print("============================================================")
        print(f"邀请码 (Code)     : {created_code}")
        print(f"最大可用次数      : {max(1, int(max_uses))}")
        print(f"每日消息上限      : {config.default_member_daily_msg_limit} 条/天")
        print(f"每日浏览器上限    : {config.default_member_daily_browser_limit} 次/天")
        if note:
            print(f"备注 (Note)       : {note}")
        print("------------------------------------------------------------")
        print(f"👉 请确保 `.env` 中已开启 `ENABLE_INVITE_CODES=true`，内测用户直接发送 `{created_code}` 即可自助激活。")
        return 0
    finally:
        store.close()


def run_list_users(config: AgentConfig) -> int:
    """Display authorized beta users, today's quota consumption, and invite code status."""
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = StateStore(config.sqlite_db_path)
    try:
        if config.enable_invite_codes and config.invite_codes:
            store.seed_static_invite_codes(
                config.invite_codes,
                daily_msg_limit=config.default_member_daily_msg_limit,
                daily_browser_limit=config.default_member_daily_browser_limit,
            )
        users = store.list_authorized_users()
        codes = store.list_invite_codes()

        print("============================================================")
        print(f"👥 已激活内测用户列表 (共 {len(users)} 人)")
        print("============================================================")
        if not users:
            print("（暂无通过邀请码激活的内测成员）")
        else:
            for u in users:
                print(
                    f"- {u['sender']} | role={u['role']} | status={u['status']} | "
                    f"今日消息={u['today_msg_count']}/{u['daily_msg_limit']} | "
                    f"今日浏览器={u['today_browser_count']}/{u['daily_browser_limit']} | "
                    f"来源码={u['activated_via'] or '-'} | 激活于={u['created_at']}"
                )

        print("\n============================================================")
        print(f"🎟️  邀请码台账 (共 {len(codes)} 个)")
        print("============================================================")
        if not codes:
            print("（暂无邀请码，可使用 `--create-invite` 创建）")
        else:
            for c in codes:
                note_part = f" | 备注={c['note']}" if c.get("note") else ""
                print(
                    f"- {c['code']} | 已用={c['used_count']}/{c['max_uses']} | "
                    f"状态={c['status']} | 配额(msg={c['daily_msg_limit']}, browser={c['daily_browser_limit']})"
                    f"{note_part}"
                )
        return 0
    finally:
        store.close()


def run_allow_user(config: AgentConfig, handle: str, note: str = "") -> int:
    """Hot-whitelist a sender handle directly into state.db without restarting the daemon."""
    store = StateStore(config.sqlite_db_path)
    try:
        try:
            norm = store.allow_user(
                sender=handle,
                daily_msg_limit=config.default_member_daily_msg_limit,
                daily_browser_limit=config.default_member_daily_browser_limit,
                note=note,
                require_mobile=True,
            )
        except ValueError as exc:
            print(f"⚠️ {exc}")
            return 1
        print("============================================================")
        print("✅ 已热更新授权白名单用户（无需重启守护进程立即生效）")
        print("============================================================")
        print(f"授权账号 : {norm}")
        print(
            f"每日配额 : 消息 {config.default_member_daily_msg_limit} 条/天 | "
            f"浏览器 {config.default_member_daily_browser_limit} 次/天"
        )
        if note:
            print(f"备注信息 : {note}")
        return 0
    finally:
        store.close()


def run_revoke_user(config: AgentConfig, handle: str) -> int:
    """Hot-revoke a sender handle in state.db immediately."""
    store = StateStore(config.sqlite_db_path)
    try:
        ok = store.revoke_user(handle)
        if not ok:
            print("⚠️ 提供的账号为空或格式无效。")
            return 1
        print("============================================================")
        print("🚫 已热吊销用户访问权限（立即生效）")
        print("============================================================")
        print(f"已吊销账号 : {handle.strip()}")
        return 0
    finally:
        store.close()


async def run_cli_repl(config: AgentConfig) -> None:
    """Interactive local terminal conversation mode (--cli) without requiring chat.db FDA."""
    store = StateStore(config.sqlite_db_path)
    bridge = CliBridge(quiet=False)
    human_gate = HumanGate(default_timeout=config.human_gate_timeout_sec)
    browser_sandbox = BrowserSandbox(config=config, human_gate=human_gate, bridge=bridge)
    orchestrator = HyperTextOrchestrator(
        config=config,
        store=store,
        bridge=bridge,
        human_gate=human_gate,
        browser_sandbox=browser_sandbox,
    )

    stop_event = asyncio.Event()
    cron_task = asyncio.create_task(
        proactive_cron_loop(orchestrator, config.cron_interval_sec, stop_event)
    )
    active_tasks: set[asyncio.Task[object]] = set()
    cli_sender = next(iter(config.allowed_senders), "local-cli-user")
    seq = 1

    print("============================================================")
    print(f"💬 HyperText v{_CLI_VERSION} 本地交互终端 (--cli) | 当前身份: {cli_sender}")
    print("输入消息回车发送；若触发 2FA 门禁可直接输入验证码回复；输入 exit / quit 退出。")
    print("============================================================")

    try:
        while True:
            try:
                line = await asyncio.to_thread(input, "你 > ")
            except (EOFError, KeyboardInterrupt):
                print("\n👋 正在退出 CLI 模式...")
                break

            text = line.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit", ":q"):
                break

            inbound = InboundMessage(
                rowid=seq,
                guid=f"cli-{seq}",
                sender=cli_sender,
                text=text,
                timestamp_unix=asyncio.get_running_loop().time(),
            )
            seq += 1
            t = asyncio.create_task(orchestrator.handle_inbound_message(inbound))
            active_tasks.add(t)
            t.add_done_callback(active_tasks.discard)
            await asyncio.sleep(0.05)
    finally:
        stop_event.set()
        cron_task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        store.close()


async def run_imessage_daemon(config: AgentConfig, *, supervisor_pid: int | None = None) -> int:
    """Main macOS iMessage polling & proactive cron daemon loop (single-instance `flock` + `SIGHUP` immune)."""
    from .daemon_control import STOP_REQUEST, WORKER_LOCK_BUSY, stop_request_id

    stop_path = config.state_dir / STOP_REQUEST
    initial_stop_request = stop_request_id(stop_path)
    with acquire_daemon_lock(config.state_dir) as lock_fd:
        if lock_fd is None:
            return WORKER_LOCK_BUSY if supervisor_pid is not None else 0

        if hasattr(signal, "SIGHUP"):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGHUP, signal.SIG_IGN)

        config.state_dir.chmod(0o700)
        store = StateStore(config.sqlite_db_path)
        human_gate = HumanGate(default_timeout=config.human_gate_timeout_sec)
        bridge = MacIMessageBridge(
            chat_db_path=config.chat_db_path,
            store=store,
            allowed_senders=config.allowed_senders,
            config=config,
            human_gate=human_gate,
            durable_inbox=True,
        )
        browser_sandbox = BrowserSandbox(config=config, human_gate=human_gate, bridge=bridge)
        orchestrator = HyperTextOrchestrator(
            config=config,
            store=store,
            bridge=bridge,
            human_gate=human_gate,
            browser_sandbox=browser_sandbox,
        )

        cursor = bridge.align_cursor_to_latest()
        logger.info(
            "HyperText iMessage Daemon v%s started | pid=%d | model=%s | mode=%s | self_chat=%s | invites=%s | initial_rowid=%d",
            _CLI_VERSION,
            os.getpid(),
            config.deepseek_model if config.provider_mode == "deepseek" else config.openai_model,
            config.provider_mode,
            config.allow_self_chat,
            config.enable_invite_codes,
            cursor,
        )

        stop_event = asyncio.Event()
        active_tasks: set[asyncio.Task[None]] = set()

        def request_shutdown() -> None:
            stop_event.set()
            human_gate.close()

        loop = asyncio.get_running_loop()
        installed_signals = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_shutdown)
                installed_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                pass

        def supervisor_lost() -> bool:
            return supervisor_pid is not None and os.getppid() != supervisor_pid

        async def watch_stop_request() -> None:
            while not stop_event.is_set():
                if supervisor_lost():
                    logger.warning("Supervisor exited; releasing worker and in-flight turns")
                    request_shutdown()
                    return
                request = stop_request_id(stop_path)
                if request is not None and request != initial_stop_request:
                    request_shutdown()
                    return
                await asyncio.sleep(0.1)

        async def notify_interruption(msg: InboundMessage) -> None:
            for attempt in range(INTERRUPTION_NOTICE_ATTEMPTS):
                if stop_event.is_set():
                    return
                try:
                    await orchestrator.notify_interrupted_imessage(msg)
                    break
                except MessageDeliveryError:
                    logger.warning("Interruption notice delivery failed for row %d", msg.rowid)
                    if attempt + 1 < INTERRUPTION_NOTICE_ATTEMPTS:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(stop_event.wait(), timeout=INTERRUPTION_NOTICE_RETRY_SEC)
            store.complete_imessage(msg.rowid)

        in_flight_inbox_rowids: set[int] = set()
        deferred_startup_rowids: set[int] = set()
        inbound_fields = set(InboundMessage.__dataclass_fields__)

        async def handle_message(msg: InboundMessage, interrupted: bool) -> None:
            try:
                if interrupted:
                    await notify_interruption(msg)
                    return
                if not msg.approval_only:
                    store.start_imessage(msg.rowid)
                try:
                    await orchestrator.handle_inbound_message(msg)
                except MessageDeliveryError:
                    logger.warning("Outgoing message delivery failed for row %d", msg.rowid)
                if not msg.approval_only:
                    store.complete_imessage(msg.rowid)
            except Exception:
                logger.exception("Could not complete iMessage row %d", msg.rowid)
            finally:
                in_flight_inbox_rowids.discard(msg.rowid)

        def schedule_message(msg: InboundMessage, *, interrupted: bool = False) -> None:
            if not msg.approval_only:
                in_flight_inbox_rowids.add(msg.rowid)
            task = asyncio.create_task(handle_message(msg, interrupted))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

        async def drain_pending_inbox(*, only_rowids: set[int] | None = None) -> set[int]:
            remaining_deferred: set[int] = set()
            for payload, interrupted in store.pending_imessages():
                raw_obj = json.loads(payload)
                filtered_obj = {
                    k: v for k, v in raw_obj.items() if k in inbound_fields
                }
                msg = InboundMessage(**filtered_obj)
                if only_rowids is not None and msg.rowid not in only_rowids:
                    continue
                if stop_event.is_set():
                    if only_rowids is not None:
                        remaining_deferred.add(msg.rowid)
                    continue
                if msg.rowid in in_flight_inbox_rowids:
                    continue
                try:
                    is_valid = await asyncio.to_thread(
                        lambda: bridge.revalidate_queued_message(msg, check_db=True)
                    )
                except (sqlite3.Error, OSError) as db_exc:
                    logger.warning(
                        "Deferring queued inbox validation for rowid=%d due to chat.db read error: %s",
                        msg.rowid,
                        db_exc,
                    )
                    remaining_deferred.add(msg.rowid)
                    continue
                if not is_valid:
                    logger.info(
                        "Dropping invalid or non-iMessage queued inbox rowid=%d",
                        msg.rowid,
                    )
                    store.complete_imessage(msg.rowid)
                    continue
                if not interrupted and msg.attachment_paths and not msg.image_data_urls:
                    try:
                        await asyncio.to_thread(bridge.restore_message_images, msg)
                    except OSError as attach_exc:
                        logger.warning(
                            "Deferring attachment restore for rowid=%d: %s",
                            msg.rowid,
                            attach_exc,
                        )
                        remaining_deferred.add(msg.rowid)
                        continue
                if stop_event.is_set():
                    break
                schedule_message(msg, interrupted=interrupted)
            return remaining_deferred

        cron_task = asyncio.create_task(
            proactive_cron_loop(orchestrator, config.cron_interval_sec, stop_event)
        )
        stop_watcher = asyncio.create_task(watch_stop_request())
        poll_sleep = max(0.05, float(config.poll_interval_sec))

        try:
            if supervisor_lost():
                request_shutdown()
            if not stop_event.is_set():
                deferred_startup_rowids = await drain_pending_inbox()
            while not stop_event.is_set():
                try:
                    if deferred_startup_rowids:
                        deferred_startup_rowids = await drain_pending_inbox(
                            only_rowids=deferred_startup_rowids
                        )
                    messages = await asyncio.to_thread(bridge.poll_new_messages)
                    for msg in messages:
                        if msg.approval_only or not stop_event.is_set():
                            schedule_message(msg)
                except sqlite3.OperationalError as db_exc:
                    logger.error(
                        "读取 chat.db 失败 (%s) — 请确认已授予完全磁盘访问权限 (Full Disk Access)。",
                        db_exc,
                    )
                except Exception:
                    logger.exception("Error during iMessage poll iteration")

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_sleep)
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            logger.info("Shutting down HyperText Daemon...")
            request_shutdown()
            if supervisor_lost():
                cron_task.cancel()
                for task in active_tasks:
                    task.cancel()
            stop_watcher.cancel()
            try:
                await asyncio.gather(cron_task, stop_watcher, *active_tasks, return_exceptions=True)
            finally:
                for sig in installed_signals:
                    loop.remove_signal_handler(sig)
                store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hypertext",
        description=f"HyperText v{_CLI_VERSION} — Your Ambient, Async AI Personal Agent over Apple Messages (iMessage) (run via `hypertext` or `python3 hypertext_agent.py`, state in ~/.hypertext)",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="指定 .env 配置文件路径（默认依次查找当前目录 .env 与项目根目录 .env）",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="启动本地终端交互模式（无需 iMessage 完全磁盘访问权限即可调试）",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="运行离线全链路 16 大子系统自动化自检",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="检查 Python/SQLite/FTS5、依赖版本、自聊/内测配置及 macOS 权限（零明文泄露）",
    )
    parser.add_argument(
        "--create-invite",
        action="store_true",
        help="生成一个新的多用户内测邀请码并写入本地状态库",
    )
    parser.add_argument(
        "--invite-code",
        type=str,
        default=None,
        help="配合 --create-invite 指定自定义邀请码（留空自动生成 HT-XXXXXX，同时兼容旧版 INST-XXXXXX）",
    )
    parser.add_argument(
        "--invite-uses",
        type=int,
        default=1,
        help="配合 --create-invite 指定最大激活人数上限（默认 1）",
    )
    parser.add_argument(
        "--invite-note",
        type=str,
        default="",
        help="配合 --create-invite 添加备注信息",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="查看所有已激活内测成员、今日配额消耗及邀请码台账",
    )
    parser.add_argument(
        "--allow-user",
        type=str,
        default=None,
        metavar="HANDLE",
        help="直接将指定真实手机号热加入内测授权白名单（无需改 .env 或重启；管理员邮箱请配置在 ALLOWED_SENDERS）",
    )
    parser.add_argument(
        "--user-note",
        type=str,
        default="",
        help="配合 --allow-user 添加用户备注",
    )
    parser.add_argument(
        "--revoke-user",
        type=str,
        default=None,
        metavar="HANDLE",
        help="立即吊销指定内测用户的访问权限（status='revoked'，无需重启立即生效）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="启用 DEBUG 级别详细日志",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    from .daemon_control import SUPERVISED_STATE_ENV, SUPERVISOR_PID_ENV

    supervised_state = os.environ.pop(SUPERVISED_STATE_ENV, None)
    supervisor = os.environ.pop(SUPERVISOR_PID_ENV, None)
    args = build_parser().parse_args(argv)
    _configure_logging(verbose=args.verbose)

    if args.self_test:
        ok = run_self_test()
        return 0 if ok else 1

    config = AgentConfig.from_env(env_file=args.env_file)

    if args.doctor:
        return run_doctor(config=config, env_file=args.env_file)

    if args.create_invite:
        return run_create_invite(
            config=config,
            code=args.invite_code,
            max_uses=args.invite_uses,
            note=args.invite_note,
        )

    if args.list_users:
        return run_list_users(config=config)

    if args.allow_user:
        return run_allow_user(config=config, handle=args.allow_user, note=args.user_note)

    if args.revoke_user:
        return run_revoke_user(config=config, handle=args.revoke_user)

    if args.cli:
        asyncio.run(run_cli_repl(config))
        return 0

    if supervised_state is not None:
        config.state_dir = Path(supervised_state)
    return asyncio.run(run_imessage_daemon(
        config, supervisor_pid=int(supervisor) if supervisor else None
    ))


if __name__ == "__main__":
    raise SystemExit(main())
