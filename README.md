# ◆ Shill Monitor

A private, read-only Telegram scanner. Your **personal account** (via Telethon) listens to the groups and channels you're already in; when a message contains a valid **Solana address** or a **Robinhood keyword/pattern you configured**, **Shill Monitor** (your own bot) sends you an alert. The whole UI lives inside Telegram — no website.

It never trades, signs, transfers, or touches contracts. Alert buttons are plain links to Solscan / DexScreener / Birdeye.

---

## What it looks like

```
◆ SHILL MONITOR
━━━━━━━━━━━━━━━━━━
Dashboard

🟢 Live · @you

Mode   👁 Watchlist only (3)
Chats  All chats
Detect ◎ Solana ✓  ·  🅡 Robinhood ✓

[🔗 Connect Account] [📡 Scanner Status]
[👁 Watchlist]       [🎯 Detection Filters]
[💬 Monitored Chats] [⏸ Pause]
[❔ Help]
```

Everything is one dashboard message that **edits in place** as you tap around (no chat spam). Alerts look like:

```
🚨 NEW ALPHA SIGNAL
━━━━━━━━━━━━━━━━━━
Detection  Solana CA + Robinhood-related
Sender     @example_user
User ID    123456789
Source     Example Trading Group · -1001234567890
Time       2026-10-12 14:30 UTC

◎ Detected address
86Uzv2fizy66zvjokCnwCRvGX8U3BFY8SRkw18eWZhwX      (tap → DexScreener)

🅡 Robinhood-related match
• robinhood

🔗 Source message
https://t.me/c/1234567890/42

Original message
▌ (collapsible block with the full text)

[🔗 Open message]
[📋 Copy] [DexScreener] [Solscan] [Birdeye]
[👁 Watch this user]
```

Long messages are split into safe chunks; no link is shown unless Telegram can actually resolve one.

---

## Project layout

```
main.py            entry point – ONE asyncio loop: health server, bot polling, Telethon, background tasks
bot.py             Shill Monitor bot: screens, commands, callbacks, login conversation, admin gate
user_client.py     Telethon user client: encrypted session storage, in-bot login, reconnect supervisor
scanner.py         event -> filters -> detection -> dedup -> alert
detectors.py       Solana + Robinhood detection (pure functions)
sources.py         parses @names / t.me links / invite links / -100… ids you paste into the bot
alerts.py          Alert model, HTML rendering, message splitting, link building
notifier.py        alert queue with rate-limit / retry handling
state.py           in-memory settings cache (hot path never hits the DB)
database.py        PostgreSQL (asyncpg) / SQLite (dev) layer + migrations + singleton lock
health.py          /healthz for Render
config.py          env var loading        logging_setup.py   structured logs with secret redaction
scripts/generate_session.py   create the Telethon session locally (recommended)
tests/             unit tests (unittest; pytest also works)
render.yaml        Render Blueprint        requirements*.txt  .env.example  .gitignore
```

---

## Add sources, pick members, forward to Ferg Bot

Everything below is done from the bot — no env vars or redeploys.

**Add a channel or group** — Dashboard → ➕ Add Channel/Group (or `/addchat`). Send one or more (space/line separated) of
`@name`, `t.me/name`, a private invite link `t.me/+…`, or a `-100…` ID. If your linked account is already in the chat it is
watched immediately. If not, the bot shows what it found and asks before joining — it joins only after you tap
**✅ Join & watch**. Tip: with *Scope = All chats* everything is scanned anyway; tap **🎯 Use Selected only** to scan just
the chats you pick.

**Pick users inside a group** — Monitored Chats → tap **👥** next to a chat. Tap people to add/remove them from the
watchlist (✅ = watched), **🔎 Search** to filter by name/@username. The watchlist is global; set Mode to *Watchlist only*
to alert only on those users. Telegram lets only admins list members of some groups/large channels — the bot tells you
when that applies and you can still add people by `@username`/ID.

