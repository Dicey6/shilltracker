import asyncio
import os
import tempfile
import unittest

from tests.helpers import make_db, make_runtime


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_init_is_idempotent(self):
        db = await make_db()
        await db.init_schema()
        self.assertEqual(await db.fetchval("SELECT COUNT(*) FROM schema_migrations"), 1)

    async def test_settings_and_watchlist_persist_across_reconnect(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = await make_db(path)
            await db.set_setting("mode", "watchlist")
            self.assertTrue(await db.add_watch(42, "bob", "Bob"))
            self.assertFalse(await db.add_watch(42, None, None))     # refresh, keeps name
            await db.close()
            db2 = await make_db(path)
            self.assertEqual(await db2.get_setting("mode"), "watchlist")
            row = (await db2.list_watch())[0]
            self.assertEqual((row["user_id"], row["username"], row["display_name"]), (42, "bob", "Bob"))
            await db2.close()

    async def test_claim_message_is_unique_and_concurrency_safe(self):
        db = await make_db()
        results = await asyncio.gather(*[db.claim_message(-100, 7, "sig") for _ in range(25)])
        self.assertEqual(results.count(True), 1)
        self.assertTrue(await db.claim_message(-100, 7, "other-sig"))   # edit with new matches
        self.assertTrue(await db.claim_message(-100, 8, "sig"))

    async def test_purge_respects_retention(self):
        db = await make_db()
        await db.claim_message(1, 1, "a")
        await db.execute("UPDATE processed_messages SET seen_at = 0")
        await db.claim_message(1, 2, "a")
        self.assertEqual(await db.purge_processed(14), 1)
        self.assertEqual(await db.fetchval("SELECT COUNT(*) FROM processed_messages"), 1)

    async def test_patterns_unique_case_insensitive_for_keywords(self):
        db = await make_db()
        self.assertTrue(await db.add_pattern("keyword", "Robinhood"))
        self.assertFalse(await db.add_pattern("keyword", "robinhood"))
        self.assertTrue(await db.remove_keyword("ROBINHOOD"))

    async def test_runtime_defaults_and_toggle(self):
        db = await make_db()
        rt = await make_runtime(db)
        self.assertEqual(rt.keywords, ("robinhood",))
        self.assertEqual(rt.mode, "all")
        self.assertEqual(await rt.toggle("mode"), "watchlist")
        self.assertEqual(await rt.toggle("paused"), "1")
        self.assertTrue(rt.paused)
        rt2 = await make_runtime(db)                    # fresh cache, same DB
        self.assertEqual(rt2.mode, "watchlist")
        self.assertEqual(rt2.keywords, ("robinhood",))  # defaults seeded only once


if __name__ == "__main__":
    unittest.main()
