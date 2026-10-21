import unittest

from scanner import Scanner
from tests.helpers import FakeNotifier, SAMPLE, make_db, make_event, make_runtime


class ScannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db()
        self.rt = await make_runtime(self.db)
        self.notifier = FakeNotifier()
        self.scanner = Scanner(self.db, self.rt, self.notifier)

    async def test_all_users_mode_alerts_with_full_metadata(self):
        self.assertTrue(await self.scanner.handle_event(make_event(f"look {SAMPLE}", msg_id=5)))
        a = self.notifier.items[0]
        self.assertEqual((a.sender_username, a.sender_id, a.chat_title, a.message_id), ("alice", 111, "Alpha Group", 5))
        self.assertEqual(a.detection.solana, (SAMPLE,))
        self.assertEqual(a.link, "https://t.me/c/1234567890/5")

    async def test_public_chat_link_and_no_link_for_basic_group(self):
        await self.scanner.handle_event(make_event(SAMPLE, msg_id=1, chat_username="pubgroup"))
        await self.scanner.handle_event(make_event(SAMPLE, msg_id=2, chat_id=-555))
        self.assertEqual(self.notifier.items[0].link, "https://t.me/pubgroup/1")
        self.assertIsNone(self.notifier.items[1].link)

    async def test_watchlist_mode_filters_senders(self):
        await self.rt.set("mode", "watchlist")
        await self.db.add_watch(111, "alice", None)
        await self.rt.refresh()
        self.assertTrue(await self.scanner.handle_event(make_event(SAMPLE, msg_id=1, sender_id=111)))
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, msg_id=2, sender_id=222)))
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, msg_id=3, sender_id=None)))

    async def test_duplicate_events_alert_once_but_edit_with_new_match_alerts(self):
        ev = make_event(SAMPLE, msg_id=9)
        self.assertTrue(await self.scanner.handle_event(ev))
        self.assertFalse(await self.scanner.handle_event(ev))                     # re-delivery
        self.assertFalse(await self.scanner.handle_event(ev, edited=True))        # edit, same matches
        edited = make_event(f"{SAMPLE} robinhood", msg_id=9)
        self.assertTrue(await self.scanner.handle_event(edited, edited=True))
        self.assertEqual(len(self.notifier.items), 2)
        self.assertTrue(self.notifier.items[1].edited)

    async def test_dedup_survives_restart(self):
        await self.scanner.handle_event(make_event(SAMPLE, msg_id=3))
        scanner2 = Scanner(self.db, await make_runtime(self.db), FakeNotifier())   # "restarted" process
        self.assertFalse(await scanner2.handle_event(make_event(SAMPLE, msg_id=3)))

    async def test_combined_detection_single_alert(self):
        await self.scanner.handle_event(make_event(f"Robinhood CA {SAMPLE}"))
        self.assertEqual(len(self.notifier.items), 1)
        self.assertEqual(self.notifier.items[0].detection.label, "Solana CA + Robinhood-related")

    async def test_paused_empty_private_own_and_scope(self):
        await self.rt.set("paused", "1")
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, msg_id=1)))
        await self.rt.set("paused", "0")
        self.assertFalse(await self.scanner.handle_event(make_event("", msg_id=2)))
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, msg_id=3, private=True)))
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, msg_id=4, out=True)))
        await self.rt.set("chat_scope", "selected")
        self.assertFalse(await self.scanner.handle_event(make_event(SAMPLE, msg_id=5)))
        await self.db.set_chat_enabled(-1001234567890, "Alpha Group", True)
        await self.rt.refresh()
        self.assertTrue(await self.scanner.handle_event(make_event(SAMPLE, msg_id=6)))

    async def test_forwarded_flag_and_missing_username(self):
        await self.scanner.handle_event(make_event(SAMPLE, fwd=True, username=None))
        a = self.notifier.items[0]
        self.assertTrue(a.forwarded)
        self.assertIsNone(a.sender_username)
        self.assertEqual(a.sender_name, "Alice")


if __name__ == "__main__":
    unittest.main()
