"""Configuration loader, .env parser, and placeholder API key protection."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("hypertext.config")

_PLACEHOLDER_PREFIXES = (
    "sk-替换为",
    "sk-你的",
    "sk-请替换",
    "sk-填写",
    "your_",
    "your-",
    "sk-your",
    "sk-xxx",
    "replace_",
    "replace-",
    "placeholder",
    "changeme",
    "todo_",
    "example_",
    "<your",
)

_PLACEHOLDER_EXACT = {
    "",
    "none",
    "null",
    "empty",
    "sk-",
    "sk-placeholder",
    "your_api_key",
    "your_api_key_here",
}

_VALID_REASONING_EFFORTS = {"none", "low", "medium", "high"}


def _is_placeholder_key(value: str | None) -> bool:
    """Return True if an API key is empty, non-ASCII, or a template placeholder.

    Prevents httpx ASCII header UnicodeEncodeError when users leave Chinese
    template comments in .env, and enables clean fallback / doctor diagnostics.
    """
    if value is None:
        return True
    cleaned = value.strip().strip("'").strip('"').strip()
    if not cleaned:
        return True
    if not cleaned.isascii():
        return True
    lower = cleaned.lower()
    if lower in _PLACEHOLDER_EXACT:
        return True
    if any(lower.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES):
        return True
    if cleaned.startswith("<") and cleaned.endswith(">"):
        return True
    if "xxxxxx" in lower or "replace_me" in lower or "your_key" in lower:
        return True
    return False


def _parse_dotenv_text(content: str) -> dict[str, str]:
    """Parse .env formatted text into a key-value dictionary using stdlib only."""
    parsed: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            quote = val[0]
            val = val[1:-1]
            if quote == '"':
                val = val.replace(r"\n", "\n").replace(r'\"', '"')
        else:
            comment_match = re.search(r"\s+#", val)
            if comment_match:
                val = val[: comment_match.start()].strip()
        parsed[key] = val
    return parsed


def _resolve_dotenv_candidates(env_file: str | Path | None = None) -> list[Path]:
    """Return ordered .env candidate paths: explicit path, else CWD .env then project root .env."""
    if env_file is not None:
        return [Path(env_file).expanduser()]
    cwd_env = Path.cwd() / ".env"
    project_root_env = Path(__file__).resolve().parent.parent / ".env"
    candidates = [cwd_env]
    try:
        if project_root_env.resolve() != cwd_env.resolve():
            candidates.append(project_root_env)
    except OSError:
        if project_root_env != cwd_env:
            candidates.append(project_root_env)
    return candidates


def _load_dotenv(env_file: str | Path | None = None, override: bool = False) -> Path | None:
    """Load environment variables from .env (CWD first, falling back to project root)."""
    loaded_primary: Path | None = None
    for candidate in _resolve_dotenv_candidates(env_file):
        try:
            if not candidate.is_file():
                continue
            data = _parse_dotenv_text(candidate.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Unable to read .env file %s: %s", candidate, exc)
            continue
        for k, v in data.items():
            if override or k not in os.environ:
                os.environ[k] = v
        if loaded_primary is None:
            loaded_primary = candidate
    return loaded_primary


def normalize_sender(sender: str) -> str:
    """Normalize phone numbers or Apple ID email handles for consistent matching."""
    s = (sender or "").strip()
    if not s:
        return ""
    if "@" in s:
        return s.lower()
    if s.startswith("+") or s[0].isdigit():
        cleaned = re.sub(r"[\s\-()]+", "", s)
        return cleaned
    return s


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return default


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass
class AgentConfig:
    """Runtime configuration for HyperText Agent."""

    provider_mode: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_reasoning_effort: str = "high"
    enable_deepseek_web_tools: bool = True

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"

    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_base_url: str = ""

    allowed_senders: set[str] = field(default_factory=set)
    admin_senders_ordered: list[str] = field(default_factory=list)
    allow_self_chat: bool = False
    self_chat_prefix: str = "/ai"
    self_chat_reply_to: str = ""

    enable_invite_codes: bool = False
    invite_codes: list[str] = field(default_factory=list)
    default_member_daily_msg_limit: int = 50
    default_member_daily_browser_limit: int = 10
    enable_instant_ack: bool = True
    fast_ack_delay_sec: float = 1.5
    heartbeat_interval_sec: float = 60.0
    max_heartbeat_per_turn: int = 1
    split_standalone_links: bool = True

    state_dir: Path = field(default_factory=lambda: Path("~/.hypertext").expanduser())
    chat_db_path: Path = field(
        default_factory=lambda: Path("~/Library/Messages/chat.db").expanduser()
    )

    poll_interval_sec: float = 1.5
    cron_interval_sec: float = 15.0
    max_history_messages: int = 40
    max_tool_rounds: int = 12
    human_gate_timeout_sec: float = 300.0

    browser_headless: bool = False
    browser_max_steps: int = 25
    browser_max_concurrency: int = 3
    browser_sensitive_data: dict[str, Any] = field(default_factory=dict)
    chromium_executable_path: str = ""

    loaded_env_file: Path | None = None
    deepseek_key_status: str = "empty"
    openai_key_status: str = "empty"

    def __post_init__(self) -> None:
        self.provider_mode = (self.provider_mode or "deepseek").strip().lower()
        if self.provider_mode not in ("deepseek", "openai"):
            self.provider_mode = "deepseek"

        self.deepseek_model = (self.deepseek_model or "deepseek-flash").strip() or "deepseek-flash"
        self.openai_model = (self.openai_model or "gpt-4.1-mini").strip() or "gpt-4.1-mini"

        effort = (self.deepseek_reasoning_effort or "high").strip().lower()
        self.deepseek_reasoning_effort = effort if effort in _VALID_REASONING_EFFORTS else "high"

        if _is_placeholder_key(self.deepseek_api_key):
            raw_ds = (self.deepseek_api_key or "").strip()
            if self.deepseek_key_status == "empty" and raw_ds:
                self.deepseek_key_status = "placeholder"
            self.deepseek_api_key = ""
        else:
            self.deepseek_api_key = self.deepseek_api_key.strip()
            self.deepseek_key_status = "configured"

        if _is_placeholder_key(self.openai_api_key):
            raw_oa = (self.openai_api_key or "").strip()
            if self.openai_key_status == "empty" and raw_oa:
                self.openai_key_status = "placeholder"
            self.openai_api_key = ""
        else:
            self.openai_api_key = self.openai_api_key.strip()
            self.openai_key_status = "configured"

        if _is_placeholder_key(self.embedding_api_key):
            self.embedding_api_key = ""
        else:
            self.embedding_api_key = self.embedding_api_key.strip()

        self.self_chat_prefix = (self.self_chat_prefix or "/ai").strip() or "/ai"
        self.self_chat_reply_to = (self.self_chat_reply_to or "").strip()
        self.invite_codes = [
            c.strip().upper() for c in (self.invite_codes or []) if c and c.strip()
        ]
        self.default_member_daily_msg_limit = max(1, int(self.default_member_daily_msg_limit))
        self.default_member_daily_browser_limit = max(
            1, int(self.default_member_daily_browser_limit)
        )
        self.browser_max_concurrency = max(1, int(self.browser_max_concurrency))
        self.fast_ack_delay_sec = max(0.0, float(self.fast_ack_delay_sec))
        self.heartbeat_interval_sec = max(0.0, float(self.heartbeat_interval_sec))
        self.max_heartbeat_per_turn = max(0, int(self.max_heartbeat_per_turn))

        self.state_dir = Path(self.state_dir).expanduser()
        self.chat_db_path = Path(self.chat_db_path).expanduser()
        raw_allowed_iter = list(self.allowed_senders) if self.allowed_senders else []
        was_sequence = isinstance(self.allowed_senders, (list, tuple))
        self.allowed_senders = {
            normalize_sender(s) for s in raw_allowed_iter if normalize_sender(s)
        }
        if self.admin_senders_ordered:
            dedup_ord: list[str] = []
            for item in self.admin_senders_ordered:
                n_item = normalize_sender(item)
                if n_item and n_item not in dedup_ord:
                    dedup_ord.append(n_item)
            for s in sorted(self.allowed_senders, key=lambda x: (0 if x.startswith("+") else 1, x)):
                if s not in dedup_ord:
                    dedup_ord.append(s)
            self.admin_senders_ordered = dedup_ord
            self.allowed_senders = set(dedup_ord)
        elif was_sequence:
            dedup_seq: list[str] = []
            for item in raw_allowed_iter:
                n_item = normalize_sender(item)
                if n_item and n_item not in dedup_seq:
                    dedup_seq.append(n_item)
            self.admin_senders_ordered = dedup_seq
        else:
            self.admin_senders_ordered = sorted(
                self.allowed_senders, key=lambda x: (0 if x.startswith("+") else 1, x)
            )

    @property
    def sqlite_db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def browser_profile_dir(self) -> Path:
        return self.state_dir / "browser_profile"

    @property
    def browser_profiles_root(self) -> Path:
        return self.state_dir / "browser_profiles"

    @property
    def canonical_admin_sender(self) -> str:
        """Primary canonical sender key unifying all admin aliases (Phone + Apple ID Email)."""
        if self.admin_senders_ordered:
            return self.admin_senders_ordered[0]
        return ""

    def resolve_active_provider(self) -> tuple[str, str, str, bool]:
        """Resolve (provider_name, api_key, base_url, is_deepseek_native)."""
        if self.provider_mode == "deepseek":
            if self.deepseek_api_key:
                return ("deepseek", self.deepseek_api_key, self.deepseek_base_url, True)
            if self.openai_api_key:
                return ("openai", self.openai_api_key, self.openai_base_url, False)
            return ("unconfigured", "", self.deepseek_base_url, False)

        if self.openai_api_key:
            return ("openai", self.openai_api_key, self.openai_base_url, False)
        if self.deepseek_api_key:
            ds_compat_url = self.deepseek_base_url.rstrip("/")
            if not ds_compat_url.endswith("/v1"):
                ds_compat_url = f"{ds_compat_url}/v1"
            return ("openai", self.deepseek_api_key, ds_compat_url, False)
        return ("unconfigured", "", self.openai_base_url, False)

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AgentConfig":
        """Load configuration from .env file and process environment variables."""
        loaded_path: Path | None = None
        merged: dict[str, str] = {}
        if environ is not None:
            merged.update(environ)
            if env_file is not None:
                p = Path(env_file).expanduser()
                if p.is_file():
                    loaded_path = p
                    file_vars = _parse_dotenv_text(p.read_text(encoding="utf-8"))
                    for k, v in file_vars.items():
                        merged.setdefault(k, v)
        else:
            for candidate in _resolve_dotenv_candidates(env_file):
                try:
                    if candidate.is_file():
                        file_vars = _parse_dotenv_text(candidate.read_text(encoding="utf-8"))
                        for k, v in file_vars.items():
                            merged.setdefault(k, v)
                            os.environ.setdefault(k, v)
                        if loaded_path is None:
                            loaded_path = candidate
                except OSError:
                    continue
            if env_file is None:
                merged.update(os.environ)
            else:
                for k, v in os.environ.items():
                    merged.setdefault(k, v)

        raw_senders = merged.get("ALLOWED_SENDERS", "")
        ordered_senders: list[str] = []
        for item in re.split(r"[,;\n]+", raw_senders):
            norm_s = normalize_sender(item)
            if norm_s and norm_s not in ordered_senders:
                ordered_senders.append(norm_s)
        senders = set(ordered_senders)

        raw_invites = merged.get("INVITE_CODES", "")
        invite_list = [
            item.strip().upper()
            for item in re.split(r"[,;\n]+", raw_invites)
            if item.strip()
        ]

        raw_sensitive = merged.get("BROWSER_SENSITIVE_JSON", "{}").strip() or "{}"
        try:
            sensitive_data = json.loads(raw_sensitive)
            if not isinstance(sensitive_data, dict):
                sensitive_data = {}
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in BROWSER_SENSITIVE_JSON; defaulting to empty dict.")
            sensitive_data = {}

        raw_ds_key = merged.get("DEEPSEEK_API_KEY", "")
        raw_oa_key = merged.get("OPENAI_API_KEY", "")

        return cls(
            provider_mode=merged.get("PROVIDER_MODE", "deepseek"),
            deepseek_api_key=raw_ds_key,
            deepseek_base_url=merged.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
            or "https://api.deepseek.com",
            deepseek_model=merged.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
            or "deepseek-flash",
            deepseek_reasoning_effort=merged.get("DEEPSEEK_REASONING_EFFORT", "high"),
            enable_deepseek_web_tools=_parse_bool(
                merged.get("ENABLE_DEEPSEEK_WEB_TOOLS"), True
            ),
            openai_api_key=raw_oa_key,
            openai_base_url=merged.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
            or "https://api.openai.com/v1",
            openai_model=merged.get("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
            embedding_model=merged.get("EMBEDDING_MODEL", "").strip(),
            embedding_api_key=merged.get("EMBEDDING_API_KEY", ""),
            embedding_base_url=merged.get("EMBEDDING_BASE_URL", "").strip(),
            allowed_senders=senders,
            admin_senders_ordered=ordered_senders,
            allow_self_chat=_parse_bool(merged.get("ALLOW_SELF_CHAT"), False),
            self_chat_prefix=merged.get("SELF_CHAT_PREFIX", "/ai").strip() or "/ai",
            self_chat_reply_to=merged.get("SELF_CHAT_REPLY_TO", "").strip(),
            enable_invite_codes=_parse_bool(merged.get("ENABLE_INVITE_CODES"), False),
            invite_codes=invite_list,
            default_member_daily_msg_limit=_parse_int(
                merged.get("MEMBER_DAILY_MSG_LIMIT"), 50
            ),
            default_member_daily_browser_limit=_parse_int(
                merged.get("MEMBER_DAILY_BROWSER_LIMIT"), 10
            ),
            enable_instant_ack=_parse_bool(merged.get("ENABLE_INSTANT_ACK"), True),
            fast_ack_delay_sec=_parse_float(merged.get("FAST_ACK_DELAY_SEC"), 1.5),
            heartbeat_interval_sec=_parse_float(
                (merged.get("HYPERTEXT_HEARTBEAT_INTERVAL_SEC") or "").strip()
                or merged.get("HEARTBEAT_INTERVAL_SEC"),
                60.0,
            ),
            max_heartbeat_per_turn=max(
                0,
                _parse_int(
                    (merged.get("HYPERTEXT_MAX_HEARTBEAT_PER_TURN") or "").strip()
                    or merged.get("MAX_HEARTBEAT_PER_TURN"),
                    1,
                ),
            ),
            split_standalone_links=_parse_bool(merged.get("SPLIT_STANDALONE_LINKS"), True),
            state_dir=Path(
                merged.get("HYPERTEXT_STATE_DIR")
                or merged.get("STATE_DIR")
                or merged.get("INSTINCT_STATE_DIR")
                or "~/.hypertext"
            ).expanduser(),
            chat_db_path=Path(
                merged.get("CHAT_DB_PATH", "~/Library/Messages/chat.db")
            ).expanduser(),
            poll_interval_sec=_parse_float(merged.get("POLL_INTERVAL_SEC"), 1.5),
            cron_interval_sec=_parse_float(merged.get("CRON_INTERVAL_SEC"), 15.0),
            max_history_messages=_parse_int(merged.get("MAX_HISTORY_MESSAGES"), 40),
            max_tool_rounds=_parse_int(merged.get("MAX_TOOL_ROUNDS"), 12),
            human_gate_timeout_sec=_parse_float(merged.get("HUMAN_GATE_TIMEOUT_SEC"), 300.0),
            browser_headless=_parse_bool(merged.get("BROWSER_HEADLESS"), False),
            browser_max_steps=_parse_int(merged.get("BROWSER_MAX_STEPS"), 25),
            browser_max_concurrency=_parse_int(merged.get("BROWSER_MAX_CONCURRENCY"), 3),
            browser_sensitive_data=sensitive_data,
            chromium_executable_path=merged.get("CHROMIUM_EXECUTABLE_PATH", "").strip(),
            loaded_env_file=loaded_path,
            deepseek_key_status=(
                "empty"
                if not raw_ds_key.strip()
                else ("placeholder" if _is_placeholder_key(raw_ds_key) else "configured")
            ),
            openai_key_status=(
                "empty"
                if not raw_oa_key.strip()
                else ("placeholder" if _is_placeholder_key(raw_oa_key) else "configured")
            ),
        )
