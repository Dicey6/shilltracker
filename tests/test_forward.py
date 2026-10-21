import unittest
from datetime import datetime, timezone

from alerts import COPY_PREFIX, Alert, address_rows, alert_buttons, ca_link, format_alert, format_forward
from detectors import Detection
from notifier import Notifier
from scanner import Scanner
from tests.helpers import FakeNotifier, SAMPLE, make_db, make_event, make_runtime

A2 = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"      # valid 32-byte key (BTC-wrapped mint)


def mk(sol=(SAMPLE,), title="Alpha <Group>", link="https://t.me/c/1/3", test=False, text="hi"):
    return Alert(detection=Detection(solana=tuple(sol)), chat_id=-1001, chat_title=title, message_id=3, text=text,
                 timestamp=datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc), sender_id=7, sender_username="bob",
                 link=link, is_test=test)


class ClickableCA(unittest.TestCase):
    def test_ca_is_a_link(self):
        self.assertEqual(ca_link(SAMPLE), f'<a href="https://dexscreener.com/solana/{SAMPLE}">{SAMPLE}</a>')
        self.assertIn(ca_link(SAMPLE), format_alert(mk())[0])

    def test_single_address_has_copy_and_three_explorers(self):
        (row,) = address_rows([SAMPLE])
        self.assertEqual(row[0], ("📋 Copy", COPY_PREFIX + SAMPLE))
        self.assertEqual([b[0] for b in row[1:]], ["DexScreener", "Solscan", "Birdeye"])

    def test_every_address_gets_its_own_row(self):
        rows = address_rows([SAMPLE, A2])
        self.assertEqual(len(rows), 2)
        self.assertIn(A2, rows[1][0][1])
        flat = [b for r in alert_buttons(mk(sol=(SAMPLE, A2)), True) for b in r]
        self.assertTrue(any(A2 in b[1] and "dexscreener" in b[1] for b in flat))

    def test_copy_payload_fits_telegram_limit(self):
        self.assertLessEqual(len(address_rows([SAMPLE])[0][0][1]), 256)

    def test_forward_message_has_source_name_and_clickable_ca_only(self):
        text, rows = format_forward(mk(text="secret chatter that must not leak"))
        self.assertIn("Alpha &lt;Group&gt;", text)
        self.assertIn('<a href="https://t.me/c/1/3">', text)
        self.assertIn(ca_link(SAMPLE), text)
        self.assertNotIn("secret chatter", text)
        self.assertNotIn("bob", text)
        self.assertEqual(rows, address_rows([SAMPLE]))

    def test_forward_without_link_does_not_invent_one(self):
        text, _ = format_forward(mk(link=None))
        self.assertNotIn("t.me", text)


class ScannerLoopProtection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db()
        self.rt = await make_runtime(self.db)
        self.n = FakeNotifier()
        self.scanner = Scanner(self.db, self.rt, self.n)

    async def test_ignores_our_own_bot(self):
        self.rt.bot_id = 999
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, sender_id=999)))
        self.assertTrue(await self.scanner.handle_event(make_event(SAMPLE, msg_id=2, sender_id=111)))

    async def test_ignores_the_forward_group(self):
        await self.rt.set("forward_chat_id", "-1005550001")
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, chat_id=-1005550001, sender_id=5)))
        self.assertTrue(await self.scanner.handle_event(make_event(SAMPLE, msg_id=2, chat_id=-1001234567890)))

    async def test_bot_dm_is_ignored_even_with_private_chats_on(self):
        await self.rt.set("include_private", "1")
        self.rt.bot_id = 999
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, private=True, sender_id=999)))


class ForwardQueue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db()
        self.rt = await make_runtime(self.db)
        await self.rt.set("forward_target", "@fergbotalgogc")
        self.dm, self.group, self.learned = [], [], []

        async def sleep(_): return None

        async def send_dm(text, rows): self.dm.append(text)

        async def send_group(target, text, rows):
            self.group.append((target, text, rows))
            return -100777

        async def learn(cid): self.learned.append(cid)

        self.n = Notifier(self.rt, sleep=sleep)
        self.n.bind(send_dm)
        self.n.bind_forward(send_group)
        self.n.on_forward_chat = learn

    async def drain(self):
        await self.n._fq.put(None)
        await self.n.run_forward()

    async def test_ca_alert_is_posted_to_group_with_source(self):
        await self.n._deliver(mk())
        await self.drain()
        self.assertEqual(len(self.dm), 1)
        (target, text, rows), = self.group
        self.assertEqual(target, "@fergbotalgogc")
        self.assertIn("Alpha &lt;Group&gt;", text)
        self.assertIn(SAMPLE, text)
        self.assertEqual(self.learned, [-100777])
        self.assertEqual(self.n.forwarded, 1)

    async def test_robinhood_only_tests_and_disabled_are_not_posted(self):
        await self.n._deliver(Alert(detection=Detection(robinhood=("robinhood",)), chat_id=1, chat_title="x",
                                    message_id=1, text="robinhood", timestamp=datetime.now(timezone.utc)))
        await self.n._deliver(mk(test=True))
        await self.rt.set("forward_enabled", "0")
        await self.n._deliver(mk())
        await self.drain()
        self.assertEqual(self.group, [])
        self.assertEqual(len(self.dm), 3)

    async def test_failure_notifies_owner_and_never_blocks_dm(self):
        async def boom(*a): raise type("Forbidden", (Exception,), {})("bot is not a member")
        self.n.bind_forward(boom)
        await self.n._deliver(mk())
        await self.drain()
        self.assertEqual(len(self.dm), 1)                      # DM went out
        self.assertEqual(self.n.forward_failed, 1)
        notice = await self.n._q.get()
        self.assertIn("Add this bot to the group", notice)

    async def test_rate_limit_is_retried(self):
        calls = []

        class RetryAfter(Exception):
            retry_after = 2

        async def flaky(target, text, rows):
            calls.append(1)
            if len(calls) == 1:
                raise RetryAfter()
            return -100777
        self.n.bind_forward(flaky)
        await self.n._deliver(mk())
        await self.drain()
        self.assertEqual((len(calls), self.n.forwarded), (2, 1))


class ForwardDefaults(unittest.IsolatedAsyncioTestCase):
    async def test_target_seeded_once_and_user_choice_wins(self):
        db = await make_db()
        from state import RuntimeState
        rt = RuntimeState(db)
        await rt.ensure_defaults("@fergbotalgogc")
        await rt.refresh()
        self.assertEqual((rt.forward_target, rt.forward_enabled, rt.forward_chat_id), ("@fergbotalgogc", True, None))
        await rt.set("forward_target", "@other")
        await rt.ensure_defaults("@fergbotalgogc")
        await rt.refresh()
        self.assertEqual(rt.forward_target, "@other")


if __name__ == "__main__":
    unittest.main()
