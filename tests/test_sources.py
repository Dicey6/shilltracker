import unittest

from sources import ChatRef, forward_target, parse_chat_ref


class ParseChatRef(unittest.TestCase):
    def test_usernames_and_links(self):
        for raw in ("@alphacalls", "alphacalls", "t.me/alphacalls", "https://t.me/alphacalls/",
                    "https://t.me/alphacalls/123", "http://telegram.me/alphacalls", "https://t.me/s/alphacalls",
                    "https://t.me/alphacalls?start=x"):
            self.assertEqual(parse_chat_ref(raw), ChatRef("username", "alphacalls"), raw)

    def test_invite_links(self):
        self.assertEqual(parse_chat_ref("https://t.me/+AbCdEfGh12"), ChatRef("invite", "AbCdEfGh12"))
        self.assertEqual(parse_chat_ref("t.me/joinchat/AbCdEfGh12"), ChatRef("invite", "AbCdEfGh12"))

    def test_private_message_link_and_numeric_id(self):
        self.assertEqual(parse_chat_ref("https://t.me/c/1234567890/42"), ChatRef("id", -1001234567890))
        self.assertEqual(parse_chat_ref("-1001234567890"), ChatRef("id", -1001234567890))

    def test_garbage_is_rejected(self):
        for raw in ("", "   ", "hi", "12345", "https://example.com/x", "tg://resolve?domain=x", "@a", "not a link"):
            self.assertIsNone(parse_chat_ref(raw), raw)


class ForwardTarget(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(forward_target("https://t.me/fergbotalgogc"), "@fergbotalgogc")
        self.assertEqual(forward_target("@fergbotalgogc"), "@fergbotalgogc")
        self.assertEqual(forward_target("-1001234567890"), -1001234567890)

    def test_invite_links_cannot_be_bot_targets(self):
        self.assertIsNone(forward_target("https://t.me/+AbCdEfGh12"))
        self.assertIsNone(forward_target("nonsense here"))


if __name__ == "__main__":
    unittest.main()
