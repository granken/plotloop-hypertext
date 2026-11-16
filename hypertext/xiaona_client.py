"""Explicit-Only Admin XiaoNa / XiaoMiGuan Relay Client: hot reload, media attachment download & per-session lock."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import logging
import mimetypes
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .config import (
    AgentConfig,
    _is_placeholder_key,
    _parse_dotenv_text,
    _resolve_dotenv_candidates,
    normalize_sender,
)
from .formatter import (
    format_for_imessage,
    is_sensitive_attachment_path,
    sanitize_attachment_filename,
)

if TYPE_CHECKING:
    from .store_sqlite import StateStore

logger = logging.getLogger("hypertext.xiaona_client")

MAX_XIAONA_PAYLOAD_BYTES = 60 * 1024
MAX_XIAONA_MEDIA_BYTES = 25 * 1024 * 1024
DEFAULT_MEDIA_DOWNLOAD_TIMEOUT_SEC = 45.0
_MEDIA_STREAM_CHUNK_BYTES = 64 * 1024
DEFAULT_XIAONA_CONVERSATION_ID = "imessage-owner"
MIN_PRODUCTION_TIMEOUT_SEC = 600.0
KEYCHAIN_SERVICE_NAME = "XIAONA_RELAY_KEY"

# Matches `小纳`, `小蜜罐` (with optional `🍯`), or case-insensitive `xiaona` / `xiaomiguan`
# with strict ASCII word boundaries (rejecting `/xiaonabot`, `/xiaomiguanbot`, `myxiaona`).
_XIAONA_NAME_PAT = (
    r"(?:"
    r"(?:小纳|小蜜罐)(?:\s*🍯)?(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?i:xiaona|xiaomiguan)(?:\s*🍯)?(?![A-Za-z0-9_])"
    r")"
)
_XIAOMIGUAN_ALIAS_RE = re.compile(
    r"(?:小蜜罐|(?<![A-Za-z0-9_])(?i:xiaomiguan)(?![A-Za-z0-9_]))"
)

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

_RAW_MEDIA_LINE_RE = re.compile(r"(?m)^\s*MEDIA:\S[^\n]*(?:\n|$)")
_CONTENT_DISP_UTF8_RE = re.compile(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\s]+)")
_CONTENT_DISP_QUOTED_RE = re.compile(r'filename\s*=\s*"([^"]+)"', re.IGNORECASE)
_CONTENT_DISP_PLAIN_RE = re.compile(r"filename\s*=\s*([^;\s]+)", re.IGNORECASE)

_MIME_EXT_OVERRIDES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
}

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


def strip_raw_media_directives(text: str) -> str:
    """Remove raw `MEDIA:...` internal lines emitted by upstream agent runtimes."""
    if not text:
        return ""
    cleaned = _RAW_MEDIA_LINE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def strip_delivered_urls_from_text(text: str, delivered_urls: list[str]) -> str:
    """Remove URLs already delivered as native iMessage attachments from text body."""
    out = strip_raw_media_directives(text or "")
    for u in delivered_urls:
        u_clean = (u or "").strip()
        if not u_clean:
            continue
        # Unpack `[label](u)` -> `label` (or empty if label was the URL itself)
        md_pat = re.compile(r"\[([^\]]*)\]\(\s*" + re.escape(u_clean) + r"\s*\)")
        out = md_pat.sub(
            lambda m: "" if m.group(1).strip() in ("", u_clean) else m.group(1).strip(),
            out,
        )
        out = out.replace(u_clean, "")
    out = "\n".join(line.rstrip() for line in out.splitlines())
    return re.sub(r"\n{3,}", "\n\n", out).strip()


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


def is_xiaomiguan_alias_used(text: str) -> bool:
    """Return True if the user addressed the relay as `小蜜罐` / `xiaomiguan`."""
    cleaned = _clean_inbound_text(text)
    if not cleaned:
        return False
    return bool(_XIAOMIGUAN_ALIAS_RE.search(cleaned))


def is_xiaona_reset_command(text: str) -> bool:
    """Return True if text is an explicit command to reset XiaoNa / XiaoMiGuan conversation context."""
    cleaned = _clean_inbound_text(text)
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", cleaned).strip()
    if not cleaned:
        return False
    return bool(_RESET_CMD_RE.fullmatch(cleaned))


def is_explicit_xiaona_request(text: str) -> bool:
    """Return True ONLY when user text explicitly addresses `小纳`/`xiaona` or `小蜜罐`/`xiaomiguan`."""
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
    """Strip leading `/xiaona`, `/小蜜罐`, `@小纳`, `问下小蜜罐`, `让小纳回答：` wrappers cleanly."""
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
    """Return True only when an explicit XiaoNa/XiaoMiGuan request ALSO asks for a local Mac action."""
    cleaned = _clean_inbound_text(text)
    if not is_explicit_xiaona_request(cleaned) or is_xiaona_reset_command(cleaned):
        return False
    return bool(_COMPOUND_LOCAL_ACTION_RE.search(cleaned))


def sanitize_conversation_id(raw_id: str | None) -> str:
    """Sanitize conversation_id to `[A-Za-z0-9_-][:64]`, defaulting to `imessage-owner`."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw_id or "").strip()).strip("-_")[:64]
    return cleaned or DEFAULT_XIAONA_CONVERSATION_ID


