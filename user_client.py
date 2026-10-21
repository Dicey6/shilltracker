"""Telethon user-account client: session storage, in-bot login, event wiring, reconnect supervision."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import telethon.errors as tle
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, events, utils
from telethon.tl import functions, types
from telethon.sessions import StringSession

from config import Config
from sources import ChatRef

log = logging.getLogger(__name__)

SESSION_KEY = "telethon_session_v1"


def _errs(*names: str) -> tuple:
    return tuple(getattr(tle, n) for n in names if hasattr(tle, n))


# Errors meaning "this session will never work again; the human must re-link".
FATAL_AUTH = _errs("AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError",
                   "SessionExpiredError", "UserDeactivatedError", "UserDeactivatedBanError", "ApiIdInvalidError")
FloodWait = tle.FloodWaitError


@dataclass
class ChatInfo:
    """What we know about a channel/group the user pasted into the bot."""
    chat_id: Optional[int]            # marked id (-100…); None for a not-yet-joined private invite
    title: str
    member: bool                      # is the linked account already in it?
    invite: Optional[str] = None      # invite hash when it came from a private link
    entity: Any = None


@dataclass(frozen=True)
class Member:
    user_id: int
    username: Optional[str]
    name: str
    is_bot: bool = False


class LoginError(Exception):
    """Carries a message that is safe to show to the user. `retry` = same step may be re-attempted."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


@dataclass
class _Login:
    client: Any
    phone: str
    phone_code_hash: str
    expires: float
    stage: str = "code"          # code -> password


def make_cipher(key: str) -> Fernet:
    """Accepts a real Fernet key, or ANY secret string (e.g. one Render generates) and derives a Fernet key from it."""
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def default_factory(config: Config) -> Callable[[Optional[str]], TelegramClient]:
    def make(session: Optional[str]) -> TelegramClient:
        return TelegramClient(StringSession(session or ""), config.api_id, config.api_hash,
                              connection_retries=5, retry_delay=2, auto_reconnect=True, flood_sleep_threshold=60)
    return make


