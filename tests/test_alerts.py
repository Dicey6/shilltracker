import unittest
from datetime import datetime, timezone

from alerts import Alert, TG_LIMIT, alert_buttons, format_alert, split_escaped
from detectors import Detection
from notifier import Notifier
from tests.helpers import SAMPLE, make_db, make_runtime


def mk(text="hi", link=None, det=None):
    return Alert(detection=det or Detection(solana=(SAMPLE,)), chat_id=-1001, chat_title="G <1>", message_id=3,
                 text=text, timestamp=datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc), sender_id=7,
                 sender_username="bob", link=link)


class AlertFormat(unittest.TestCase):
    def test_contains_required_fields_and_escapes_html(self):
        text = format_alert(mk("a < b & c", link="https://t.me/c/1/3"))[0]
        for needle in ("NEW ALPHA SIGNAL", "@bob", "<code>7</code>", "G &lt;1&gt;", "2026-10-12 14:30 UTC",
                       SAMPLE, "a &lt; b &amp; c", "https://t.me/c/1/3"):
            self.assertIn(needle, text)

    def test_no_link_line_when_unavailable(self):
        self.assertNotIn("Source message", format_alert(mk(link=None))[0])

    def test_long_message_is_split_without_loss_and_within_limit(self):
        body = ("<&>word " * 2000) + "END"
        chunks = format_alert(mk(body))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= TG_LIMIT for c in chunks))
        self.assertIn("END", chunks[-1])

    def test_split_preserves_content(self):
        text = "line one\nline two & <three>\n" * 500
        self.assertEqual("".join(split_escaped(text, 500)).count("&lt;three&gt;"), 500)

    def test_both_detection_types_listed(self):
        det = Detection(solana=(SAMPLE,), robinhood=("robinhood", "RH-1234"))
        text = format_alert(mk(det=det))[0]
        self.assertIn("Solana CA + Robinhood-related", text)
        self.assertIn("RH-1234", text)

    def test_buttons(self):
        rows = alert_buttons(mk(link="https://t.me/c/1/3"), already_watched=False)
        flat = [b for r in rows for b in r]
        self.assertIn(("👁 Watch this user", "wa:7"), flat)
        self.assertTrue(any("solscan.io" in b[1] for b in flat))
        self.assertNotIn(("👁 Watch this user", "wa:7"), [b for r in alert_buttons(mk(), True) for b in r])


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_rate_limit_then_delivers(self):
        rt = await make_runtime(await make_db())
        sleeps, calls = [], []

        async def fake_sleep(s): sleeps.append(s)

        class RetryAfter(Exception):
            retry_after = 3

        async def send(text, rows):
            calls.append(text)
            if len(calls) == 1:
                raise RetryAfter()

        n = Notifier(rt, pace=0, sleep=fake_sleep)
        n.bind(send)
        await n.enqueue(mk("hello"))
        await n.stop()
        await n.run()
        self.assertEqual(len(calls), 2)
        self.assertIn(4, sleeps)
        self.assertEqual(n.sent, 1)


if __name__ == "__main__":
    unittest.main()
