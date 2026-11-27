# Lilush bot — Oracle Cloud Free Tier deploy

One-time provisioning steps for a fresh Oracle Cloud VM (Ubuntu 22.04
ARM Ampere or x86 — both work; the Ampere shape gives you 4 OCPU /
24 GB RAM free forever).

Files in this folder:

| File | Purpose |
|---|---|
| `lilush-bot.service` | systemd unit (`/etc/systemd/system/`) |
| `nginx.conf`         | reverse-proxy + TLS termination (`/etc/nginx/sites-available/`) |
| `.env.example`       | template for `/etc/lilush-bot.env` |
| `README.md`          | this file |

The high-level path is the same as Render: clone repo → install deps
→ point a public URL at the bot. The difference is *you* run nginx
+ certbot + systemd instead of Render doing it for you. Same code,
no Render-specific hardcodes — see `bot/config.resolve_public_url`
and `tests/test_public_url_resolution.py`.

## 1. Spin up the VM

1. Oracle Cloud Console → **Compute → Instances → Create instance**.
2. Shape: *VM.Standard.A1.Flex* (Ampere ARM, always-free) or
   *VM.Standard.E2.1.Micro* (x86, always-free).
3. Image: **Ubuntu 22.04 LTS Minimal**.
4. Networking: assign a public IPv4. Open ports **22, 80, 443** in
   the VCN security list (default Oracle VCN blocks them).
5. SSH in: `ssh ubuntu@<public-ip>`.

## 2. DNS

Point an A-record at the VM's public IP (e.g. `bot.example.com`).
Required for Let's Encrypt — TLS won't work on bare IP.

## 3. System packages

```bash
sudo apt update
sudo apt install -y \
    python3.11 python3.11-venv python3-pip \
    git nginx certbot python3-certbot-nginx \
    ffmpeg
```

Add a service user (so the bot doesn't run as root):

```bash
sudo useradd --system --create-home --shell /bin/bash lilush
sudo mkdir -p /var/lib/lilush-bot
sudo chown lilush:lilush /var/lib/lilush-bot
```

## 4. Clone repo + install

```bash
sudo -u lilush git clone <repo-url> /opt/lilush-bot
cd /opt/lilush-bot
sudo -u lilush python3.11 -m venv .venv
sudo -u lilush .venv/bin/pip install --upgrade pip
sudo -u lilush .venv/bin/pip install -e .
```

(Using `uv` instead of `pip` works too — just install uv first:
`pipx install uv`, then `sudo -u lilush uv pip install -e .`.)

## 5. Configure environment

```bash
sudo cp /opt/lilush-bot/deploy/oracle/.env.example /etc/lilush-bot.env
sudo chmod 600 /etc/lilush-bot.env
sudo nano /etc/lilush-bot.env       # fill in BOT_TOKEN, PUBLIC_URL, keys
```

At minimum set `BOT_TOKEN` and `PUBLIC_URL=https://bot.example.com`.

## 6. Install systemd unit

```bash
sudo cp /opt/lilush-bot/deploy/oracle/lilush-bot.service \
        /etc/systemd/system/lilush-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now lilush-bot
sudo systemctl status lilush-bot          # should say "active (running)"
```

Live logs: `sudo journalctl -u lilush-bot -f`.

## 7. nginx + TLS

```bash
# Place the site config
sudo cp /opt/lilush-bot/deploy/oracle/nginx.conf \
        /etc/nginx/sites-available/lilush-bot
sudo nano /etc/nginx/sites-available/lilush-bot   # set server_name
sudo ln -s /etc/nginx/sites-available/lilush-bot /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Issue Let's Encrypt cert (auto-edits the nginx config)
sudo certbot --nginx -d bot.example.com \
             --non-interactive --agree-tos -m you@example.com
```

Cert renewal is handled by certbot's systemd timer
(`systemctl status certbot.timer`).

## 8. Verify

```bash
curl https://bot.example.com/healthz                  # → {"ok": true}
```

Open Telegram → message your bot → `/start`. You should be greeted
exactly like on Render. The «🧪 Тест» and «🛠 Master Room» buttons
appear in the main menu for the owner; the rest of the surface is
identical.

## 9. Updates

```bash
cd /opt/lilush-bot
sudo -u lilush git pull
sudo -u lilush .venv/bin/pip install -e .
sudo systemctl restart lilush-bot
```

## Troubleshooting

* **`systemctl status lilush-bot` shows `code=exited, status=203/EXEC`**
  → venv path wrong. Make sure `/opt/lilush-bot/.venv/bin/python` exists.
* **502 from nginx** → bot isn't listening on `$PORT`. Check journal logs.
* **Telegram says "webhook URL is not HTTPS"** → certbot didn't run,
  or `PUBLIC_URL` in `/etc/lilush-bot.env` is `http://...`. Fix and
  `systemctl restart lilush-bot`.
* **Want polling instead of webhook?** Set `BOT_MODE=polling` in
  `/etc/lilush-bot.env`. The aiohttp surface (Test / Fable5 /
  health) still serves on `$PORT`; nginx still proxies it.
