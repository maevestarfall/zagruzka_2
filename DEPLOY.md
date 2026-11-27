# Lilush bot — deployment guide

The bot is **portable**: same code base, two officially supported
hosts. Pick whichever fits your budget / control needs.

| Host | Cost | Always-on | Setup work | Best for |
|---|---|---|---|---|
| Render            | Free / Starter $7 | Free: idle after 15 min (self-ping keeps it warm) | None — Blueprint | "I want it running by lunch" |
| Oracle Cloud Free Tier | $0 forever | Yes | ~30 min one-time provisioning | "I want full control + always-on for free" |

Both paths use the **same env vars** and the **same** `bot.config.resolve_public_url`
fallback chain:

```
PUBLIC_URL  →  RENDER_EXTERNAL_URL  →  RAILWAY_PUBLIC_DOMAIN
            →  FLY_APP_NAME         →  ORACLE_PUBLIC_HOST
```

So switching hosts later is a config change, not a code change.
Tests guarding this are in `tests/test_public_url_resolution.py`.

The web surfaces live under `<PUBLIC_URL>/...`:

* `/healthz`        — liveness probe (`{"ok": true}`)
* `/test/<token>/`  — «🧪 Тест» preview slot (token-gated, owner-only)
* `/fable5/<token>/` — Fable5 live-access for Viktor (token-gated)
* `/masterroom/<token>/` — *reserved namespace* (no routes yet)
* `/tg/<token-prefix>` — Telegram webhook (only in `BOT_MODE=webhook`)

---

## Path A — Render (Blueprint)

The repo ships with `render.yaml`, so this is the fastest path.

1. **Fork** the repo into your GitHub account.
2. [Render Dashboard](https://dashboard.render.com) → **New + → Blueprint**
   → pick your fork → **Apply**.
3. After the service is created, open it → **Environment** and set:
   * `BOT_TOKEN` — from @BotFather
   * `OPENROUTER_API_KEY` (and/or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`)
   * Leave `PUBLIC_URL` empty — Render auto-injects `RENDER_EXTERNAL_URL`
     and `resolve_public_url()` picks it up.
4. **Manual deploy** → wait for "Live".
5. Open Telegram → `/start` your bot.

Free-tier note: Render Free idles your service after 15 min without
traffic. The bot self-pings to stay warm — see `KEEP_ALIVE_INTERVAL`
and `KEEP_ALIVE_MIN_SECONDS` in `bot/config.py`. If you upgrade to a
paid plan (no idle), set `KEEP_ALIVE_INTERVAL=0` to disable the loop.

To use webhook mode instead of polling:

* Render → Environment → `BOT_MODE=webhook`.
* `resolve_public_url()` returns Render's URL automatically; webhook
  registration happens in `bot/main.py::main_webhook`.

---

## Path B — Oracle Cloud Free Tier

Full step-by-step lives in [`deploy/oracle/README.md`](deploy/oracle/README.md).
Short version:

```bash
# On a fresh Ubuntu 22.04 VM, public-IP'd, with bot.example.com → IP
sudo apt install -y python3.11 python3.11-venv git nginx \
                    certbot python3-certbot-nginx ffmpeg
sudo useradd --system --create-home --shell /bin/bash lilush
sudo -u lilush git clone <repo-url> /opt/lilush-bot
cd /opt/lilush-bot && sudo -u lilush python3.11 -m venv .venv \
    && sudo -u lilush .venv/bin/pip install -e .

sudo cp deploy/oracle/.env.example /etc/lilush-bot.env
sudo chmod 600 /etc/lilush-bot.env
sudo nano /etc/lilush-bot.env          # BOT_TOKEN, PUBLIC_URL=https://bot.example.com

sudo cp deploy/oracle/lilush-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lilush-bot

sudo cp deploy/oracle/nginx.conf /etc/nginx/sites-available/lilush-bot
sudo ln -s /etc/nginx/sites-available/lilush-bot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d bot.example.com --non-interactive --agree-tos \
             -m you@example.com

curl https://bot.example.com/healthz   # → {"ok": true}
```

The provided systemd unit:

* runs the bot as the `lilush` user (no root privileges)
* caps memory at 2 GB (bump if you're on the 24 GB Ampere shape)
* restarts on crash with a 5-second back-off
* logs to journald (`journalctl -u lilush-bot -f`)

The provided nginx config:

* terminates TLS on 443 (certbot fills in the cert paths)
* redirects HTTP → HTTPS
* proxies `/`, `/healthz`, `/test/`, `/fable5/`, `/masterroom/`,
  `/tg/` to the bot's aiohttp on `127.0.0.1:8080`
* tunes `proxy_read_timeout` per surface (60s for Test, 130s for
  Fable5 to accommodate its 120s exec cap)

Oracle Free Tier doesn't idle the VM, so you can safely set
`KEEP_ALIVE_INTERVAL=0` to disable the self-ping (the default in
`deploy/oracle/.env.example`).

---

## Switching between hosts

Because there are no platform hardcodes:

| You move from | → | What changes |
|---|---|---|
| Render → Oracle | set `PUBLIC_URL`, follow Path B | nothing in code |
| Oracle → Railway | set `RAILWAY_PUBLIC_DOMAIN` or `PUBLIC_URL` | nothing in code |
| Oracle → Fly    | set `FLY_APP_NAME` or `PUBLIC_URL` | nothing in code |

State (owner_id, keys, addon flags, Fable5 / Test tokens) lives in
`$DATA_DIR/state.json`. Copy that file across when you migrate
hosts and the bot wakes up with the same identity.

---

## Sanity checks after deploy

```bash
# 1. Health probe
curl -s https://<your-host>/healthz
# {"ok": true}

# 2. Test slot is gated (expect 403 before you generate a token)
curl -s -o /dev/null -w "%{http_code}\n" https://<your-host>/test/nonsense/
# 403

# 3. In Telegram, open ⚙️ Настройки → 🧠 Перенастроить мозг → 🌀 Fable5
#    → «Сгенерировать ссылку», then test that URL with curl:
curl -s https://<your-host>/fable5/<token>/ping
# {"ok": true, ...}

# 4. In Telegram, open the main menu → 🧪 Тест → «Сгенерировать ссылку»,
#    open the printed URL in a browser — splash page should render.
```

If any of the above misbehaves, the first thing to check is
`PUBLIC_URL` (or the auto-detected platform var) — that's the single
source of truth for every link the bot mints.
