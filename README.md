# Telegram Recorder Bot V4 - Koyeb + Github Ready

**Features:**
- No 50MB limit (uses API_ID/API_HASH MTProto) - 2GB/4GB tak
- Auto split >1.9GB to multiple parts
- Perfect progress bar: recording + uploading
- FloodWait protection (bot hang nahi hoga)
- Health check on PORT 8080 for Koyeb
- Recording till offline + auto send

**Only for your own content or with written consent**

## Koyeb Deploy Steps

1. Github pe repo banao aur ye files push karo:
   - main.py
   - requirements.txt
   - Dockerfile
   - .env.example

2. Koyeb.com pe:
   - Create Service -> From Github
   - Builder: Dockerfile
   - Port: 8080, Protocol: http, Path: /health
   - Env Variables:
     ```
     API_ID=your_id
     API_HASH=your_hash
     BOT_TOKEN=your_bot_token
     PORT=8080
     ```
   - Deploy

3. Health check: `https://your-app.koyeb.app/health` should show "Bot Alive"

## Local Docker
```bash
cp .env.example .env  # edit
docker-compose up --build
# or
docker build -t recorder .
docker run -p 8080:8080 --env-file .env -v ./recordings:/app/recordings recorder
```

## Commands in Telegram
- `/record <name> <direct_m3u8_link>` - e.g. `/record my_show https://.../playlist.m3u8`
- `/status` - active recordings
- `/stop <name>`


## FloodWait Fix
Bot me har `edit_message` aur `send_document` pe FloodWait catch hai + 1.5 sec delay + retry. Isse Koyeb pe hang nahi hoga.
