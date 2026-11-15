"""Per-sender isolated Browser-Use Chromium sandbox with live domain/step milestones, HumanGate 2FA, and screenshot delivery."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from .bridge_imessage import MessageBridge
from .config import AgentConfig, normalize_sender
from .formatter import format_for_imessage, is_sensitive_attachment_path
from .human_gate import HumanGate

logger = logging.getLogger("hypertext.sandbox_browser")

_IGNORED_LOG_DOMAINS = {
    "api.deepseek.com",
    "api.openai.com",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
}

_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)(?::\d+)?", re.IGNORECASE)
_STEP_RE = re.compile(r"(?:\bStep\s+(\d+)\b|第\s*(\d+)\s*步)", re.IGNORECASE)


class _BrowserUseLogObserver(logging.Handler):
    """Lightweight scoped log observer capturing visited hostnames and step counts from browser_use."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.step_count: int = 0
        self.latest_domain: str = ""
        self.visited_domains: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if not msg:
            return

        for m_step in _STEP_RE.finditer(msg):
            raw_num = m_step.group(1) or m_step.group(2)
            if raw_num:
                try:
                    num = int(raw_num)
                    if num > self.step_count:
                        self.step_count = num
                except ValueError:
                    pass

        for m_host in _URL_HOST_RE.finditer(msg):
            host = m_host.group(1).lower().strip(".")
            if not host or "." not in host or host in _IGNORED_LOG_DOMAINS:
                continue
            self.latest_domain = host
            if host not in self.visited_domains:
                self.visited_domains.append(host)


