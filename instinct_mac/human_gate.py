"""Per-sender asynchronous Human-in-the-Loop (2FA / confirmation) gate."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .config import normalize_sender

if TYPE_CHECKING:
    from .bridge_imessage import MessageBridge

logger = logging.getLogger("instinct_mac.human_gate")


@dataclass
class PendingApproval:
    sender: str
    question: str
    future: asyncio.Future[str]
    created_at: float


class HumanGate:
    """Manages per-sender asyncio.Future suspension for 2FA codes and approvals."""

    def __init__(self, default_timeout: float = 300.0) -> None:
        self.default_timeout = default_timeout
        self._pending: dict[str, PendingApproval] = {}

    def has_pending(self, sender: str) -> bool:
        key = normalize_sender(sender)
        entry = self._pending.get(key)
        return bool(entry is not None and not entry.future.done())

    def has_pending_any(self, senders: Iterable[str]) -> bool:
        return any(self.has_pending(s) for s in senders if s)

    def get_pending_question(self, sender: str) -> str | None:
        key = normalize_sender(sender)
        entry = self._pending.get(key)
        if entry is not None and not entry.future.done():
            return entry.question
        return None

    def try_resolve(self, sender: str, reply_text: str) -> bool:
        """If sender has a suspended 2FA/approval Future, resolve it immediately."""
        key = normalize_sender(sender)
        entry = self._pending.get(key)
        if entry is None or entry.future.done():
            return False
        answer = (reply_text or "").strip()
        entry.future.set_result(answer)
        self._pending.pop(key, None)
        logger.info("HumanGate unlocked for sender=%s", key)
        return True

    def try_resolve_any(self, senders: Iterable[str], reply_text: str) -> bool:
        """Try resolving pending Future across multiple aliases of the same user."""
        seen: set[str] = set()
        for s in senders:
            norm = normalize_sender(s)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            if self.try_resolve(norm, reply_text):
                return True
        return False

    async def request_input(
        self,
        sender: str,
        question: str,
        bridge: "MessageBridge | None" = None,
        timeout: float | None = None,
        reply_to: str | None = None,
    ) -> str:
        """Send prompt via bridge and suspend current task until sender replies."""
        key = normalize_sender(sender)
        outbound_target = (reply_to or "").strip() or sender
        effective_timeout = self.default_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()

        old = self._pending.pop(key, None)
        if old is not None and not old.future.done():
            old.future.cancel()

        fut: asyncio.Future[str] = loop.create_future()
        self._pending[key] = PendingApproval(
            sender=key,
            question=question,
            future=fut,
            created_at=time.time(),
        )

        if bridge is not None:
            await bridge.send_text(outbound_target, question)

        try:
            return await asyncio.wait_for(fut, timeout=effective_timeout)
        except asyncio.TimeoutError:
            logger.warning("HumanGate timed out after %.1fs for sender=%s", effective_timeout, key)
            if bridge is not None:
                await bridge.send_text(
                    outbound_target,
                    f"⏱️ 等待验证/确认回复已超时（{int(effective_timeout)}秒），本次敏感操作已安全中止。",
                )
            return "TIMEOUT: 用户未在规定时间内回复验证码或确认指令。"
        finally:
            current = self._pending.get(key)
            if current is not None and current.future is fut:
                self._pending.pop(key, None)
