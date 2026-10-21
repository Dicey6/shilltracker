"""Turns Telethon message events into alerts. Duck-typed on the event so it is easy to test."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from alerts import Alert, build_link
from detectors import detect
from notifier import Notifier
from state import RuntimeState

log = logging.getLogger(__name__)


def _username(entity: Any) -> Optional[str]:
    name = getattr(entity, "username", None)
    if name:
        return name
    for u in getattr(entity, "usernames", None) or []:
        if getattr(u, "active", True) and getattr(u, "username", None):
            return u.username
    return None


class Scanner:
    def __init__(self, db, runtime: RuntimeState, notifier: Notifier):
        self.db, self.runtime, self.notifier = db, runtime, notifier
        self.seen = 0
        self.matched = 0
        self.alerts = 0
        self.last_alert_at: Optional[datetime] = None

    async def handle_event(self, event: Any, edited: bool = False) -> bool:
        """Return True if an alert was queued."""
        rt = self.runtime
        if rt.paused:
            return False
        msg = event.message
        text = (getattr(msg, "message", None) or "").strip()     # includes media captions
        if not text:
            return False
        chat_id = event.chat_id
        if chat_id is None:
            return False
        if getattr(msg, "out", False) and not rt.include_own:
            return False
        private = bool(event.is_private)
        if private and not rt.include_private:
            return False
        if not private and rt.chat_scope == "selected" and chat_id not in rt.enabled_chats:
            return False
        sender_id = getattr(msg, "sender_id", None)
        # Loop protection: never scan our own bot's messages, or the group we forward into.
        if rt.bot_id is not None and sender_id == rt.bot_id:
            return False
        if rt.forward_chat_id is not None and chat_id == rt.forward_chat_id:
            return False
        if rt.mode == "watchlist" and (sender_id is None or sender_id not in rt.watch_ids):
            return False

        self.seen += 1
        det = detect(text, rt.detection)
        if not det.matched:
            return False
        if not await self.db.claim_message(chat_id, msg.id, det.signature):
            log.debug("Duplicate suppressed chat=%s msg=%s", chat_id, msg.id)
            return False

        self.matched += 1
        sender = chat = None
        try:
            sender = await event.get_sender()
        except Exception as exc:
            log.debug("Could not resolve sender: %s", type(exc).__name__)
        try:
            chat = await event.get_chat()
        except Exception as exc:
            log.debug("Could not resolve chat: %s", type(exc).__name__)

        names = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
        sender_name = " ".join(n for n in names if n) or getattr(sender, "title", None)
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Unknown chat"
        ts = getattr(msg, "date", None) or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        alert = Alert(
            detection=det, chat_id=chat_id, chat_title=title, message_id=msg.id, text=text, timestamp=ts,
            sender_id=sender_id, sender_username=_username(sender) if sender else None, sender_name=sender_name,
            link=build_link(chat_id, msg.id, _username(chat) if chat else None),
            edited=edited, forwarded=getattr(msg, "fwd_from", None) is not None)
        await self.notifier.enqueue(alert)
        self.alerts += 1
        self.last_alert_at = datetime.now(timezone.utc)
        return True
