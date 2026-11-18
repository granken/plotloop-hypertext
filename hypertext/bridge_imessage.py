"""macOS iMessage read-only SQLite bridge, 4-layer Self-Chat anti-loop, attributedBody decoder, and parameterized AppleScript sender."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import base64
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import normalize_sender

if TYPE_CHECKING:
    from .config import AgentConfig
    from .human_gate import HumanGate
    from .store_sqlite import StateStore

logger = logging.getLogger("hypertext.bridge_imessage")

APPLE_EPOCH_OFFSET = 978307200.0  # Seconds between 1970-01-01 and 2001-01-01 UTC
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024  # 12 MB multimodal cap
MAX_OUTBOUND_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB outbound attachment ceiling
MAX_IMESSAGE_CHUNK_CHARS = 3500

# Invisible Unicode Separator prefixed on every bot outbound chunk (preserved in chat.db, invisible on iOS/macOS)
BOT_ECHO_MARKER = "\u2063"
BOT_MESSAGE_PREFIXES = (
    "🔐 [",
    "🌐 [",
    "🖥️ ",
    "⏰ ",
    "⚠️ ",
    "🎉 ",
    "👋 ",
    "🔔 [",
    "🏓 ",
    "🧹 ",
    "📎 ",
)
OUTBOUND_ECHO_TTL_SEC = 180.0
SELF_TWIN_DEDUP_TTL_SEC = 20.0

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".bmp",
    ".tiff",
    ".tif",
}

_TYPEDSTREAM_NOISE_TOKENS = {
    "streamtyped",
    "NSAttributedString",
    "NSMutableAttributedString",
    "NSObject",
    "NSString",
    "NSMutableString",
    "NSDictionary",
    "NSMutableDictionary",
    "NSNumber",
    "NSValue",
    "NSArray",
    "__kIMMessagePartAttributeName",
    "__kIMFileTransferGUIDAttributeName",
    "__kIMBaseWritingDirectionAttributeName",
    "__kIMCalendarEventAttributeName",
    "__kIMLinkAttributeName",
    "NSFont",
    "NSColor",
    "NSParagraphStyle",
}

# Strictly parameterized AppleScript: message text and recipient are NEVER interpolated into source.
# When targetHandle contains ";" (e.g. "iMessage;-;+86..."), pins to exact `chat id` first, then falls back cleanly.
APPLESCRIPT_SEND_IMESSAGE = """on run argv
    set targetHandle to item 1 of argv
    set targetMessage to item 2 of argv
    set fallbackHandle to targetHandle
    if targetHandle contains ";" then
        set oldDelims to AppleScript's text item delimiters
        set AppleScript's text item delimiters to ";"
        set fallbackHandle to last text item of targetHandle
        set AppleScript's text item delimiters to oldDelims
    end if
    tell application "Messages"
        if targetHandle contains ";" then
            try
                send targetMessage to chat id targetHandle
                return
            end try
        end if
        set targetService to 1st account whose service type = iMessage
        set theBuddy to participant fallbackHandle of targetService
        send targetMessage to theBuddy
    end tell
end run
"""

# Parameterized attachment AppleScript: POSIX file path and recipient handle passed strictly via argv.
APPLESCRIPT_SEND_ATTACHMENT = """on run argv
    set targetHandle to item 1 of argv
    set filePath to item 2 of argv
    set theAttachment to POSIX file filePath
    set fallbackHandle to targetHandle
    if targetHandle contains ";" then
        set oldDelims to AppleScript's text item delimiters
        set AppleScript's text item delimiters to ";"
        set fallbackHandle to last text item of targetHandle
        set AppleScript's text item delimiters to oldDelims
    end if
    tell application "Messages"
        if targetHandle contains ";" then
            try
                send theAttachment to chat id targetHandle
                return
            end try
        end if
        set targetAccount to 1st account whose service type = iMessage
        send theAttachment to participant fallbackHandle of targetAccount
    end tell
