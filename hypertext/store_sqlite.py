"""SQLite WAL state persistence, FTS5 hybrid memory, invite beta & quotas, and thinking-chain history store."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import secrets
import sqlite3
import string
import threading
import time
from pathlib import Path
from typing import Any

from .config import normalize_sender

logger = logging.getLogger("hypertext.store_sqlite")

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_INVITE_ALPHABET = string.ascii_uppercase + string.digits
_INVITE_CODE_RE = re.compile(r"(?i)\b((?:HT|INST)-[A-Z0-9]{4,16})\b")
_INST_CODE_RE = _INVITE_CODE_RE
_ACTIVATE_CMD_RE = re.compile(r"(?i)^/(?:activate|invite|redeem)\s+([A-Za-z0-9_\-]{3,32})\s*$")
_SINGLE_UPPER_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{3,31}$")


def _tokenize_for_fts(text: str) -> str:
    """Tokenize mixed Chinese + English text into space-delimited FTS5 tokens."""
    if not text:
        return ""
    lowered = text.lower()
    tokens: list[str] = []

    for m in _ASCII_WORD_RE.finditer(lowered):
        tokens.append(m.group(0))

    for m in _CJK_RUN_RE.finditer(lowered):
        run = m.group(0)
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
        for ch in run:
            tokens.append(ch)

    return " ".join(tokens)


def _build_fts_match_expression(query: str) -> str:
    """Build a safe FTS5 MATCH expression combining bigrams, ASCII prefix words, and CJK chars."""
    if not query or not query.strip():
        return ""
    lowered = query.lower().strip()
    parts: list[str] = []
    seen: set[str] = set()

    for m in _ASCII_WORD_RE.finditer(lowered):
        w = m.group(0)
        expr = f'"{w}"*' if len(w) >= 2 else f'"{w}"'
        if expr not in seen:
            seen.add(expr)
            parts.append(expr)

    for m in _CJK_RUN_RE.finditer(lowered):
        run = m.group(0)
        for i in range(len(run) - 1):
            bg = f'"{run[i : i + 2]}"'
            if bg not in seen:
                seen.add(bg)
                parts.append(bg)
        for ch in run:
            cg = f'"{ch}"'
            if cg not in seen:
                seen.add(cg)
                parts.append(cg)

    return " OR ".join(parts)


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return dot / (norm_a * norm_b)


def sanitize_conversation_history(
    raw_messages: list[dict[str, Any]],
    is_deepseek_native: bool = True,
) -> list[dict[str, Any]]:
    """Sanitize stored conversation history for DeepSeek native or OpenAI-compatible APIs."""
    sanitized: list[dict[str, Any]] = []
    n = len(raw_messages)
    i = 0

    def _format_assistant(msg: dict[str, Any], include_tool_calls: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": "assistant",
            "content": msg.get("content") if msg.get("content") is not None else "",
        }
        if include_tool_calls and msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        if is_deepseek_native:
            rc = msg.get("reasoning_content")
            out["reasoning_content"] = rc if isinstance(rc, str) else ""
            stc = msg.get("server_tool_calls")
            if stc:
                out["server_tool_calls"] = stc
        return out

    while i < n:
        msg = raw_messages[i]
        role = msg.get("role")

        if role == "tool":
            i += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]
            expected_ids = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("id")
            ]
            j = i + 1
            following_tools: list[dict[str, Any]] = []
            while j < n and raw_messages[j].get("role") == "tool":
                following_tools.append(raw_messages[j])
                j += 1

            found_by_id = {
                t.get("tool_call_id"): t
                for t in following_tools
                if t.get("tool_call_id")
            }

            if expected_ids and all(tc_id in found_by_id for tc_id in expected_ids):
                sanitized.append(_format_assistant(msg, include_tool_calls=True))
                for tc_id in expected_ids:
                    t_msg = found_by_id[tc_id]
                    tool_entry: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": (
                            t_msg.get("content")
                            if isinstance(t_msg.get("content"), str)
                            else json.dumps(t_msg.get("content") or "", ensure_ascii=False)
                        ),
                    }
                    if t_msg.get("name"):
                        tool_entry["name"] = t_msg["name"]
                    sanitized.append(tool_entry)
            else:
                text_content = msg.get("content")
                if isinstance(text_content, str) and text_content.strip():
                    sanitized.append(_format_assistant(msg, include_tool_calls=False))
                logger.warning(
                    "Sanitized incomplete assistant tool_calls turn (expected=%s, found=%s)",
                    expected_ids,
                    list(found_by_id.keys()),
                )
            i = j
            continue

        if role == "assistant":
            sanitized.append(_format_assistant(msg, include_tool_calls=False))
            i += 1
            continue

        if role in ("user", "system"):
            entry: dict[str, Any] = {
                "role": role,
                "content": msg.get("content") if msg.get("content") is not None else "",
            }
            sanitized.append(entry)
            i += 1
            continue

        i += 1

    return sanitized


class StateStore:
    """Thread-safe SQLite WAL persistence with FTS5 hybrid memory, invite beta, and daily quotas."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path_str = str(db_path)
        if self.db_path_str != ":memory:":
            p = Path(db_path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            self.db_path_str = str(p)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path_str, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute("PRAGMA foreign_keys=ON;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    mem_key TEXT NOT NULL,
                    mem_value TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    embedding_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(sender, mem_key)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_sender_updated ON memories(sender, updated_at DESC);"
            )

            mem_cols = {
                r["name"] for r in cur.execute("PRAGMA table_info(memories);").fetchall()
            }
            if "tags" not in mem_cols:
                cur.execute("ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '';")
            if "embedding_json" not in mem_cols:
                cur.execute("ALTER TABLE memories ADD COLUMN embedding_json TEXT;")

            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    mem_id UNINDEXED,
                    sender UNINDEXED,
                    key_tokens,
                    value_tokens,
                    tag_tokens,
                    tokenize='unicode61'
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    reasoning_content TEXT,
                    server_tool_calls_json TEXT,
                    created_at REAL NOT NULL
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_sender_id ON conversation_messages(sender, id ASC);"
            )

            conv_cols = {
                r["name"]
                for r in cur.execute("PRAGMA table_info(conversation_messages);").fetchall()
            }
            if "reasoning_content" not in conv_cols:
                cur.execute("ALTER TABLE conversation_messages ADD COLUMN reasoning_content TEXT;")
            if "server_tool_calls_json" not in conv_cols:
                cur.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN server_tool_calls_json TEXT;"
                )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    run_at REAL NOT NULL,
                    interval_seconds REAL NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at REAL,
                    created_at REAL NOT NULL
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_tasks(enabled, run_at ASC);"
            )

            # v0.3.0 Multi-User Beta & Quota tables
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS authorized_users (
                    sender TEXT PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'member',
                    activated_via TEXT,
                    daily_msg_limit INTEGER NOT NULL DEFAULT 50,
                    daily_browser_limit INTEGER NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS invite_codes (
                    code TEXT PRIMARY KEY,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    daily_msg_limit INTEGER NOT NULL DEFAULT 50,
                    daily_browser_limit INTEGER NOT NULL DEFAULT 10,
                    note TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    sender TEXT NOT NULL,
                    date_key TEXT NOT NULL,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    browser_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (sender, date_key)
                );
                """
            )

            self._conn.commit()
            self._backfill_fts_if_needed()

    def _backfill_fts_if_needed(self) -> None:
        """Automatically backfill/sync memories_fts if existing rows in memories are missing."""
        with self._lock:
            missing_rows = self._conn.execute(
                """
                SELECT m.id, m.sender, m.mem_key, m.mem_value, m.tags
                FROM memories m
                LEFT JOIN memories_fts f ON CAST(f.mem_id AS INTEGER) = m.id
                WHERE f.mem_id IS NULL
                """
            ).fetchall()
            if not missing_rows:
                return
            for r in missing_rows:
                self._conn.execute(
                    """
                    INSERT INTO memories_fts(mem_id, sender, key_tokens, value_tokens, tag_tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(r["id"]),
                        r["sender"],
                        _tokenize_for_fts(r["mem_key"]),
                        _tokenize_for_fts(r["mem_value"]),
                        _tokenize_for_fts(r["tags"] or ""),
                    ),
                )
            self._conn.commit()
            logger.info("Backfilled %d existing memory rows into memories_fts.", len(missing_rows))

    def rebuild_fts_index(self) -> int:
        with self._lock:
            self._conn.execute("DELETE FROM memories_fts;")
            rows = self._conn.execute(
                "SELECT id, sender, mem_key, mem_value, tags FROM memories"
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    """
                    INSERT INTO memories_fts(mem_id, sender, key_tokens, value_tokens, tag_tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(r["id"]),
                        r["sender"],
                        _tokenize_for_fts(r["mem_key"]),
                        _tokenize_for_fts(r["mem_value"]),
                        _tokenize_for_fts(r["tags"] or ""),
                    ),
                )
            self._conn.commit()
            return len(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv_meta WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO kv_meta(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(value), now),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Multi-User Invite Codes, Authorization & Daily Quotas
    # ------------------------------------------------------------------
    @staticmethod
    def _now_iso() -> str:
        return dt.datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _today_key() -> str:
        return dt.datetime.now().strftime("%Y-%m-%d")

    def create_invite_code(
        self,
        code: str | None = None,
        max_uses: int = 1,
        daily_msg_limit: int = 50,
        daily_browser_limit: int = 10,
        note: str = "",
    ) -> str:
        """Create (or update) an invite code, auto-generating `HT-XXXXXX` when code is None."""
        uses = max(1, int(max_uses))
        msg_lim = max(1, int(daily_msg_limit))
        br_lim = max(1, int(daily_browser_limit))
        created = self._now_iso()

        with self._lock:
            if code is not None and code.strip():
                clean_code = code.strip().upper()
                self._conn.execute(
                    """
                    INSERT INTO invite_codes(
                        code, max_uses, used_count, daily_msg_limit, daily_browser_limit, note, status, created_at
                    )
                    VALUES (?, ?, 0, ?, ?, ?, 'active', ?)
                    ON CONFLICT(code) DO UPDATE SET
                        max_uses = excluded.max_uses,
                        daily_msg_limit = excluded.daily_msg_limit,
                        daily_browser_limit = excluded.daily_browser_limit,
                        note = excluded.note,
                        status = 'active'
                    """,
                    (clean_code, uses, msg_lim, br_lim, (note or "").strip(), created),
                )
                self._conn.commit()
                return clean_code

            for _ in range(32):
                candidate = "HT-" + "".join(
                    secrets.choice(_INVITE_ALPHABET) for _ in range(6)
                )
                exists = self._conn.execute(
                    "SELECT 1 FROM invite_codes WHERE code = ?", (candidate,)
                ).fetchone()
                if not exists:
                    self._conn.execute(
                        """
                        INSERT INTO invite_codes(
                            code, max_uses, used_count, daily_msg_limit, daily_browser_limit, note, status, created_at
                        )
                        VALUES (?, ?, 0, ?, ?, ?, 'active', ?)
                        """,
                        (candidate, uses, msg_lim, br_lim, (note or "").strip(), created),
                    )
                    self._conn.commit()
                    return candidate
            raise RuntimeError("Failed to generate a unique invite code.")

    def seed_static_invite_codes(
        self,
        codes: list[str],
        daily_msg_limit: int = 50,
        daily_browser_limit: int = 10,
    ) -> None:
        """Idempotently seed static bootstrap invite codes with max_uses=999 if missing."""
        if not codes:
            return
        msg_lim = max(1, int(daily_msg_limit))
        br_lim = max(1, int(daily_browser_limit))
        created = self._now_iso()
        with self._lock:
            for raw in codes:
                clean = (raw or "").strip().upper()
                if not clean:
                    continue
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO invite_codes(
                        code, max_uses, used_count, daily_msg_limit, daily_browser_limit, note, status, created_at
                    )
                    VALUES (?, 999, 0, ?, ?, 'static_bootstrap', 'active', ?)
                    """,
                    (clean, msg_lim, br_lim, created),
                )
            self._conn.commit()

    def is_authorized_user(self, sender: str) -> bool:
        norm = normalize_sender(sender)
        if not norm:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM authorized_users WHERE sender = ? AND status = 'active'",
                (norm,),
            ).fetchone()
            return row is not None

    def is_revoked_user(self, sender: str) -> bool:
        norm = normalize_sender(sender)
        if not norm:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM authorized_users WHERE sender = ? AND status = 'revoked'",
                (norm,),
            ).fetchone()
            return row is not None

    def allow_user(
        self,
        sender: str,
        daily_msg_limit: int = 50,
        daily_browser_limit: int = 10,
        note: str = "",
    ) -> str:
        """Directly whitelist/activate a sender in SQLite (hot-effective without daemon restart)."""
        norm = normalize_sender(sender)
        if not norm:
            raise ValueError("用户名/手机号/邮箱不能为空")
        via = f"cli:{note.strip()}" if note and note.strip() else "cli:allow-user"
        now = self._now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO authorized_users (
                    sender, role, activated_via, daily_msg_limit, daily_browser_limit, status, created_at
                )
                VALUES (?, 'member', ?, ?, ?, 'active', ?)
                ON CONFLICT(sender) DO UPDATE SET
                    status = 'active',
                    activated_via = excluded.activated_via,
                    daily_msg_limit = excluded.daily_msg_limit,
                    daily_browser_limit = excluded.daily_browser_limit
                """,
                (
                    norm,
                    via,
                    max(1, int(daily_msg_limit)),
                    max(1, int(daily_browser_limit)),
                    now,
                ),
            )
            self._conn.commit()
        logger.info("Directly authorized user %s via CLI (%s)", norm, via)
        return norm

    def revoke_user(self, sender: str) -> bool:
        """Hot-revoke a sender in authorized_users (sets status='revoked' immediately)."""
        norm = normalize_sender(sender)
        if not norm:
            return False
        now = self._now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO authorized_users (
                    sender, role, activated_via, daily_msg_limit, daily_browser_limit, status, created_at
                )
                VALUES (?, 'member', 'cli:revoke-user', 50, 10, 'revoked', ?)
                ON CONFLICT(sender) DO UPDATE SET
                    status = 'revoked'
                """,
                (norm, now),
            )
            self._conn.commit()
        logger.info("Revoked user %s in authorized_users", norm)
        return True

    @staticmethod
    def _extract_candidate_invite_code(raw_text: str) -> str | None:
        cleaned = (raw_text or "").replace("\u2063", "").replace("\ufffc", "").strip()
        if not cleaned:
            return None
        m_inv = _INVITE_CODE_RE.search(cleaned)
        if m_inv:
            return m_inv.group(1).upper()
        m_cmd = _ACTIVATE_CMD_RE.match(cleaned)
        if m_cmd:
            return m_cmd.group(1).upper()
        if _SINGLE_UPPER_CODE_RE.match(cleaned):
            return cleaned.upper()
        return None

    def redeem_invite_code(self, sender: str, raw_text: str) -> tuple[bool, str]:
        """Attempt to extract and atomically redeem an invite code for sender."""
        norm = normalize_sender(sender)
        if not norm:
            return (False, "empty_sender")
        candidate = self._extract_candidate_invite_code(raw_text)
        if not candidate:
            return (False, "no_code_detected")

        now_iso = self._now_iso()
        with self._lock:
            if self.is_authorized_user(norm):
                return (True, candidate)

            row = self._conn.execute(
                """
                SELECT code, max_uses, used_count, daily_msg_limit, daily_browser_limit, status
                FROM invite_codes
                WHERE code = ?
                """,
                (candidate,),
            ).fetchone()
            if row is None:
                return (False, "invalid_code")

            cur = self._conn.execute(
                """
                UPDATE invite_codes
                SET used_count = used_count + 1
                WHERE code = ? AND status = 'active' AND used_count < max_uses
                """,
                (candidate,),
            )
            if cur.rowcount == 0:
                self._conn.commit()
                return (False, "code_exhausted_or_inactive")

            self._conn.execute(
                """
                INSERT INTO authorized_users(
                    sender, role, activated_via, daily_msg_limit, daily_browser_limit, status, created_at
                )
                VALUES (?, 'member', ?, ?, ?, 'active', ?)
                ON CONFLICT(sender) DO UPDATE SET
                    status = 'active',
                    activated_via = excluded.activated_via,
                    daily_msg_limit = excluded.daily_msg_limit,
                    daily_browser_limit = excluded.daily_browser_limit
                """,
                (
                    norm,
                    candidate,
                    int(row["daily_msg_limit"]),
                    int(row["daily_browser_limit"]),
                    now_iso,
                ),
            )
            self._conn.commit()
            logger.info("Sender %s redeemed invite code %s", norm, candidate)
            return (True, candidate)

    def check_and_increment_quota(
        self,
        sender: str,
        kind: str = "msg",
        is_admin: bool = False,
    ) -> tuple[bool, str]:
        """Enforce daily per-member message ('msg') or browser ('browser') quota."""
        if kind not in ("msg", "browser"):
            raise ValueError(f"Unsupported quota kind: {kind}")
        if is_admin:
            return (True, "")

        norm = normalize_sender(sender)
        date_key = self._today_key()

        with self._lock:
            user_row = self._conn.execute(
                """
                SELECT role, daily_msg_limit, daily_browser_limit, status
                FROM authorized_users
                WHERE sender = ?
                """,
                (norm,),
            ).fetchone()

            if user_row is not None:
                if user_row["status"] != "active":
                    return (False, "⚠️ 当前内测账号状态不可用，请联系管理员。")
                if user_row["role"] == "admin":
                    return (True, "")
                msg_limit = int(user_row["daily_msg_limit"])
                browser_limit = int(user_row["daily_browser_limit"])
            else:
                msg_limit = 50
                browser_limit = 10

            self._conn.execute(
                """
                INSERT OR IGNORE INTO daily_usage(sender, date_key, msg_count, browser_count)
                VALUES (?, ?, 0, 0)
                """,
                (norm, date_key),
            )
            usage_row = self._conn.execute(
                """
                SELECT msg_count, browser_count
                FROM daily_usage
                WHERE sender = ? AND date_key = ?
                """,
                (norm, date_key),
            ).fetchone()
            msg_count = int(usage_row["msg_count"]) if usage_row else 0
            browser_count = int(usage_row["browser_count"]) if usage_row else 0

            if kind == "msg":
                if msg_count >= msg_limit:
                    return (
                        False,
                        f"⚠️ 今日消息额度已达上限（{msg_count}/{msg_limit} 条），将于明日 00:00 自动重置。",
                    )
                self._conn.execute(
                    "UPDATE daily_usage SET msg_count = msg_count + 1 WHERE sender = ? AND date_key = ?",
                    (norm, date_key),
                )
            else:
                if browser_count >= browser_limit:
                    return (
                        False,
                        f"⚠️ 今日浏览器任务额度已达上限（{browser_count}/{browser_limit} 次），将于明日 00:00 自动重置。",
                    )
                self._conn.execute(
                    "UPDATE daily_usage SET browser_count = browser_count + 1 WHERE sender = ? AND date_key = ?",
                    (norm, date_key),
                )

            self._conn.commit()
            return (True, "")

    def should_send_invite_prompt(
        self, sender: str, cooldown_sec: int = 86400
    ) -> bool:
        """Enforce 24h anti-spam cooldown before sending polite invite prompt to unknown senders."""
        norm = normalize_sender(sender)
        if not norm:
            return False
        meta_key = f"invite_prompt_ts:{norm}"
        now = time.time()
        with self._lock:
            raw_prev = self.get_meta(meta_key)
            if raw_prev is not None:
                try:
                    prev_ts = float(raw_prev)
                    if now - prev_ts < float(cooldown_sec):
                        return False
                except ValueError:
                    pass
            self.set_meta(meta_key, str(now))
            return True

    def list_authorized_users(self) -> list[dict[str, Any]]:
        date_key = self._today_key()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    u.sender,
                    u.role,
                    u.activated_via,
                    u.daily_msg_limit,
                    u.daily_browser_limit,
                    u.status,
                    u.created_at,
                    COALESCE(d.msg_count, 0) AS today_msg_count,
                    COALESCE(d.browser_count, 0) AS today_browser_count
                FROM authorized_users u
                LEFT JOIN daily_usage d
                    ON d.sender = u.sender AND d.date_key = ?
                ORDER BY u.created_at DESC, u.sender ASC
                """,
                (date_key,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_invite_codes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    code,
                    max_uses,
                    used_count,
                    daily_msg_limit,
                    daily_browser_limit,
                    note,
                    status,
                    created_at
                FROM invite_codes
                ORDER BY created_at DESC, code ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Long-term memory + SQLite FTS5 Hybrid Retrieval
    # ------------------------------------------------------------------
    def save_memory(
        self,
        sender: str,
        mem_key: str,
        mem_value: str,
        tags: str = "",
        embedding: list[float] | None = None,
    ) -> None:
        norm_sender = normalize_sender(sender)
        key_clean = mem_key.strip()
        val_clean = mem_value.strip()
        tags_clean = (tags or "").strip()
        emb_json = json.dumps(embedding) if embedding else None
        now = time.time()

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memories(sender, mem_key, mem_value, tags, embedding_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sender, mem_key) DO UPDATE SET
                    mem_value = excluded.mem_value,
                    tags = excluded.tags,
                    embedding_json = COALESCE(excluded.embedding_json, memories.embedding_json),
                    updated_at = excluded.updated_at
                """,
                (norm_sender, key_clean, val_clean, tags_clean, emb_json, now, now),
            )
            row = self._conn.execute(
                "SELECT id FROM memories WHERE sender = ? AND mem_key = ?",
                (norm_sender, key_clean),
            ).fetchone()
            if row is not None:
                mem_id = int(row["id"])
                self._conn.execute(
                    "DELETE FROM memories_fts WHERE CAST(mem_id AS INTEGER) = ?",
                    (mem_id,),
                )
                self._conn.execute(
                    """
                    INSERT INTO memories_fts(mem_id, sender, key_tokens, value_tokens, tag_tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(mem_id),
                        norm_sender,
                        _tokenize_for_fts(key_clean),
                        _tokenize_for_fts(val_clean),
                        _tokenize_for_fts(tags_clean),
                    ),
                )
            self._conn.commit()

    def delete_memory(self, sender: str, mem_key: str) -> bool:
        norm_sender = normalize_sender(sender)
        key_clean = mem_key.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM memories WHERE sender = ? AND mem_key = ?",
                (norm_sender, key_clean),
            ).fetchone()
            if row is None:
                return False
            mem_id = int(row["id"])
            self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            self._conn.execute(
                "DELETE FROM memories_fts WHERE CAST(mem_id AS INTEGER) = ?",
                (mem_id,),
            )
            self._conn.commit()
            return True

    def search_memories(
        self,
        sender: str,
        query: str = "",
        limit: int = 15,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid memory search: FTS5 BM25 + exact LIKE boost + optional embedding cosine fusion."""
        norm_sender = normalize_sender(sender)
        q_clean = (query or "").strip()
        effective_limit = max(1, int(limit))

        with self._lock:
            self._backfill_fts_if_needed()

            if not q_clean and not query_embedding:
                rows = self._conn.execute(
                    """
                    SELECT id, mem_key, mem_value, tags, updated_at
                    FROM memories
                    WHERE sender = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (norm_sender, effective_limit),
                ).fetchall()
                return [
                    {
                        "key": r["mem_key"],
                        "value": r["mem_value"],
                        "tags": r["tags"],
                        "updated_at": r["updated_at"],
                        "score": 1.0,
                    }
                    for r in rows
                ]

            scores: dict[int, float] = {}
            row_cache: dict[int, sqlite3.Row] = {}

            fts_expr = _build_fts_match_expression(q_clean)
            if fts_expr:
                fts_rows = self._conn.execute(
                    """
                    SELECT
                        m.id,
                        m.mem_key,
                        m.mem_value,
                        m.tags,
                        m.embedding_json,
                        m.updated_at,
                        bm25(memories_fts, 0.0, 0.0, 3.0, 2.0, 2.5) AS raw_bm25
                    FROM memories_fts
                    INNER JOIN memories m ON m.id = CAST(memories_fts.mem_id AS INTEGER)
                    WHERE memories_fts MATCH ?
                      AND m.sender = ?
                    """,
                    (fts_expr, norm_sender),
                ).fetchall()
                for r in fts_rows:
                    mid = int(r["id"])
                    row_cache[mid] = r
                    bm25_positive = max(0.05, -float(r["raw_bm25"]))
                    scores[mid] = scores.get(mid, 0.0) + bm25_positive

            raw_terms = [t for t in re.split(r"[\s,，;；、]+", q_clean.lower()) if t]
            all_sender_rows = self._conn.execute(
                """
                SELECT id, mem_key, mem_value, tags, embedding_json, updated_at
                FROM memories
                WHERE sender = ?
                """,
                (norm_sender,),
            ).fetchall()

            q_lower = q_clean.lower()
            for r in all_sender_rows:
                mid = int(r["id"])
                k_low = (r["mem_key"] or "").lower()
                v_low = (r["mem_value"] or "").lower()
                t_low = (r["tags"] or "").lower()

                exact_boost = 0.0
                if q_lower:
                    if q_lower in k_low:
                        exact_boost += 6.0
                    if q_lower in t_low:
                        exact_boost += 4.5
                    if q_lower in v_low:
                        exact_boost += 4.0

                matched_term_count = 0
                for term in raw_terms:
                    hit = False
                    if term in k_low:
                        exact_boost += 3.0
                        hit = True
                    if term in t_low:
                        exact_boost += 2.2
                        hit = True
                    if term in v_low:
                        exact_boost += 2.0
                        hit = True
                    if hit:
                        matched_term_count += 1

                if matched_term_count >= 2:
                    exact_boost += 2.5 * matched_term_count

                vec_boost = 0.0
                if query_embedding and r["embedding_json"]:
                    try:
                        mem_vec = json.loads(r["embedding_json"])
                        if isinstance(mem_vec, list):
                            cos_sim = _cosine_similarity(query_embedding, mem_vec)
                            if cos_sim >= 0.25:
                                vec_boost = 5.0 * cos_sim
                    except (ValueError, TypeError):
                        pass

                if exact_boost > 0.0 or vec_boost > 0.0:
                    row_cache[mid] = r
                    scores[mid] = scores.get(mid, 0.0) + exact_boost + vec_boost

            ranked_ids = sorted(
                scores.keys(),
                key=lambda mid: (scores[mid], float(row_cache[mid]["updated_at"]), mid),
                reverse=True,
            )[:effective_limit]

            return [
                {
                    "key": row_cache[mid]["mem_key"],
                    "value": row_cache[mid]["mem_value"],
                    "tags": row_cache[mid]["tags"],
                    "updated_at": row_cache[mid]["updated_at"],
                    "score": round(scores[mid], 4),
                }
                for mid in ranked_ids
            ]

    def append_conversation_message(
        self,
        sender: str,
        role: str,
        content: Any,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        reasoning_content: str | None = None,
        server_tool_calls: list[dict[str, Any]] | None = None,
    ) -> int:
        norm_sender = normalize_sender(sender)
        content_json = json.dumps(content if content is not None else "", ensure_ascii=False)
        tool_calls_json = (
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        )
        if role == "assistant":
            stored_reasoning: str | None = (
                reasoning_content if isinstance(reasoning_content, str) else ""
            )
        else:
            stored_reasoning = reasoning_content
        server_tc_json = (
            json.dumps(server_tool_calls, ensure_ascii=False)
            if server_tool_calls
            else None
        )
        now = time.time()

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO conversation_messages(
                    sender, role, content_json, tool_calls_json, tool_call_id,
                    name, reasoning_content, server_tool_calls_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm_sender,
                    role,
                    content_json,
                    tool_calls_json,
                    tool_call_id,
                    name,
                    stored_reasoning,
                    server_tc_json,
                    now,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def get_conversation_history(
        self,
        sender: str,
        limit: int = 40,
        is_deepseek_native: bool = True,
    ) -> list[dict[str, Any]]:
        """Load recent conversation messages, preserving complete tool chains and sanitizing crashes."""
        norm_sender = normalize_sender(sender)
        effective_limit = max(2, int(limit))
        fetch_window = effective_limit + 16

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    id, role, content_json, tool_calls_json, tool_call_id,
                    name, reasoning_content, server_tool_calls_json
                FROM conversation_messages
                WHERE sender = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (norm_sender, fetch_window),
            ).fetchall()

        rows_asc = list(reversed(rows))
        raw_msgs: list[dict[str, Any]] = []
        for r in rows_asc:
            try:
                content = json.loads(r["content_json"])
            except json.JSONDecodeError:
                content = r["content_json"]

            tool_calls = None
            if r["tool_calls_json"]:
                try:
                    tool_calls = json.loads(r["tool_calls_json"])
                except json.JSONDecodeError:
                    tool_calls = None

            server_tool_calls = None
            if r["server_tool_calls_json"]:
                try:
                    server_tool_calls = json.loads(r["server_tool_calls_json"])
                except json.JSONDecodeError:
                    server_tool_calls = None

            raw_msgs.append(
                {
                    "role": r["role"],
                    "content": content,
                    "tool_calls": tool_calls,
                    "tool_call_id": r["tool_call_id"],
                    "name": r["name"],
                    "reasoning_content": r["reasoning_content"],
                    "server_tool_calls": server_tool_calls,
                }
            )

        sanitized = sanitize_conversation_history(
            raw_msgs, is_deepseek_native=is_deepseek_native
        )

        while len(sanitized) > effective_limit:
            sanitized.pop(0)
            while sanitized and sanitized[0].get("role") == "tool":
                sanitized.pop(0)

        return sanitized

    def clear_conversation_history(self, sender: str) -> None:
        norm_sender = normalize_sender(sender)
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversation_messages WHERE sender = ?", (norm_sender,)
            )
            self._conn.commit()

    def add_scheduled_task(
        self,
        sender: str,
        task_type: str,
        payload: str,
        run_at: float,
        interval_seconds: float = 0.0,
    ) -> int:
        if task_type not in ("direct_notify", "agent_wakeup"):
            raise ValueError(f"Unsupported task_type: {task_type}")
        norm_sender = normalize_sender(sender)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO scheduled_tasks(
                    sender, task_type, payload, run_at, interval_seconds, enabled, created_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    norm_sender,
                    task_type,
                    payload.strip(),
                    float(run_at),
                    max(0.0, float(interval_seconds)),
                    now,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_scheduled_tasks(
        self, sender: str | None = None, only_enabled: bool = True
    ) -> list[dict[str, Any]]:
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if sender is not None:
                clauses.append("sender = ?")
                params.append(normalize_sender(sender))
            if only_enabled:
                clauses.append("enabled = 1")
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = self._conn.execute(
                f"""
                SELECT id, sender, task_type, payload, run_at, interval_seconds, enabled, last_run_at
                FROM scheduled_tasks
                {where_sql}
                ORDER BY run_at ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def cancel_scheduled_task(self, task_id: int, sender: str | None = None) -> bool:
        with self._lock:
            if sender is not None:
                cur = self._conn.execute(
                    "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND sender = ? AND enabled = 1",
                    (int(task_id), normalize_sender(sender)),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND enabled = 1",
                    (int(task_id),),
                )
            self._conn.commit()
            return cur.rowcount > 0

    def claim_due_tasks(self, now_ts: float | None = None) -> list[dict[str, Any]]:
        """Atomically claim due tasks and reschedule recurring ones or disable one-shot ones."""
        now = time.time() if now_ts is None else float(now_ts)
        with self._lock:
            due_rows = self._conn.execute(
                """
                SELECT id, sender, task_type, payload, run_at, interval_seconds
                FROM scheduled_tasks
                WHERE enabled = 1 AND run_at <= ?
                ORDER BY run_at ASC, id ASC
                """,
                (now,),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for r in due_rows:
                tid = int(r["id"])
                interval = float(r["interval_seconds"] or 0.0)
                if interval > 0.0:
                    next_run = now + interval
                    self._conn.execute(
                        "UPDATE scheduled_tasks SET run_at = ?, last_run_at = ? WHERE id = ?",
                        (next_run, now, tid),
                    )
                else:
                    self._conn.execute(
                        "UPDATE scheduled_tasks SET enabled = 0, last_run_at = ? WHERE id = ?",
                        (now, tid),
                    )
                claimed.append(dict(r))
            if claimed:
                self._conn.commit()
            return claimed
