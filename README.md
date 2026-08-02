# Public Live Recorder Bot — Button-only UI

A focused Telegram bot for recording **publicly accessible and authorized** individual live-room URLs from `xham.live` and `stripchat.com`, then uploading the finalized media to Telegram.

## No commands needed

Send an individual room URL in a private message. The bot replies with a control panel:

- **Start 15 / 30 / 60 min**
- **Start maximum** (owner-configured maximum)
- **Check public stream**
- **Live Status**
- **Stop & Upload**
- **Close**

## Scope and safety

The bot uses normal public playback resolution only. It deliberately has no login automation, cookies, account credential handling, paywall/private-room bypass, DRM bypass, or Cloudflare/bot-check bypass. Use it only where you are authorized and where it complies with platform terms and applicable law.

## Setup

```bash
cp .env.example .env
# fill API_ID, API_HASH, BOT_TOKEN, OWNER_IDS
pip install -r requirements.txt
sudo apt-get install -y ffmpeg
python -m livebot
```

Or `docker compose up --build -d` after creating `.env`.
