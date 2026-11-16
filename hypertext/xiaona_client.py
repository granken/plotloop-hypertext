"""Explicit-Only Admin XiaoNa Relay Client: hot .env/Keychain reload, per-conversation lock & zero-leak error handling."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .config import (
    AgentConfig,
    _is_placeholder_key,
    _parse_dotenv_text,
    _resolve_dotenv_candidates,
    normalize_sender,
)
from .formatter import format_for_imessage

if TYPE_CHECKING:
    from .store_sqlite import StateStore

logger = logging.getLogger("hypertext.xiaona_client")

MAX_XIAONA_PAYLOAD_BYTES = 60 * 1024
DEFAULT_XIAONA_CONVERSATION_ID = "imessage-owner"
MIN_PRODUCTION_TIMEOUT_SEC = 300.0
KEYCHAIN_SERVICE_NAME = "XIAONA_RELAY_KEY"

# Matches `小纳` or case-insensitive `xiaona` without ASCII word continuation (rejects `/xiaonabot`, `myxiaona`, `小纳bot`)
_XIAONA_NAME_PAT = r"(?:小纳(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?i:xiaona)(?![A-Za-z0-9_]))"

_SLASH_OR_AT_TRIGGER_RE = re.compile(
    rf"^[/／@]\s*{_XIAONA_NAME_PAT}(?:\s|$|[:：,，!！?？.。、~～\-]|(?=[\u4e00-\u9fff]))"
)
_LEADING_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_XIAONA_NAME_PAT}(?:\s*[:：,，!！、~～]|\s+\S|$)"
)
_DELEGATION_VERB_RE = re.compile(
    rf"(?:问下|问一下|问问|请问|让|请|叫|找|喊|麻烦|呼叫|唤醒|转给|交给|发给|委托|调用|用)\s*{_XIAONA_NAME_PAT}"
)
_WITH_XIAONA_VERB_RE = re.compile(
    rf"跟\s*{_XIAONA_NAME_PAT}\s*(?:说|问|确认|查|核对)"
)
_XIAONA_ACTION_RE = re.compile(
    rf"{_XIAONA_NAME_PAT}\s*(?:那边|那里|帮我|回答|查|看)"
)

_RESET_CMD_RE = re.compile(
    rf"^(?:"
    rf"[/／@]?\s*{_XIAONA_NAME_PAT}\s*(?:[:：]\s*)?(?:reset|clear|重置|清空|重置会话|清空会话|重置上下文|清空上下文)"
    rf"|(?:重置|清空)\s*{_XIAONA_NAME_PAT}\s*(?:的)?(?:会话|上下文|对话)?"
    rf")$",
    re.IGNORECASE,
)

_STRIP_SLASH_AT_RE = re.compile(rf"^[/／@]\s*{_XIAONA_NAME_PAT}[\s:：,，、\-]*")
_STRIP_DELEGATION_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*"
    rf"(?:问下|问一下|问问|请问|让|请|叫|找|喊|麻烦|呼叫|唤醒|转给|交给|发给|委托|调用|用)\s*"
    rf"{_XIAONA_NAME_PAT}"
    rf"(?:\s*(?:回答一下|回答下|回答|说一下|说下|讲一下|讲下))?"
    rf"[\s:：,，、]*"
)
_STRIP_WITH_XIAONA_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*跟\s*{_XIAONA_NAME_PAT}\s*(?:说一下|说下|说|问一下|问下|问)?[\s:：,，、]*"
)
_STRIP_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_XIAONA_NAME_PAT}(?:\s*[:：,，!！、~～]+|\s+|$)"
)

_COMPOUND_LOCAL_ACTION_RE = re.compile(
    r"(?:"
    r"定个闹钟|设个闹钟|定闹钟|设闹钟"
    r"|设个提醒|定个提醒|设提醒|定提醒|加个提醒|创建提醒|定时提醒"
    r"|分钟后提醒|小时后提醒|点提醒我|点半提醒我|提醒我一下"
    r"|用本地浏览器|打开本地浏览器|打开本地网页|在本机打开|截屏|截图|发本地文件"
    r")"
)

_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(?:bearer\s+)?(?:xn_|sk-)[A-Za-z0-9_\-.]+")


def _clean_inbound_text(text: str) -> str:
    return (text or "").replace("\u2063", "").replace("\ufffc", "").replace("\x00", "").strip()


def _clean_relay_key(raw_key: str | None) -> str:
    if raw_key is None:
        return ""
    cleaned = raw_key.strip().strip("'").strip('"').strip()
    if not cleaned or _is_placeholder_key(cleaned):
        return ""
    if cleaned.endswith("...") or cleaned.lower() in {"xn_", "xn-", "xn_xxx", "xn-xxx"}:
        return ""
    return cleaned


def normalize_xiaona_ask_url(raw_url: str) -> str:
    """Normalize XiaoNa relay endpoint URL so non-empty URLs always end with `/ask`."""
    cleaned = (raw_url or "").strip().strip("'").strip('"').strip().rstrip("/")
    if not cleaned:
        return ""
    if not cleaned.lower().startswith(("http://", "https://")):
        return ""
    if cleaned.lower().endswith("/health"):
        return cleaned[:-7] + "/ask"
    if not cleaned.lower().endswith("/ask"):
        return cleaned + "/ask"
    return cleaned


def normalize_xiaona_health_url(raw_url: str) -> str:
    """Normalize XiaoNa relay endpoint URL to its `/health` sibling endpoint."""
    ask_url = normalize_xiaona_ask_url(raw_url)
    if not ask_url:
        return ""
    return ask_url[:-4] + "/health"


def is_xiaona_reset_command(text: str) -> bool:
    """Return True if text is an explicit command to reset XiaoNa conversation context."""
    cleaned = _clean_inbound_text(text)
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", cleaned).strip()
    if not cleaned:
        return False
    return bool(_RESET_CMD_RE.fullmatch(cleaned))


def is_explicit_xiaona_request(text: str) -> bool:
    """Return True ONLY when user text explicitly addresses or delegates to `小纳` / `xiaona`."""
    cleaned = _clean_inbound_text(text)
    if not cleaned:
        return False
    if is_xiaona_reset_command(cleaned):
        return True
    if _SLASH_OR_AT_TRIGGER_RE.search(cleaned):
        return True
    if _LEADING_VOCATIVE_RE.search(cleaned):
        return True
    if _DELEGATION_VERB_RE.search(cleaned):
        return True
    if _WITH_XIAONA_VERB_RE.search(cleaned):
        return True
    if _XIAONA_ACTION_RE.search(cleaned):
        return True
    return False


def extract_xiaona_query(text: str) -> str:
    """Strip leading `/xiaona`, `@小纳`, `问下小纳`, `让小纳回答：` wrappers while keeping natural context."""
    cleaned = _clean_inbound_text(text)
    if not cleaned:
        return ""
    for pattern in (
        _STRIP_SLASH_AT_RE,
        _STRIP_DELEGATION_RE,
        _STRIP_WITH_XIAONA_RE,
        _STRIP_VOCATIVE_RE,
    ):
        updated, count = pattern.subn("", cleaned, count=1)
        if count > 0:
            return updated.strip()
    return cleaned


def is_compound_xiaona_request(text: str) -> bool:
    """Return True only when an explicit XiaoNa request ALSO asks for a local Mac tool action."""
    cleaned = _clean_inbound_text(text)
    if not is_explicit_xiaona_request(cleaned) or is_xiaona_reset_command(cleaned):
        return False
    return bool(_COMPOUND_LOCAL_ACTION_RE.search(cleaned))


def sanitize_conversation_id(raw_id: str | None) -> str:
    """Sanitize conversation_id to `[A-Za-z0-9_-][:64]`, defaulting to `imessage-owner`."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw_id or "").strip()).strip("-_")[:64]
    return cleaned or DEFAULT_XIAONA_CONVERSATION_ID


