"""Entry point: one asyncio loop runs the health server, bot polling, Telethon and background tasks."""
from __future__ import annotations

import asyncio
import logging
import signal

from bot import BotApp
from config import ConfigError, load_config
from database import Database
from health import HealthServer
from logging_setup import setup_logging
from notifier import Notifier
from scanner import Scanner
from state import RuntimeState
from user_client import UserClient

log = logging.getLogger("shill_monitor")


async def connect_db(db: Database, stop: asyncio.Event) -> bool:
    delay = 2.0
    while not stop.is_set():
        try:
            await db.connect()
            await db.init_schema()
            return True
        except Exception as exc:
            log.error("Database unavailable (%s); retrying in %.0fs", type(exc).__name__, delay)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)
    return False


async def purge_loop(db: Database, days: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            removed = await db.purge_processed(days)
            if removed:
                log.info("Purged %s processed-message records", removed)
        except Exception as exc:
            log.warning("Purge failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), 6 * 3600)
        except asyncio.TimeoutError:
            pass


async def guard_loop(db: Database, stop: asyncio.Event) -> None:
    """If the DB connection holding the singleton lock dies, exit so Render restarts us cleanly."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            if not await db.singleton_lock_alive():
                log.error("Lost the singleton database lock; shutting down for a clean restart")
                stop.set()


async def run() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")
    setup_logging(config.log_level, config.secrets())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    db = Database(config.database_url)
    role = {"value": "starting"}
    components: dict = {}

    def snapshot() -> dict:
        uc = components.get("uc")
        return {"status": "ok", "role": role["value"], "telethon": uc.state if uc else "n/a"}

    # Health first: during a Render zero-downtime deploy the new instance must be "healthy"
    # while it waits for the old instance to release the singleton lock.
    health = HealthServer(config.port, snapshot) if config.port else None
    if health:
        await health.start()

    tasks: list[asyncio.Task] = []
    bot = None
    uc = None
    try:
        if not await connect_db(db, stop):
            return
        runtime = RuntimeState(db)
        await runtime.ensure_defaults(config.forward_chat)
        await runtime.refresh()

        role["value"] = "standby"
        while not stop.is_set():
            if await db.try_singleton_lock():
                break
            log.info("Another instance holds the lock; standing by")
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except asyncio.TimeoutError:
                pass
        if stop.is_set():
            return
        role["value"] = "active"

        notifier = Notifier(runtime)
        scanner = Scanner(db, runtime, notifier)
        uc = UserClient(config, db, scanner)
        components["uc"] = uc
        bot = BotApp(config, db, runtime, uc, scanner, notifier)
        bot.build()
        notifier.bind(bot.send_to_admin)
        notifier.bind_forward(bot.send_to_group)
        notifier.on_forward_chat = lambda cid: runtime.set("forward_chat_id", str(cid))
        uc.notify = notifier.notice

        await bot.start()
        await uc.start()
        tasks = [asyncio.create_task(notifier.run(), name="notifier"),
                 asyncio.create_task(notifier.run_forward(), name="forwarder"),
                 asyncio.create_task(uc.supervise(stop), name="supervisor"),
                 asyncio.create_task(purge_loop(db, config.retention_days, stop), name="purge"),
                 asyncio.create_task(guard_loop(db, stop), name="lock-guard")]
        await notifier.notice(f"🟢 <b>Shill Monitor</b> started · Telethon: <code>{uc.state}</code>")
        log.info("Shill Monitor running")
        await stop.wait()
    finally:
        log.info("Shutting down")
        role["value"] = "stopping"
        if bot is not None:
            await bot.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if uc is not None:
            await uc.stop()
        await db.close()                  # releases the singleton lock
        if health:
            await health.stop()


if __name__ == "__main__":
    asyncio.run(run())
