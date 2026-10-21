"""Shill Monitor control panel: a Telegram bot with a single, in-place-editing dashboard."""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import (BotCommand, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions,
                      Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults,
                          MessageHandler, filters)

from alerts import COPY_PREFIX, Alert, Button, format_forward
from config import Config
from detectors import Detection, validate_regex
from sources import forward_target, parse_chat_ref
from user_client import LoginError, UserClient

log = logging.getLogger(__name__)

BRAND = "◆ <b>SHILL MONITOR</b>"
RULE = "━━━━━━━━━━━━━━━━━━"
PAGE = 8
AWAIT_TTL = 600
MEMPAGE = 8
MAX_ADD_AT_ONCE = 10
SAMPLE_ADDRESS = "86Uzv2fizy66zvjokCnwCRvGX8U3BFY8SRkw18eWZhwX"
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
PHONE_RE = re.compile(r"^\+\d{8,15}$")

COMMANDS = [
    ("start", "Open the dashboard"), ("status", "Scanner status"), ("watchlist", "Watched users & mode"),
    ("adduser", "Add a user to the watchlist"), ("removeuser", "Remove a watched user"),
    ("users", "List monitored users"), ("filters", "Detection filters"), ("chats", "Choose monitored chats"),
    ("addchat", "Add a channel/group to watch"), ("forward", "Ferg Bot group forwarding"),
    ("connect", "Link your Telegram account"), ("disconnect", "Unlink the account"),
    ("pause", "Pause alerts"), ("resume", "Resume alerts"), ("testalert", "Send a sample alert"),
    ("help", "Help"),
]

HELP = (
    "<b>Commands</b>\n"
    "/start – dashboard\n/status – connection &amp; settings\n/connect · /disconnect – account link\n"
    "/watchlist · /users – watched users\n/adduser <code>@name | id</code> · /removeuser <code>@name | id</code>\n"
    "/mode <code>all | watchlist</code> – who is scanned\n/filters – Solana &amp; Robinhood detection\n"
    "/addkeyword <code>word</code> · /removekeyword <code>word</code>\n/addregex <code>pattern</code> – Robinhood regex\n"
    "/chats – pick monitored chats (👥 next to a chat lists its members)\n"
    "/addchat <code>@channel | t.me link | invite link | -100…</code> – add (and join) sources\n"
    "/forward – post every CA + its source to your Ferg Bot group\n"
    "/pause · /resume\n/testalert – send a sample alert\n\n"
    "<b>How it works</b>\nYour linked account listens for new messages. When one contains a valid Solana address "
    "or a Robinhood keyword you configured, you get an alert here. Read-only: nothing is ever traded or signed."
)


def esc(x: object) -> str:
    return html.escape(str(x), quote=False)


def _button(label: str, data: str) -> InlineKeyboardButton:
    if data.startswith(COPY_PREFIX):
        return InlineKeyboardButton(label, copy_text=CopyTextButton(text=data[len(COPY_PREFIX):]))
    if data.startswith("http"):
        return InlineKeyboardButton(label, url=data)
    return InlineKeyboardButton(label, callback_data=data)


def kb(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_button(label, data) for label, data in row] for row in rows])


def screen(title: str, body: str) -> str:
    return f"{BRAND}\n{RULE}\n<b>{title}</b>\n\n{body}"


def onoff(flag: bool) -> str:
    return "🟢 ON" if flag else "⚪ OFF"