class UserClient:
    def __init__(self, config: Config, db, scanner, *, client_factory=None, sleep=asyncio.sleep):
        self.config, self.db, self.scanner = config, db, scanner
        self._factory = client_factory or default_factory(config)
        self._sleep = sleep
        self.client: Any = None
        self.state = "disconnected"   # disconnected|connecting|reconnecting|connected|unauthorized|failed
        self.me: Any = None
        self.last_error: Optional[str] = None
        self.session_source: Optional[str] = None
        self.notify: Callable[[str], Awaitable[None]] = _noop
        self._login: Optional[_Login] = None
        self._cipher = make_cipher(config.session_encryption_key) if config.session_encryption_key else None
        self._dialogs: tuple[float, list[tuple[int, str]]] = (0.0, [])
        self._reconnect_lock = asyncio.Lock()
        self._flood = 0
        self._members: dict[tuple[int, str], tuple[float, list[Member]]] = {}

    # ------------------------------------------------------------ properties
    @property
    def remote_login_enabled(self) -> bool:
        return self._cipher is not None

    @property
    def login_pending(self) -> bool:
        return self._login is not None and time.monotonic() < self._login.expires

    @property
    def is_connected(self) -> bool:
        return self.state == "connected"

    @property
    def account_label(self) -> str:
        if not self.me:
            return "—"
        uname = getattr(self.me, "username", None)
        return f"@{uname}" if uname else (getattr(self.me, "first_name", None) or str(getattr(self.me, "id", "?")))

    # ------------------------------------------------------------ session persistence
    async def _load_session(self) -> Optional[str]:
        blob = await self.db.get_state(SESSION_KEY)
        if blob and self._cipher:
            try:
                self.session_source = "database (encrypted)"
                return self._cipher.decrypt(blob.encode()).decode()
            except InvalidToken:
                log.error("Stored session could not be decrypted - was SESSION_ENCRYPTION_KEY changed?")
        if self.config.telethon_session:
            self.session_source = "TELETHON_SESSION env var"
            return self.config.telethon_session
        self.session_source = None
        return None

    async def _save_session(self, session: str) -> None:
        assert self._cipher is not None
        await self.db.set_state(SESSION_KEY, self._cipher.encrypt(session.encode()).decode())
        self.session_source = "database (encrypted)"

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        session = await self._load_session()
        if not session:
            self.state = "disconnected"
            log.info("No Telegram session configured; use /connect in the bot.")
            return
        self.client = self._make_client(session)
        await self._connect_once()

    async def stop(self) -> None:
        await self._cancel_login()
        if self.client is not None:
            try:
                await asyncio.wait_for(self.client.disconnect(), 10)
            except Exception:
                pass
        self.state = "disconnected"

    def _make_client(self, session: Optional[str]) -> Any:
        client = self._factory(session)
        client.add_event_handler(self._on_new, events.NewMessage())
        client.add_event_handler(self._on_edit, events.MessageEdited())
        return client

    async def _on_new(self, event: Any) -> None:
        await self._dispatch(event, False)

    async def _on_edit(self, event: Any) -> None:
        await self._dispatch(event, True)

    async def _dispatch(self, event: Any, edited: bool) -> None:
        try:
            await self.scanner.handle_event(event, edited=edited)
        except FloodWait as exc:
            log.warning("FloodWait while handling an event (%ss)", exc.seconds)
        except Exception:
            log.exception("Error while handling a message event")

    async def _connect_once(self) -> bool:
        """One connection attempt. Never raises; updates self.state / self.last_error."""
        self.state = "connecting" if self.state != "reconnecting" else "reconnecting"
        try:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                self.state, self.last_error = "unauthorized", "Session expired or revoked"
                return False
            self.me = await self.client.get_me()
            self.state, self.last_error = "connected", None
            log.info("Telethon connected")
            return True
        except FloodWait as exc:
            self.last_error = f"FloodWait {exc.seconds}s"
            self._flood = exc.seconds
            self.state = "reconnecting"
            return False
        except FATAL_AUTH as exc:
            self.state, self.last_error = "unauthorized", f"Session invalid ({type(exc).__name__})"
            return False
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.state = "reconnecting"
            log.warning("Telethon connect failed: %s", type(exc).__name__)
            return False

    async def reconnect_cycle(self) -> bool:
        """Bounded exponential backoff. Gives up after MAX_RECONNECT_ATTEMPTS instead of looping forever."""
        if self._reconnect_lock.locked() or self.client is None:
            return False
        async with self._reconnect_lock:
            self.state = "reconnecting"
            for attempt in range(1, self.config.max_reconnect_attempts + 1):
                delay = min(self.config.reconnect_base_delay * 2 ** (attempt - 1), 300)
                await self._sleep(delay)
                self._flood = 0
                if await self._connect_once():
                    await self.notify("🟢 Telegram connection restored.")
                    return True
                if self.state == "unauthorized":
                    await self.notify("🔴 Your Telegram session is no longer valid. Open the bot → Connect Account to re-link.")
                    return False
                if self._flood:
                    await self._sleep(min(self._flood + 1, 900))
            self.state = "failed"
            await self.notify(f"🔴 Could not reconnect after {self.config.max_reconnect_attempts} attempts "
                              f"({self.last_error}). Tap Reconnect in Scanner Status when ready.")
            return False

    async def manual_reconnect(self) -> bool:
        if self.client is None:
            session = await self._load_session()
            if not session:
                return False
            self.client = self._make_client(session)
        self.state = "reconnecting"
        if await self._connect_once():
            return True
        return False

    async def supervise(self, stop: asyncio.Event, interval: float = 20.0) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except asyncio.TimeoutError:
                pass
            if self.client is None or self.state in ("disconnected", "unauthorized", "failed"):
                continue
            if self.state == "connected" and self.client.is_connected():
                continue
            await self.reconnect_cycle()

    # ------------------------------------------------------------ in-bot login
    async def begin_login(self, phone: str) -> None:
        if not self._cipher:
            raise LoginError("Remote login is disabled: set SESSION_ENCRYPTION_KEY, or generate a session locally "
                             "(see README).")
        await self._cancel_login()
        client = self._factory(None)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except FloodWait as exc:
            await _safe_disconnect(client)
            raise LoginError(f"Telegram asked us to wait {exc.seconds}s before requesting another code.")
        except _errs("PhoneNumberInvalidError", "PhoneNumberBannedError"):
            await _safe_disconnect(client)
            raise LoginError("Telegram rejected that phone number. Use international format, e.g. +15551234567.", retry=True)
        except _errs("ApiIdInvalidError"):
            await _safe_disconnect(client)
            raise LoginError("TELEGRAM_API_ID / TELEGRAM_API_HASH were rejected by Telegram.")
        except Exception as exc:
            await _safe_disconnect(client)
            log.warning("send_code_request failed: %s", type(exc).__name__)
            raise LoginError("Could not request a login code. Try again in a minute.")
        self._login = _Login(client, phone, sent.phone_code_hash, time.monotonic() + self.config.login_timeout)

    def _active_login(self) -> _Login:
        if self._login is None or time.monotonic() >= self._login.expires:
            raise LoginError("That login attempt expired. Start again from Connect Account.")
        return self._login

    async def submit_code(self, raw: str) -> str:
        """Returns 'ok' or 'password'."""
        login = self._active_login()
        digits = re.sub(r"\D", "", raw)
        if not 4 <= len(digits) <= 8:
            raise LoginError("That doesn't look like a login code. Send the digits, e.g. 1 2 3 4 5.", retry=True)
        try:
            await login.client.sign_in(phone=login.phone, code=digits, phone_code_hash=login.phone_code_hash)
        except tle.SessionPasswordNeededError:
            login.stage = "password"
            return "password"
        except _errs("PhoneCodeExpiredError"):
            await self._cancel_login()
            raise LoginError("The code expired. Start again and request a new one.")
        except _errs("PhoneCodeInvalidError"):
            raise LoginError("Wrong code. Try again.", retry=True)
        except FloodWait as exc:
            await self._cancel_login()
            raise LoginError(f"Too many attempts. Wait {exc.seconds}s and start again.")
        except Exception as exc:
            await self._cancel_login()
            log.warning("sign_in failed: %s", type(exc).__name__)
            raise LoginError("Login failed. Start again.")
        await self._finish_login(login)
        return "ok"

    async def submit_password(self, password: str) -> None:
        login = self._active_login()
        if login.stage != "password":
            raise LoginError("No two-step verification is pending.")
        try:
            await login.client.sign_in(password=password)
        except _errs("PasswordHashInvalidError"):
            raise LoginError("Wrong password. Try again.", retry=True)
        except FloodWait as exc:
            await self._cancel_login()
            raise LoginError(f"Too many attempts. Wait {exc.seconds}s and start again.")
        except Exception as exc:
            await self._cancel_login()
            log.warning("2FA sign_in failed: %s", type(exc).__name__)
            raise LoginError("Login failed. Start again.")
        await self._finish_login(login)

    async def _finish_login(self, login: _Login) -> None:
        await self._save_session(login.client.session.save())
        old, self.client = self.client, login.client
        self._login = None
        if old is not None and old is not login.client:
            await _safe_disconnect(old)
        login.client.add_event_handler(self._on_new, events.NewMessage())
        login.client.add_event_handler(self._on_edit, events.MessageEdited())
        self.me = await login.client.get_me()
        self.state, self.last_error = "connected", None
        log.info("Telegram account linked")

    async def _cancel_login(self) -> None:
        login, self._login = self._login, None
        if login is not None:
            await _safe_disconnect(login.client)

    async def cancel_login(self) -> None:
        await self._cancel_login()

    async def logout(self) -> None:
        """Revoke the session on Telegram's side and forget it locally."""
        await self._cancel_login()
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception as exc:
                log.warning("log_out failed (%s); disconnecting instead", type(exc).__name__)
                await _safe_disconnect(self.client)
        await self.db.delete_state(SESSION_KEY)
        self.client, self.me, self.state, self.last_error = None, None, "disconnected", None

    # ------------------------------------------------------------ queries
    async def list_dialogs(self, force: bool = False) -> list[tuple[int, str]]:
        ts, cached = self._dialogs
        if cached and not force and time.monotonic() - ts < 60:
            return cached
        if not self.is_connected:
            return cached
        out: list[tuple[int, str]] = []
        try:
            async for d in self.client.iter_dialogs(limit=500):
                if d.is_group or d.is_channel:
                    out.append((d.id, d.name or str(d.id)))
        except FloodWait as exc:
            log.warning("FloodWait listing dialogs (%ss)", exc.seconds)
            return cached
        out.sort(key=lambda x: x[1].lower())
        self._dialogs = (time.monotonic(), out)
        return out

    # ------------------------------------------------------------ manual sources
    def _need_link(self) -> None:
        if not self.is_connected:
            raise LookupError("Link your Telegram account first (Connect Account).")

    async def resolve_chat(self, ref: ChatRef) -> ChatInfo:
        """Look up a channel/group from @name, t.me link, invite link or -100… id. Never joins anything."""
        self._need_link()
        try:
            if ref.kind == "invite":
                res = await self.client(functions.messages.CheckChatInviteRequest(str(ref.value)))
                if isinstance(res, types.ChatInviteAlready):
                    return ChatInfo(utils.get_peer_id(res.chat), getattr(res.chat, "title", "") or "Private chat", True,
                                    entity=res.chat)
                return ChatInfo(None, getattr(res, "title", None) or "Private chat", False, invite=str(ref.value))
            ent = await self.client.get_entity(ref.value)
        except _errs("InviteHashExpiredError", "InviteHashInvalidError"):
            raise LookupError("That invite link is invalid or expired.")
        except FloodWait as exc:
            raise LookupError(f"Telegram rate-limited the lookup. Try again in {exc.seconds}s.")
        except Exception as exc:
            log.debug("resolve_chat failed: %s", type(exc).__name__)
            raise LookupError("Telegram couldn't find that. Check the link/@username, or for a private chat "
                              "you're already in, send its -100… ID.")
        if isinstance(ent, types.User):
            raise LookupError("That's a person, not a channel or group. Add people from 👁 Watchlist.")
        if not hasattr(ent, "title"):
            raise LookupError("That doesn't look like a channel or group.")
        return ChatInfo(utils.get_peer_id(ent), ent.title, not getattr(ent, "left", False), entity=ent)

    async def join_chat(self, info: ChatInfo) -> ChatInfo:
        """Join with the linked account. Only ever called after the owner taps 'Join & watch'."""
        self._need_link()
        try:
            if info.invite:
                upd = await self.client(functions.messages.ImportChatInviteRequest(info.invite))
                chat = upd.chats[0]
                info = ChatInfo(utils.get_peer_id(chat), getattr(chat, "title", info.title), True, entity=chat)
            else:
                await self.client(functions.channels.JoinChannelRequest(info.entity))
                info.member = True
        except _errs("UserAlreadyParticipantError"):
            info.member = True
        except _errs("InviteRequestSentError"):
            raise LookupError("Join request sent - an admin has to approve it. Add the chat again afterwards.")
        except _errs("InviteHashExpiredError", "InviteHashInvalidError"):
            raise LookupError("That invite link is invalid or expired.")
        except _errs("ChannelsTooMuchError"):
            raise LookupError("This account has joined the maximum number of chats. Leave one first.")
        except _errs("ChannelPrivateError"):
            raise LookupError("That chat is private - send an invite link instead.")
        except FloodWait as exc:
            raise LookupError(f"Telegram asked us to wait {exc.seconds}s before joining. Try again later.")
        except Exception as exc:
            log.warning("join_chat failed: %s", type(exc).__name__)
            raise LookupError("Couldn't join that chat.")
        await self.list_dialogs(force=True)
        return info

    async def _entity(self, chat_id: int) -> Any:
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:                         # not in Telethon's cache yet (e.g. after a restart)
            await self.list_dialogs(force=True)
            return await self.client.get_input_entity(chat_id)

    async def list_members(self, chat_id: int, search: str = "", limit: int = 200, force: bool = False) -> list[Member]:
        """Members of a group (first `limit`, or those matching `search`). Cached for two minutes."""
        self._need_link()
        key = (chat_id, search.strip().lower())
        hit = self._members.get(key)
        if hit and not force and time.monotonic() - hit[0] < 120:
            return hit[1]
        out: list[Member] = []
        try:
            ent = await self._entity(chat_id)
            async for u in self.client.iter_participants(ent, limit=limit, search=search.strip()):
                if getattr(u, "deleted", False) or not isinstance(u, types.User):
                    continue
                name = " ".join(x for x in (u.first_name, u.last_name) if x) or str(u.id)
                out.append(Member(u.id, u.username, name, bool(getattr(u, "bot", False))))
        except _errs("ChatAdminRequiredError", "ChannelPrivateError"):
            raise LookupError("Telegram only lets admins list the members of this chat. "
                              "Add people by @username or ID from 👁 Watchlist instead.")
        except FloodWait as exc:
            raise LookupError(f"Telegram rate-limited the member list. Try again in {exc.seconds}s.")
        except LookupError:
            raise
        except Exception as exc:
            log.warning("list_members failed: %s", type(exc).__name__)
            raise LookupError("Couldn't load the member list for that chat.")
        out.sort(key=lambda m: (m.is_bot, m.name.lower()))
        self._members[key] = (time.monotonic(), out)
        return out

    async def resolve_user(self, ref: str | int) -> tuple[int, Optional[str], Optional[str]]:
        """Returns (user_id, username, display_name). Raises LookupError with a user-safe message."""
        if not self.is_connected:
            raise LookupError("Link your Telegram account first so usernames can be resolved.")
        try:
            ent = await self.client.get_entity(ref)
        except FloodWait as exc:
            raise LookupError(f"Telegram rate-limited the lookup. Try again in {exc.seconds}s.")
        except Exception:
            raise LookupError("Telegram couldn't resolve that. Send the numeric user ID instead.")
        name = " ".join(x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x) \
            or getattr(ent, "title", None)
        return ent.id, getattr(ent, "username", None), name


async def _noop(_: str) -> None:
    return None


async def _safe_disconnect(client: Any) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), 10)
    except Exception:
        pass
