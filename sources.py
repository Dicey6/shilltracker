"""Parse the chat references you paste into the bot (channels, groups, forward target). Pure functions, no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union

_NAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", re.IGNORECASE)
_INVITE = re.compile(r"^(?:\+|joinchat/)([A-Za-z0-9_-]{8,})$")
_PRIVATE_LINK = re.compile(r"^c/(\d{5,})(?:/\d+)*$")
_NAME_ONLY = re.compile(rf"^({_NAME})(?:/\d+)*$")


@dataclass(frozen=True)
class ChatRef:
    kind: str                    # "username" | "invite" | "id"
    value: Union[str, int]

    @property
    def is_invite(self) -> bool:
        return self.kind == "invite"


def parse_chat_ref(raw: str) -> Optional[ChatRef]:
    """Understands @name, name, t.me/name, t.me/name/123, t.me/+HASH, t.me/joinchat/HASH,
    t.me/c/1234567890/5 (private message link) and a negative numeric chat ID like -1001234567890."""
    s = (raw or "").strip()
    if not s:
        return None
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if re.fullmatch(r"-\d{5,}", s):
        return ChatRef("id", int(s))
    if s.startswith("tg://"):
        return None
    path = _PREFIX.sub("", s) if _PREFIX.match(s) else s
    if path.startswith("@"):
        path = path[1:]
    m = _INVITE.match(path)
    if m:
        return ChatRef("invite", m.group(1))
    m = _PRIVATE_LINK.match(path)
    if m:
        return ChatRef("id", int(f"-100{m.group(1)}"))
    if path.startswith("s/"):
        path = path[2:]
    m = _NAME_ONLY.match(path)
    if m:
        return ChatRef("username", m.group(1))
    return None


def forward_target(raw: str) -> Optional[Union[str, int]]:
    """Value usable as `chat_id` for the Bot API. Invite links can't be used by a bot, so they return None."""
    ref = parse_chat_ref(raw)
    if ref is None or ref.is_invite:
        return None
    return f"@{ref.value}" if ref.kind == "username" else int(ref.value)