**Clickable CAs** — every address in an alert (and in the group post) is a link to DexScreener, and has a row of
buttons: 📋 Copy · DexScreener · Solscan · Birdeye. Multiple addresses get one row each.

**Forward to your Ferg Bot group** — every detected Solana CA from any monitored source is posted to the group as
*source name (linked to the message when Telegram can resolve it) + clickable CA + buttons*. Only that leaves the bot — no
message text, no sender. Dashboard → 📤 Forward to toggle, change the group, or **🧪 Send test**.
**The bot must be added to the group** (as admin if the target is a channel). Default target is `@fergbotalgogc`
(`FORWARD_CHAT`); Robinhood-only matches and test alerts are not forwarded.

*Loop protection:* the scanner ignores messages sent by this bot and anything in the forward group, so a CA posted to the
group can never trigger a new alert, even if your own account is a member of it and Scope is *All chats*.

---

## 1. Create the bot

1. In Telegram open **@BotFather** → `/newbot`.
2. Name: **Shill Monitor**; pick a username ending in `bot`. Copy the token → `TELEGRAM_BOT_TOKEN`.
3. (Optional) `/setdescription`, `/setuserpic`. The app also sets the name, short description and command menu itself on startup.
4. Get your numeric user ID from **@userinfobot** → `AUTHORIZED_USER_ID`. Only this ID can use the bot; everyone else gets "⛔ This is a private bot."
5. Open your bot and press **Start** once so it is allowed to message you.

## 2. Get Telegram API credentials

1. Sign in at <https://my.telegram.org> with your phone number.
2. **API development tools** → create an application (any name).
3. Copy **api_id** → `TELEGRAM_API_ID` and **api_hash** → `TELEGRAM_API_HASH`.

---

## 3. Connecting your personal account (read this)

A Telethon session is a **full login to your Telegram account**. Whoever holds it can read your chats. Two options:

### Option A — generate locally (recommended)

```bash
pip install telethon
export TELEGRAM_API_ID=...   TELEGRAM_API_HASH=...
python scripts/generate_session.py
```
Enter your phone, the code, and your 2FA password **in your own terminal**. Copy the printed string into the `TELETHON_SESSION` secret on Render. Nothing secret ever passes through a Telegram chat.

### Option B — link inside the bot (convenient, riskier)

Set `SESSION_ENCRYPTION_KEY` (any long random string; the Render Blueprint generates one for you automatically), then **Dashboard → Connect Account → Link with phone number**.

Risks, plainly: your login code and 2FA password transit Telegram's bot channel and the Render process (briefly, in memory). Mitigations built in: only your user ID can start it; state expires after 5 minutes; the bot **deletes your messages immediately**; codes/passwords are never logged or stored; the resulting session is **Fernet-encrypted** before it is written to PostgreSQL.

> **Type the code with spaces — `1 2 3 4 5`.** Telegram automatically invalidates login codes that appear in plain text in a chat. The bot strips the spaces for you.

If `SESSION_ENCRYPTION_KEY` is not set, Option B is disabled (the bot tells you why).

**Which session wins?** The encrypted one in the database (created by the bot) is used first, then `TELETHON_SESSION`.

### Reconnecting after a session expires

You'll get a "🔴 session is no longer valid" message. Then either re-run `generate_session.py` and update `TELETHON_SESSION` on Render (redeploys automatically), or use **Connect Account** again. If an old session is stuck in the DB, use **Disconnect** first (revokes it on Telegram and deletes the stored copy). If you also set `TELETHON_SESSION`, remove it there too.

Transient drops are handled automatically: exponential backoff (5s → 300s cap), honouring FloodWait, and it **gives up after `MAX_RECONNECT_ATTEMPTS`** (default 8) and tells you, instead of looping forever. Tap **Scanner Status → Reconnect** to retry.

---

