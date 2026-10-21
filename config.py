"""Environment-driven configuration. Nothing secret is ever hardcoded."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _int(env: Mapping[str, str], name: str, default: Optional[int] = None, minimum: int = 0) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"Missing required environment variable: {name}")
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    authorized_user_id: int
    database_url: str
    telethon_session: Optional[str] = None
    session_encryption_key: Optional[str] = None
    port: Optional[int] = None
    log_level: str = "INFO"
    retention_days: int = 14
    max_reconnect_attempts: int = 8
    reconnect_base_delay: float = 5.0
    login_timeout: int = 300
    forward_chat: Optional[str] = None      # default Ferg Bot group; changeable from the bot

    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        vals = [self.bot_token, self.api_hash, self.telethon_session, self.session_encryption_key]
        return [v for v in vals if v and len(v) >= 6]


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    port_raw = (env.get("PORT") or "").strip()
    return Config(
        bot_token=_required(env, "TELEGRAM_BOT_TOKEN"),
        api_id=_int(env, "TELEGRAM_API_ID", minimum=1),
        api_hash=_required(env, "TELEGRAM_API_HASH"),
        authorized_user_id=_int(env, "AUTHORIZED_USER_ID", minimum=1),
        database_url=(env.get("DATABASE_URL") or "sqlite:///shill_monitor.db").strip(),
        telethon_session=(env.get("TELETHON_SESSION") or "").strip() or None,
        session_encryption_key=(env.get("SESSION_ENCRYPTION_KEY") or "").strip() or None,
        port=int(port_raw) if port_raw.isdigit() else None,
        log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
        retention_days=_int(env, "RETENTION_DAYS", default=14, minimum=1),
        max_reconnect_attempts=_int(env, "MAX_RECONNECT_ATTEMPTS", default=8, minimum=1),
        reconnect_base_delay=float(_int(env, "RECONNECT_BASE_DELAY", default=5, minimum=0)),
        forward_chat=(env.get("FORWARD_CHAT") or "@fergbotalgogc").strip() or None,
    )
