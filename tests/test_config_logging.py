import logging
import unittest

from config import ConfigError, load_config
from logging_setup import RedactingFormatter, redact

ENV = {"TELEGRAM_BOT_TOKEN": "123456789:AAH" + "x" * 32, "TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "abcdef0123456789",
       "AUTHORIZED_USER_ID": "42", "DATABASE_URL": "postgresql://u:p@h/db"}


class ConfigTests(unittest.TestCase):
    def test_loads_and_applies_defaults(self):
        c = load_config(ENV)
        self.assertEqual((c.api_id, c.authorized_user_id, c.retention_days, c.port), (12345, 42, 14, None))

    def test_missing_or_bad_values_fail_clearly(self):
        for key in ENV:
            bad = {k: v for k, v in ENV.items() if k != key}
            if key == "DATABASE_URL":
                continue   # defaults to local SQLite
            with self.assertRaises(ConfigError):
                load_config(bad)
        with self.assertRaises(ConfigError):
            load_config({**ENV, "TELEGRAM_API_ID": "abc"})


class RedactionTests(unittest.TestCase):
    def test_secrets_never_reach_log_output(self):
        c = load_config({**ENV, "TELETHON_SESSION": "1" + "A" * 300})
        fmt = RedactingFormatter("%(message)s", c.secrets())
        rec = logging.LogRecord("t", logging.INFO, "f", 1,
                                f"token {c.bot_token} hash {c.api_hash} session {c.telethon_session} code=12345 password=hunter2",
                                None, None)
        out = fmt.format(rec)
        for leaked in (c.bot_token, c.api_hash, c.telethon_session, "12345", "hunter2"):
            self.assertNotIn(leaked, out)

    def test_unknown_token_shaped_strings_are_redacted(self):
        self.assertNotIn("AAH" + "y" * 35, redact("url /bot987654321:AAH" + "y" * 35 + "/getMe"))


if __name__ == "__main__":
    unittest.main()