## 4. Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # fill it in; DATABASE_URL may stay as the SQLite default for local dev
set -a; source .env; set +a
python main.py
```
Open your bot → `/start`.

## 5. Tests

```bash
python -m unittest discover -s tests -t .      # or:  pytest
```
All Telegram interaction is mocked; no real account needed. Covered: Solana validation, multiple addresses, Robinhood keyword/regex, combined detection, watchlist vs all-users filtering, duplicate prevention (re-delivery, restart, concurrent claims), edits, DB init/persistence/retention, unauthorized rejection, reconnect/backoff/give-up, session encryption, log redaction, long-message splitting, rate-limit retry.

---

## 6. Deploy to Render

**Service type.** The default `render.yaml` uses a **Web Service** because it gives Render a health check (`/healthz`, which reports liveness only — never account data). A **Background Worker** also works: `PORT` is unset, so the health server simply doesn't start, and there's no health check. Either way you need a **paid instance** — free web services spin down when idle (monitoring would stop). Free Postgres has historically expired; use a paid database. *Verify plan names and prices on Render's pricing page; `render.yaml` uses `starter` / `basic-256mb`.*

**No persistent disk needed.** The session lives in PostgreSQL (encrypted) or a Render secret — never on the local filesystem.

**Zero-downtime deploys are safe.** Two processes using one Telegram session can get it killed (`AuthKeyDuplicated`). On startup each instance takes a PostgreSQL advisory lock; a new instance reports healthy but stays in *standby* until the old one exits. You may see a brief bot "Conflict" warning during a deploy — harmless.

### Checklist

1. Push this repo to GitHub (private). Confirm `.env` and `*.session` are not committed.
2. Render → **New → Blueprint** → select the repo (or create a Postgres + Web Service manually).
3. Set secrets (`sync: false` ones prompt you): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `AUTHORIZED_USER_ID`, and either `TELETHON_SESSION` (Option A) or `SESSION_ENCRYPTION_KEY` (Option B).
4. `DATABASE_URL` comes from the database via the Blueprint (use the **Internal** connection string).
5. Build: `pip install -r requirements.txt` · Start: `python main.py` · Health check path: `/healthz` · `PYTHON_VERSION=3.11.9`.
6. Deploy; watch logs for `Bot polling started`, `Telethon connected`, `Shill Monitor running`.
7. Open the bot → `/start` → `/status`.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot from @BotFather |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | ✅ | my.telegram.org credentials for Telethon |
| `AUTHORIZED_USER_ID` | ✅ | The only user allowed to use the bot |
| `DATABASE_URL` | ✅ | Postgres URL (SQLite URL allowed for local dev) |
| `TELETHON_SESSION` | one of | Locally generated session string (Option A) |
| `SESSION_ENCRYPTION_KEY` | one of | Fernet key; enables in-bot login + encrypted storage (Option B) |
| `LOG_LEVEL` | – | `INFO` default |
| `RETENTION_DAYS` | – | Processed-message retention (default 14) |
| `MAX_RECONNECT_ATTEMPTS`, `RECONNECT_BASE_DELAY` | – | Reconnect policy (8, 5s) |
| `PORT` | auto | Set by Render Web Services |

Keep `SESSION_ENCRYPTION_KEY` stable — changing it makes the stored session undecryptable (re-link).

---

## 7. Testing alerts

1. **`/testalert`** pushes a sample alert (sample address `86Uzv2fizy66zvjokCnwCRvGX8U3BFY8SRkw18eWZhwX`, plus a Robinhood match) through the real formatting + delivery path. It does **not** exercise Telethon.
2. **End-to-end:** have a second account post the sample address in a group you're both in. Messages *you* send are ignored by default (they're "outgoing"); to self-test, enable **Monitored Chats → 🙋 My own msgs** temporarily, post the address, then turn it off.
3. If nothing arrives, check `/status`: Telethon must be 🟢, not paused, mode/scope must include that sender/chat.

Private 1-to-1 chats are off by default (**Monitored Chats → 💬 Private chats**).

---

## Using it

- **Mode:** *All users* or *Watchlist only* — switch any time, no restart (Watchlist screen, Filters screen, or `/mode`).
- **Watchlist:** `/adduser @name` resolves the username through your account (needs the account linked) and stores the **numeric ID** as the identity. If a username can't be resolved, send the numeric ID. Tap **👁 Watch this user** on any alert to add the sender.
- **Chats:** scope *All chats* (default) or *Selected only* with per-chat toggles.
- **Robinhood detection** is entirely user-defined — no address format is assumed. Default keyword: `robinhood` (whole word, case-insensitive; plain "RH" never matches on its own). Add keywords via **Filters → ➕ Keyword** or `/addkeyword`, and identifiers via `/addregex <pattern>` (case-insensitive; must not match empty text; ≤200 chars). Example: `\bRH-[A-Z0-9]{4,}\b`. Regexes are yours, so avoid catastrophic patterns.
- **Solana detection:** base58 candidates of 32–44 chars validated as real 32-byte public keys (`solders`, with a built-in fallback). A valid key is a *candidate* — not proof of a token mint or of legitimacy. A small **known-program filter** (System/Token/ATA/Memo/Sysvar/wSOL/USDC/USDT…) is on by default and toggleable; it only drops those exact addresses.
- **Duplicates:** keyed on `(chat_id, message_id, match signature)` with a DB unique constraint. Restarts and re-delivery never re-alert; an *edit* that adds a new address does alert once.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Configuration error: Missing required environment variable` | Set the named variable on Render. |
| "⛔ This is a private bot." for you | `AUTHORIZED_USER_ID` is wrong — use the ID from @userinfobot. |
| Bot silent | Press Start in the bot chat; check logs for `Bot polling started`; make sure the token is current. |
| Logs: `Conflict ... another instance` | Normal for a few seconds during deploys. If persistent, another copy (e.g. your laptop) is polling the same token — stop it. |
| Login code "expired"/"invalid" immediately | You typed it without spaces. Use `1 2 3 4 5`. |
| "Remote login is disabled" | Set `SESSION_ENCRYPTION_KEY`, or use Option A. |
| 🔴 Session expired / unauthorized | Revoked from Telegram → *Devices*, or password changed. Re-link (see above). |
| `AuthKeyDuplicated` | The same session is in use elsewhere (your laptop run?). Stop the other copy and re-link. |
| `Stored session could not be decrypted` | `SESSION_ENCRYPTION_KEY` changed. Restore it or re-link. |
| 🔴 Connection failed after N attempts | Network/Telegram outage. Tap Reconnect in Status. |
| `FloodWait` in logs | Telegram asked us to slow down; the client waits it out automatically. |
| DB errors / "Database unavailable" at boot | The app retries with backoff. Verify `DATABASE_URL` is the *Internal* URL and the DB is in the same region. |
| Alerts missing for a channel | The account must be able to read it. Messages sent while the service was down are **not** replayed. |
| Deploy "failed health check" | Check start logs; `/healthz` must answer on `$PORT` (Web Service). |

---

## Audit notes & honesty section

**Verified in development:** all 52 tests pass. 37 run on the standard library alone; the 15 that need python-telegram-bot / Telethon were executed against minimal *stand-in* modules, which validates this project's own logic and imports but **not** the real libraries' APIs.

**Not verified (cannot be from here, and you should not assume otherwise):** running against real Telegram, real PostgreSQL/asyncpg, or a real Render deployment; compatibility with the latest releases of `python-telegram-bot`, `telethon`, `aiohttp`, `solders` (requirements use compatible ranges, not pins); `<blockquote expandable>` rendering on very old Telegram clients. Do a first run locally and watch the logs before relying on it.

**Design review checklist (done):** one event loop; PTB driven via `initialize/start/start_polling` (not `run_polling`, which would own the loop); no duplicate polling or competing Telethon connections (advisory lock); graceful SIGTERM shutdown in reverse order; no local-file persistence; secrets redacted from formatted log lines *including tracebacks* (and httpx request logging silenced); no endpoints expose account data; read-only by construction.

**Known limits:** alerts queued in memory (max 500) are lost if the process dies before delivery; backlog during downtime isn't replayed; one operator only (the `AUTHORIZED_USER_ID` check is strict by design).
