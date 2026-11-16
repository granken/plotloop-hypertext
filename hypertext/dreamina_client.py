"""Explicit-Only Admin Dreamina Canvas (`即梦画布` / `dreamina-canvas` CLI) Integration (v0.6.2)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .config import (
    AgentConfig,
    _parse_dotenv_text,
    _resolve_dotenv_candidates,
    normalize_sender,
)
from .formatter import format_for_imessage, is_sensitive_attachment_path
from .workbuddy_client import (
    SubprocessRunnerCallable,
    _default_subprocess_runner,
    _is_executable_file,
)

if TYPE_CHECKING:
    from .store_sqlite import StateStore

logger = logging.getLogger("hypertext.dreamina_client")

MIN_DREAMINA_TIMEOUT_SEC = 600.0
MAX_DREAMINA_DELIVERABLE_BYTES = 25 * 1024 * 1024
DEFAULT_CANVAS_TITLE = "HyperText 即梦创作室"

_PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)

_IMAGE_UPLOAD_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".heic",
    ".heif",
    ".tiff",
}

_URL_STRIP_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DELEGATION_VERBS_PAT = (
    r"(?:问一下|问下|问问|请问|问|让|请|叫|找|喊|麻烦|呼叫|唤醒|转给|交给|发给|委托|调用|用)"
)

_DREAMINA_LEAD_PAT = r"(?:即梦(?:\s*画布)?(?![A-Za-z0-9_])|(?i:dreamina(?:-canvas)?|jimeng|jm)(?![A-Za-z0-9_]))"
_DREAMINA_WORD_PAT = (
    r"(?:即梦(?:\s*画布)?(?![A-Za-z0-9_])|(?<![A-Za-z0-9_./-])(?i:dreamina(?:-canvas)?|jimeng)(?![A-Za-z0-9_]))"
)

_DREAMINA_LEAD_RE = re.compile(
    rf"^[/／@]\s*{_DREAMINA_LEAD_PAT}(?:$|\s+|[:：,，!！?？.。、~～\-]|(?=[\u4e00-\u9fff]))"
)
_DREAMINA_WORD_RE = re.compile(_DREAMINA_WORD_PAT)
_JM_DELEGATION_RE = re.compile(
    rf"{_DELEGATION_VERBS_PAT}\s*(?<![A-Za-z0-9_./-])(?i:jm)(?![A-Za-z0-9_])"
)

_DREAMINA_RESET_RE = re.compile(
    rf"^(?:"
    rf"[/／@]\s*{_DREAMINA_LEAD_PAT}\s*(?:[:：]\s*)?(?:reset|clear|重置|清空|新建|新建画布|重置画布|清空画布|重置会话|清空会话)"
    rf"|{_DREAMINA_WORD_PAT}\s*(?:[:：]\s*)?(?:reset|clear)"
    rf"|(?:新建|重置|清空)\s*{_DREAMINA_WORD_PAT}\s*(?:的)?(?:画布|会话|上下文|项目)?"
    rf")$",
    re.IGNORECASE,
)

_STRIP_DREAMINA_LEAD_RE = re.compile(rf"^[/／@]\s*{_DREAMINA_LEAD_PAT}[\s:：,，、\-]*")
_STRIP_DREAMINA_DELEGATION_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*{_DELEGATION_VERBS_PAT}\s*"
    rf"(?:即梦(?:\s*画布)?(?![A-Za-z0-9_])|(?<![A-Za-z0-9_./-])(?i:dreamina(?:-canvas)?|jimeng|jm)(?![A-Za-z0-9_]))"
    rf"(?:\s*(?:帮我|给我|画一张|画一幅|画个|生成一张|生成一段|生成一个|生成|做一张|做一段|制作))?"
    rf"[\s:：,，、]*"
)
_STRIP_DREAMINA_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_DREAMINA_WORD_PAT}(?:\s*[:：,，!！、~～]+|\s+|$)"
)

_VIDEO_INTENT_RE = re.compile(
    r"(?:视频|动画|动起来|短片|成片|镜头|\bt2v\b|\bm2v\b|seedance|vidu|happyhorse|\d+\s*(?:秒|s)\s*视频)",
    re.IGNORECASE,
)
_MUSIC_INTENT_RE = re.compile(
    r"(?:背景音乐|纯音乐|音乐|伴奏|作曲|编曲|\bbgm\b|seed_music)",
    re.IGNORECASE,
)
_TTS_INTENT_RE = re.compile(
    r"(?:配音|朗读|旁白|口播|念一下|转语音|\btts\b)",
    re.IGNORECASE,
)

_PREV_NODE_VIDEO_REF_RE = re.compile(
    r"(?:刚才那张|上一张|刚才的图|把这张|这幅图|这张图|动起来|变成视频|转成视频|做成视频)"
)
_PREV_NODE_IMAGE_EDIT_RE = re.compile(
    r"(?:把刚才那张|上一张图|上一张|改一下刚才|在此基础上|把刚才的图|接着刚才)"
)

_EXPLICIT_RATIO_RE = re.compile(r"\b(16:9|9:16|3:4|4:3|21:9|1:1)\b")
_DURATION_SEC_RE = re.compile(r"(\d{1,3})\s*(?:秒|s\b)", re.IGNORECASE)
_WANT_4K_RE = re.compile(r"\b4k\b", re.IGNORECASE)
_WANT_1080P_RE = re.compile(r"\b1080p\b", re.IGNORECASE)

_AUTH_SETUP_HINT = (
    "本机即梦画布（dreamina-canvas）尚未登录或授权已过期，请在 Mac mini 终端运行一次 "
    "dreamina-canvas auth login 完成登录，无需重启即可直接在手机上调用。"
)
_CLI_MISSING_HINT = (
    "未在本机检测到即梦画布命令行（dreamina-canvas），请先安装 dreamina-canvas "
    "或在 .env 中设置 DREAMINA_CLI_PATH，无需重启即可直接调用。"
)

IMAGE_MODEL_CHAIN: tuple[tuple[str, str], ...] = (
    ("seedream_5.5", "2K"),
    ("seedream_5.0_pro", "2K"),
    ("high_aes_general_v50_flash", "1.5K"),
)
VIDEO_MODEL_CHAIN: tuple[tuple[str, str], ...] = (
    ("seedance_2.5", "720p"),
    ("seedance_2.5_flash", "720p"),
    ("seedance_pro_fast", "720p"),
    ("happyhorse_1.1", "720P"),
)


def _clean_inbound(text: str) -> str:
    return (text or "").replace("\u2063", "").replace("\ufffc", "").replace("\x00", "").strip()


def _strip_urls(text: str) -> str:
    return _URL_STRIP_RE.sub(" ", text or "")


def _read_live_dotenv(
    config: AgentConfig, allow_repo_dotenv_fallback: bool = True
) -> dict[str, str]:
    paths: list[Path]
    if config.loaded_env_file is not None:
        paths = [Path(config.loaded_env_file).expanduser()]
    elif allow_repo_dotenv_fallback:
        paths = _resolve_dotenv_candidates(None)
    else:
        paths = []

    merged: dict[str, str] = {}
    for cand in paths:
        try:
            if not cand.is_file():
                continue
            parsed = _parse_dotenv_text(cand.read_text(encoding="utf-8"))
            for k, v in parsed.items():
                merged.setdefault(k, v)
        except OSError:
            continue
    return merged


# ============================================================================
# Explicit-Only URL-Safe Trigger & Command Parsers
# ============================================================================


def is_dreamina_reset_command(text: str) -> bool:
    """Return True if text explicitly resets or creates a fresh Dreamina canvas."""
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", _clean_inbound(text)).strip()
    return bool(cleaned and _DREAMINA_RESET_RE.fullmatch(cleaned))


def is_explicit_dreamina_request(text: str) -> bool:
    """Return True ONLY when user explicitly addresses `即梦` / `dreamina` / `/jm`."""
    cleaned = _clean_inbound(text)
    if not cleaned:
        return False
    if is_dreamina_reset_command(cleaned) or _DREAMINA_LEAD_RE.search(cleaned):
        return True
    no_url = _strip_urls(cleaned)
    return bool(_DREAMINA_WORD_RE.search(no_url) or _JM_DELEGATION_RE.search(no_url))


def extract_dreamina_query(text: str) -> str:
    """Strip leading `/即梦`, `/dreamina`, `/jm`, `让即梦` wrappers while keeping full prompt."""
    cleaned = _clean_inbound(text)
    if not cleaned:
        return ""
    for pat in (
        _STRIP_DREAMINA_LEAD_RE,
        _STRIP_DREAMINA_DELEGATION_RE,
        _STRIP_DREAMINA_VOCATIVE_RE,
    ):
        updated, count = pat.subn("", cleaned, count=1)
        if count > 0:
            return updated.strip()
    return cleaned


# ============================================================================
# Binary Discovery & Domestic China Direct-Network Environment
# ============================================================================


def discover_dreamina_cli(
    config: AgentConfig,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    static_candidates: Sequence[str | Path] | None = None,
    allow_repo_dotenv_fallback: bool = True,
) -> str:
    """Locate `dreamina-canvas` binary across `.env`, PATH, `~/.local/bin`, and Homebrew."""
    live_vars = _read_live_dotenv(config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback)
    live_p = (live_vars.get("DREAMINA_CLI_PATH") or "").strip()
    mem_p = (getattr(config, "dreamina_cli_path", "") or "").strip()
    for raw_c in ((live_p, mem_p) if config.loaded_env_file is not None else (mem_p, live_p)):
        if raw_c:
            exp = os.path.abspath(os.path.expanduser(raw_c))
            if _is_executable_file(exp):
                return exp

    found = which_fn("dreamina-canvas")
    if found and _is_executable_file(found):
        return os.path.abspath(os.path.expanduser(found))

    fallbacks: Sequence[str | Path] = (
        static_candidates
        if static_candidates is not None
        else (
            "~/.local/bin/dreamina-canvas",
            "/opt/homebrew/bin/dreamina-canvas",
            "/usr/local/bin/dreamina-canvas",
        )
    )
    for fb in fallbacks:
        exp_fb = os.path.abspath(os.path.expanduser(str(fb)))
        if _is_executable_file(exp_fb):
            return exp_fb
    return ""


def build_dreamina_child_env(
    cli_path: str,
    config: AgentConfig,
    *,
    base_env: Mapping[str, str] | None = None,
    allow_repo_dotenv_fallback: bool = True,
) -> dict[str, str]:
    """Build domestic direct-network child env (stripping foreign proxies unless `DREAMINA_HTTP_PROXY` set)."""
    env = dict(base_env if base_env is not None else os.environ)
    if cli_path:
        cli_dir = os.path.dirname(os.path.abspath(os.path.expanduser(cli_path)))
        cur_path = env.get("PATH", "")
        parts = [p for p in cur_path.split(os.pathsep) if p] if cur_path else []
        if not parts or parts[0] != cli_dir:
            if cli_dir in parts:
                parts.remove(cli_dir)
            parts.insert(0, cli_dir)
            env["PATH"] = os.pathsep.join(parts)

    # Strip all HTTP/SOCKS proxy vars by default so domestic `--region cn` traffic connects directly!
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)

    live_vars = _read_live_dotenv(config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback)
    live_px = (live_vars.get("DREAMINA_HTTP_PROXY") or "").strip()
    mem_px = (getattr(config, "dreamina_http_proxy", "") or "").strip()
    explicit_px = (
        (live_px or mem_px)
        if config.loaded_env_file is not None
        else (mem_px or live_px)
    )
    if explicit_px:
        env["http_proxy"] = explicit_px
        env["https_proxy"] = explicit_px
        env["HTTP_PROXY"] = explicit_px
        env["HTTPS_PROXY"] = explicit_px

    return env


# ============================================================================
# Envelope & Creative Intent Helpers
# ============================================================================


def parse_dreamina_envelope(stdout_str: str, stderr_str: str = "") -> tuple[bool, dict[str, Any], str]:
    """Parse `dreamina-canvas --format json` envelope -> `(ok, data_dict, error_message)`."""
    for blob in (stdout_str, stderr_str):
        raw = (blob or "").strip()
        if not raw:
            continue
        candidates = [raw] + [
            ln.strip() for ln in reversed(raw.splitlines()) if ln.strip().startswith("{")
        ]
        for cand in candidates:
            try:
                obj = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ok_flag = bool(obj.get("ok") is True)
            data_val = obj.get("data")
            data_dict = data_val if isinstance(data_val, dict) else {}
            err_val = obj.get("error")
            err_msg = ""
            if isinstance(err_val, dict):
                code = str(err_val.get("code") or "").strip()
                msg = str(err_val.get("message") or "").strip()
                err_msg = f"{code}: {msg}" if (code and msg) else (msg or code)
            elif isinstance(err_val, str):
                err_msg = err_val.strip()
            return (ok_flag, data_dict, err_msg)

    fallback_err = ((stderr_str or stdout_str or "").strip().splitlines() or [""])[-1][:180]
    return (False, {}, fallback_err)


def _inject_ref_tokens_into_prompt(prompt: str, refs: Sequence[str]) -> str:
    if not refs:
        return prompt
    missing_tokens = [f"{{{{{r}}}}}" for r in refs if f"{{{{{r}}}}}" not in prompt]
    if not missing_tokens:
        return prompt
    prefix = " ".join(missing_tokens)
    return f"{prefix} {prompt}".strip()


def _infer_media_kind(prompt: str, explicit_media_type: str = "") -> tuple[str, str]:
    """Return `(media_type, audio_submode)` where `media_type` in `image|video|audio`."""
    exp = (explicit_media_type or "").strip().lower()
    if exp in ("video",):
        return ("video", "")
    if exp in ("music",):
        return ("audio", "music")
    if exp in ("tts",):
        return ("audio", "tts")
    if exp == "audio":
        if _TTS_INTENT_RE.search(prompt) and not _MUSIC_INTENT_RE.search(prompt):
            return ("audio", "tts")
        return ("audio", "music")
    if exp == "image":
        return ("image", "")

    if _VIDEO_INTENT_RE.search(prompt):
        return ("video", "")
    if _MUSIC_INTENT_RE.search(prompt):
        return ("audio", "music")
    if _TTS_INTENT_RE.search(prompt):
        return ("audio", "tts")
    return ("image", "")


def _infer_image_ratio(prompt: str, explicit_ratio: str = "") -> str:
    if explicit_ratio and explicit_ratio.strip():
        return explicit_ratio.strip()
    m = _EXPLICIT_RATIO_RE.search(prompt)
    if m:
        return m.group(1)
    low = prompt.lower()
    if any(k in low for k in ("海报", "竖图", "小红书", "写真")):
        return "3:4"
    if any(k in low for k in ("壁纸", "竖屏", "手机桌面")):
        return "9:16"
    if any(k in low for k in ("横图", "封面", "横版", "ppt", "16比9")):
        return "16:9"
    return "1:1"


def _infer_video_ratio(prompt: str, explicit_ratio: str = "") -> str:
    if explicit_ratio and explicit_ratio.strip():
        return explicit_ratio.strip()
    m = _EXPLICIT_RATIO_RE.search(prompt)
    if m:
        return m.group(1)
    if any(k in prompt for k in ("竖屏", "竖版", "抖音", "手机全屏")):
        return "9:16"
    return "16:9"


def _infer_duration(prompt: str, explicit_duration: int | float | None, *, min_s: int, max_s: int, default_s: int) -> int:
    if explicit_duration is not None and float(explicit_duration) > 0:
        return max(min_s, min(max_s, int(round(float(explicit_duration)))))
    m = _DURATION_SEC_RE.search(prompt)
    if m:
        with contextlib.suppress(ValueError):
            return max(min_s, min(max_s, int(m.group(1))))
    return default_s


def _is_retryable_model_or_vip_error(err_text: str) -> bool:
    low = (err_text or "").lower()
    if not low:
        return True
    if any(
        fatal in low
        for fatal in ("auth", "login", "unauthorized", "not logged", "insufficient credit", "积分不足")
    ):
        return False
    return True


def _is_missing_project_error(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(
        tok in low
        for tok in (
            "project not found",
            "canvas not found",
            "unknown project",
            "invalid project",
            "project_not_found",
        )
    )


def _extract_resource_ids_from_node_data(data: Mapping[str, Any]) -> list[str]:
    rids: list[str] = []
    seen: set[str] = set()

    def _collect_from_list(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            cand = ""
            if isinstance(item, str):
                cand = item.strip()
            elif isinstance(item, dict):
                cand = str(
                    item.get("resourceId")
                    or item.get("id")
                    or item.get("selectedResourceId")
                    or ""
                ).strip()
            if cand and cand not in seen:
                seen.add(cand)
                rids.append(cand)

    sub = data.get("submission")
    if isinstance(sub, dict):
        _collect_from_list(sub.get("resources"))
        sel = str(sub.get("selectedResourceId") or "").strip()
        if sel and sel not in seen:
            seen.add(sel)
            rids.append(sel)

    node = data.get("node")
    if isinstance(node, dict):
        _collect_from_list(node.get("resources"))
        sel_n = str(node.get("selectedResourceId") or "").strip()
        if sel_n and sel_n not in seen:
            seen.add(sel_n)
            rids.append(sel_n)

    _collect_from_list(data.get("resources"))
    return rids


def _extract_voice_names(data: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    models_map = data.get("models")
    if isinstance(models_map, dict):
        for model_entry in models_map.values():
            if not isinstance(model_entry, dict):
                continue
            items = model_entry.get("items")
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        vn = str(it.get("voiceName") or "").strip()
                        if vn and vn not in seen:
                            seen.add(vn)
                            names.append(vn)

    for top_key in ("items", "voices"):
        arr = data.get(top_key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    vn = str(it.get("voiceName") or "").strip()
                    if vn and vn not in seen:
                        seen.add(vn)
                        names.append(vn)
    return names


@dataclasses.dataclass
class DreaminaResult:
    """Structured execution result from `DreaminaClient.ask`."""

    ok: bool
    exit_code: int
    media_type: str
    mode: str
    model_used: str
    project_id: str
    web_url: str
    node_id: str
    resource_ids: list[str]
    deliverable_paths: list[Path]
    reply: str
    formatted_reply: str
    error: str = ""


class DreaminaClient:
    """Owner-only async client for `dreamina-canvas` with 4-layer canvas state & domestic direct network."""

    PROJECT_ID_META_PREFIX = "dreamina_project_id:"
    WEB_URL_META_PREFIX = "dreamina_web_url:"
    LAST_NODE_META_PREFIX = "dreamina_last_node_id:"
    LAST_RES_META_PREFIX = "dreamina_last_resource_id:"

    def __init__(
        self,
        config: AgentConfig,
        *,
        runner: SubprocessRunnerCallable | None = None,
        cli_path_override: str | None = None,
        sips_bin: str = "/usr/bin/sips",
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._cli_path_override = cli_path_override
        self._sips_bin = sips_bin
        self._allow_repo_dotenv_fallback = allow_repo_dotenv_fallback
        self._timeout_override = timeout_override
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def resolve_cli_path(self) -> str:
        if self._cli_path_override is not None:
            return self._cli_path_override.strip()
        return discover_dreamina_cli(
            self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
        )

    def check_readiness(self) -> dict[str, Any]:
        cli_path = self.resolve_cli_path()
        t_val = max(
            float(getattr(self.config, "dreamina_timeout_sec", MIN_DREAMINA_TIMEOUT_SEC)),
            MIN_DREAMINA_TIMEOUT_SEC,
        )
        return {
            "cli_found": bool(cli_path),
            "timeout_sec": int(t_val) if t_val.is_integer() else t_val,
        }

    @staticmethod
    def _norm_key(prefix: str, sender: str) -> str:
        return f"{prefix}{normalize_sender(sender) or 'owner'}"

    def get_canvas_state(self, store: "StateStore | None", sender: str = "") -> dict[str, str]:
        if store is None:
            return {
                "project_id": "",
                "web_url": "",
                "last_node_id": "",
                "last_resource_id": "",
            }
        return {
            "project_id": (store.get_meta(self._norm_key(self.PROJECT_ID_META_PREFIX, sender)) or "").strip(),
            "web_url": (store.get_meta(self._norm_key(self.WEB_URL_META_PREFIX, sender)) or "").strip(),
            "last_node_id": (store.get_meta(self._norm_key(self.LAST_NODE_META_PREFIX, sender)) or "").strip(),
            "last_resource_id": (store.get_meta(self._norm_key(self.LAST_RES_META_PREFIX, sender)) or "").strip(),
        }

    def reset_canvas(self, store: "StateStore", sender: str = "") -> None:
        """Clear all 4 Dreamina canvas persistence keys for `sender`."""
        for prefix in (
            self.PROJECT_ID_META_PREFIX,
            self.WEB_URL_META_PREFIX,
            self.LAST_NODE_META_PREFIX,
            self.LAST_RES_META_PREFIX,
        ):
            store.set_meta(self._norm_key(prefix, sender), "")

    async def _run_cli(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout_sec: float,
    ) -> tuple[int, str, str]:
        if self._runner is not None:
            return await asyncio.wait_for(
                self._runner(cmd, cwd, env, timeout_sec), timeout=timeout_sec
            )
        return await _default_subprocess_runner(cmd, cwd, env, timeout_sec)

    async def _ensure_authenticated(
        self, cli_path: str, cwd: Path, env: dict[str, str], timeout_sec: float
    ) -> bool:
        rc, out_s, err_s = await self._run_cli(
            [cli_path, "auth", "status", "--format", "json", "--non-interactive"],
            cwd,
            env,
            min(timeout_sec, 30.0),
        )
        ok_flag, data, _ = parse_dreamina_envelope(out_s, err_s)
        if rc == 0 and ok_flag and data.get("loggedIn") is not False:
            return True

        # Attempt one non-interactive `auth refresh`
        rc_ref, out_ref, err_ref = await self._run_cli(
            [cli_path, "auth", "refresh", "--format", "json", "--non-interactive"],
            cwd,
            env,
            min(timeout_sec, 30.0),
        )
        ok_ref, data_ref, _ = parse_dreamina_envelope(out_ref, err_ref)
        if rc_ref == 0 and ok_ref and data_ref.get("loggedIn") is True:
            return True

        rc2, out2, err2 = await self._run_cli(
            [cli_path, "auth", "status", "--format", "json", "--non-interactive"],
            cwd,
            env,
            min(timeout_sec, 30.0),
        )
        ok2, data2, _ = parse_dreamina_envelope(out2, err2)
        return bool(rc2 == 0 and ok2 and data2.get("loggedIn") is True)

    async def _create_canvas_project(
        self,
        cli_path: str,
        cwd: Path,
        env: dict[str, str],
        timeout_sec: float,
        store: "StateStore | None",
        sender: str,
    ) -> tuple[str, str, str]:
        rc, out_s, err_s = await self._run_cli(
            [
                cli_path,
                "canvas",
                "create",
                DEFAULT_CANVAS_TITLE,
                "--format",
                "json",
                "--non-interactive",
            ],
            cwd,
            env,
            min(timeout_sec, 60.0),
        )
        ok_flag, data, err_msg = parse_dreamina_envelope(out_s, err_s)
        if rc != 0 or not ok_flag:
            return ("", "", err_msg or "创建即梦画布失败")
        proj = data.get("project") if isinstance(data.get("project"), dict) else data
        project_id = str(proj.get("projectId") or "").strip()
        web_url = str(proj.get("webUrl") or "").strip()
        if store is not None and project_id:
            store.set_meta(self._norm_key(self.PROJECT_ID_META_PREFIX, sender), project_id)
            store.set_meta(self._norm_key(self.WEB_URL_META_PREFIX, sender), web_url)
            store.set_meta(self._norm_key(self.LAST_NODE_META_PREFIX, sender), "")
            store.set_meta(self._norm_key(self.LAST_RES_META_PREFIX, sender), "")
        return (project_id, web_url, "")

    def _prepare_uploadable_images(
        self, attachment_paths: Sequence[str | Path] | None, downloads_dir: Path
    ) -> tuple[list[Path], list[Path]]:
        """Filter existing image attachments, converting `.heic`/`.heif` via `sips` when available."""
        ready: list[Path] = []
        temp_created: list[Path] = []
        for raw_p in attachment_paths or []:
            if not raw_p:
                continue
            p = Path(raw_p).expanduser()
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            ext = p.suffix.lower()
            if ext not in _IMAGE_UPLOAD_EXTS:
                continue
            if ext in (".heic", ".heif") and sys.platform == "darwin" and os.path.isfile(self._sips_bin):
                try:
                    fd, tmp_name = tempfile.mkstemp(suffix=".jpg", dir=str(downloads_dir))
                    os.close(fd)
                    tmp_jpg = Path(tmp_name)
                    subprocess.run(
                        [self._sips_bin, "-s", "format", "jpeg", str(p), "--out", str(tmp_jpg)],
                        check=True,
                        capture_output=True,
                        timeout=20,
                    )
                    if tmp_jpg.is_file() and tmp_jpg.stat().st_size > 0:
                        ready.append(tmp_jpg)
                        temp_created.append(tmp_jpg)
                        continue
                    tmp_jpg.unlink(missing_ok=True)
                except Exception:
                    logger.debug("HEIC conversion via sips skipped for %s", p.name, exc_info=True)
            ready.append(p)
        return (ready, temp_created)

    async def _resolve_tts_voice_name(
        self, cli_path: str, prompt: str, cwd: Path, env: dict[str, str], timeout_sec: float
    ) -> str:
        try:
            rc, out_s, err_s = await self._run_cli(
                [
                    cli_path,
                    "voice",
                    "list",
                    "--offset",
                    "0",
                    "--count",
                    "50",
                    "--format",
                    "json",
                    "--non-interactive",
                ],
                cwd,
                env,
                min(timeout_sec, 30.0),
            )
            ok_flag, data, _ = parse_dreamina_envelope(out_s, err_s)
            if rc != 0 or not ok_flag:
                return ""
            voices = _extract_voice_names(data)
            if not voices:
                return ""
            for v in voices:
                if v in prompt:
                    return v
            return voices[0]
        except Exception:
            logger.debug("Dreamina voice list lookup skipped", exc_info=True)
            return ""

    async def ask(
        self,
        prompt: str,
        *,
        sender: str = "",
        store: "StateStore | None" = None,
        attachment_paths: Sequence[str | Path] | None = None,
        media_type: str = "",
        ratio: str = "",
        duration_sec: int | float | None = None,
        _timeout_override: float | None = None,
    ) -> DreaminaResult:
        """Execute an owner-only Dreamina Canvas generation task (`image` / `video` / `audio`)."""
        clean_prompt = (prompt or "").strip()
        if not clean_prompt and attachment_paths:
            clean_prompt = "保持主体清晰，电影感光影细节优化"

        if not clean_prompt:
            err_txt = (
                "请直接描述你想用即梦生成的画面、视频或音乐，"
                "例如：/即梦 一只戴宇航头盔的橘猫在月球喝咖啡，电影感海报"
            )
            return DreaminaResult(
                ok=False,
                exit_code=400,
                media_type="image",
                mode="t2i",
                model_used="",
                project_id="",
                web_url="",
                node_id="",
                resource_ids=[],
                deliverable_paths=[],
                reply="",
                formatted_reply=err_txt,
                error=err_txt,
            )

        cli_path = self.resolve_cli_path()
        if not cli_path:
            return DreaminaResult(
                ok=False,
                exit_code=127,
                media_type="image",
                mode="t2i",
                model_used="",
                project_id="",
                web_url="",
                node_id="",
                resource_ids=[],
                deliverable_paths=[],
                reply="",
                formatted_reply=_CLI_MISSING_HINT,
                error=_CLI_MISSING_HINT,
            )

        downloads_dir = self.config.state_dir / "dreamina_downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        env = build_dreamina_child_env(
            cli_path,
            self.config,
            allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback,
        )

        override = _timeout_override if _timeout_override is not None else self._timeout_override
        eff_timeout = (
            max(0.05, float(override))
            if override is not None
            else max(
                float(getattr(self.config, "dreamina_timeout_sec", MIN_DREAMINA_TIMEOUT_SEC)),
                MIN_DREAMINA_TIMEOUT_SEC,
            )
        )

        lock_key = normalize_sender(sender) or "owner"
        temp_imgs: list[Path] = []
        async with self._get_lock(lock_key):
            try:
                authed = await self._ensure_authenticated(
                    cli_path, downloads_dir, env, eff_timeout
                )
                if not authed:
                    return DreaminaResult(
                        ok=False,
                        exit_code=401,
                        media_type="image",
                        mode="t2i",
                        model_used="",
                        project_id="",
                        web_url="",
                        node_id="",
                        resource_ids=[],
                        deliverable_paths=[],
                        reply="",
                        formatted_reply=_AUTH_SETUP_HINT,
                        error=_AUTH_SETUP_HINT,
                    )

                state = self.get_canvas_state(store, sender)
                project_id = state["project_id"]
                web_url = state["web_url"]
                last_node_id = state["last_node_id"]

                if not project_id:
                    project_id, web_url, create_err = await self._create_canvas_project(
                        cli_path, downloads_dir, env, eff_timeout, store, sender
                    )
                    last_node_id = ""
                    if not project_id:
                        err_txt = f"初始化即梦画布失败：{create_err}"
                        return DreaminaResult(
                            ok=False,
                            exit_code=1,
                            media_type="image",
                            mode="t2i",
                            model_used="",
                            project_id="",
                            web_url="",
                            node_id="",
                            resource_ids=[],
                            deliverable_paths=[],
                            reply="",
                            formatted_reply=err_txt,
                            error=err_txt,
                        )

                # Upload any inbound iMessage image attachments (`resource upload`)
                upload_files, temp_imgs = self._prepare_uploadable_images(
                    attachment_paths, downloads_dir
                )
                uploaded_rids: list[str] = []
                for img_file in upload_files:
                    up_cmd = [
                        cli_path,
                        "resource",
                        "upload",
                        "--file",
                        str(img_file),
                        "--project-id",
                        project_id,
                        "--format",
                        "json",
                        "--non-interactive",
                    ]
                    rc_u, out_u, err_u = await self._run_cli(
                        up_cmd, downloads_dir, env, min(eff_timeout, 120.0)
                    )
                    ok_u, data_u, msg_u = parse_dreamina_envelope(out_u, err_u)
                    if (rc_u != 0 or not ok_u) and _is_missing_project_error(msg_u):
                        project_id, web_url, _ = await self._create_canvas_project(
                            cli_path, downloads_dir, env, eff_timeout, store, sender
                        )
                        last_node_id = ""
                        if project_id:
                            up_cmd[up_cmd.index("--project-id") + 1] = project_id
                            rc_u, out_u, err_u = await self._run_cli(
                                up_cmd, downloads_dir, env, min(eff_timeout, 120.0)
                            )
                            ok_u, data_u, _ = parse_dreamina_envelope(out_u, err_u)

                    if rc_u == 0 and ok_u:
                        res_obj = (
                            data_u.get("resource")
                            if isinstance(data_u.get("resource"), dict)
                            else data_u
                        )
                        rid = str(
                            res_obj.get("resourceId") or data_u.get("resourceId") or ""
                        ).strip()
                        if rid:
                            uploaded_rids.append(rid)

                resolved_media, audio_submode = _infer_media_kind(clean_prompt, media_type)

                # Build node create attempts according to authoritative Dreamina CLI contract
                rc_node = 1
                ok_node = False
                node_data: dict[str, Any] = {}
                last_err_msg = ""
                chosen_mode = ""
                chosen_model = ""
                chosen_ratio = ""
                chosen_dur = 0

                if resolved_media == "video":
                    if uploaded_rids:
                        refs = [f"res:{r}" for r in uploaded_rids]
                    elif last_node_id and _PREV_NODE_VIDEO_REF_RE.search(clean_prompt):
                        refs = [f"node:{last_node_id}"]
                    else:
                        refs = []

                    # Authoritative Video Mode Rule:
                    # - 0 refs -> `t2v`
                    # - 2 refs -> `first_last_frame` (`--ref start --ref end`)
                    # - 1 ref (or >2 refs) -> `m2v` (`--ref ...`)
                    if len(refs) == 0:
                        chosen_mode = "t2v"
                    elif len(refs) == 2:
                        chosen_mode = "first_last_frame"
                    else:
                        chosen_mode = "m2v"

                    final_prompt = _inject_ref_tokens_into_prompt(clean_prompt, refs)
                    chosen_ratio = _infer_video_ratio(clean_prompt, ratio)
                    chosen_dur = _infer_duration(
                        clean_prompt, duration_sec, min_s=4, max_s=10, default_s=5
                    )

                    video_candidates: list[tuple[str, str]] = []
                    if _WANT_1080P_RE.search(clean_prompt):
                        video_candidates.append(("seedance_2.5", "1080p"))
                    for m_pair in VIDEO_MODEL_CHAIN:
                        if m_pair not in video_candidates:
                            video_candidates.append(m_pair)

                    ref_flags: list[str] = []
                    for r_item in refs:
                        ref_flags.extend(["--ref", r_item])

                    for idx, (cand_model, cand_res) in enumerate(video_candidates):
                        chosen_model = cand_model
                        v_cmd = [
                            cli_path,
                            "node",
                            "create",
                            "video",
                            "--project-id",
                            project_id,
                            "--mode",
                            chosen_mode,
                            "--model",
                            cand_model,
                            "--ratio",
                            chosen_ratio,
                            "--resolution",
                            cand_res,
                            "--duration",
                            str(chosen_dur),
                            *ref_flags,
                            "--prompt",
                            final_prompt,
                            "--run",
                            "--wait",
                            "--timeout",
                            "9m",
                            "--yes",
                            "--non-interactive",
                            "--format",
                            "json",
                        ]
                        rc_node, out_n, err_n = await self._run_cli(
                            v_cmd, downloads_dir, env, eff_timeout
                        )
                        ok_node, node_data, last_err_msg = parse_dreamina_envelope(out_n, err_n)
                        if (rc_node != 0 or not ok_node) and _is_missing_project_error(last_err_msg):
                            project_id, web_url, _ = await self._create_canvas_project(
                                cli_path, downloads_dir, env, eff_timeout, store, sender
                            )
                            if project_id:
                                v_cmd[v_cmd.index("--project-id") + 1] = project_id
                                rc_node, out_n, err_n = await self._run_cli(
                                    v_cmd, downloads_dir, env, eff_timeout
                                )
                                ok_node, node_data, last_err_msg = parse_dreamina_envelope(
                                    out_n, err_n
                                )
                        if rc_node == 0 and ok_node:
                            break
                        if idx + 1 < len(video_candidates) and _is_retryable_model_or_vip_error(
                            last_err_msg
                        ):
                            continue
                        break

                elif resolved_media == "audio":
                    chosen_mode = audio_submode or "music"
                    if chosen_mode == "music":
                        chosen_model = "seed_music_1.0"
                        chosen_dur = _infer_duration(
                            clean_prompt, duration_sec, min_s=30, max_s=180, default_s=30
                        )
                        # Strict `music` rule: pass `--model seed_music_1.0 --duration <30..180>`, NEVER `--voice-name`
                        a_cmd = [
                            cli_path,
                            "node",
                            "create",
                            "audio",
                            "--project-id",
                            project_id,
                            "--mode",
                            "music",
                            "--model",
                            chosen_model,
                            "--duration",
                            str(chosen_dur),
                            "--prompt",
                            clean_prompt,
                            "--run",
                            "--wait",
                            "--timeout",
                            "9m",
                            "--yes",
                            "--non-interactive",
                            "--format",
                            "json",
                        ]
                    else:
                        # Strict `tts` rule: NEVER pass `--model` or `--duration`; resolve `--voice-name` via `voice list`
                        chosen_model = ""
                        voice_name = await self._resolve_tts_voice_name(
                            cli_path, clean_prompt, downloads_dir, env, eff_timeout
                        )
                        a_cmd = [
                            cli_path,
                            "node",
                            "create",
                            "audio",
                            "--project-id",
                            project_id,
                            "--mode",
                            "tts",
                            *(["--voice-name", voice_name] if voice_name else []),
                            "--prompt",
                            clean_prompt,
                            "--run",
                            "--wait",
                            "--timeout",
                            "9m",
                            "--yes",
                            "--non-interactive",
                            "--format",
                            "json",
                        ]

                    rc_node, out_n, err_n = await self._run_cli(
                        a_cmd, downloads_dir, env, eff_timeout
                    )
                    ok_node, node_data, last_err_msg = parse_dreamina_envelope(out_n, err_n)
                    if (rc_node != 0 or not ok_node) and _is_missing_project_error(last_err_msg):
                        project_id, web_url, _ = await self._create_canvas_project(
                            cli_path, downloads_dir, env, eff_timeout, store, sender
                        )
                        if project_id:
                            a_cmd[a_cmd.index("--project-id") + 1] = project_id
                            rc_node, out_n, err_n = await self._run_cli(
                                a_cmd, downloads_dir, env, eff_timeout
                            )
                            ok_node, node_data, last_err_msg = parse_dreamina_envelope(
                                out_n, err_n
                            )

                else:
                    # Image (`t2i` vs `i2i`)
                    if uploaded_rids:
                        refs = [f"res:{r}" for r in uploaded_rids]
                    elif last_node_id and _PREV_NODE_IMAGE_EDIT_RE.search(clean_prompt):
                        refs = [f"node:{last_node_id}"]
                    else:
                        refs = []

                    chosen_mode = "i2i" if refs else "t2i"
                    final_prompt = _inject_ref_tokens_into_prompt(clean_prompt, refs)
                    chosen_ratio = _infer_image_ratio(clean_prompt, ratio)

                    image_candidates: list[tuple[str, str]] = []
                    if _WANT_4K_RE.search(clean_prompt):
                        image_candidates.append(("seedream_5.0_pro", "4K"))
                    for m_pair in IMAGE_MODEL_CHAIN:
                        if m_pair not in image_candidates:
                            image_candidates.append(m_pair)

                    ref_flags = []
                    for r_item in refs:
                        ref_flags.extend(["--ref", r_item])

                    for idx, (cand_model, cand_res) in enumerate(image_candidates):
                        chosen_model = cand_model
                        i_cmd = [
                            cli_path,
                            "node",
                            "create",
                            "image",
                            "--project-id",
                            project_id,
                            "--mode",
                            chosen_mode,
                            "--model",
                            cand_model,
                            "--ratio",
                            chosen_ratio,
                            "--resolution",
                            cand_res,
                            *ref_flags,
                            "--prompt",
                            final_prompt,
                            "--run",
                            "--wait",
                            "--timeout",
                            "9m",
                            "--yes",
                            "--non-interactive",
                            "--format",
                            "json",
                        ]
                        rc_node, out_n, err_n = await self._run_cli(
                            i_cmd, downloads_dir, env, eff_timeout
                        )
                        ok_node, node_data, last_err_msg = parse_dreamina_envelope(out_n, err_n)
                        if (rc_node != 0 or not ok_node) and _is_missing_project_error(last_err_msg):
                            project_id, web_url, _ = await self._create_canvas_project(
                                cli_path, downloads_dir, env, eff_timeout, store, sender
                            )
                            if project_id:
                                i_cmd[i_cmd.index("--project-id") + 1] = project_id
                                rc_node, out_n, err_n = await self._run_cli(
                                    i_cmd, downloads_dir, env, eff_timeout
                                )
                                ok_node, node_data, last_err_msg = parse_dreamina_envelope(
                                    out_n, err_n
                                )
                        if rc_node == 0 and ok_node:
                            break
                        if idx + 1 < len(image_candidates) and _is_retryable_model_or_vip_error(
                            last_err_msg
                        ):
                            continue
                        break

                if rc_node != 0 or not ok_node:
                    clean_err = last_err_msg or f"exit={rc_node}"
                    home_str = os.path.expanduser("~").replace("\\", "/")
                    if home_str and home_str != "~":
                        clean_err = clean_err.replace(home_str, "~")
                    err_txt = f"即梦画布生成未完成（{clean_err[:140]}），请稍后调整描述再试。"
                    if web_url:
                        err_txt = f"{err_txt}\n\n{web_url}"
                    return DreaminaResult(
                        ok=False,
                        exit_code=rc_node or 1,
                        media_type=resolved_media,
                        mode=chosen_mode,
                        model_used=chosen_model,
                        project_id=project_id,
                        web_url=web_url,
                        node_id="",
                        resource_ids=[],
                        deliverable_paths=[],
                        reply="",
                        formatted_reply=err_txt,
                        error=clean_err,
                    )

                node_obj = (
                    node_data.get("node")
                    if isinstance(node_data.get("node"), dict)
                    else node_data
                )
                node_id = str(node_obj.get("nodeId") or node_data.get("nodeId") or "").strip()
                gen_rids = _extract_resource_ids_from_node_data(node_data)
                live_web_url = str(
                    node_data.get("webUrl")
                    or (
                        node_data.get("project", {}).get("webUrl")
                        if isinstance(node_data.get("project"), dict)
                        else ""
                    )
                    or web_url
                ).strip()
                if live_web_url:
                    web_url = live_web_url

                if store is not None:
                    if project_id:
                        store.set_meta(
                            self._norm_key(self.PROJECT_ID_META_PREFIX, sender), project_id
                        )
                    if web_url:
                        store.set_meta(self._norm_key(self.WEB_URL_META_PREFIX, sender), web_url)
                    if node_id:
                        store.set_meta(
                            self._norm_key(self.LAST_NODE_META_PREFIX, sender), node_id
                        )
                    if gen_rids:
                        store.set_meta(
                            self._norm_key(self.LAST_RES_META_PREFIX, sender), gen_rids[0]
                        )

                # Download generated media into `state_dir / "dreamina_downloads"` (`<= 25 MB`).
                # CRITICAL: If `resource download` fails (e.g. video/audio stream restriction),
                # DO NOT fail the task — deliver `web_url` link card gracefully!
                deliverables: list[Path] = []
                for rid in gen_rids:
                    try:
                        rc_dl, out_dl, err_dl = await self._run_cli(
                            [
                                cli_path,
                                "resource",
                                "download",
                                rid,
                                "-o",
                                str(downloads_dir),
                                "--project-id",
                                project_id,
                                "--format",
                                "json",
                                "--non-interactive",
                            ],
                            downloads_dir,
                            env,
                            min(eff_timeout, 120.0),
                        )
                        ok_dl, data_dl, _ = parse_dreamina_envelope(out_dl, err_dl)
                        if rc_dl == 0 and ok_dl:
                            raw_dl_path = str(
                                data_dl.get("path")
                                or data_dl.get("filePath")
                                or (
                                    data_dl.get("file", {}).get("path")
                                    if isinstance(data_dl.get("file"), dict)
                                    else ""
                                )
                                or ""
                            ).strip()
                            if raw_dl_path:
                                dl_p = Path(raw_dl_path).expanduser()
                                if (
                                    dl_p.is_file()
                                    and 0 < dl_p.stat().st_size <= MAX_DREAMINA_DELIVERABLE_BYTES
                                    and not is_sensitive_attachment_path(dl_p)[0]
                                ):
                                    deliverables.append(dl_p)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.info(
                            "Dreamina resource download skipped for rid=%s; delivering webUrl card.",
                            rid,
                        )

                if resolved_media == "video":
                    summary = (
                        f"即梦视频已生成完毕（{chosen_dur}秒 · {chosen_ratio}），成片已直接发你，也可点开画布在线查看："
                        if deliverables
                        else f"即梦视频已生成完毕（{chosen_dur}秒 · {chosen_ratio}），点开下方即梦画布链接即可直接播放成片："
                    )
                elif resolved_media == "audio":
                    label_a = "即梦音乐" if chosen_mode == "music" else "即梦配音"
                    summary = (
                        f"{label_a}已生成完毕，音频文件已发你，也可在画布在线试听："
                        if deliverables
                        else f"{label_a}已生成完毕，点开下方即梦画布链接即可直接试听："
                    )
                else:
                    summary = (
                        f"即梦画面已生成完毕（比例 {chosen_ratio}），原图已直接发你，也可在画布继续精修："
                        if deliverables
                        else f"即梦画面已生成完毕（比例 {chosen_ratio}），点开下方即梦画布链接即可查看原图："
                    )

                raw_reply = f"{summary}\n\n{web_url}" if web_url else summary.rstrip("：:") + "。"
                formatted = format_for_imessage(raw_reply)
                return DreaminaResult(
                    ok=True,
                    exit_code=0,
                    media_type=resolved_media,
                    mode=chosen_mode,
                    model_used=chosen_model,
                    project_id=project_id,
                    web_url=web_url,
                    node_id=node_id,
                    resource_ids=gen_rids,
                    deliverable_paths=deliverables,
                    reply=raw_reply,
                    formatted_reply=formatted,
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                err_txt = "等待本机即梦画布生成超时了，视频或高分大图耗时较长，稍后可打开画布查看或重试。"
                return DreaminaResult(
                    ok=False,
                    exit_code=124,
                    media_type="image",
                    mode="",
                    model_used="",
                    project_id="",
                    web_url="",
                    node_id="",
                    resource_ids=[],
                    deliverable_paths=[],
                    reply="",
                    formatted_reply=err_txt,
                    error=err_txt,
                )
            finally:
                for tmp_f in temp_imgs:
                    with contextlib.suppress(OSError):
                        tmp_f.unlink(missing_ok=True)