def short(s: str, n: int = 26) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(self: "BotApp", update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, chat = update.effective_user, update.effective_chat
        if user is None or user.id != self.config.authorized_user_id:
            log.warning("Rejected unauthorized access from user_id=%s", getattr(user, "id", None))
            await self._deny(update)
            return None
        if chat is not None and chat.type != "private":
            return None  # control panel works only in the private bot chat
        return await fn(self, update, context)
    return wrapper


class BotApp:
    def __init__(self, config: Config, db, runtime, uc: UserClient, scanner, notifier):
        self.config, self.db, self.rt, self.uc, self.scanner, self.notifier = config, db, runtime, uc, scanner, notifier
        self.application: Optional[Application] = None
        self.started_at = time.time()

    # ------------------------------------------------------------------ lifecycle
    def build(self) -> Application:
        app = (Application.builder().token(self.config.bot_token)
               .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
               .build())
        for name, fn in {
            "start": self.cmd_start, "help": self.cmd_help, "connect": self.cmd_connect,
            "disconnect": self.cmd_disconnect, "status": self.cmd_status, "watchlist": self.cmd_watchlist,
            "adduser": self.cmd_adduser, "removeuser": self.cmd_removeuser, "users": self.cmd_users,
            "filters": self.cmd_filters, "chats": self.cmd_chats, "pause": self.cmd_pause,
            "resume": self.cmd_resume, "mode": self.cmd_mode, "addkeyword": self.cmd_addkeyword,
            "removekeyword": self.cmd_removekeyword, "addregex": self.cmd_addregex, "testalert": self.cmd_testalert,
            "addchat": self.cmd_addchat, "forward": self.cmd_forward,
        }.items():
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))
        app.add_error_handler(self.on_error)
        self.application = app
        return app

    async def start(self) -> None:
        app = self.application or self.build()
        await app.initialize()
        self.rt.bot_id = app.bot.id          # the scanner ignores our own bot's messages (prevents alert loops)
        await app.start()
        for call in (lambda: app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS]),
                     lambda: app.bot.set_my_name("Shill Monitor"),
                     lambda: app.bot.set_my_short_description("Private Solana CA & Robinhood alert scanner")):
            try:
                await call()
            except Exception as exc:  # cosmetic; BotFather limits how often names can change
                log.debug("Bot profile update skipped: %s", type(exc).__name__)
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
        log.info("Bot polling started")

    async def stop(self) -> None:
        app = self.application
        if app is None:
            return
        for step in (app.updater.stop, app.stop, app.shutdown):
            try:
                await step()
            except Exception as exc:
                log.debug("Bot shutdown step failed: %s", type(exc).__name__)

    async def send_to_admin(self, text: str, rows: Optional[list[list[Button]]] = None) -> None:
        await self.application.bot.send_message(
            chat_id=self.config.authorized_user_id, text=text, reply_markup=kb(rows) if rows else None)

    async def send_to_group(self, target, text: str, rows: Optional[list[list[Button]]] = None) -> Optional[int]:
        """Post into the Ferg Bot group. Returns the numeric chat id Telegram resolved it to."""
        chat_id = int(target) if str(target).lstrip("-").isdigit() else target
        msg = await self.application.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=kb(rows) if rows else None)
        return msg.chat_id

    # ------------------------------------------------------------------ plumbing
    async def _deny(self, update: Update) -> None:
        try:
            if update.callback_query:
                await update.callback_query.answer("⛔ Private bot.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ This is a private bot.")
        except Exception:
            pass

    async def _render(self, update: Update, text: str, rows: list[list[Button]]) -> None:
        markup = kb(rows)
        q = update.callback_query
        if q is not None:
            try:
                await q.edit_message_text(text, reply_markup=markup)
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                log.debug("edit failed (%s); sending new message", exc)
        await update.effective_message.reply_text(text, reply_markup=markup)

    async def _say(self, update: Update, text: str, rows: Optional[list[list[Button]]] = None) -> None:
        await update.effective_message.reply_text(text, reply_markup=kb(rows) if rows else None)

    def _set_await(self, context, kind: str) -> None:
        context.user_data["await"] = (kind, time.time())

    @staticmethod
    def _clear_await(context) -> None:
        context.user_data.pop("await", None)

    # ------------------------------------------------------------------ screens
    def _status_line(self) -> str:
        s = self.uc.state
        if s == "connected":
            return f"⏸ <b>Paused</b> · {esc(self.uc.account_label)}" if self.rt.paused else \
                f"🟢 <b>Live</b> · {esc(self.uc.account_label)}"
        return {"disconnected": "⚪ <b>Not linked</b>", "connecting": "🟡 <b>Connecting…</b>",
                "reconnecting": "🟡 <b>Reconnecting…</b>", "unauthorized": "🔴 <b>Session expired</b>",
                "failed": "🔴 <b>Connection failed</b>"}.get(s, s)

    def _mode_line(self) -> str:
        return "👥 All users" if self.rt.mode == "all" else f"👁 Watchlist only ({len(self.rt.watch_ids)})"

    def _scope_line(self) -> str:
        return "All chats" if self.rt.chat_scope == "all" else f"{len(self.rt.enabled_chats)} selected"

    def _forward_line(self) -> str:
        if not self.rt.forward_target:
            return "⚪ not set"
        return f"{'🟢' if self.rt.forward_enabled else '⚪'} {esc(self.rt.forward_target)}"

    def _detect_line(self) -> str:
        d = self.rt.detection
        return f"◎ Solana {'✓' if d.solana_enabled else '✗'}  ·  🅡 Robinhood {'✓' if d.robinhood_enabled else '✗'}"

    def home(self):
        body = (f"{self._status_line()}\n\n<b>Mode</b>  {self._mode_line()}\n<b>Chats</b>  {self._scope_line()}\n"
                f"<b>Detect</b>  {self._detect_line()}\n<b>Forward</b>  {self._forward_line()}\n\n<i>Signals sent this session: {self.notifier.sent}</i>")
        pause = ("▶️ Resume", "act:resume") if self.rt.paused else ("⏸ Pause", "act:pause")
        rows = [[("🔗 Connect Account", "nav:connect"), ("📡 Scanner Status", "nav:status")],
                [("👁 Watchlist", "nav:watch"), ("🎯 Detection Filters", "nav:filters")],
                [("💬 Monitored Chats", "chats:0"), pause],
                [("➕ Add Channel/Group", "add:chat"), ("📤 Forward", "nav:forward")],
                [("❔ Help", "nav:help")]]
        return screen("Dashboard", body), rows

    def status(self):
        uptime = int(time.time() - self.started_at)
        h, m = uptime // 3600, uptime % 3600 // 60
        chats = len(self.rt.enabled_chats) if self.rt.chat_scope == "selected" else \
            f"all accessible ({len(self.uc._dialogs[1])} known)"
        last = self.scanner.last_alert_at.strftime("%H:%M UTC") if self.scanner.last_alert_at else "—"
        lines = [
            f"<b>Bot</b>  🟢 running · {h}h {m}m",
            f"<b>Telethon</b>  {self._status_line()}",
            f"<b>Session</b>  {esc(self.uc.session_source or 'none')}",
            f"<b>Database</b>  {'PostgreSQL' if self.db.is_pg else 'SQLite (dev)'}",
            "", f"<b>Monitoring</b>  {chats}", f"<b>Mode</b>  {self._mode_line()}",
            f"<b>Solana CA</b>  {onoff(self.rt.detection.solana_enabled)}   <b>Known-program filter</b>  {onoff(self.rt.detection.filter_known_programs)}",
            f"<b>Robinhood</b>  {onoff(self.rt.detection.robinhood_enabled)} · {len(self.rt.keywords)} keywords · {len(self.rt.regexes)} regex",
            f"<b>Forward</b>  {self._forward_line()} · sent {getattr(self.notifier, 'forwarded', 0)} · "
            f"failed {getattr(self.notifier, 'forward_failed', 0)}",
            "", f"<b>Scanned</b> {self.scanner.seen} · <b>Matched</b> {self.scanner.matched} · "
                f"<b>Alerts</b> {self.scanner.alerts} · <b>Last</b> {last}",
        ]
        if self.uc.last_error:
            lines.append(f"\n⚠️ Last error: <code>{esc(self.uc.last_error)}</code>")
        rows = [[("🔄 Refresh", "nav:status"), ("🔌 Reconnect", "act:reconnect")], [("‹ Back", "nav:home")]]
        return screen("Scanner Status", "\n".join(lines)), rows

    async def watch(self):
        users = await self.db.list_watch()
        if users:
            lines = []
            for u in users:
                who = f"@{esc(u['username'])}" if u["username"] else esc(u["display_name"] or "unknown")
                lines.append(f"• {who} · <code>{u['user_id']}</code>")
            body = "\n".join(lines[:30])
        else:
            body = "<i>No users yet. Add one to use Watchlist mode.</i>"
        body = f"<b>Mode</b>  {self._mode_line()}\n\n{body}"
        rows = [[("➕ Add user", "add:user"),
                 ("🔀 Switch to " + ("Watchlist" if self.rt.mode == "all" else "All users"), "tg:mode:watch")]]
        rows += [[(f"✕ {short('@' + u['username'] if u['username'] else (u['display_name'] or str(u['user_id'])), 22)}",
                   f"ru:{u['user_id']}")] for u in users[:12]]
        rows.append([("‹ Back", "nav:home")])
        return screen("Watchlist", body), rows

    async def filters_screen(self):
        d = self.rt.detection
        kws = ", ".join(f"<code>{esc(k)}</code>" for k in d.keywords) or "<i>none</i>"
        rxs = ", ".join(f"<code>{esc(r)}</code>" for r in d.regexes) or "<i>none</i>"
        body = (f"<b>Mode</b>  {self._mode_line()}\n\n◎ <b>Solana CA</b>  {onoff(d.solana_enabled)}\n"
                f"🛡 <b>Skip known programs</b>  {onoff(d.filter_known_programs)}\n"
                f"🅡 <b>Robinhood</b>  {onoff(d.robinhood_enabled)}\n\n<b>Keywords</b>\n{kws}\n\n<b>Regex</b>\n{rxs}")
        rows: list[list[Button]] = [
            [(f"◎ Solana: {'ON' if d.solana_enabled else 'OFF'}", "tg:solana_enabled:filters"),
             (f"🅡 Robinhood: {'ON' if d.robinhood_enabled else 'OFF'}", "tg:robinhood_enabled:filters")],
            [(f"🛡 Known programs: {'skip' if d.filter_known_programs else 'show'}", "tg:filter_known:filters"),
             ("🔀 Mode", "tg:mode:filters")],
            [("➕ Keyword", "add:keyword"), ("➕ Regex", "add:regex")]]
        patterns = await self.db.list_patterns()
        rm = [(f"✕ {short(p['pattern'], 18)}", f"rp:{p['pid']}") for p in patterns[:12]]
        rows += [rm[i:i + 2] for i in range(0, len(rm), 2)]
        rows.append([("‹ Back", "nav:home")])
        return screen("Detection Filters", body), rows

    async def chats(self, page: int):
        if not self.uc.is_connected:
            return screen("Monitored Chats", "Link your account first to list chats."), \
                [[("🔗 Connect Account", "nav:connect")], [("‹ Back", "nav:home")]]
        dialogs = await self.uc.list_dialogs()
        pages = max(1, (len(dialogs) + PAGE - 1) // PAGE)
        page = max(0, min(page, pages - 1))
        chunk = dialogs[page * PAGE:(page + 1) * PAGE]
        selected = self.rt.chat_scope == "selected"
        hint = ("Tap a chat to include/exclude it · 👥 lists its members." if selected else
                "Scope is <b>All chats</b>. Switch to <b>Selected only</b> to pick specific chats.")
        lines = [f"{'✅' if selected and cid in self.rt.enabled_chats else '▫️'} {esc(short(name, 34))} · <code>{cid}</code>"
                 for cid, name in chunk] or ["<i>No groups or channels found.</i>"]
        body = f"{hint}\n\n" + "\n".join(lines) + f"\n\n<i>Page {page + 1}/{pages} · {len(dialogs)} chats</i>"
        rows = [[(f"{'✅' if selected and cid in self.rt.enabled_chats else '▫️'} {short(name, 24)}", f"ct:{cid}:{page}"),
                 ("👥", f"mb:{cid}:0:{page}")] for cid, name in chunk]
        nav = []
        if page > 0:
            nav.append(("‹ Prev", f"chats:{page - 1}"))
        if page < pages - 1:
            nav.append(("Next ›", f"chats:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("➕ Add channel/group", "add:chat")])
        rows.append([(f"🎯 Scope: {'Selected only' if selected else 'All chats'}", f"tg:chat_scope:chats:{page}"),
                     ("🔄 Reload", f"chats:{page}:r")])
        rows.append([(f"💬 Private chats: {'on' if self.rt.include_private else 'off'}", f"tg:include_private:chats:{page}"),
                     (f"🙋 My own msgs: {'on' if self.rt.include_own else 'off'}", f"tg:include_own:chats:{page}")])
        rows.append([("‹ Back", "nav:home")])
        return screen("Monitored Chats", body), rows

    async def members(self, context, cid: int, mpage: int, cpage: int):
        term = (context.user_data.get("msearch") or {}).get(cid, "")
        title = dict(self.uc._dialogs[1]).get(cid) or str(cid)
        back = [("‹ Chats", f"chats:{cpage}")]
        try:
            people = await self.uc.list_members(cid, term)
        except LookupError as exc:
            return screen("Members", f"<b>{esc(short(title, 40))}</b>\n\n⚠️ {esc(exc)}"), [back]
        pages = max(1, (len(people) + MEMPAGE - 1) // MEMPAGE)
        mpage = max(0, min(mpage, pages - 1))
        chunk = people[mpage * MEMPAGE:(mpage + 1) * MEMPAGE]
        watched = sum(1 for m in people if m.user_id in self.rt.watch_ids)
        body = (f"<b>{esc(short(title, 40))}</b>\n"
                f"Tap people to add/remove them from your watchlist ({watched} of {len(people)} shown are watched).\n"
                f"<i>The watchlist is global. Set Mode to “Watchlist only” (👁 Watchlist) to alert only on these users.</i>")
        if term:
            body += f"\n\n🔎 Search: <code>{esc(term)}</code>"
        if not people:
            body += "\n\n<i>No members found.</i>"
        rows: list[list[Button]] = []
        for m in chunk:
            label = f"{'✅' if m.user_id in self.rt.watch_ids else '▫️'} {'🤖 ' if m.is_bot else ''}{m.name}"
            if m.username:
                label += f" @{m.username}"
            rows.append([(short(label, 40), f"mw:{cid}:{m.user_id}:{mpage}:{cpage}")])
        nav = []
        if mpage > 0:
            nav.append(("‹ Prev", f"mb:{cid}:{mpage - 1}:{cpage}"))
        if mpage < pages - 1:
            nav.append(("Next ›", f"mb:{cid}:{mpage + 1}:{cpage}"))
        if nav:
            rows.append(nav)
        search = [("🔎 Search", f"ms:{cid}:{cpage}")]
        if term:
            search.append(("✕ Clear search", f"mx:{cid}:{cpage}"))
        rows += [search, back]
        return screen("Group Members", body + f"\n\n<i>Page {mpage + 1}/{pages}</i>"), rows

    async def forward_screen(self, note: str = ""):
        rt, n = self.rt, self.notifier
        lines = [f"<b>Posting</b>  {onoff(rt.forward_enabled)}",
                 f"<b>Group</b>  {esc(rt.forward_target) if rt.forward_target else '<i>not set</i>'}"
                 + (f" · <code>{rt.forward_chat_id}</code>" if rt.forward_chat_id else ""),
                 f"<b>Sent</b> {getattr(n, 'forwarded', 0)} · <b>Failed</b> {getattr(n, 'forward_failed', 0)}",
                 "", "Every detected Solana CA (from any monitored source) is posted there with the source name "
                     "and clickable links. Robinhood-only matches and test alerts are not forwarded.",
                 "", "<i>The bot must be a member of that group (admin if it's a channel).</i>"]
        err = getattr(n, "last_forward_error", None)
        if err:
            lines.append(f"\n⚠️ Last error: <code>{esc(err)}</code>")
        if note:
            lines.insert(0, note + "\n")
        rows = [[(f"📤 Forwarding: {'ON' if rt.forward_enabled else 'OFF'}", "tg:forward_enabled:forward"),
                 ("✏️ Change group", "add:forward")],
                [("🧪 Send test", "act:fwdtest")], [("‹ Back", "nav:home")]]
        return screen("Forward to Ferg Bot", "\n".join(lines)), rows

    def connect(self):
        uc = self.uc
        if uc.is_connected:
            body = (f"🟢 Linked as <b>{esc(uc.account_label)}</b>\nSession: {esc(uc.session_source or '—')}\n\n"
                    "Your account is read-only here: the scanner only listens for new messages.")
            rows = [[("🔌 Reconnect", "act:reconnect"), ("⛔ Disconnect", "logout:ask")], [("‹ Back", "nav:home")]]
        elif uc.remote_login_enabled:
            body = ("Link your personal Telegram account so the scanner can read the groups you're in.\n\n"
                    "🔒 Login codes and passwords are used once, never stored or logged, and your messages are deleted "
                    "right after. The session is encrypted before it's saved.\n\n"
                    "<i>Safest alternative: generate the session on your own computer (see README) and set TELETHON_SESSION.</i>")
            rows = [[("📱 Link with phone number", "login:start")]]
            if uc.state in ("unauthorized", "failed"):
                rows.insert(0, [("🔌 Retry connection", "act:reconnect")])
            rows.append([("‹ Back", "nav:home")])
        else:
            body = ("In-bot login is <b>disabled</b> because <code>SESSION_ENCRYPTION_KEY</code> isn't set.\n\n"
                    "Generate a session locally with <code>python scripts/generate_session.py</code> and store it in the "
                    "<code>TELETHON_SESSION</code> secret on Render — or set an encryption key to enable in-bot login.")
            rows = [[("🔌 Retry connection", "act:reconnect")], [("‹ Back", "nav:home")]]
        return screen("Connect Account", f"{self._status_line()}\n\n{body}"), rows

    def prompt(self, title: str, body: str, cancel: str = "cancel"):
        return screen(title, body), [[("✕ Cancel", cancel)]]

    # ------------------------------------------------------------------ commands
    @admin_only
    async def cmd_start(self, update, context):
        self._clear_await(context)
        text, rows = self.home()
        await self._say(update, text, rows)

    @admin_only
    async def cmd_help(self, update, context):
        await self._say(update, screen("Help", HELP), [[("‹ Back", "nav:home")]])

    @admin_only
    async def cmd_status(self, update, context):
        text, rows = self.status()
        await self._say(update, text, rows)

    @admin_only
    async def cmd_connect(self, update, context):
        text, rows = self.connect()
        await self._say(update, text, rows)

    @admin_only
    async def cmd_disconnect(self, update, context):
        await self._say(update, *self._logout_confirm())

    @admin_only
    async def cmd_watchlist(self, update, context):
        text, rows = await self.watch()
        await self._say(update, text, rows)

    @admin_only
    async def cmd_users(self, update, context):
        users = await self.db.list_watch()
        if not users:
            return await self._say(update, screen("Monitored Users", "<i>The watchlist is empty.</i>"),
                                   [[("➕ Add user", "add:user")]])
        lines = [f"• {('@' + esc(u['username'])) if u['username'] else esc(u['display_name'] or 'unknown')} · <code>{u['user_id']}</code>"
                 for u in users]
        await self._say(update, screen(f"Monitored Users ({len(users)})", "\n".join(lines)), [[("👁 Manage", "nav:watch")]])

    @admin_only
    async def cmd_adduser(self, update, context):
        if context.args:
            return await self._add_user(update, " ".join(context.args))
        self._set_await(context, "adduser")
        await self._say(update, *self.prompt("Add User", "Send an <code>@username</code> or a numeric user ID."))

    @admin_only
    async def cmd_removeuser(self, update, context):
        if not context.args:
            text, rows = await self.watch()
            return await self._say(update, text + "\n\nTap ✕ to remove, or use <code>/removeuser @name</code>.", rows)
        ref = " ".join(context.args).strip()
        uid = int(ref) if ref.lstrip("-").isdigit() else None
        if uid is None:
            row = await self.db.find_watch_by_username(ref)
            uid = row["user_id"] if row else None
        if uid is not None and await self.db.remove_watch(uid):
            await self.rt.refresh()
            return await self._say(update, f"✅ Removed <code>{uid}</code> from the watchlist.", [[("👁 Watchlist", "nav:watch")]])
        await self._say(update, "⚠️ That user isn't on the watchlist. See /users.")

    @admin_only
    async def cmd_filters(self, update, context):
        text, rows = await self.filters_screen()
        await self._say(update, text, rows)

    @admin_only
    async def cmd_chats(self, update, context):
        text, rows = await self.chats(0)
        await self._say(update, text, rows)

    @admin_only
    async def cmd_pause(self, update, context):
        await self.rt.set("paused", "1")
        await self._say(update, "⏸ <b>Paused.</b> The account stays connected; no alerts will be sent.",
                        [[("▶️ Resume", "act:resume")]])

    @admin_only
    async def cmd_resume(self, update, context):
        await self.rt.set("paused", "0")
        await self._say(update, "▶️ <b>Resumed.</b> Scanning and alerts are active.", [[("📡 Status", "nav:status")]])

    @admin_only
    async def cmd_mode(self, update, context):
        arg = (context.args[0].lower() if context.args else "")
        if arg not in ("all", "watchlist"):
            return await self._say(update, f"Current mode: {self._mode_line()}\nUsage: <code>/mode all</code> or <code>/mode watchlist</code>")
        await self.rt.set("mode", arg)
        await self._say(update, f"✅ Mode is now {self._mode_line()}.")

    @admin_only
    async def cmd_addkeyword(self, update, context):
        if not context.args:
            self._set_await(context, "addkeyword")
            return await self._say(update, *self.prompt("Add Keyword", "Send the Robinhood keyword (case-insensitive, whole word)."))
        await self._add_keyword(update, " ".join(context.args))

    @admin_only
    async def cmd_removekeyword(self, update, context):
        if not context.args:
            return await self._say(update, "Usage: <code>/removekeyword word</code>")
        kw = " ".join(context.args)
        if await self.db.remove_keyword(kw):
            await self.rt.refresh()
            return await self._say(update, f"✅ Removed keyword <code>{esc(kw)}</code>.", [[("🎯 Filters", "nav:filters")]])
        await self._say(update, "⚠️ No such keyword. Open /filters to see them.")

    @admin_only
    async def cmd_addregex(self, update, context):
        if not context.args:
            self._set_await(context, "addregex")
            return await self._say(update, *self.prompt("Add Regex", "Send a regular expression for a Robinhood identifier, e.g. <code>\\bRH-[A-Z0-9]{4,}\\b</code>"))
        await self._add_regex(update, " ".join(context.args))

    @admin_only
    async def cmd_testalert(self, update, context):
        alert = Alert(detection=Detection(solana=(SAMPLE_ADDRESS,), robinhood=("robinhood",)),
                      chat_id=-1000000000000, chat_title="Shill Monitor test", message_id=1,
                      text=f"Test signal: robinhood CA {SAMPLE_ADDRESS}", timestamp=datetime.now(timezone.utc),
                      sender_id=None, sender_username="test_user", is_test=True)
        await self.notifier.enqueue(alert)

    @admin_only
    async def cmd_addchat(self, update, context):
        if context.args:
            return await self._add_chats(update, context, " ".join(context.args))
        self._set_await(context, "addchat")
        await self._say(update, *self.prompt("Add Channel / Group", self._addchat_help()))

    @admin_only
    async def cmd_forward(self, update, context):
        text, rows = await self.forward_screen()
        await self._say(update, text, rows)

    @staticmethod
    def _addchat_help() -> str:
        return ("Send one or more (space or line separated):\n"
                "• <code>@channelname</code> or <code>t.me/channelname</code>\n"
                "• a private invite link <code>t.me/+AbCd…</code>\n"
                "• a numeric ID like <code>-1001234567890</code>\n\n"
                "If your account isn't in the chat yet I'll ask before joining.")

    async def cmd_unknown(self, update, context):
        user = update.effective_user
        if user is None or user.id != self.config.authorized_user_id:
            return await self._deny(update)
        await self._say(update, "🤔 Unknown command. Try /help or /start.")

    # ------------------------------------------------------------------ mutations
    async def _add_user(self, update, raw: str) -> None:
        raw = raw.strip().replace("https://t.me/", "").replace("t.me/", "")
        try:
            if raw.lstrip("-").isdigit():
                uid = int(raw)
                try:
                    uid, uname, name = await self.uc.resolve_user(uid)
                except LookupError:
                    uname = name = None            # numeric IDs are accepted even if unresolvable
            else:
                m = USERNAME_RE.match(raw)
                if not m:
                    return await self._say(update, "⚠️ That doesn't look like a username or numeric ID.")
                uid, uname, name = await self.uc.resolve_user("@" + m.group(1))
        except LookupError as exc:
            return await self._say(update, f"⚠️ {esc(exc)}")
        new = await self.db.add_watch(uid, uname, name)
        await self.rt.refresh()
        who = f"@{esc(uname)}" if uname else esc(name or uid)
        await self._say(update, f"✅ {'Added' if new else 'Updated'} <b>{who}</b> · <code>{uid}</code>",
                        [[("👁 Watchlist", "nav:watch")]])

    async def _add_chats(self, update, context, raw: str) -> None:
        tokens = raw.split()[:MAX_ADD_AT_ONCE]
        if not tokens:
            return await self._say(update, "⚠️ Nothing to add.")
        lines: list[str] = []
        pending = []
        added: list[int] = []
        for tok in tokens:
            ref = parse_chat_ref(tok)
            if ref is None:
                lines.append(f"⚠️ <code>{esc(short(tok, 40))}</code> – not a channel/group reference")
                continue
            try:
                info = await self.uc.resolve_chat(ref)
            except LookupError as exc:
                lines.append(f"⚠️ <code>{esc(short(tok, 40))}</code> – {esc(exc)}")
                continue
            if info.member:
                await self.db.set_chat_enabled(info.chat_id, info.title, True)
                added.append(info.chat_id)
                lines.append(f"✅ <b>{esc(info.title)}</b> – watching")
            else:
                pending.append(info)
                lines.append(f"➕ <b>{esc(info.title)}</b> – your account isn't in it yet")
        await self.rt.refresh()
        rows: list[list[Button]] = []
        if pending:
            context.user_data["pending_join"] = (time.time(), pending)
            rows.append([(f"✅ Join & watch ({len(pending)})", "jn:yes"), ("✕ Skip", "cancel")])
        if len(added) == 1:
            rows.append([("👥 Members", f"mb:{added[0]}:0:0")])
        if added and self.rt.chat_scope == "all":
            lines.append("\n<i>Scope is “All chats”, so everything is scanned anyway. Switch to “Selected only” "
                         "to scan just the chats you pick.</i>")
            rows.append([("🎯 Use Selected only", "tg:chat_scope:chats:0")])
        rows.append([("💬 Monitored Chats", "chats:0"), ("◆ Dashboard", "nav:home")])
        await self._say(update, screen("Add Channel / Group", "\n".join(lines)), rows)

    async def _join_pending(self, update, context) -> None:
        entry = context.user_data.pop("pending_join", None)
        if not entry or time.time() - entry[0] > AWAIT_TTL:
            return await self._render(update, screen("Join", "That request expired. Add the chat again."),
                                      [[("➕ Add Channel/Group", "add:chat")], [("‹ Back", "nav:home")]])
        lines: list[str] = []
        for i, info in enumerate(entry[1]):
            if i:
                await asyncio.sleep(2)                       # be gentle with Telegram's join limits
            try:
                joined = await self.uc.join_chat(info)
            except LookupError as exc:
                lines.append(f"⚠️ <b>{esc(info.title)}</b> – {esc(exc)}")
                continue
            if joined.chat_id is not None:
                await self.db.set_chat_enabled(joined.chat_id, joined.title, True)
                lines.append(f"✅ Joined &amp; watching <b>{esc(joined.title)}</b>")
        await self.rt.refresh()
        rows = [[("💬 Monitored Chats", "chats:0"), ("◆ Dashboard", "nav:home")]]
        await self._render(update, screen("Add Channel / Group", "\n".join(lines) or "Nothing to join."), rows)

    async def _toggle_member(self, context, cid: int, uid: int) -> None:
        if uid in self.rt.watch_ids:
            await self.db.remove_watch(uid)
        else:
            term = (context.user_data.get("msearch") or {}).get(cid, "")
            person = next((m for m in await self.uc.list_members(cid, term) if m.user_id == uid), None)
            await self.db.add_watch(uid, person.username if person else None, person.name if person else None)
        await self.rt.refresh()

    async def _set_forward(self, update, raw: str) -> None:
        target = forward_target(raw)
        if target is None:
            return await self._say(update, "⚠️ Send the group as <code>@username</code>, <code>t.me/username</code> or a "
                                           "<code>-100…</code> ID. (Invite links can't be used by a bot.)",
                                   [[("✕ Cancel", "cancel")]])
        await self.rt.set("forward_target", str(target))
        await self.rt.set("forward_chat_id", "")
        await self._say(update, *await self.forward_screen(f"✅ Group set to <b>{esc(target)}</b>. "
                                                          "Add this bot to it, then tap 🧪 Send test."))

    async def _forward_test(self, update) -> None:
        if not self.rt.forward_target:
            return await self._render(update, *await self.forward_screen("⚠️ No group set yet."))
        sample = Alert(detection=Detection(solana=(SAMPLE_ADDRESS,)), chat_id=0, chat_title="Shill Monitor test",
                       message_id=1, text="", timestamp=datetime.now(timezone.utc), is_test=True)
        text, rows = format_forward(sample)
        try:
            cid = await self.send_to_group(self.rt.forward_target, text, rows)
        except Exception as exc:
            log.warning("Forward test failed: %s", type(exc).__name__)
            note = (f"❌ <b>Test failed</b> ({type(exc).__name__}): <code>{esc(str(exc)[:150])}</code>\n"
                    "Add this bot to the group (as admin if it's a channel) and try again.")
            return await self._render(update, *await self.forward_screen(note))
        if cid is not None:
            await self.rt.set("forward_chat_id", str(cid))
        await self._render(update, *await self.forward_screen("✅ <b>Test posted.</b> Check the group."))

    async def _add_keyword(self, update, kw: str) -> None:
        kw = kw.strip()
        if not 2 <= len(kw) <= 64:
            return await self._say(update, "⚠️ Keywords must be 2–64 characters.")
        added = await self.db.add_pattern("keyword", kw)
        await self.rt.refresh()
        await self._say(update, f"✅ Keyword <code>{esc(kw)}</code> {'added' if added else 'already exists'}.",
                        [[("🎯 Filters", "nav:filters")]])

    async def _add_regex(self, update, pattern: str) -> None:
        err = validate_regex(pattern.strip())
        if err:
            return await self._say(update, f"⚠️ {esc(err)}")
        added = await self.db.add_pattern("regex", pattern.strip())
        await self.rt.refresh()
        await self._say(update, f"✅ Regex <code>{esc(pattern)}</code> {'added' if added else 'already exists'}.",
                        [[("🎯 Filters", "nav:filters")]])

    def _logout_confirm(self):
        body = ("This revokes the session on Telegram and deletes the stored copy. Scanning stops.\n"
                "If you also set <code>TELETHON_SESSION</code> on Render, remove it there too.")
        return screen("Disconnect Account?", body), [[("⛔ Yes, disconnect", "logout:yes"), ("Keep linked", "nav:connect")]]

    # ------------------------------------------------------------------ text (multi-step input)
    @admin_only
    async def on_text(self, update, context):
        pending = context.user_data.get("await")
        if not pending or time.time() - pending[1] > AWAIT_TTL:
            self._clear_await(context)
            return await self._say(update, "Use the buttons below or /help.", [[("◆ Dashboard", "nav:home")]])
        kind, text = pending[0], (update.effective_message.text or "").strip()
        if kind in ("phone", "code", "password"):
            try:                                   # remove secrets from the chat immediately
                await update.effective_message.delete()
            except Exception:
                pass
        if kind == "phone":
            return await self._handle_phone(update, context, text)
        if kind == "code":
            return await self._handle_code(update, context, text)
        if kind == "password":
            return await self._handle_password(update, context, text)
        self._clear_await(context)
        if kind == "adduser":
            await self._add_user(update, text)
        elif kind == "addkeyword":
            await self._add_keyword(update, text)
        elif kind == "addregex":
            await self._add_regex(update, text)
        elif kind == "addchat":
            await self._add_chats(update, context, text)
        elif kind == "setforward":
            await self._set_forward(update, text)
        elif kind.startswith("msearch:"):
            _, cid, cpage = kind.split(":")
            context.user_data.setdefault("msearch", {})[int(cid)] = text[:64]
            await self._say(update, *await self.members(context, int(cid), 0, int(cpage)))

    async def _handle_phone(self, update, context, text):
        phone = re.sub(r"[\s\-()]", "", text)
        if not PHONE_RE.match(phone):
            return await self._say(update, "⚠️ Use international format with a leading +, e.g. <code>+15551234567</code>.",
                                   [[("✕ Cancel", "login:cancel")]])
        try:
            await self.uc.begin_login(phone)
        except LoginError as exc:
            if not exc.retry:
                self._clear_await(context)
            return await self._say(update, f"⚠️ {esc(exc)}", [[("✕ Cancel", "login:cancel")]])
        self._set_await(context, "code")
        await self._say(update, *self.prompt(
            "Enter Login Code",
            "Telegram sent you a code (check your Telegram app).\n\n"
            "⚠️ Type it <b>with spaces between digits</b>, e.g. <code>1 2 3 4 5</code>. Telegram blocks codes "
            "that are sent in plain text.", "login:cancel"))

    async def _handle_code(self, update, context, text):
        try:
            result = await self.uc.submit_code(text)
        except LoginError as exc:
            if not exc.retry:
                self._clear_await(context)
            return await self._say(update, f"⚠️ {esc(exc)}", [[("✕ Cancel", "login:cancel")]] if exc.retry else
                                   [[("📱 Start again", "login:start")]])
        if result == "password":
            self._set_await(context, "password")
            return await self._say(update, *self.prompt(
                "Two-Step Verification", "Send your Telegram two-step verification password. "
                "It's used once and the message is deleted.", "login:cancel"))
        self._clear_await(context)
        await self._finish(update)

    async def _handle_password(self, update, context, text):
        try:
            await self.uc.submit_password(text)
        except LoginError as exc:
            if not exc.retry:
                self._clear_await(context)
            return await self._say(update, f"⚠️ {esc(exc)}", [[("✕ Cancel", "login:cancel")]] if exc.retry else
                                   [[("📱 Start again", "login:start")]])
        self._clear_await(context)
        await self._finish(update)

    async def _finish(self, update):
        await self.rt.refresh()
        await self._say(update, screen("Account Linked ✅", f"Connected as <b>{esc(self.uc.account_label)}</b>.\n"
                                       "Scanning is live. Pick which chats to watch next."),
                        [[("💬 Monitored Chats", "chats:0"), ("◆ Dashboard", "nav:home")]])

    # ------------------------------------------------------------------ callbacks
    @admin_only
    async def on_callback(self, update, context):
        q = update.callback_query
        data = q.data or ""
        head, _, rest = data.partition(":")
        if head != "wa":                               # "wa" answers with its own toast (a query can be answered once)
            try:
                await q.answer()
            except Exception:
                pass
        if head == "nav":
            return await self._nav(update, rest)
        if head == "chats":
            parts = rest.split(":")
            if len(parts) > 1 and parts[1] == "r":
                await self.uc.list_dialogs(force=True)
            return await self._render(update, *await self.chats(int(parts[0])))
        if head == "ct":
            cid, page = rest.split(":")
            names = dict(self.uc._dialogs[1])
            await self.db.set_chat_enabled(int(cid), names.get(int(cid), cid), int(cid) not in self.rt.enabled_chats)
            await self.rt.refresh()
            return await self._render(update, *await self.chats(int(page)))
        if head == "mb":
            cid, mp, cp = rest.split(":")
            return await self._render(update, *await self.members(context, int(cid), int(mp), int(cp)))
        if head == "mw":
            cid, uid, mp, cp = rest.split(":")
            try:
                await self._toggle_member(context, int(cid), int(uid))
            except LookupError:
                pass                                   # members() below renders the same error on screen
            return await self._render(update, *await self.members(context, int(cid), int(mp), int(cp)))
        if head == "ms":
            cid, cp = rest.split(":")
            self._set_await(context, f"msearch:{cid}:{cp}")
            return await self._render(update, *self.prompt("Search Members", "Send part of a name or @username."))
        if head == "mx":
            cid, cp = rest.split(":")
            (context.user_data.get("msearch") or {}).pop(int(cid), None)
            return await self._render(update, *await self.members(context, int(cid), 0, int(cp)))
        if head == "jn":
            return await self._join_pending(update, context)
        if head == "tg":
            key, _, where = rest.partition(":")
            await self.rt.toggle(key)
            return await self._nav(update, where or "home")
        if head == "act":
            return await self._action(update, rest)
        if head == "add":
            self._set_await(context, {"user": "adduser", "keyword": "addkeyword", "regex": "addregex",
                                      "chat": "addchat", "forward": "setforward"}[rest])
            msgs = {"user": ("Add User", "Send an <code>@username</code> or a numeric user ID."),
                    "chat": ("Add Channel / Group", self._addchat_help()),
                    "forward": ("Forward Group", "Send the group to post into, e.g. <code>@fergbotalgogc</code> "
                                                 "or <code>t.me/fergbotalgogc</code>."),
                    "keyword": ("Add Keyword", "Send the Robinhood keyword (case-insensitive, whole word)."),
                    "regex": ("Add Regex", "Send a regular expression, e.g. <code>\\bRH-[A-Z0-9]{4,}\\b</code>")}
            return await self._render(update, *self.prompt(*msgs[rest]))
        if head == "ru":
            await self.db.remove_watch(int(rest))
            await self.rt.refresh()
            return await self._render(update, *await self.watch())
        if head == "rp":
            await self.db.remove_pattern(rest)
            await self.rt.refresh()
            return await self._render(update, *await self.filters_screen())
        if head == "wa":
            return await self._watch_from_alert(update, int(rest))
        if head == "cancel":
            self._clear_await(context)
            return await self._render(update, *self.home())
        if head == "login":
            return await self._login_action(update, context, rest)
        if head == "logout":
            if rest == "ask":
                return await self._render(update, *self._logout_confirm())
            await self.uc.logout()
            await self.rt.refresh()
            return await self._render(update, *self.connect())

    async def _nav(self, update, where: str) -> None:
        if where == "home":
            return await self._render(update, *self.home())
        if where == "status":
            return await self._render(update, *self.status())
        if where == "watch":
            return await self._render(update, *await self.watch())
        if where == "filters":
            return await self._render(update, *await self.filters_screen())
        if where == "connect":
            return await self._render(update, *self.connect())
        if where == "forward":
            return await self._render(update, *await self.forward_screen())
        if where == "help":
            return await self._render(update, screen("Help", HELP), [[("‹ Back", "nav:home")]])
        if where.startswith("chats"):
            page = int(where.split(":")[1]) if ":" in where else 0
            return await self._render(update, *await self.chats(page))
        return await self._render(update, *self.home())

    async def _action(self, update, what: str) -> None:
        if what == "pause":
            await self.rt.set("paused", "1")
        elif what == "resume":
            await self.rt.set("paused", "0")
        elif what == "fwdtest":
            return await self._forward_test(update)
        elif what == "reconnect":
            await self.uc.manual_reconnect()
            return await self._render(update, *self.status())
        await self._render(update, *self.home())

    async def _login_action(self, update, context, what: str) -> None:
        if what == "start":
            if not self.uc.remote_login_enabled:
                return await self._render(update, *self.connect())
            self._set_await(context, "phone")
            return await self._render(update, *self.prompt(
                "Link Account", "Send your phone number in international format, e.g. <code>+15551234567</code>.\n"
                "Your message is deleted as soon as it's read.", "login:cancel"))
        await self.uc.cancel_login()
        self._clear_await(context)
        await self._render(update, *self.connect())

    async def _watch_from_alert(self, update, uid: int) -> None:
        try:
            uid, uname, name = await self.uc.resolve_user(uid)
        except LookupError:
            uname = name = None
        await self.db.add_watch(uid, uname, name)
        await self.rt.refresh()
        who = f"@{uname}" if uname else str(uid)
        await update.callback_query.answer(f"Watching {who}", show_alert=False)

    # ------------------------------------------------------------------ errors
    async def on_error(self, update, context):
        err = context.error
        if isinstance(err, Conflict):
            log.warning("Bot API Conflict: another instance is polling (expected briefly during deploys)")
            return
        if isinstance(err, NetworkError):
            log.warning("Bot API network error: %s", type(err).__name__)
            return
        log.error("Unhandled bot error: %s: %s", type(err).__name__, err)
        try:
            if isinstance(update, Update) and update.effective_user and \
                    update.effective_user.id == self.config.authorized_user_id and update.effective_message:
                await update.effective_message.reply_text("⚠️ Something went wrong. It has been logged — try again.")
        except Exception:
            pass
