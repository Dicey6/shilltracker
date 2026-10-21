import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database  # noqa: E402
from state import RuntimeState  # noqa: E402

SAMPLE = "86Uzv2fizy66zvjokCnwCRvGX8U3BFY8SRkw18eWZhwX"
SAMPLE2 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # valid, but a known program
OTHER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # valid USDC mint (known filter)


async def make_db(path=":memory:"):
    db = Database(f"sqlite:///{path}")
    await db.connect()
    await db.init_schema()
    return db


async def make_runtime(db):
    rt = RuntimeState(db)
    await rt.ensure_defaults()
    await rt.refresh()
    return rt


class FakeNotifier:
    def __init__(self):
        self.items = []

    async def enqueue(self, item):
        self.items.append(item)


def make_event(text, *, chat_id=-1001234567890, msg_id=1, sender_id=111, private=False, out=False,
               username="alice", chat_username=None, fwd=False):
    msg = SimpleNamespace(message=text, id=msg_id, sender_id=sender_id, out=out,
                          date=datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc),
                          fwd_from=object() if fwd else None)
    sender = SimpleNamespace(username=username, first_name="Alice", last_name=None)
    chat = SimpleNamespace(title="Alpha Group", username=chat_username)

    async def get_sender(): return sender
    async def get_chat(): return chat

    return SimpleNamespace(message=msg, chat_id=chat_id, is_private=private, get_sender=get_sender, get_chat=get_chat)