end run
"""


def _clean_decoded_text(text: str, strip_echo_marker: bool = False) -> str:
    """Strip inline attachment replacement char (U+FFFC) and NUL bytes."""
    cleaned = text.replace("\ufffc", "").replace("\x00", "")
    if strip_echo_marker:
        cleaned = cleaned.replace(BOT_ECHO_MARKER, "")
    return cleaned.strip()


def _normalize_for_echo_hash(text: str) -> str:
    """Compute whitespace-normalized SHA-256 hex digest (stripping BOT_ECHO_MARKER) for Layer-1 echo suppression."""
    cleaned = (
        (text or "")
        .replace(BOT_ECHO_MARKER, "")
        .replace("\ufffc", "")
        .replace("\x00", "")
    )
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if not collapsed:
        return ""
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def _strip_self_chat_prefix(body: str, prefix: str) -> tuple[bool, str]:
    """Match and strip self-chat trigger prefix (e.g. '/ai') case-insensitively with word boundary protection."""
    p = (prefix or "/ai").strip()
    b = (body or "").strip()
    if not b or not p:
        return (False, "")
    if b.lower() == p.lower():
        return (True, "")
    if b.lower().startswith(p.lower()):
        rest = b[len(p) :]
        # Reject ASCII alphanumeric continuation like '/airdrop' when prefix is '/ai'
        if rest and rest[0].isascii() and rest[0].isalnum():
            return (False, b)
        stripped = rest.lstrip(" \t\r\n:：,，-")
        return (True, stripped.strip())
    return (False, b)


_SELF_CHAT_XIAONA_SLASH_RE = re.compile(
    r"^[/／]\s*(?:"
    r"(?:小纳|小蜜罐)(?:\s*🍯)?(?![A-Za-z0-9_])"
    r"|(?i:xiaona|xiaomiguan)(?:\s*🍯)?(?![A-Za-z0-9_])"
    r"|(?i:workbuddy|codebuddy|wb)(?![A-Za-z0-9_])"
    r"|(?i:dsh|codex|cdx|agy|antigravity)(?![A-Za-z0-9_])"
    r"|即梦(?:\s*画布)?(?![A-Za-z0-9_])"
    r"|(?i:dreamina(?:-canvas)?|jimeng|jm)(?![A-Za-z0-9_])"
    r")"
)


def _is_self_chat_xiaona_slash_trigger(body: str) -> bool:
    """Match `/xiaona`, `/小蜜罐`, `/wb`, `/dsh`, `/codex`, `/agy`, `/即梦`, `/jimeng`, `/dreamina`, `/jm` in self-chat."""
    b = (body or "").strip()
    if not b:
        return False
    return bool(_SELF_CHAT_XIAONA_SLASH_RE.search(b))


def _read_typedstream_length(blob: bytes, pos: int) -> tuple[int, int] | None:
    """Parse Apple typedstream length prefix at blob[pos]."""
    total = len(blob)
    if pos < 0 or pos >= total:
        return None
    tag = blob[pos]
    if tag == 0 or (0x80 <= tag and tag not in (0x81, 0x82, 0x83)):
        return None

    if tag < 0x80:
        length = tag
        start = pos + 1
    elif tag == 0x81:
        if pos + 3 > total:
            return None
        length = struct.unpack_from("<H", blob, pos + 1)[0]
        start = pos + 3
    elif tag == 0x82:
        if pos + 5 > total:
            return None
        length = struct.unpack_from("<I", blob, pos + 1)[0]
        start = pos + 5
    else:
        if pos + 9 > total:
            return None
        length = struct.unpack_from("<Q", blob, pos + 1)[0]
        start = pos + 9

    if length <= 0 or start + length > total:
        return None
    return (length, start)


def _extract_after_marker(blob: bytes, marker: bytes) -> str | None:
    """Locate NSString / NSMutableString marker and decode length-prefixed UTF-8 payload."""
    search_from = 0
    while True:
        idx = blob.find(marker, search_from)
        if idx == -1:
            return None
        after_marker = idx + len(marker)
        window_end = min(len(blob), after_marker + 64)
        window = blob[after_marker:window_end]

        candidate_offsets: list[int] = []
        p1 = window.find(b"\x01+")
        if p1 != -1:
            candidate_offsets.append(after_marker + p1 + 2)
        p2 = window.find(b"+")
        if p2 != -1 and (after_marker + p2 + 1) not in candidate_offsets:
            candidate_offsets.append(after_marker + p2 + 1)
        if after_marker < len(blob) and blob[after_marker] == 0:
            candidate_offsets.append(after_marker + 1)
        candidate_offsets.append(after_marker)

        for offset in candidate_offsets:
            parsed = _read_typedstream_length(blob, offset)
            if parsed is None:
                continue
            length, payload_start = parsed
            raw_slice = blob[payload_start : payload_start + length]
            try:
                decoded = raw_slice.decode("utf-8")
            except UnicodeDecodeError:
                decoded = raw_slice.decode("utf-8", errors="replace")
            cleaned = _clean_decoded_text(decoded)
            if cleaned or length > 0:
                return cleaned

        search_from = after_marker


def decode_attributed_body(blob: bytes | None) -> str:
    """Decode macOS Messages chat.db attributedBody (typedstream NSAttributedString)."""
    if not blob:
        return ""
    if isinstance(blob, memoryview):
        blob = blob.tobytes()

    for marker in (b"NSString", b"NSMutableString"):
        extracted = _extract_after_marker(blob, marker)
        if extracted is not None:
            return extracted

    decoded_lossy = blob.decode("utf-8", errors="ignore")
    for noise in _TYPEDSTREAM_NOISE_TOKENS:
        decoded_lossy = decoded_lossy.replace(noise, "\x00")

    candidates: list[str] = []
    for match in re.finditer(
        r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef\u2063\x20-\x7e\n\r\t]{2,}",
        decoded_lossy,
    ):
        seg = _clean_decoded_text(match.group(0))
        seg = re.sub(r"\s+", " ", seg).strip()
        if seg and not re.fullmatch(r"[@+iI0-9\W_]{1,4}", seg):
            candidates.append(seg)

    if not candidates:
        return ""
    return max(candidates, key=len)


def encode_attributed_body(text: str, class_name: str = "NSString") -> bytes:
    """Encode text into an authentic Apple typedstream attributedBody blob."""
    payload = text.encode("utf-8")
    n = len(payload)
    if n < 0x80:
        len_bytes = bytes([n])
    elif n <= 0xFFFF:
        len_bytes = b"\x81" + struct.pack("<H", n)
    elif n <= 0xFFFFFFFF:
        len_bytes = b"\x82" + struct.pack("<I", n)
    else:
        len_bytes = b"\x83" + struct.pack("<Q", n)

    cls_bytes = class_name.encode("ascii")
    header = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
        b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
        + bytes([len(cls_bytes)])
        + cls_bytes
        + b"\x01\x94\x84\x01+"
    )
    trailer = b"\x86\x84\x02iI\x01\x92\x84\x96\x96\x00"
    return header + len_bytes + payload + trailer


def _chunk_text(text: str, max_chars: int = MAX_IMESSAGE_CHUNK_CHARS - 1) -> list[str]:
    """Split outbound message text into chunks leaving 1 char headroom for BOT_ECHO_MARKER."""
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= max_chars:
        return [body]

    chunks: list[str] = []
    remaining = body
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at <= max_chars // 3:
            split_at = max_chars
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks


@dataclass
class InboundMessage:
    """Normalized inbound message from iMessage or local CLI."""

    rowid: int
    guid: str
    sender: str
    text: str
    timestamp_unix: float
    image_data_urls: list[str] = field(default_factory=list)
    attachment_paths: list[str] = field(default_factory=list)
    is_from_me: int = 0
    reply_target: str = ""
    approval_only: bool = False
    image_mime_types: list[str | None] = field(default_factory=list)


class MessageBridge(abc.ABC):
    """Abstract transport interface for inbound/outbound user messaging."""

    @abc.abstractmethod
    async def send_text(self, recipient: str, text: str) -> None:
        raise NotImplementedError

    async def send_message(self, recipient: str, text: str) -> None:
        """Alias for send_text."""
        await self.send_text(recipient, text)

    async def send_attachment(
        self, recipient: str, file_path: Path | str, caption: str = ""
    ) -> bool:
        """Send a file attachment (and optional caption) to recipient."""
        p = Path(file_path).expanduser()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"附件文件不存在或不是普通文件: {p}")
        if p.stat().st_size > MAX_OUTBOUND_ATTACHMENT_BYTES:
            raise ValueError(f"附件超过 50MB 上限: {p.name}")
        from .formatter import format_for_imessage

        note = f"📎 [附件] {p.name}"
        if caption and caption.strip():
            note = f"{note}\n{format_for_imessage(caption)}"
        await self.send_text(recipient, note)
        return True


class MacIMessageBridge(MessageBridge):
    """Read-only chat.db poller + 4-layer Self-Chat anti-loop + parameterized AppleScript sender."""

    CURSOR_META_KEY = "last_imessage_rowid"

    def __init__(
        self,
        chat_db_path: str | Path | None = None,
        store: "StateStore | None" = None,
        allowed_senders: set[str] | None = None,
        config: "AgentConfig | None" = None,
        human_gate: "HumanGate | None" = None,
        allow_self_chat: bool | None = None,
        self_chat_prefix: str | None = None,
        enable_invite_codes: bool | None = None,
        sips_bin: str = "/usr/bin/sips",
        osascript_bin: str = "/usr/bin/osascript",
        *,
        durable_inbox: bool = False,
    ) -> None:
        if store is None:
            raise ValueError("MacIMessageBridge requires a StateStore instance.")
        self.store = store
        self.durable_inbox = durable_inbox
        self.config = config
        self.human_gate = human_gate

        resolved_db = (
            chat_db_path
            if chat_db_path is not None
            else (config.chat_db_path if config is not None else "~/Library/Messages/chat.db")
        )
        self.chat_db_path = Path(resolved_db).expanduser()

        raw_allowed = (
            allowed_senders
            if allowed_senders is not None
            else (config.allowed_senders if config is not None else set())
        )
        self.allowed_senders = {
            normalize_sender(s) for s in raw_allowed if normalize_sender(s)
        }

        self.allow_self_chat = (
            bool(allow_self_chat)
            if allow_self_chat is not None
            else (bool(config.allow_self_chat) if config is not None else False)
        )
        self.self_chat_prefix = (
            (self_chat_prefix or "/ai").strip()
            if self_chat_prefix is not None
            else (config.self_chat_prefix if config is not None else "/ai")
        )
        self.enable_invite_codes = (
            bool(enable_invite_codes)
            if enable_invite_codes is not None
            else (bool(config.enable_invite_codes) if config is not None else False)
        )

        self.sips_bin = sips_bin
        self.osascript_bin = osascript_bin
        self._recent_outbound: dict[str, float] = {}
        self._recent_self_inbound: dict[tuple[str, str], float] = {}

    def attach_human_gate(self, gate: "HumanGate") -> None:
        """Attach HumanGate instance so bare 2FA replies bypass self_chat_prefix when pending."""
        self.human_gate = gate

    def record_outbound_echo(self, text: str, now_ts: float | None = None) -> None:
        """Record normalized SHA-256 of outbound bot text (TTL 180s) for Layer-1 anti-loop."""
        now = time.time() if now_ts is None else float(now_ts)
        expired = [
            h for h, ts in self._recent_outbound.items() if now - ts > OUTBOUND_ECHO_TTL_SEC
        ]
        for h in expired:
            self._recent_outbound.pop(h, None)

        full_hash = _normalize_for_echo_hash(text)
        if full_hash:
            self._recent_outbound[full_hash] = now
        for chunk in _chunk_text(text):
            ch_hash = _normalize_for_echo_hash(chunk)
            if ch_hash:
                self._recent_outbound[ch_hash] = now

    def _is_bot_echo(self, raw_or_decoded: str, now_ts: float | None = None) -> bool:
        """Layer 1: Detect bot outbound echoes via invisible marker, bot prefix, or 180s SHA-256 cache."""
        if not raw_or_decoded:
            return False
        if BOT_ECHO_MARKER in raw_or_decoded:
            return True
        cleaned = _clean_decoded_text(raw_or_decoded, strip_echo_marker=True)
        if not cleaned:
            return False
        if any(cleaned.startswith(prefix) for prefix in BOT_MESSAGE_PREFIXES):
            return True

        now = time.time() if now_ts is None else float(now_ts)
        expired = [
            h for h, ts in self._recent_outbound.items() if now - ts > OUTBOUND_ECHO_TTL_SEC
        ]
        for h in expired:
            self._recent_outbound.pop(h, None)

        digest = _normalize_for_echo_hash(cleaned)
        return bool(digest and digest in self._recent_outbound)

    def _is_duplicate_self_twin(
        self,
        sender_norm: str,
        post_prefix_body: str,
        img_count: int,
        now_ts: float,
        recent: dict[tuple[str, str], float],
        is_admin_self: bool = True,
    ) -> bool:
        """Deduplicate macOS twin rows (including cross-alias email + phone self-chat) within 20s window."""
        expired = [
            k
            for k, ts in recent.items()
            if now_ts - ts > SELF_TWIN_DEDUP_TTL_SEC
        ]
        for k in expired:
            recent.pop(k, None)

        norm_body = re.sub(r"\s+", " ", (post_prefix_body or "")).strip()
        dedup_scope = "__admin_self__" if is_admin_self else sender_norm
        key = (dedup_scope, f"{norm_body}|img={img_count}")
        if key in recent:
            return True
        recent[key] = now_ts
        return False

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open macOS Messages chat.db strictly in read-only URI mode."""
        uri = f"file:{self.chat_db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def align_cursor_to_latest(self) -> int:
        """Ensure cursor starts at current MAX(ROWID) if not yet initialized in StateStore."""
        existing = self.store.get_meta(self.CURSOR_META_KEY)
        if existing is not None:
            try:
                return int(existing)
            except ValueError:
                pass

        with contextlib.closing(self._connect_readonly()) as conn:
            row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) AS max_id FROM message").fetchone()
            max_id = int(row["max_id"]) if row else 0
        self.store.set_meta(self.CURSOR_META_KEY, str(max_id))
        logger.info("Initialized iMessage cursor %s=%d", self.CURSOR_META_KEY, max_id)
        return max_id

    @staticmethod
    def _apple_date_to_unix(apple_date: int | float | None) -> float:
        if not apple_date:
            return time.time()
        val = float(apple_date)
        if val > 1_000_000_000_000:
            return val / 1_000_000_000.0 + APPLE_EPOCH_OFFSET
        return val + APPLE_EPOCH_OFFSET

    def _encode_image_attachment(
        self, raw_filename: str, mime_type: str | None
    ) -> tuple[str | None, str | None]:
        """Load image <=12MB, converting .heic/.heif via /usr/bin/sips -s format jpeg."""
        if not raw_filename:
            return (None, None)
        path = Path(raw_filename).expanduser()
        try:
            if not path.is_file():
                return (None, None)
            size = path.stat().st_size
            if size <= 0 or size > MAX_ATTACHMENT_BYTES:
                logger.warning(
                    "Skipping attachment %s due to size=%d bytes (limit=%d)",
                    path,
                    size,
                    MAX_ATTACHMENT_BYTES,
                )
                return (None, None)
        except OSError:
            return (None, None)

        ext = path.suffix.lower()
        mime = (mime_type or "").lower().strip()
        if not mime:
            guessed, _ = mimetypes.guess_type(str(path))
            mime = (guessed or "").lower()

        is_image = mime.startswith("image/") or ext in _IMAGE_EXTENSIONS
        if not is_image:
            return (None, None)

        if ext in (".heic", ".heif") or mime in ("image/heic", "image/heif"):
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_f:
                tmp_jpg = Path(tmp_f.name)
            try:
                subprocess.run(
                    [self.sips_bin, "-s", "format", "jpeg", str(path), "--out", str(tmp_jpg)],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
                jpg_bytes = tmp_jpg.read_bytes()
                if len(jpg_bytes) > MAX_ATTACHMENT_BYTES:
                    return (None, None)
                b64 = base64.b64encode(jpg_bytes).decode("ascii")
                return (f"data:image/jpeg;base64,{b64}", str(path))
            except Exception as exc:
                logger.warning("Failed to convert HEIC attachment %s via sips: %s", path, exc)
                return (None, None)
            finally:
                tmp_jpg.unlink(missing_ok=True)

        try:
            raw_bytes = path.read_bytes()
        except OSError:
            return (None, None)
        out_mime = mime if mime.startswith("image/") else f"image/{ext.lstrip('.') or 'jpeg'}"
        if out_mime == "image/jpg":
            out_mime = "image/jpeg"
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        return (f"data:{out_mime};base64,{b64}", str(path))

    def _fetch_attachments_for_message(
        self, conn: sqlite3.Connection, message_rowid: int, has_attachment_tables: bool
    ) -> tuple[list[str], list[str], list[str | None]]:
        if not has_attachment_tables:
            return ([], [], [])
        rows = conn.execute(
            """
            SELECT a.filename AS filename, a.mime_type AS mime_type
            FROM attachment a
            INNER JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            WHERE maj.message_id = ?
            ORDER BY a.ROWID ASC
            """,
            (message_rowid,),
        ).fetchall()
        data_urls: list[str] = []
        paths: list[str] = []
        mime_types: list[str | None] = []
        for r in rows:
            url, p = self._encode_image_attachment(r["filename"], r["mime_type"])
            if url:
                data_urls.append(url)
            if p:
                paths.append(p)
                mime_types.append(r["mime_type"])
        return (data_urls, paths, mime_types)

    def poll_new_messages(self) -> list[InboundMessage]:
        """Poll read-only chat.db with 4-layer Self-Chat anti-loop and multi-user routing."""
        last_rowid = self.align_cursor_to_latest()
        recent_twins = self._recent_self_inbound.copy()
        now_ts = time.time()

        with contextlib.closing(self._connect_readonly()) as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            has_attachment_tables = {"attachment", "message_attachment_join"}.issubset(tables)
            has_chat_tables = {"chat", "chat_message_join"}.issubset(tables)

            msg_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(message)").fetchall()
            }
            cache_attach_expr = (
                "m.cache_has_attachments"
                if "cache_has_attachments" in msg_cols
                else "1"
            )
            guid_expr = "COALESCE(m.guid, '')" if "guid" in msg_cols else "''"
            date_expr = "m.date" if "date" in msg_cols else "0"

            if has_chat_tables:
                chat_cols = {
                    r["name"] for r in conn.execute("PRAGMA table_info(chat)").fetchall()
                }
                chat_guid_col = "COALESCE(c.guid, '')" if "guid" in chat_cols else "''"
                chat_ident_col = (
                    "COALESCE(c.chat_identifier, '')"
                    if "chat_identifier" in chat_cols
                    else "''"
                )
                sender_expr = (
                    f"{chat_guid_col} AS chat_guid, "
                    f"{chat_ident_col} AS chat_identifier, "
                    "COALESCE(h.id, '') AS raw_handle_id"
                )
                chat_joins = (
                    "LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
                    "LEFT JOIN chat c ON c.ROWID = cmj.chat_id"
                )
            else:
                sender_expr = (
                    "'' AS chat_guid, "
                    "'' AS chat_identifier, "
                    "COALESCE(h.id, '') AS raw_handle_id"
                )
                chat_joins = ""

            from_me_clause = (
                "m.is_from_me IN (0, 1)" if self.allow_self_chat else "m.is_from_me = 0"
            )

            max_seen_row = conn.execute(
                "SELECT COALESCE(MAX(ROWID), ?) AS max_id FROM message WHERE ROWID > ?",
                (last_rowid, last_rowid),
            ).fetchone()
            max_seen = int(max_seen_row["max_id"]) if max_seen_row else last_rowid

            rows = conn.execute(
                f"""
                SELECT
                    m.ROWID AS rowid,
                    m.is_from_me AS is_from_me,
                    m.text AS text,
                    m.attributedBody AS attributed_body,
                    {cache_attach_expr} AS cache_has_attachments,
                    {sender_expr},
                    {guid_expr} AS guid,
                    {date_expr} AS msg_date
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                {chat_joins}
                WHERE m.ROWID > ?
                  AND {from_me_clause}
                  AND (m.associated_message_type IS NULL OR m.associated_message_type = 0)
                ORDER BY m.ROWID ASC
                """,
                (last_rowid,),
            ).fetchall()

            results: list[InboundMessage] = []
            seen_rowids: set[int] = set()
            seen_approval_replies: dict[str, set[str]] = {}
            pinned_self_reply = (
                self.config.self_chat_reply_to.strip()
                if self.config is not None and getattr(self.config, "self_chat_reply_to", "")
                else ""
            )

            for row in rows:
                rowid = int(row["rowid"])
                # Guard against duplicate rows when a message joins multiple chat records
                if rowid in seen_rowids:
                    continue
                seen_rowids.add(rowid)

                if rowid > max_seen:
                    max_seen = rowid

                is_from_me = int(row["is_from_me"] or 0)
                raw_text_col = row["text"] or ""
                decoded_attr = (
                    decode_attributed_body(row["attributed_body"])
                    if row["attributed_body"]
                    else ""
                )

                # Layer 1: Bot outbound echo suppression (applies to BOTH is_from_me=0 and is_from_me=1)
                if self._is_bot_echo(raw_text_col, now_ts=now_ts) or self._is_bot_echo(
                    decoded_attr, now_ts=now_ts
                ):
                    logger.debug("Skipped bot echo message rowid=%d", rowid)
                    continue

                chat_guid = (row["chat_guid"] or "").strip()
                chat_identifier = (row["chat_identifier"] or "").strip()
                raw_handle_id = (row["raw_handle_id"] or "").strip()

                if is_from_me == 1:
                    # Prefer exact destination thread typed on iPhone first!
                    raw_sender = chat_identifier or raw_handle_id
                else:
                    raw_sender = raw_handle_id or chat_identifier

                sender_norm = normalize_sender(raw_sender)
                if not sender_norm:
                    continue

                is_admin = sender_norm in self.allowed_senders
                is_beta_member = (not is_admin) and self.store.is_authorized_user(sender_norm)

                # is_from_me == 1 is ONLY considered for admin self-chat (never outbound messages to others)
                if is_from_me == 1:
                    if not (self.allow_self_chat and is_admin):
                        continue
                else:
                    # is_from_me == 0 routing: admin, active beta member, or invite-code candidate
                    if not (is_admin or is_beta_member or self.enable_invite_codes):
                        if not self.allowed_senders and not self.enable_invite_codes:
                            logger.warning(
                                "Dropping inbound message rowid=%d because ALLOWED_SENDERS is empty.",
                                rowid,
                            )
                        continue

                if self.allow_self_chat and is_admin and pinned_self_reply:
                    reply_target = pinned_self_reply
                elif chat_guid:
                    reply_target = chat_guid
                else:
                    reply_target = raw_sender or sender_norm

                body = _clean_decoded_text(raw_text_col, strip_echo_marker=True)
                if not body and decoded_attr:
                    body = _clean_decoded_text(decoded_attr, strip_echo_marker=True)

                image_urls, attach_paths, mime_types = self._fetch_attachments_for_message(
                    conn, rowid, has_attachment_tables
                )

                # Approval replies belong to a live HumanGate and must never be replayed.
                admin_self_chat = self.allow_self_chat and is_admin
                gate_senders = self.allowed_senders if admin_self_chat else {sender_norm}
                gate_scope = "__admin_self__" if admin_self_chat else sender_norm
                has_pending_approval = (
                    self.human_gate is not None
                    and self.human_gate.has_pending_any(gate_senders)
                )

                # Layer 2: Self-Chat prefix & pending-2FA bypass & cross-alias twin deduplication
                if admin_self_chat:
                    matched_prefix, stripped_body = _strip_self_chat_prefix(
                        body, self.self_chat_prefix
                    )
                    if matched_prefix:
                        body = stripped_body
                        if not body and image_urls:
                            body = "[用户发送了图片]"
                        if not body and not image_urls:
                            continue
                    elif _is_self_chat_xiaona_slash_trigger(body):
                        # Preserve `/xiaona ...` / `/小纳 ...` intact so Orchestrator detects explicit XiaoNa request
                        pass
                    elif has_pending_approval and gate_scope not in seen_approval_replies and body:
                        # Bare 2FA code like "654321" unlocks HumanGate without requiring /ai
                        pass
                    else:
                        # Plain personal note sent to oneself (e.g. "买牛奶") -> ignore on both rows!
                        continue

                    if self._is_duplicate_self_twin(
                        sender_norm,
                        body,
                        len(image_urls),
                        now_ts=now_ts,
                        recent=recent_twins,
                        is_admin_self=True,
                    ):
                        logger.debug(
                            "Suppressed self-chat mirror twin rowid=%d for sender=%s",
                            rowid,
                            sender_norm,
                        )
                        continue
                else:
                    if not body and image_urls:
                        body = "[用户发送了图片]"
                    if not body and not image_urls:
                        continue

                if has_pending_approval:
                    answer = "".join(body.split())
                    previous = seen_approval_replies.setdefault(gate_scope, set())
                    # A second copy of an approval must not become a queued prompt.
                    if answer in previous:
                        continue
                    has_pending_approval = not previous
                    previous.add(answer)
                results.append(
                    InboundMessage(
                        rowid=rowid,
                        guid=row["guid"] or f"rowid-{rowid}",
                        sender=raw_sender or sender_norm,
                        text=body,
                        timestamp_unix=self._apple_date_to_unix(row["msg_date"]),
                        image_data_urls=image_urls,
                        attachment_paths=attach_paths,
                        is_from_me=is_from_me,
                        reply_target=reply_target,
                        approval_only=has_pending_approval,
                        image_mime_types=mime_types,
                    )
                )

        if max_seen > last_rowid:
            if self.durable_inbox:
                self.store.record_imessage_batch(
                    self.CURSOR_META_KEY,
                    max_seen,
                    [
                        (msg.rowid, json.dumps({**asdict(msg), "image_data_urls": []}))
                        for msg in results
                        if not msg.approval_only
                    ],
                )
            else:
                self.store.set_meta(self.CURSOR_META_KEY, str(max_seen))

        self._recent_self_inbound = recent_twins
        return results

    def restore_message_images(self, msg: InboundMessage) -> None:
        """Reopen local attachments for queued messages without duplicating them in SQLite."""
        if msg.image_data_urls:
            return
        for i, path in enumerate(msg.attachment_paths):
            mime = msg.image_mime_types[i] if i < len(msg.image_mime_types) else None
            image_url, _ = self._encode_image_attachment(path, mime)
            if image_url:
                msg.image_data_urls.append(image_url)

    def poll_inbound(self) -> list[InboundMessage]:
        """Backward-compatible alias for poll_new_messages."""
        return self.poll_new_messages()

    async def send_text(self, recipient: str, text: str) -> None:
        """Send text via parameterized osascript argv; pure URLs omit BOT_ECHO_MARKER for Rich Link Cards."""
        chunks = _chunk_text(text, max_chars=MAX_IMESSAGE_CHUNK_CHARS - 1)
        if not chunks:
            return
        self.record_outbound_echo(text)
        target = recipient.strip()
        for chunk in chunks:
            clean_candidate = chunk.replace(BOT_ECHO_MARKER, "").strip()
            is_pure_url = bool(re.fullmatch(r"https?://\S+", clean_candidate))
            if is_pure_url:
                self.record_outbound_echo(clean_candidate)
                wire_chunk = clean_candidate
            else:
                self.record_outbound_echo(chunk)
                wire_chunk = (
                    chunk if chunk.startswith(BOT_ECHO_MARKER) else f"{BOT_ECHO_MARKER}{chunk}"
                )
            proc = await asyncio.create_subprocess_exec(
                self.osascript_bin,
                "-e",
                APPLESCRIPT_SEND_IMESSAGE,
                target,
                wire_chunk,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await proc.communicate()
            if proc.returncode != 0:
                err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"osascript 发送 iMessage 失败 (exit={proc.returncode}): {err_msg}"
                )

    def _stage_to_imessage_outbox(self, src_path: Path) -> Path:
        """Copy outgoing file to ~/Pictures/PlotLoopOutbox (fallback state_dir/outbox) for MobileSMS sandbox access."""
        from .formatter import sanitize_attachment_filename

        safe_name = sanitize_attachment_filename(src_path.name, default_stem="HyperText附件")
        primary_dir = Path.home() / "Pictures" / "PlotLoopOutbox"
        fallback_base = (
            self.config.state_dir
            if self.config is not None
            else self.store.db_path.parent
        )
        fallback_dir = Path(fallback_base) / "outbox"

        for candidate_dir in (primary_dir, fallback_dir):
            try:
                candidate_dir.mkdir(parents=True, exist_ok=True)
                dest = candidate_dir / safe_name
                if src_path.resolve() != dest.resolve():
                    shutil.copy2(src_path, dest)
                return dest.resolve()
            except OSError:
                continue

        raise OSError(f"无法写入附件发件箱目录: {primary_dir} / {fallback_dir}")

    async def send_attachment(
        self, recipient: str, file_path: Path | str, caption: str = ""
    ) -> bool:
        """Validate, stage to MobileSMS-friendly outbox, and send attachment via parameterized osascript."""
        from .formatter import format_for_imessage

        src = Path(file_path).expanduser()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"附件文件不存在或不是普通文件: {src}")
        size_bytes = src.stat().st_size
        if size_bytes > MAX_OUTBOUND_ATTACHMENT_BYTES:
            raise ValueError(
                f"附件大小 ({size_bytes} bytes) 超过 50MB 限制: {src.name}"
            )

        staged_path = self._stage_to_imessage_outbox(src)
        self.record_outbound_echo(staged_path.name)
        target = recipient.strip()

        proc = await asyncio.create_subprocess_exec(
            self.osascript_bin,
            "-e",
            APPLESCRIPT_SEND_ATTACHMENT,
            target,
            str(staged_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"osascript 发送附件失败 (exit={proc.returncode}): {err_msg}"
            )

        if caption and caption.strip():
            formatted_caption = format_for_imessage(caption)
            if formatted_caption:
                await self.send_text(target, formatted_caption)
        return True


class CliBridge(MessageBridge):
    """Local interactive terminal bridge (also records outbox messages & attachments for tests)."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.outbox: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str, str]] = []

    async def send_text(self, recipient: str, text: str) -> None:
        self.outbox.append((recipient, text))
        if not self.quiet:
            print(f"\n🤖 [HyperText -> {recipient}]\n{text}\n", flush=True)

    async def send_attachment(
        self, recipient: str, file_path: Path | str, caption: str = ""
    ) -> bool:
        from .formatter import format_for_imessage

        p = Path(file_path).expanduser()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"附件文件不存在或不是普通文件: {p}")
        if p.stat().st_size > MAX_OUTBOUND_ATTACHMENT_BYTES:
            raise ValueError(f"附件超过 50MB 上限: {p.name}")

        clean_cap = format_for_imessage(caption) if caption and caption.strip() else ""
        self.attachments.append((recipient, str(p), clean_cap))
        if not self.quiet:
            print(f"\n📎 [iMessage 附件 -> {recipient}] {p}", flush=True)
        if clean_cap:
            await self.send_text(recipient, clean_cap)
        return True