def format_xiaona_reply(reply: str, media_urls: list[str] | None = None) -> str:
    """Format XiaoNa reply via HyperText SMS formatter and append standalone media URLs."""
    formatted_body = format_for_imessage(reply or "")
    extra_urls: list[str] = []
    seen: set[str] = set()
    for item in media_urls or []:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if not re.fullmatch(r"https?://\S+", candidate):
            continue
        if candidate in seen or candidate in formatted_body:
            continue
        seen.add(candidate)
        extra_urls.append(candidate)

    if extra_urls:
        url_block = "\n".join(extra_urls)
        return f"{formatted_body}\n\n{url_block}" if formatted_body else url_block
    return formatted_body


def _redact_sensitive_text(text: str, *secrets: str) -> str:
    """Strip bearer keys, URLs, and explicit secret values from error text before logging or replying."""
    redacted = text or ""
    for sec in secrets:
        s_clean = (sec or "").strip()
        if s_clean:
            redacted = redacted.replace(s_clean, "[REDACTED]")
    redacted = _BEARER_TOKEN_RE.sub("[REDACTED_KEY]", redacted)
    redacted = _HTTP_URL_RE.sub("[REDACTED_URL]", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[:180]


@dataclasses.dataclass
class XiaoNaResult:
    """Structured result from `XiaoNaClient.ask`."""

    ok: bool
    status_code: int
    reply: str
    media_urls: list[str]
    session_id: str
    duration_ms: int
    conversation_id: str
    formatted_reply: str
    error: str = ""


TransportCallable = Callable[
    [str, bytes, dict[str, str], float], Awaitable[tuple[int, bytes]]
]


def _urllib_post_json_sync(
    url: str,
    body_bytes: bytes,
    headers: dict[str, str],
    timeout_sec: float,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read(2 * 1024 * 1024)
            return (status, raw)
    except urllib.error.HTTPError as http_err:
        try:
            err_raw = http_err.read(256 * 1024)
        except Exception:
            err_raw = b""
        return (int(http_err.code), err_raw)
    except (TimeoutError, socket.timeout) as exc:
        raise TimeoutError("XiaoNa HTTP request timed out") from exc
    except urllib.error.URLError as url_err:
        if isinstance(url_err.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError("XiaoNa HTTP request timed out") from url_err
        raise ConnectionError("XiaoNa relay connection failed") from url_err


class XiaoNaClient:
    """Async client for the owner's private XiaoNa workspace relay with hot reload & per-session locking."""

    CONV_META_PREFIX = "xiaona_conv_id:"

    def __init__(
        self,
        config: AgentConfig,
        *,
        transport: TransportCallable | None = None,
        enable_keychain_fallback: bool = True,
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._enable_keychain_fallback = enable_keychain_fallback
        self._allow_repo_dotenv_fallback = allow_repo_dotenv_fallback
        self._timeout_override = timeout_override
        self._conv_locks: dict[str, asyncio.Lock] = {}

    def _get_conv_lock(self, conv_id: str) -> asyncio.Lock:
        lock = self._conv_locks.get(conv_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_locks[conv_id] = lock
        return lock

    def _dotenv_search_paths(self) -> list[Path]:
        if self.config.loaded_env_file is not None:
            return [Path(self.config.loaded_env_file).expanduser()]
        if self._allow_repo_dotenv_fallback:
            return _resolve_dotenv_candidates(None)
        return []

    def _read_macos_keychain_key(self) -> str:
        if not self._enable_keychain_fallback or sys.platform != "darwin":
            return ""
        try:
            proc = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    KEYCHAIN_SERVICE_NAME,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return _clean_relay_key(proc.stdout)
        except Exception:
            logger.debug("macOS Keychain lookup for XIAONA_RELAY_KEY skipped/failed", exc_info=True)
        return ""

    def resolve_credentials(self) -> tuple[str, str, str]:
        """Hot-resolve `(normalized_ask_url, relay_key, source)` on every call without daemon restart."""
        url = normalize_xiaona_ask_url(getattr(self.config, "xiaona_relay_url", ""))
        key = _clean_relay_key(getattr(self.config, "xiaona_relay_key", ""))
        source = "config" if (url and key) else "partial_config"

        # Live re-read .env on disk so adding or rotating XIAONA_RELAY_URL / XIAONA_RELAY_KEY works zero-restart
        for candidate in self._dotenv_search_paths():
            try:
                if not candidate.is_file():
                    continue
                parsed = _parse_dotenv_text(candidate.read_text(encoding="utf-8"))
            except OSError:
                continue

            live_url = normalize_xiaona_ask_url(parsed.get("XIAONA_RELAY_URL", ""))
            live_key = _clean_relay_key(parsed.get("XIAONA_RELAY_KEY", ""))

            if live_url and (not url or self.config.loaded_env_file is not None):
                url = live_url
                self.config.xiaona_relay_url = live_url
                source = "dotenv"
            if live_key and (not key or self.config.loaded_env_file is not None):
                key = live_key
                self.config.xiaona_relay_key = live_key
                source = "dotenv"
            if url and key:
                break

        if not key:
            kc_key = self._read_macos_keychain_key()
            if kc_key:
                key = kc_key
                source = "keychain"

        if not url and not key:
            source = "unconfigured"
        return (url, key, source)

    def check_readiness(self) -> dict[str, bool]:
        """Return masked boolean readiness dict without network calls or leaking secret values."""
        url, key, _ = self.resolve_credentials()
        return {
            "url_configured": bool(url),
            "key_configured": bool(key),
        }

    @classmethod
    def _conv_meta_key(cls, sender: str) -> str:
        norm = normalize_sender(sender) or "owner"
        return f"{cls.CONV_META_PREFIX}{norm}"

    def get_conversation_id(self, store: "StateStore | None", sender: str = "") -> str:
        """Fetch persistent XiaoNa conversation_id from StateStore, defaulting to `imessage-owner`."""
        if store is not None:
            saved = store.get_meta(self._conv_meta_key(sender))
            if saved:
                return sanitize_conversation_id(saved)
        return DEFAULT_XIAONA_CONVERSATION_ID

    def reset_conversation(self, store: "StateStore", sender: str = "") -> str:
        """Rotate persistent XiaoNa conversation_id to `imessage-owner-<unix_ts>` (<=64 chars)."""
        meta_key = self._conv_meta_key(sender)
        current = store.get_meta(meta_key) or DEFAULT_XIAONA_CONVERSATION_ID
        ts_val = int(time.time())
        candidate = sanitize_conversation_id(f"imessage-owner-{ts_val}")
        if candidate == current:
            candidate = sanitize_conversation_id(f"imessage-owner-{ts_val + 1}")
        store.set_meta(meta_key, candidate)
        return candidate

    async def _send_raw(
        self,
        url: str,
        body_bytes: bytes,
        headers: dict[str, str],
        timeout_sec: float,
    ) -> tuple[int, bytes]:
        if self._transport is not None:
            return await self._transport(url, body_bytes, headers, timeout_sec)
        return await asyncio.to_thread(
            _urllib_post_json_sync, url, body_bytes, headers, timeout_sec
        )

    async def ask(
        self,
        message: str,
        *,
        sender: str = "",
        store: "StateStore | None" = None,
        conversation_id: str | None = None,
        _timeout_override: float | None = None,
    ) -> XiaoNaResult:
        """Send an explicit owner query to XiaoNa relay under per-conversation lock."""
        conv_id = (
            sanitize_conversation_id(conversation_id)
            if conversation_id is not None
            else self.get_conversation_id(store, sender)
        )
        msg_clean = (message or "").strip()
        if not msg_clean:
            err_txt = "请直接附上要问小纳的内容，例如：/xiaona 今天有什么日程"
            return XiaoNaResult(
                ok=False,
                status_code=400,
                reply="",
                media_urls=[],
                session_id="",
                duration_ms=0,
                conversation_id=conv_id,
                formatted_reply=err_txt,
                error=err_txt,
            )

        ask_url, relay_key, _ = self.resolve_credentials()
        if not ask_url or not relay_key:
            err_txt = "小纳通道尚未完成 URL 或密钥配置。"
            return XiaoNaResult(
                ok=False,
                status_code=0,
                reply="",
                media_urls=[],
                session_id="",
                duration_ms=0,
                conversation_id=conv_id,
                formatted_reply=err_txt,
                error=err_txt,
            )

        payload_bytes = json.dumps(
            {"message": msg_clean, "conversation_id": conv_id},
            ensure_ascii=False,
        ).encode("utf-8")

        if len(payload_bytes) >= MAX_XIAONA_PAYLOAD_BYTES:
            err_txt = "发给小纳的内容太长了（超过 60KB 上限），请精简一下或分段发我。"
            return XiaoNaResult(
                ok=False,
                status_code=413,
                reply="",
                media_urls=[],
                session_id="",
                duration_ms=0,
                conversation_id=conv_id,
                formatted_reply=err_txt,
                error=err_txt,
            )

        override = _timeout_override if _timeout_override is not None else self._timeout_override
        if override is not None:
            effective_timeout = max(0.01, float(override))
        else:
            effective_timeout = max(
                float(getattr(self.config, "xiaona_timeout_sec", MIN_PRODUCTION_TIMEOUT_SEC)),
                MIN_PRODUCTION_TIMEOUT_SEC,
            )

        headers = {
            "Authorization": f"Bearer {relay_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        lock = self._get_conv_lock(conv_id)
        async with lock:
            try:
                status_code, resp_bytes = await asyncio.wait_for(
                    self._send_raw(ask_url, payload_bytes, headers, effective_timeout),
                    timeout=effective_timeout,
                )
            except asyncio.CancelledError:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning("XiaoNa relay call timed out after %.1fs (conv_id=%s)", effective_timeout, conv_id)
                err_txt = "等待小纳工作台回复超时了，它那边任务可能比较重，稍后再问我一下。"
                return XiaoNaResult(
                    ok=False,
                    status_code=504,
                    reply="",
                    media_urls=[],
                    session_id="",
                    duration_ms=0,
                    conversation_id=conv_id,
                    formatted_reply=err_txt,
                    error=err_txt,
                )
            except Exception as exc:
                safe_reason = _redact_sensitive_text(str(exc), relay_key, ask_url)
                logger.warning("XiaoNa relay network error (conv_id=%s): %s", conv_id, safe_reason)
                err_txt = "暂时连不上小纳工作台通道，请稍后再试。"
                return XiaoNaResult(
                    ok=False,
                    status_code=0,
                    reply="",
                    media_urls=[],
                    session_id="",
                    duration_ms=0,
                    conversation_id=conv_id,
                    formatted_reply=err_txt,
                    error=err_txt,
                )

        raw_text = (
            resp_bytes.decode("utf-8", errors="replace")
            if isinstance(resp_bytes, (bytes, bytearray))
            else str(resp_bytes or "")
        )

        parsed_json: dict[str, Any] = {}
        if raw_text.strip():
            try:
                loaded = json.loads(raw_text)
                if isinstance(loaded, dict):
                    parsed_json = loaded
            except json.JSONDecodeError:
                parsed_json = {}

        if status_code == 200 and parsed_json:
            reply_raw = str(parsed_json.get("reply") or "").strip()
            raw_media = parsed_json.get("media_urls")
            media_list = (
                [str(u).strip() for u in raw_media if isinstance(u, str) and str(u).strip()]
                if isinstance(raw_media, list)
                else []
            )
            session_id = str(parsed_json.get("session_id") or "")
            try:
                duration_ms = int(parsed_json.get("duration_ms") or 0)
            except (TypeError, ValueError):
                duration_ms = 0
            formatted = format_xiaona_reply(reply_raw, media_list)
            if not formatted:
                formatted = "小纳已处理完成。"
            return XiaoNaResult(
                ok=True,
                status_code=200,
                reply=reply_raw,
                media_urls=media_list,
                session_id=session_id,
                duration_ms=duration_ms,
                conversation_id=conv_id,
                formatted_reply=formatted,
            )

        if status_code == 400:
            err_txt = "发给小纳的请求参数未通过校验（400），请换个简短清楚的说法再试一次。"
        elif status_code in (401, 403):
            err_txt = "小纳通道鉴权未通过（401），请检查 XIAONA_RELAY_KEY 是否正确配置。"
        elif status_code == 502 or status_code >= 500:
            backend_err = _redact_sensitive_text(
                str(parsed_json.get("error") or ""), relay_key, ask_url
            )
            if backend_err:
                err_txt = f"小纳工作台暂时未能完成响应（{status_code}）：{backend_err}"
            else:
                err_txt = f"小纳工作台暂时繁忙或响应异常（{status_code}），请稍后再试。"
        else:
            err_txt = f"小纳工作台返回了非预期响应（HTTP {status_code}），请稍后再试。"

        logger.warning("XiaoNa relay returned HTTP %d (conv_id=%s)", status_code, conv_id)
        return XiaoNaResult(
            ok=False,
            status_code=status_code,
            reply="",
            media_urls=[],
            session_id="",
            duration_ms=0,
            conversation_id=conv_id,
            formatted_reply=err_txt,
            error=err_txt,
        )
