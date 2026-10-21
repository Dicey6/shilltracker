"""Structured logging with secret redaction (applied to the final formatted line,
so tracebacks are covered too)."""
from __future__ import annotations

import logging
import re
import sys
from typing import Iterable

_PATTERNS = [
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),     # bot tokens (also inside /bot<token>/ URLs)
    re.compile(r"\b1[A-Za-z0-9_=+/-]{180,}"),               # Telethon StringSession
]
_KV = re.compile(r"(?i)\b(api_hash|password|login_code|code)=\S+")


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: Iterable[str] = ()):
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _KV.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    # httpx logs full request URLs, which contain the bot token.
    for noisy in ("httpx", "httpcore", "telethon", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
