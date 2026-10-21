import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.helpers import make_db

HAS_TELETHON = importlib.util.find_spec("telethon") is not None


class FakeClient:
    def __init__(self, connect_failures=0, authorized=True):
        self.failures, self.authorized, self.connect_calls, self._connected = connect_failures, authorized, 0, False
        self.handlers = []

    def add_event_handler(self, fn, ev): self.handlers.append(fn)
    def is_connected(self): return self._connected
    async def is_user_authorized(self): return self.authorized
    async def get_me(self): return SimpleNamespace(id=1, username="me", first_name="Me")
    async def disconnect(self): self._connected = False

    async def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.failures:
            raise ConnectionError("network down")
        self._connected = True


def make_uc(fake, key=None, attempts=3):
    from user_client import UserClient
    cfg = SimpleNamespace(max_reconnect_attempts=attempts, reconnect_base_delay=0, session_encryption_key=key,
                          telethon_session="env-session-string", login_timeout=300, api_id=1, api_hash="h")
    async def nosleep(_): return None
    uc = UserClient(cfg, None, None, client_factory=lambda s: fake, sleep=nosleep)
    uc.client = fake
    uc.notify = AsyncMock()
    return uc


@unittest.skipUnless(HAS_TELETHON, "telethon not installed")
class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_after_transient_failures_and_notifies(self):
        fake = FakeClient(connect_failures=2)
        uc = make_uc(fake)
        self.assertTrue(await uc.reconnect_cycle())
        self.assertEqual((uc.state, fake.connect_calls), ("connected", 3))
        uc.notify.assert_awaited_once()

    async def test_gives_up_instead_of_looping_forever(self):
        fake = FakeClient(connect_failures=999)
        uc = make_uc(fake, attempts=4)
        self.assertFalse(await uc.reconnect_cycle())
        self.assertEqual((uc.state, fake.connect_calls), ("failed", 4))
        self.assertIn("Could not reconnect", uc.notify.await_args.args[0])

    async def test_revoked_session_stops_retrying_and_asks_to_relink(self):
        fake = FakeClient(authorized=False)
        uc = make_uc(fake)
        self.assertFalse(await uc.reconnect_cycle())
        self.assertEqual((uc.state, fake.connect_calls), ("unauthorized", 1))
        self.assertIn("re-link", uc.notify.await_args.args[0])


@unittest.skipUnless(HAS_TELETHON, "telethon not installed")
class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_is_encrypted_at_rest_and_roundtrips(self):
        from cryptography.fernet import Fernet
        from user_client import SESSION_KEY
        uc = make_uc(FakeClient(), key=Fernet.generate_key().decode())
        uc.db = await make_db()
        await uc._save_session("super-secret-session")
        stored = await uc.db.get_state(SESSION_KEY)
        self.assertNotIn("super-secret", stored)
        self.assertEqual(await uc._load_session(), "super-secret-session")

    async def test_env_session_used_when_database_empty(self):
        uc = make_uc(FakeClient())
        uc.db = await make_db()
        self.assertEqual(await uc._load_session(), "env-session-string")
        self.assertIn("env", uc.session_source)

    async def test_remote_login_requires_encryption_key(self):
        from user_client import LoginError
        uc = make_uc(FakeClient(), key=None)
        with self.assertRaises(LoginError):
            await uc.begin_login("+15551234567")

    async def test_expired_login_attempt_is_rejected(self):
        from user_client import LoginError, _Login
        uc = make_uc(FakeClient())
        uc._login = _Login(client=FakeClient(), phone="+1", phone_code_hash="h", expires=0.0)
        with self.assertRaises(LoginError):
            await uc.submit_code("1 2 3 4 5")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_TELETHON, "telethon not installed")
class CipherKeyTests(unittest.TestCase):
    def test_any_string_works_and_is_stable(self):
        from user_client import make_cipher
        a, b = make_cipher("render-generated-random-string"), make_cipher("render-generated-random-string")
        self.assertEqual(b.decrypt(a.encrypt(b"session")), b"session")
        from cryptography.fernet import Fernet
        k = Fernet.generate_key().decode()
        self.assertEqual(make_cipher(k).decrypt(Fernet(k.encode()).encrypt(b"x")), b"x")