class BrowserSandbox:
    """Executes interactive web tasks in per-user isolated Chromium profiles with concurrency pooling."""

    def __init__(
        self,
        config: AgentConfig,
        human_gate: HumanGate,
        bridge: MessageBridge | None = None,
    ) -> None:
        self.config = config
        self.human_gate = human_gate
        self.bridge = bridge
        self._sender_locks: dict[str, asyncio.Lock] = {}
        self._concurrency_sem = asyncio.Semaphore(max(1, int(config.browser_max_concurrency)))

    @property
    def profile_dir(self) -> Path:
        """Legacy default browser profile path (per-user profiles use profile_dir_for)."""
        return self.config.browser_profile_dir

    def profile_dir_for(self, sender: str) -> Path:
        """Return deterministic isolated Chromium user-data-dir for a given sender."""
        norm = normalize_sender(sender) or "anonymous"
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        return self.config.browser_profiles_root / f"user_{digest}"

    def _get_sender_lock(self, sender: str) -> asyncio.Lock:
        norm = normalize_sender(sender) or "anonymous"
        lock = self._sender_locks.get(norm)
        if lock is None:
            lock = asyncio.Lock()
            self._sender_locks[norm] = lock
        return lock

    def _detect_local_chromium(self) -> str | None:
        if self.config.chromium_executable_path:
            cand = Path(self.config.chromium_executable_path).expanduser()
            if cand.exists():
                return str(cand)
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return None

    @staticmethod
    def _load_bu_classes() -> tuple[Any, Any, Any, Any]:
        from browser_use import Agent as BrowserAgent, Browser, ChatOpenAI, Tools  # type: ignore

        return BrowserAgent, Browser, ChatOpenAI, Tools

    async def _capture_page_screenshot(self, active_browser: Any, out_path: Path) -> bool:
        """Attempt best-effort screenshot capture across Browser-Use / Playwright versions."""
        if active_browser is None:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)

        for method_name in ("take_screenshot", "screenshot"):
            fn = getattr(active_browser, method_name, None)
            if callable(fn):
                try:
                    res = fn(path=str(out_path))
                except TypeError:
                    res = fn()
                if asyncio.iscoroutine(res):
                    res = await res
                if out_path.exists() and out_path.stat().st_size > 0:
                    return True
                if isinstance(res, (bytes, bytearray)) and len(res) > 0:
                    out_path.write_bytes(bytes(res))
                    return True
                if isinstance(res, str) and res.strip():
                    import base64

                    raw_b64 = res.split(",", 1)[-1]
                    out_path.write_bytes(base64.b64decode(raw_b64))
                    return True

        get_page = getattr(active_browser, "get_current_page", None)
        if callable(get_page):
            page = get_page()
            if asyncio.iscoroutine(page):
                page = await page
            if page is not None and hasattr(page, "screenshot"):
                res = page.screenshot(path=str(out_path))
                if asyncio.iscoroutine(res):
                    res = await res
                if out_path.exists() and out_path.stat().st_size > 0:
                    return True
                if isinstance(res, (bytes, bytearray)) and len(res) > 0:
                    out_path.write_bytes(bytes(res))
                    return True

        return False

    async def run_task(
        self,
        sender: str,
        task_prompt: str,
        max_steps: int | None = None,
        reply_to: str | None = None,
        progress_interval_sec: float | None = None,
        on_progress_sent: Callable[[], None] | None = None,
    ) -> str:
        """Run a browser errand inside sender's isolated Chromium profile with live progress milestones."""
        provider, api_key, base_url, is_ds = self.config.resolve_active_provider()
        if provider == "unconfigured":
            return "尚未配置可用的大模型密钥，暂时无法打开网页执行操作。"

        effective_recipient = (reply_to or sender).strip()
        model_name = (
            self.config.deepseek_model if is_ds else self.config.openai_model
        )
        bu_base_url = base_url.rstrip("/")
        if is_ds and not bu_base_url.endswith("/v1"):
            bu_base_url = f"{bu_base_url}/v1"

        BrowserAgent, Browser, ChatOpenAI, Tools = self._load_bu_classes()

        llm_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": bu_base_url,
        }
        if is_ds and self.config.deepseek_reasoning_effort != "none":
            llm_kwargs["reasoning_effort"] = self.config.deepseek_reasoning_effort
        try:
            llm = ChatOpenAI(model=model_name, **llm_kwargs)
        except TypeError:
            llm = ChatOpenAI(model_name, **llm_kwargs)

        browser_ref: dict[str, Any] = {"browser": None}
        progress_state: dict[str, float] = {"last_sent_at": time.time()}
        tools = Tools()

        def _mark_progress_sent() -> None:
            progress_state["last_sent_at"] = time.time()
            if on_progress_sent is not None:
                try:
                    on_progress_sent()
                except Exception:
                    pass

        @tools.action(
            "Ask the human user via iMessage for a 2FA/OTP verification code "
            "or explicit approval before submitting an order, booking, payment, or account change."
        )
        async def ask_human_via_imessage(question: str) -> str:
            clean_q = format_for_imessage(question)
            reply = await self.human_gate.request_input(
                sender=sender,
                question=f"🔐 [需要你确认] {clean_q}\n直接回复验证码或你的决定就行：",
                bridge=self.bridge,
                reply_to=effective_recipient,
            )
            _mark_progress_sent()
            return reply

        @tools.action(
            "Send a concise 1-sentence progress update with key findings to the user via iMessage "
            "whenever switching websites or completing an important sub-step."
        )
        async def notify_progress_via_imessage(status_update: str) -> str:
            if self.bridge is not None and status_update.strip():
                clean_note = format_for_imessage(status_update.strip())
                _mark_progress_sent()
                await self.bridge.send_text(
                    effective_recipient, f"🌐 [网页进展] {clean_note}"
                )
            return "Progress update delivered."

        @tools.action(
            "Capture a live browser screenshot or send a downloaded local file to the user via iMessage. "
            "Pass an optional short caption or a local file path."
        )
        async def send_screenshot_or_file_via_imessage(path_or_caption: str) -> str:
            if self.bridge is None:
                return "Message bridge is not attached."

            arg = (path_or_caption or "").strip()
            if arg:
                cand = Path(arg).expanduser()
                if cand.exists() and cand.is_file():
                    blocked, reason = is_sensitive_attachment_path(cand)
                    if blocked:
                        return f"Blocked by security policy: {reason}"
                    _mark_progress_sent()
                    await self.bridge.send_attachment(effective_recipient, cand)
                    return f"File {cand.name} sent to user via iMessage."

            shots_dir = self.config.state_dir / "deliverables"
            shot_file = shots_dir / f"页面截图_{int(time.time() * 1000)}.png"
            captured = await self._capture_page_screenshot(browser_ref["browser"], shot_file)
            if not captured:
                return "Unable to capture live browser screenshot on current driver."
            _mark_progress_sent()
            await self.bridge.send_attachment(effective_recipient, shot_file, caption=arg)
            return f"Screenshot {shot_file.name} sent to user via iMessage."

        safety_rules = (
            "CRITICAL RULES:\n"
            "1. Whenever you switch websites or finish an important sub-step, call `notify_progress_via_imessage` "
            "with a concise 1-sentence summary of what you just found or completed.\n"
            "2. If the website requests a 2FA/MFA code, SMS/email OTP, or login verification, "
            "call `ask_human_via_imessage` immediately and wait for the code.\n"
            "3. Before clicking any final Pay, Purchase, Cancel Subscription, or Delete button, "
            "call `ask_human_via_imessage` with the exact item/price summary to get explicit approval.\n"
            "4. When visual proof, QR login, or order confirmation screenshot helps the user, "
            "call `send_screenshot_or_file_via_imessage`.\n"
            "5. Reply concisely in plain natural language (no Markdown symbols) when finished."
        )

        effective_steps = max(1, int(max_steps or self.config.browser_max_steps))
        user_profile_dir = self.profile_dir_for(sender)
        ticker_sec = float(progress_interval_sec) if progress_interval_sec is not None else 18.0
        quiet_threshold = 16.0 if ticker_sec >= 18.0 else max(0.01, ticker_sec * 0.8)

        gate_candidates = [sender, effective_recipient, *self.config.admin_senders_ordered]

        async with self._get_sender_lock(sender):
            async with self._concurrency_sem:
                user_profile_dir.mkdir(parents=True, exist_ok=True)
                try:
                    browser = Browser(
                        "chromium",
                        user_data_dir=str(user_profile_dir),
                        headless=self.config.browser_headless,
                    )
                except TypeError:
                    browser = Browser(
                        executable_path=self._detect_local_chromium(),
                        user_data_dir=str(user_profile_dir),
                        headless=self.config.browser_headless,
                    )
                browser_ref["browser"] = browser

                agent_kwargs: dict[str, Any] = {
                    "task": task_prompt,
                    "llm": llm,
                    "browser": browser,
                    "tools": tools,
                    "extend_system_message": safety_rules,
                }
                if self.config.browser_sensitive_data:
                    agent_kwargs["sensitive_data"] = self.config.browser_sensitive_data

                bu_logger = logging.getLogger("browser_use")
                prev_bu_level = bu_logger.level
                prev_bu_disabled = bu_logger.disabled
                bu_logger.disabled = False
                if bu_logger.getEffectiveLevel() > logging.INFO:
                    bu_logger.setLevel(logging.INFO)
                log_observer = _BrowserUseLogObserver()
                log_observer.setLevel(logging.INFO)
                bu_logger.addHandler(log_observer)

                bu_agent = BrowserAgent(**agent_kwargs)
                progress_ticker_task: asyncio.Task[None] | None = None

                if self.bridge is not None and ticker_sec > 0:

                    async def _live_milestone_ticker() -> None:
                        while True:
                            await asyncio.sleep(ticker_sec)
                            if self.human_gate.has_pending_any(gate_candidates):
                                continue
                            now = time.time()
                            if now - progress_state["last_sent_at"] < quiet_threshold:
                                continue
                            domain = log_observer.latest_domain
                            step = log_observer.step_count
                            if domain and step > 0:
                                msg_txt = (
                                    f"正在网页上继续核对（刚查看了 {domain}，已完成第 {step} 步），有进展随时同步你…"
                                )
                            elif domain:
                                msg_txt = f"正在网页上继续核对（刚查看了 {domain}），有进展随时同步你…"
                            elif step > 0:
                                msg_txt = f"正在网页上继续核对（已完成第 {step} 步），有进展随时同步你…"
                            else:
                                msg_txt = "正在网页上继续加载并核对页面内容，有进展随时同步你…"
                            _mark_progress_sent()
                            if self.bridge is not None:
                                await self.bridge.send_text(effective_recipient, msg_txt)

                    progress_ticker_task = asyncio.create_task(_live_milestone_ticker())

                try:
                    history = await bu_agent.run(max_steps=effective_steps)
                    if hasattr(history, "final_result") and callable(history.final_result):
                        res = history.final_result()
                        return str(res) if res else "网页操作已完成。"
                    return str(history)
                finally:
                    bu_logger.removeHandler(log_observer)
                    bu_logger.setLevel(prev_bu_level)
                    bu_logger.disabled = prev_bu_disabled
                    if progress_ticker_task is not None and not progress_ticker_task.done():
                        progress_ticker_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await progress_ticker_task
                    browser_ref["browser"] = None
                    for method_name in ("stop", "close"):
                        fn = getattr(browser, method_name, None)
                        if callable(fn):
                            maybe_coro = fn()
                            if asyncio.iscoroutine(maybe_coro):
                                await maybe_coro
                            break

    async def execute_web_errand(
        self,
        sender: str,
        task_prompt: str,
        max_steps: int | None = None,
        reply_to: str | None = None,
    ) -> str:
        """Backward-compatible alias for run_task."""
        return await self.run_task(
            sender=sender,
            task_prompt=task_prompt,
            max_steps=max_steps,
            reply_to=reply_to,
        )
