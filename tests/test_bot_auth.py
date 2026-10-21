import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tests.helpers import FakeNotifier, make_db, make_runtime

HAS_PTB = importlib.util.find_spec("telegram") is not None


def make_update(user_id, chat_type="private", callback=False, text=None):
    msg = SimpleNamespace(reply_text=AsyncMock(), text=text, delete=AsyncMock())
    q = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(), data="act:pause") if callback else None
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type=chat_type),
                           effective_message=msg, callback_query=q)


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


@unittest.skipUnless(HAS_PTB, "python-telegram-bot not installed")
class BotAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bot import BotApp
        from scanner import Scanner
        self.db = await make_db()
        self.rt = await make_runtime(self.db)
        self.notifier = FakeNotifier()
        self.notifier.sent = 0
        self.uc = SimpleNamespace(state="disconnected", is_connected=False, account_label="—", session_source=None,
                                  remote_login_enabled=True, _dialogs=(0, []), last_error=None,
                                  resolve_user=AsyncMock(side_effect=LookupError("Link your Telegram account first")))
        self.bot = BotApp(SimpleNamespace(authorized_user_id=1, bot_token="x"), self.db, self.rt, self.uc,
                          Scanner(self.db, self.rt, self.notifier), self.notifier)
        self.ctx = SimpleNamespace(args=[], user_data={})

    async def test_unauthorized_command_is_rejected_and_has_no_effect(self):
        for handler in (self.bot.cmd_status, self.bot.cmd_pause, self.bot.cmd_adduser, self.bot.cmd_connect):
            u = make_update(999)
            await handler(u, self.ctx)
            u.effective_message.reply_text.assert_awaited_once()
            self.assertIn("private", u.effective_message.reply_text.await_args.args[0].lower())
        self.assertFalse(self.rt.paused)
        self.assertEqual(await self.db.list_watch(), [])

    async def test_unauthorized_callback_rejected(self):
        u = make_update(999, callback=True)
        await self.bot.on_callback(u, self.ctx)
        u.callback_query.answer.assert_awaited_once()
        self.assertFalse(self.rt.paused)

    async def test_unauthorized_plain_text_and_unknown_command_rejected(self):
        u = make_update(999, text="+15551234567")
        self.ctx.user_data["await"] = ("phone", 9e12)
        await self.bot.on_text(u, self.ctx)
        await self.bot.cmd_unknown(make_update(999), self.ctx)
        self.assertIn("private", u.effective_message.reply_text.await_args.args[0].lower())

    async def test_authorized_user_in_group_is_ignored(self):
        u = make_update(1, chat_type="supergroup")
        await self.bot.cmd_status(u, self.ctx)
        u.effective_message.reply_text.assert_not_awaited()

    async def test_start_shows_main_menu_buttons(self):
        u = make_update(1)
        await self.bot.cmd_start(u, self.ctx)
        text = u.effective_message.reply_text.await_args.args[0]
        self.assertIn("SHILL MONITOR", text)
        names = " ".join(labels(u.effective_message.reply_text.await_args.kwargs["reply_markup"]))
        for needle in ("Connect Account", "Scanner Status", "Watchlist", "Detection Filters", "Monitored Chats", "Pause", "Help"):
            self.assertIn(needle, names)

    async def test_pause_resume_commands(self):
        await self.bot.cmd_pause(make_update(1), self.ctx)
        self.assertTrue(self.rt.paused)
        await self.bot.cmd_resume(make_update(1), self.ctx)
        self.assertFalse(self.rt.paused)

    async def test_adduser_numeric_id_works_offline_but_username_needs_resolution(self):
        self.ctx.args = ["12345"]
        await self.bot.cmd_adduser(make_update(1), self.ctx)
        self.assertEqual([w["user_id"] for w in await self.db.list_watch()], [12345])
        u = make_update(1)
        self.ctx.args = ["@someone"]
        await self.bot.cmd_adduser(u, self.ctx)
        self.assertIn("Link your Telegram account", u.effective_message.reply_text.await_args.args[0])
        self.assertEqual(len(await self.db.list_watch()), 1)

    async def test_keyword_commands_and_invalid_regex(self):
        self.ctx.args = ["RH-token"]
        await self.bot.cmd_addkeyword(make_update(1), self.ctx)
        self.assertIn("RH-token", self.rt.keywords)
        await self.bot.cmd_removekeyword(make_update(1), self.ctx)
        self.assertNotIn("RH-token", self.rt.keywords)
        u = make_update(1)
        self.ctx.args = ["("]
        await self.bot.cmd_addregex(u, self.ctx)
        self.assertIn("Invalid regex", u.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.rt.regexes, ())


if __name__ == "__main__":
    unittest.main()