def format_xiaona_reply(reply: str, media_urls: list[str] | None = None) -> str:
    """Format XiaoNa reply via HyperText SMS formatter (stripping `MEDIA:...`) and append fallback URLs."""
    cleaned_raw = strip_raw_media_directives(reply or "")
    formatted_body = format_for_imessage(cleaned_raw)
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


def _extract_filename_from_headers_or_url(headers: Any, url: str) -> str:
    """Extract a clean candidate filename from Content-Disposition or URL path."""
    content_disp = ""
    content_type = ""
    if headers is not None and hasattr(headers, "get"):
        content_disp = str(headers.get("Content-Disposition") or "").strip()
        content_type = (
            str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
        )

    candidate = ""
    if content_disp:
        m_utf8 = _CONTENT_DISP_UTF8_RE.search(content_disp)
        if m_utf8:
            candidate = urllib.parse.unquote(m_utf8.group(1).strip().strip("\"'"))
        else:
            m_q = _CONTENT_DISP_QUOTED_RE.search(content_disp)
            if m_q:
                candidate = urllib.parse.unquote(m_q.group(1).strip())
            else:
                m_p = _CONTENT_DISP_PLAIN_RE.search(content_disp)
                if m_p:
                    candidate = urllib.parse.unquote(m_p.group(1).strip().strip("\"'"))

    if not candidate:
        parsed_path = urllib.parse.urlparse(url).path
        raw_base = PurePosixPath(parsed_path).name
        candidate = urllib.parse.unquote(raw_base).strip()

    guessed_ext = ""
    if not Path(candidate).suffix and content_type:
        guessed_ext = _MIME_EXT_OVERRIDES.get(content_type) or (
            mimetypes.guess_extension(content_type) or ""
        )
        if guessed_ext == ".jpe":
            guessed_ext = ".jpg"

    default_stem = f"xiaona_media_{int(time.time())}"
    return sanitize_attachment_filename(candidate, default_stem=default_stem, ext=guessed_ext)


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
MediaDownloaderCallable = Callable[..., Awaitable[Path | None]]
UrlOpenCallable = Callable[[urllib.request.Request, float], Any]


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
    """Async client for the owner's private XiaoNa / XiaoMiGuan relay with hot reload & media download."""

    CONV_META_PREFIX = "xiaona_conv_id:"

    def __init__(
        self,
        config: AgentConfig,
        *,
        transport: TransportCallable | None = None,
        media_downloader: MediaDownloaderCallable | None = None,
        url_opener: UrlOpenCallable | None = None,
        enable_keychain_fallback: bool = True,
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._media_downloader = media_downloader
        self._url_opener = url_opener
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

    def _open_url_sync(self, req: urllib.request.Request, timeout_sec: float) -> Any:
        if self._url_opener is not None:
            return self._url_opener(req, timeout_sec)
        return urllib.request.urlopen(req, timeout=timeout_sec)

    def _download_media_sync(
        self,
        clean_url: str,
        target_dir: Path,
        max_bytes: int,
        timeout_sec: float,
        relay_ask_url: str,
        relay_key: str,
    ) -> Path | None:
        parsed_media = urllib.parse.urlparse(clean_url)
        media_host = parsed_media.netloc.lower()
        relay_host = (
            urllib.parse.urlparse(relay_ask_url).netloc.lower() if relay_ask_url else ""
        )

        # Step 1: Always attempt unauthenticated GET first so Bearer key NEVER leaks to 3rd-party CDNs
        req = urllib.request.Request(clean_url, headers={"Accept": "*/*"}, method="GET")
        resp_cm: Any = None
        try:
            resp_cm = self._open_url_sync(req, timeout_sec)
        except urllib.error.HTTPError as http_err:
            # Step 2: Retry ONCE with Bearer token ONLY on HTTP 401 AND exact same netloc as xiaona_relay_url
            if (
                int(http_err.code) == 401
                and relay_host
                and media_host == relay_host
                and relay_key
            ):
                auth_req = urllib.request.Request(
                    clean_url,
                    headers={
                        "Accept": "*/*",
                        "Authorization": f"Bearer {relay_key}",
                    },
                    method="GET",
                )
                resp_cm = self._open_url_sync(auth_req, timeout_sec)
            else:
                raise

        tmp_path = target_dir / f".tmp_{uuid.uuid4().hex}"
        try:
            with contextlib.closing(resp_cm) as resp:
                resp_headers = getattr(resp, "headers", None)
                if resp_headers is not None and hasattr(resp_headers, "get"):
                    cl_raw = resp_headers.get("Content-Length")
                    if cl_raw:
                        with contextlib.suppress(ValueError):
                            if int(str(cl_raw).strip()) > max_bytes:
                                logger.warning(
                                    "XiaoNa media exceeds max_bytes (%s > %d); falling back to URL.",
                                    str(cl_raw).strip(),
                                    max_bytes,
                                )
                                return None

                safe_name = _extract_filename_from_headers_or_url(resp_headers, clean_url)
                out_path = target_dir / safe_name
                if out_path.exists():
                    out_path = (
                        target_dir
                        / f"{out_path.stem}_{uuid.uuid4().hex[:6]}{out_path.suffix}"
                    )

                blocked, _ = is_sensitive_attachment_path(out_path)
                if blocked:
                    logger.warning("Blocked sensitive XiaoNa media filename: %s", out_path.name)
                    return None

                total_bytes = 0
                with tmp_path.open("wb") as fh:
                    while True:
                        chunk = resp.read(_MEDIA_STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            logger.warning(
                                "XiaoNa media stream exceeded %d bytes; aborting download.",
                                max_bytes,
                            )
                            fh.close()
                            tmp_path.unlink(missing_ok=True)
                            return None
                        fh.write(chunk)

                if total_bytes <= 0:
                    tmp_path.unlink(missing_ok=True)
                    return None

                os.replace(tmp_path, out_path)
                return out_path
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    async def download_media_url(
        self,
        url: str,
        dest_dir: Path | None = None,
        max_bytes: int = MAX_XIAONA_MEDIA_BYTES,
        timeout_sec: float = DEFAULT_MEDIA_DOWNLOAD_TIMEOUT_SEC,
    ) -> Path | None:
        """Download a XiaoNa `media_urls` item (`<= 25 MB`) to `~/.hypertext/xiaona_downloads/` safely."""
        clean_url = (url or "").strip()
        if not clean_url:
            return None

        parsed = urllib.parse.urlparse(clean_url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return None

        target_dir = (
            Path(dest_dir).expanduser()
            if dest_dir is not None
            else (self.config.state_dir / "xiaona_downloads")
        )
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        if self._media_downloader is not None:
            try:
                sig = inspect.signature(self._media_downloader)
                params = list(sig.parameters.values())
                has_varargs = any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
                )
                if has_varargs or len(params) >= 4:
                    return await self._media_downloader(
                        clean_url, target_dir, max_bytes, timeout_sec
                    )
                if len(params) == 2:
                    return await self._media_downloader(clean_url, target_dir)
                return await self._media_downloader(clean_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ask_u, r_key, _ = self.resolve_credentials()
                logger.warning(
                    "Custom XiaoNa media_downloader failed: %s",
                    _redact_sensitive_text(str(exc), r_key, ask_u, clean_url),
                )
                return None

        ask_url, relay_key, _ = self.resolve_credentials()
        eff_timeout = max(0.05, float(timeout_sec))
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._download_media_sync,
                    clean_url,
                    target_dir,
                    int(max_bytes),
                    eff_timeout,
                    ask_url,
                    relay_key,
                ),
                timeout=eff_timeout + 2.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "XiaoNa media download skipped/fell back to URL: %s",
                _redact_sensitive_text(str(exc), relay_key, ask_url, clean_url),
            )
            return None

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
                logger.warning(
                    "XiaoNa relay call timed out after %.1fs (conv_id=%s)",
                    effective_timeout,
                    conv_id,
                )
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
            reply_raw = strip_raw_media_directives(str(parsed_json.get("reply") or "").strip())
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
