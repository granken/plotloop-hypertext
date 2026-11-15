"""Browser-Use 1.0 sandboxed runner with per-user profile isolation, concurrency pool, 2FA gate & screenshot delivery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

from .config import normalize_sender
from .formatter import format_for_imessage, is_sensitive_attachment_path

if TYPE_CHECKING:
    from .bridge_imessage import MessageBridge
    from .config import AgentConfig
    from .human_gate import HumanGate

logger = logging.getLogger(__name__)


class BrowserSandbox:
    """Isolated Chromium sandbox powered by Browser-Use 1.0 with per-sender profiles & global concurrency limit."""

    def __init__(
        self,
        config: "AgentConfig",
        human_gate: "HumanGate",
        bridge: "MessageBridge | None" = None,
    ) -> None:
        self.config = config
        self.human_gate = human_gate
        self.bridge = bridge
        self._sender_locks: dict[str, asyncio.Lock] = {}
        self._concurrency_sem = asyncio.Semaphore(
            max(1, int(self.config.browser_max_concurrency))
        )

    @property
    def profile_dir(self) -> Path:
        """Legacy default profile path kept for backward compatibility."""
        return self.config.state_dir / "browser_profile"

    def profile_dir_for(self, sender: str) -> Path:
        """Return isolated per-user Chromium user_data_dir with zero PII in folder names."""
        norm = normalize_sender(sender) or "anonymous"
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        return self.config.state_dir / "browser_profiles" / f"user_{digest}"

    def _get_sender_lock(self, sender: str) -> asyncio.Lock:
        key = normalize_sender(sender) or "anonymous"
        lock = self._sender_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sender_locks[key] = lock
        return lock

    @staticmethod
    def _detect_local_chromium() -> str | None:
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            shutil.which("chromium"),
            shutil.which("google-chrome"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return str(c)
        return None

    @staticmethod
    def _load_bu_classes() -> tuple[Any, Any, Any, Any]:
        from browser_use import Agent as BrowserAgent, Browser, ChatOpenAI, Tools

        return BrowserAgent, Browser, ChatOpenAI, Tools

    async def _capture_page_screenshot(self, active_browser: Any, out_path: Path) -> bool:
        """Best-effort screenshot capture via Browser-Use 1.0 Browser / Playwright / CDP page object."""
        if active_browser is None:
            return False

        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Direct take_screenshot on Browser instance if exposed
        take_fn = getattr(active_browser, "take_screenshot", None)
        if callable(take_fn):
            try:
                res = take_fn(path=str(out_path))
            except TypeError:
                res = take_fn()
            if asyncio.iscoroutine(res):
                res = await res
            if out_path.exists() and out_path.stat().st_size > 0:
                return True
            if isinstance(res, (bytes, bytearray)) and res:
                out_path.write_bytes(bytes(res))
                return True
            if isinstance(res, str) and res.strip():
                raw_str = res.strip()
                if raw_str.startswith("data:image") and "," in raw_str:
                    raw_str = raw_str.split(",", 1)[1]
                try:
                    out_path.write_bytes(base64.b64decode(raw_str))
                    return True
                except Exception:
                    if Path(raw_str).is_file():
                        shutil.copy2(raw_str, out_path)
                        return True

        # 2. Playwright / CDP page via get_current_page()
        get_page = getattr(active_browser, "get_current_page", None)
        if callable(get_page):
            page = get_page()
            if asyncio.iscoroutine(page):
                page = await page
            shot_fn = getattr(page, "screenshot", None) if page is not None else None
            if callable(shot_fn):
                try:
                    res = shot_fn(path=str(out_path))
                except TypeError:
                    res = shot_fn()
                if asyncio.iscoroutine(res):
                    res = await res
                if out_path.exists() and out_path.stat().st_size > 0:
                    return True
                if isinstance(res, (bytes, bytearray)) and res:
                    out_path.write_bytes(bytes(res))
                    return True

        return False

    async def run_task(
        self,
        sender: str,
        task_prompt: str,
        max_steps: int | None = None,
    ) -> str:
        """Run a browser errand inside sender's isolated Chromium profile with concurrency pooling."""
        provider, api_key, base_url, is_ds = self.config.resolve_active_provider()
        if provider == "unconfigured":
            return "尚未配置可用的大模型密钥，暂时无法打开网页执行操作。"

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
        tools = Tools()

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
            )
            return f"User response: {reply}"

        @tools.action(
            "Send a concise status update to the human user via iMessage during multi-step browser tasks"
        )
        async def notify_progress_via_imessage(status_update: str) -> str:
            if self.bridge is not None and status_update.strip():
                clean_status = format_for_imessage(status_update.strip())
                await self.bridge.send_text(
                    sender, f"🌐 [网页进展] {clean_status}"
                )
            return "Status update sent."

        @tools.action(
            "Save or send a screenshot/file from local path (or capture current browser page screenshot) "
            "to the user via iMessage when visual confirmation, QR code, or receipt proof is helpful."
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
                    await self.bridge.send_attachment(sender, cand)
                    return f"File {cand.name} sent to user via iMessage."

            shots_dir = self.config.state_dir / "deliverables"
            shot_file = shots_dir / f"页面截图_{int(time.time() * 1000)}.png"
            captured = await self._capture_page_screenshot(browser_ref["browser"], shot_file)
            if not captured:
                return "Unable to capture live browser screenshot on current driver."
            await self.bridge.send_attachment(sender, shot_file, caption=arg)
            return f"Screenshot {shot_file.name} sent to user via iMessage."

        safety_rules = (
            "CRITICAL RULES:\n"
            "1. If the website requests a 2FA/MFA code, SMS/email OTP, or login verification, "
            "call `ask_human_via_imessage` immediately and wait for the code.\n"
            "2. Before clicking any final Pay, Purchase, Cancel Subscription, or Delete button, "
            "call `ask_human_via_imessage` with the exact item/price summary to get explicit approval.\n"
            "3. When visual proof, QR login, or order confirmation screenshot helps the user, "
            "call `send_screenshot_or_file_via_imessage`.\n"
            "4. Reply concisely in plain natural language (no Markdown symbols) when finished."
        )

        effective_steps = max(1, int(max_steps or self.config.browser_max_steps))
        user_profile_dir = self.profile_dir_for(sender)

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

                bu_agent = BrowserAgent(**agent_kwargs)
                try:
                    history = await bu_agent.run(max_steps=effective_steps)
                    if hasattr(history, "final_result") and callable(history.final_result):
                        res = history.final_result()
                        return str(res) if res else "网页操作已完成。"
                    return str(history)
                finally:
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
    ) -> str:
        """Backward-compatible alias for run_task."""
        return await self.run_task(sender=sender, task_prompt=task_prompt, max_steps=max_steps)
