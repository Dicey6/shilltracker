import importlib.util
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.helpers import FakeNotifier, make_db, make_runtime

HAS_PTB = importlib.util.find_spec("telegram") is not None
CHAT = -1001234567890


def cb_update(user_id, data):
    msg = SimpleNamespace(reply_text=AsyncMock(), text=None, delete=AsyncMock())
    q = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(), data=data)
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type="private"),
                           effective_message=msg, callback_query=q)


def msg_update(user_id, text=None):
    msg = SimpleNamespace(reply_text=AsyncMock(), text=text, delete=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type="private"),
                           effective_message=msg, callback_query=None)


def shown(u):
    """(text, [button labels]) of whatever the bot last displayed."""
    if u.callback_query is not None and u.callback_query.edit_message_text.await_args:
        call = u.callback_query.edit_message_text.await_args
    else:
        call = u.effective_message.reply_text.await_args
    markup = call.kwargs.get("reply_markup")
    labels = [b.text for row in markup.inline_keyboard for b in row] if markup else []
    return call.args[0], labels


@unittest.skipUnless(HAS_PTB, "python-telegram-bot not installed")
class SourceFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bot import BotApp
        from scanner import Scanner
        from user_client import ChatInfo, Member
        self.ChatInfo, self.Member = ChatInfo, Member
        self.db = await make_db()
        self.rt = await make_runtime(self.db)
        self.notifier = FakeNotifier()
        self.notifier.sent = 0
        self.people = [Member(11, "alice", "Alice A"), Member(12, None, "Bob B"), Member(13, "helper_bot", "Helper", True)]
        self.uc = SimpleNamespace(
            state="connected", is_connected=True, account_label="@me", session_source="x", remote_login_enabled=True,
            _dialogs=(0, [(CHAT, "Alpha Group")]), last_error=None,
            resolve_chat=AsyncMock(), join_chat=AsyncMock(), list_members=AsyncMock(return_value=self.people),
            resolve_user=AsyncMock(side_effect=LookupError("n/a")))
        self.bot = BotApp(SimpleNamespace(authorized_user_id=1, bot_token="x"), self.db, self.rt, self.uc,
                          Scanner(self.db, self.rt, self.notifier), self.notifier)
        self.ctx = SimpleNamespace(args=[], user_data={})

    # ---------------------------------------------------------------- add channel / group
    async def test_add_chat_already_joined_is_watched_immediately(self):
        self.uc.resolve_chat.return_value = self.ChatInfo(CHAT, "Alpha Group", True)
        u = msg_update(1, "t.me/alphagroup")
        self.ctx.user_data["await"] = ("addchat", 9e12)
        await self.bot.on_text(u, self.ctx)
        text, labels = shown(u)
        self.assertIn("Alpha Group", text)
        self.assertIn("👥 Members", labels)
        self.assertEqual([r["chat_id"] for r in await self.db.list_chat_rows()], [CHAT])
        self.assertIn(CHAT, self.rt.enabled_chats)

    async def test_add_chat_not_joined_asks_before_joining(self):
        self.uc.resolve_chat.return_value = self.ChatInfo(None, "Secret Calls", False, invite="AbCdEfGh12")
        u = msg_update(1)
        await self.bot._add_chats(u, self.ctx, "https://t.me/+AbCdEfGh12")
        text, labels = shown(u)
        self.assertIn("isn't in it yet", text)
        self.assertTrue(any("Join & watch" in l for l in labels))
        self.uc.join_chat.assert_not_awaited()                       # nothing joined without the tap
        self.assertEqual(await self.db.list_chat_rows(), [])

        self.uc.join_chat.return_value = self.ChatInfo(-1009990001, "Secret Calls", True)
        tap = cb_update(1, "jn:yes")
        await self.bot.on_callback(tap, self.ctx)
        self.uc.join_chat.assert_awaited_once()
        self.assertIn(-1009990001, self.rt.enabled_chats)
        self.assertIn("Joined", shown(tap)[0])

    async def test_bad_references_and_lookup_errors_are_reported_not_saved(self):
        self.uc.resolve_chat.side_effect = LookupError("Telegram couldn't find that.")
        u = msg_update(1)
        await self.bot._add_chats(u, self.ctx, "??? @missingchannel")
        text, _ = shown(u)
        self.assertIn("not a channel/group reference", text)
        self.assertIn("couldn't find", text)
        self.assertEqual(await self.db.list_chat_rows(), [])

    async def test_join_request_expires(self):
        self.ctx.user_data["pending_join"] = (0, [self.ChatInfo(None, "Old", False, invite="x")])
        tap = cb_update(1, "jn:yes")
        await self.bot.on_callback(tap, self.ctx)
        self.uc.join_chat.assert_not_awaited()
        self.assertIn("expired", shown(tap)[0])

    # ---------------------------------------------------------------- members picker
    async def test_members_list_and_toggle_watch(self):
        u = cb_update(1, f"mb:{CHAT}:0:0")
        await self.bot.on_callback(u, self.ctx)
        text, labels = shown(u)
        self.assertIn("Alpha Group", text)
        self.assertTrue(any("Alice A @alice" in l for l in labels))
        tap = cb_update(1, f"mw:{CHAT}:11:0:0")
        await self.bot.on_callback(tap, self.ctx)
        self.assertEqual([w["user_id"] for w in await self.db.list_watch()], [11])
        self.assertEqual((await self.db.list_watch())[0]["username"], "alice")
        self.assertTrue(any(l.startswith("✅ Alice") for l in shown(tap)[1]))
        await self.bot.on_callback(cb_update(1, f"mw:{CHAT}:11:0:0"), self.ctx)      # tap again -> removed
        self.assertEqual(await self.db.list_watch(), [])

    async def test_members_error_is_shown_on_screen(self):
        self.uc.list_members.side_effect = LookupError("Telegram only lets admins list the members of this chat.")
        u = cb_update(1, f"mb:{CHAT}:0:0")
        await self.bot.on_callback(u, self.ctx)
        self.assertIn("only lets admins", shown(u)[0])

    async def test_member_search_prompt_and_term(self):
        await self.bot.on_callback(cb_update(1, f"ms:{CHAT}:0"), self.ctx)
        self.assertEqual(self.ctx.user_data["await"][0], f"msearch:{CHAT}:0")
        u = msg_update(1, "ali")
        await self.bot.on_text(u, self.ctx)
        self.uc.list_members.assert_awaited_with(CHAT, "ali")
        self.assertIn("ali", shown(u)[0])

    async def test_chats_list_has_members_and_add_buttons(self):
        self.uc.list_dialogs = AsyncMock(return_value=[(CHAT, "Alpha Group")])
        u = cb_update(1, "chats:0")
        await self.bot.on_callback(u, self.ctx)
        labels = shown(u)[1]
        self.assertIn("👥", labels)
        self.assertIn("➕ Add channel/group", labels)

    # ---------------------------------------------------------------- forwarding
    async def test_set_forward_group_normalises_and_resets_learned_id(self):
        await self.rt.set("forward_chat_id", "-100111")
        u = msg_update(1)
        await self.bot._set_forward(u, "https://t.me/fergbotalgogc")
        self.assertEqual((self.rt.forward_target, self.rt.forward_chat_id), ("@fergbotalgogc", None))
        bad = msg_update(1)
        await self.bot._set_forward(bad, "https://t.me/+AbCdEfGh12")
        self.assertEqual(self.rt.forward_target, "@fergbotalgogc")          # unchanged
        self.assertIn("Invite links", shown(bad)[0])

    async def test_forward_test_success_learns_chat_id(self):
        await self.rt.set("forward_target", "@fergbotalgogc")
        self.bot.send_to_group = AsyncMock(return_value=-100777)
        u = cb_update(1, "act:fwdtest")
        await self.bot.on_callback(u, self.ctx)
        target, text, rows = self.bot.send_to_group.await_args.args
        self.assertEqual(target, "@fergbotalgogc")
        self.assertIn("Shill Monitor test", text)
        self.assertEqual(self.rt.forward_chat_id, -100777)
        self.assertIn("Test posted", shown(u)[0])

    async def test_forward_test_failure_explains_the_fix(self):
        await self.rt.set("forward_target", "@fergbotalgogc")
        self.bot.send_to_group = AsyncMock(side_effect=RuntimeError("Forbidden: bot is not a member"))
        u = cb_update(1, "act:fwdtest")
        await self.bot.on_callback(u, self.ctx)
        text = shown(u)[0]
        self.assertIn("Test failed", text)
        self.assertIn("Add this bot to the group", text)
        self.assertIsNone(self.rt.forward_chat_id)

    async def test_forward_toggle_and_screen(self):
        u = cb_update(1, "tg:forward_enabled:forward")
        await self.bot.on_callback(u, self.ctx)
        self.assertFalse(self.rt.forward_enabled)
        self.assertIn("Forward to Ferg Bot", shown(u)[0])

    async def test_dashboard_shows_new_buttons(self):
        u = msg_update(1)
        await self.bot.cmd_start(u, self.ctx)
        labels = " ".join(shown(u)[1])
        self.assertIn("Add Channel/Group", labels)
        self.assertIn("Forward", labels)

    # ---------------------------------------------------------------- auth + buttons
    async def test_new_callbacks_reject_strangers(self):
        for data in (f"mw:{CHAT}:11:0:0", "jn:yes", "act:fwdtest", f"mb:{CHAT}:0:0", "add:chat", "add:forward"):
            await self.bot.on_callback(cb_update(999, data), self.ctx)
        self.assertEqual(await self.db.list_watch(), [])
        self.uc.list_members.assert_not_awaited()
        self.uc.join_chat.assert_not_awaited()

    def test_copy_and_link_buttons(self):
        from bot import kb
        rows = kb([[("📋 Copy", "cp=ABC123"), ("Dex", "https://dexscreener.com/solana/ABC123"), ("x", "nav:home")]])
        copy, link, cb = rows.inline_keyboard[0]
        self.assertEqual(copy.copy_text.text, "ABC123")
        self.assertTrue(link.url.startswith("https://"))
        self.assertEqual(cb.callback_data, "nav:home")


if __name__ == "__main__":
    unittest.main()
