"""Rate-limit-aware alert delivery queue. Transport is injected, so no Telegram imports here."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Awaitable, Callable, Optional, Union

from alerts import Alert, Button, alert_buttons, format_alert, format_forward

log = logging.getLogger(__name__)
Sender = Callable[[str, Optional[list[list[Button]]]], Awaitable[None]]
# (target chat, text, buttons) -> numeric chat id the message landed in
ForwardSender = Callable[[Union[str, int], str, Optional[list[list[Button]]]], Awaitable[Optional[int]]]


class Notifier:
    def __init__(self, runtime, pace: float = 0.5, max_queue: int = 500, sleep=asyncio.sleep,
                 forward_pace: float = 3.0):
        self.runtime = runtime
        self.pace = pace
        self._sleep = sleep
        self._q: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._send: Optional[Sender] = None
        self._fwd: Optional[ForwardSender] = None
        self._fq: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self.forward_pace = forward_pace
        self.sent = 0
        self.failed = 0
        self.forwarded = 0
        self.forward_failed = 0
        self.last_forward_error: Optional[str] = None
        self.on_forward_chat: Callable[[int], Awaitable[None]] = _noop_id

    def bind(self, send: Sender) -> None:
        self._send = send

    def bind_forward(self, send: ForwardSender) -> None:
        self._fwd = send

    async def enqueue(self, item: Union[Alert, str]) -> None:
        if self._q.full():
            try:
                self._q.get_nowait()
                log.warning("Alert queue full; dropped oldest item")
            except asyncio.QueueEmpty:
                pass
        self._q.put_nowait(item)

    async def notice(self, text: str) -> None:
        await self.enqueue(text)

    async def stop(self) -> None:
        await self._q.put(None)
        await self._fq.put(None)

    async def run(self) -> None:
        while True:
            item = await self._q.get()
            if item is None:
                return
            try:
                await self._deliver(item)
            except Exception:
                self.failed += 1
                log.exception("Failed to deliver alert")
            await self._sleep(self.pace)

    async def _deliver(self, item: Union[Alert, str]) -> None:
        if isinstance(item, str):
            await self._send_retry(item, None)
            return
        chunks = format_alert(item)
        rows = alert_buttons(item, item.sender_id in self.runtime.watch_ids)
        for i, chunk in enumerate(chunks):
            await self._send_retry(chunk, rows if i == len(chunks) - 1 else None)
        self.sent += 1
        await self._queue_forward(item)

    async def _queue_forward(self, alert: Alert) -> None:
        """Hand a CA-bearing alert to the Ferg Bot group queue (separate, so DMs are never delayed by it)."""
        rt = self.runtime
        if self._fwd is None or not rt.forward_enabled or not rt.forward_target:
            return
        if not alert.detection.solana or alert.is_test:      # tests never post to the group
            return
        if self._fq.full():
            try:
                self._fq.get_nowait()
                log.warning("Forward queue full; dropped oldest item")
            except asyncio.QueueEmpty:
                pass
        self._fq.put_nowait(alert)

    async def run_forward(self) -> None:
        while True:
            alert = await self._fq.get()
            if alert is None:
                return
            try:
                await self._forward(alert)
            except Exception as exc:
                self.forward_failed += 1
                self.last_forward_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.warning("Forward to group failed: %s", type(exc).__name__)
                await self.notice(self._forward_help(exc))
            await self._sleep(self.forward_pace)

    @staticmethod
    def _forward_help(exc: Exception) -> str:
        name = type(exc).__name__
        base = f"⚠️ <b>Could not post to the Ferg Bot group</b> ({name})."
        if name in {"Forbidden", "BadRequest"}:
            return (base + "\nAdd this bot to the group as a member (as an admin if it's a channel), "
                    "then tap 📤 Forward → 🧪 Send test.")
        return base + "\nOpen 📤 Forward in the bot to check the target."

    async def _forward(self, alert: Alert) -> None:
        rt = self.runtime
        target = rt.forward_target
        if not (self._fwd and rt.forward_enabled and target):
            return
        text, rows = format_forward(alert)
        chat_id = await self._forward_send(target, text, rows)
        self.forwarded += 1
        self.last_forward_error = None
        if chat_id is not None and chat_id != rt.forward_chat_id:
            await self.on_forward_chat(chat_id)

    async def _forward_send(self, target, text, rows) -> Optional[int]:
        for attempt in range(1, 6):
            try:
                return await self._fwd(target, text, rows)
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
                    log.warning("Group rate limit; waiting %.0fs", delay)
                    await self._sleep(delay + 1)
                elif type(exc).__name__ in {"TimedOut", "NetworkError"} and attempt < 5:
                    await self._sleep(min(2 ** attempt, 30))
                else:
                    raise
        raise RuntimeError("Forward retries exhausted")

    async def _send_retry(self, text: str, rows) -> None:
        if self._send is None:
            raise RuntimeError("Notifier has no transport bound")
        for attempt in range(1, 6):
            try:
                await self._send(text, rows)
                return
            except Exception as exc:  # classify by attribute/name to stay library-agnostic
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
                    log.warning("Bot API rate limit; waiting %.0fs", delay)
                    await self._sleep(delay + 1)
                elif type(exc).__name__ in {"TimedOut", "NetworkError"} and attempt < 5:
                    await self._sleep(min(2 ** attempt, 30))
                else:
                    log.error("Giving up on a notification: %s", type(exc).__name__)
                    raise
        raise RuntimeError("Notification retries exhausted")


async def _noop_id(_: int) -> None:
    return None
