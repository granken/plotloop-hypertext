"""Consistent redaction for local logs and diagnostic tails."""

from __future__ import annotations

import logging
import os
import re

_MASKS = (
    (r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]"),
    (r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^/\s@]+:[^/\s@]*@", r"\1[redacted]@"),
    (
        r"(?i)([?&](?:key|token|api[_-]?key|access[_-]?token|password|secret|signature|sig)=)[^&#\s]+",
        r"\1[redacted]",
    ),
    (
        r"""(?i)(\b(?:[A-Z0-9_-]+_)?(?:api[-_ ]?key|access[-_ ]?token|secret|password)\b['"]?\s*[:=]\s*['"]?)[^'"\s,;]+""",
        r"\1[redacted]",
    ),
    (r"\bsk-[A-Za-z0-9_.-]{4,}", "sk-***"),
    (r"\bxn_[A-Za-z0-9_.-]{4,}", "xn_***"),
    (r"(?i)\b(?:HT|INST)-[A-Z0-9]{4,16}\b", "[redacted-invite]"),
    (r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[redacted-email]"),
    (
        r"(?<!\w)(?:\+\d{7,15}|1[3-9]\d{9}|(?:\(\d{3}\)|\d{3})[ -]?\d{3}[ -]?\d{4})(?!\w)",
        "[redacted-phone]",
    ),
)


def redact_log_text(text: str) -> str:
    for pattern, replacement in _MASKS:
        text = re.sub(pattern, replacement, text)
    # Also catch configured secrets when an upstream error omits the field name.
    secrets = {
        value for key, value in os.environ.items()
        if len(value) >= 8 and key.endswith(("_API_KEY", "_ACCESS_TOKEN", "_RELAY_KEY", "_SECRET"))
    }
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


class RedactedLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))
