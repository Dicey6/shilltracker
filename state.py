"""In-memory cache of settings so the hot message path never touches the database."""
from __future__ import annotations

from detectors import DetectionConfig

DEFAULTS = {
    "paused": "0",
    "mode": "all",              # all | watchlist
    "chat_scope": "all",        # all | selected
    "include_private": "0",
    "include_own": "0",
    "solana_enabled": "1",
    "robinhood_enabled": "1",
    "filter_known": "1",
    "forward_enabled": "1",     # send CA + source name to the Ferg Bot group
    "forward_target": "",       # @username or numeric chat id of that group
    "forward_chat_id": "",      # resolved numeric id, learned on first successful send (loop protection)
}
BOOL_KEYS = {"paused", "include_private", "include_own", "solana_enabled", "robinhood_enabled", "filter_known",
             "forward_enabled"}
CYCLE_KEYS = {"mode": ("all", "watchlist"), "chat_scope": ("all", "selected")}


class RuntimeState:
    def __init__(self, db):
        self.db = db
        self._s = dict(DEFAULTS)
        self.detection = DetectionConfig()
        self.watch_ids: frozenset[int] = frozenset()
        self.enabled_chats: frozenset[int] = frozenset()
        self.keywords: tuple[str, ...] = ()
        self.regexes: tuple[str, ...] = ()
        self.bot_id: int | None = None          # our own bot's user id; its messages are never scanned

    async def ensure_defaults(self, forward_target: str | None = None) -> None:
        if await self.db.get_setting("defaults_seeded") != "1":
            await self.db.add_pattern("keyword", "robinhood")
            await self.db.set_setting("defaults_seeded", "1")
        if forward_target and await self.db.get_setting("forward_target") is None:
            await self.db.set_setting("forward_target", forward_target)

    async def refresh(self) -> None:
        s = {**DEFAULTS, **await self.db.get_all_settings()}
        patterns = await self.db.list_patterns()
        watch = await self.db.list_watch()
        chats = await self.db.list_chat_rows()
        keywords = tuple(p["pattern"] for p in patterns if p["kind"] == "keyword")
        regexes = tuple(p["pattern"] for p in patterns if p["kind"] == "regex")
        self.detection = DetectionConfig(
            solana_enabled=s["solana_enabled"] == "1",
            robinhood_enabled=s["robinhood_enabled"] == "1",
            filter_known_programs=s["filter_known"] == "1",
            keywords=keywords, regexes=regexes)
        self.keywords, self.regexes = keywords, regexes
        self.watch_ids = frozenset(int(w["user_id"]) for w in watch)
        self.enabled_chats = frozenset(int(c["chat_id"]) for c in chats if int(c["enabled"]))
        self._s = s

    def get(self, key: str) -> str:
        return self._s[key]

    def flag(self, key: str) -> bool:
        return self._s[key] == "1"

    @property
    def paused(self) -> bool: return self.flag("paused")
    @property
    def mode(self) -> str: return self._s["mode"]
    @property
    def chat_scope(self) -> str: return self._s["chat_scope"]
    @property
    def include_private(self) -> bool: return self.flag("include_private")
    @property
    def include_own(self) -> bool: return self.flag("include_own")
    @property
    def forward_enabled(self) -> bool: return self.flag("forward_enabled")
    @property
    def forward_target(self) -> str: return self._s["forward_target"]

    @property
    def forward_chat_id(self) -> int | None:
        raw = self._s["forward_chat_id"]
        return int(raw) if raw.lstrip("-").isdigit() else None

    async def set(self, key: str, value: str) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        await self.db.set_setting(key, value)
        await self.refresh()

    async def toggle(self, key: str) -> str:
        if key in BOOL_KEYS:
            new = "0" if self.flag(key) else "1"
        elif key in CYCLE_KEYS:
            a, b = CYCLE_KEYS[key]
            new = b if self._s[key] == a else a
        else:
            raise KeyError(key)
        await self.set(key, new)
        return new
