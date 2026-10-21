"""Alert model + Telegram-HTML rendering. No Telegram library imports (easy to test)."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from detectors import Detection

TG_LIMIT = 4096
HEAD_BUDGET = 3900          # leave headroom under Telegram's 4096 limit
CHUNK_BUDGET = 3800
Button = tuple[str, str]    # (label, callback_data | https URL)


@dataclass
class Alert:
    detection: Detection
    chat_id: int
    chat_title: str
    message_id: int
    text: str
    timestamp: datetime
    sender_id: Optional[int] = None
    sender_username: Optional[str] = None
    sender_name: Optional[str] = None
    link: Optional[str] = None
    edited: bool = False
    forwarded: bool = False
    is_test: bool = False


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def short_addr(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}"


def dex_url(addr: str) -> str:
    return f"https://dexscreener.com/solana/{addr}"


def solscan_url(addr: str) -> str:
    return f"https://solscan.io/account/{addr}"


def birdeye_url(addr: str) -> str:
    return f"https://birdeye.so/token/{addr}?chain=solana"


def ca_link(addr: str) -> str:
    """A tappable contract address (opens DexScreener). Base58 contains no HTML-special characters."""
    return f'<a href="{dex_url(addr)}">{esc(addr)}</a>'


COPY_PREFIX = "cp="          # button data meaning "copy this text to the clipboard"


def address_rows(addresses, limit: int = 4) -> list[list[Button]]:
    """One row per address: copy + DexScreener + Solscan + Birdeye. Used by alerts AND the group forward."""
    addrs = list(addresses)[:limit]
    rows: list[list[Button]] = []
    for a in addrs:
        multi = len(addrs) > 1
        rows.append([(f"📋 {short_addr(a)}" if multi else "📋 Copy", f"{COPY_PREFIX}{a}"),
                     ("Dex" if multi else "DexScreener", dex_url(a)),
                     ("Scan" if multi else "Solscan", solscan_url(a)),
                     ("Bird" if multi else "Birdeye", birdeye_url(a))])
    return rows


def build_link(chat_id: int, message_id: int, chat_username: Optional[str]) -> Optional[str]:
    """Only return links Telegram can actually resolve; never fabricate one."""
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    if str(chat_id).startswith("-100"):          # channel / supergroup (members only if private)
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None


def split_raw(text: str, limit: int) -> list[str]:
    """Split raw text into chunks whose HTML-*escaped* length is <= limit."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        j = min(n, i + limit)
        while len(esc(text[i:j])) > limit:
            j = i + max(1, (j - i) * 3 // 4)
        if j < n:
            half = i + (j - i) // 2
            cut = max(text.rfind("\n", i, j), -1)
            if cut <= half:
                cut = text.rfind(" ", i, j)
            if cut > half:
                j = cut + 1
        out.append(text[i:j])
        i = j
    return out or [""]


def split_escaped(text: str, limit: int) -> list[str]:
    return [esc(c) for c in split_raw(text, limit)]


def _sender_line(a: Alert) -> str:
    if a.sender_username:
        return f"@{esc(a.sender_username)}"
    return esc(a.sender_name) if a.sender_name else "<i>unknown (no username)</i>"


def format_alert(a: Alert) -> list[str]:
    ts = a.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    flags = [f for f, on in (("✏️ edited", a.edited), ("↪️ forwarded", a.forwarded), ("🧪 test", a.is_test)) if on]
    lines = [
        "🚨 <b>NEW ALPHA SIGNAL</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"<b>Detection</b>  {esc(a.detection.label)}",
        f"<b>Sender</b>  {_sender_line(a)}",
        f"<b>User ID</b>  <code>{a.sender_id}</code>" if a.sender_id is not None else "<b>User ID</b>  <i>n/a</i>",
        f"<b>Source</b>  {esc(a.chat_title)} · <code>{a.chat_id}</code>",
        f"<b>Time</b>  {ts}",
    ]
    if flags:
        lines.append(" · ".join(flags))
    if a.detection.solana:
        lines += ["", f"◎ <b>Detected address{'es' if len(a.detection.solana) > 1 else ''}</b>"]
        lines += [ca_link(x) for x in a.detection.solana]
    if a.detection.robinhood:
        lines += ["", "🅡 <b>Robinhood-related match</b>"]
        lines += [f"• <code>{esc(x)}</code>" for x in a.detection.robinhood]
    if a.link:
        lines += ["", f"🔗 <b>Source message</b>\n{esc(a.link)}"]
    head = "\n".join(lines) + "\n\n<b>Original message</b>\n"

    avail = max(300, HEAD_BUDGET - len(head) - 40)
    first_raw = split_raw(a.text, avail)[0]
    rest_text = a.text[len(first_raw):]
    pieces = [esc(first_raw)] + (split_escaped(rest_text, CHUNK_BUDGET) if rest_text else [])
    chunks = [head + f"<blockquote expandable>{pieces[0]}</blockquote>"]
    for idx, piece in enumerate(pieces[1:], start=2):
        chunks.append(f"<i>Original message (part {idx}/{len(pieces)})</i>\n<blockquote expandable>{piece}</blockquote>")
    return chunks


def alert_buttons(a: Alert, already_watched: bool) -> list[list[Button]]:
    rows: list[list[Button]] = []
    if a.link:
        rows.append([("🔗 Open message", a.link)])
    rows += address_rows(a.detection.solana)
    if a.sender_id is not None and not already_watched and not a.is_test:
        rows.append([("👁 Watch this user", f"wa:{a.sender_id}")])
    return rows


def format_forward(a: Alert) -> tuple[str, list[list[Button]]]:
    """Compact post for the Ferg Bot group: source name + clickable CA(s). Nothing else leaves the bot."""
    src = esc(a.chat_title)
    if a.link:
        src = f'<a href="{html.escape(a.link, quote=True)}">{src}</a>'
    n = len(a.detection.solana)
    lines = [f"📡 <b>Source:</b> {src}", "", f"◎ <b>Contract address{'es' if n > 1 else ''}</b>"]
    lines += [ca_link(x) for x in a.detection.solana]
    return "\n".join(lines), address_rows(a.detection.solana)
