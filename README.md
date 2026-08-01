# Telegram Recorder Bot V5 (Enterprise & Pro Edition) — Koyeb + GitHub Ready

A high-performance Telegram stream & video recorder bot designed for **Koyeb**, **Docker**, and self-hosted deployments. Upgraded with **Level 1, Level 2, and Level 3 Enterprise Features**, including public webpage stream extraction via `yt-dlp`, Telegram playable video streaming with auto-generated thumbnails, timed scheduled recordings, multi-resolution quality selection, concurrency queue control, Telegram Premium 4GB upload support, and SQLite persistence.

---

## 🔥 What's New in V5 (Why Public URLs Were Failing & How It's Fixed)

* **Bug Fix — Public URL & Direct Stream Extraction (`yt-dlp` Integration):**
  Previously, the bot rejected URLs that did not explicitly contain `.m3u8` or streaming keywords in their substring. V5 automatically resolves public webpage URLs (YouTube, live streaming platforms, OTT links, and indirect media pages) to direct HLS/MP4 streams using `yt-dlp`.
* **URL Normalization (Mirror Domain Support):**
  Automatically converts mirror URLs (e.g., `stripchatgirls.com/username` → `stripchat.com/username`) to their canonical domains so `yt-dlp` specialized extractors recognize them instantly.
* **Web Thumbnail in Status Header & Video Cover:**
  When a public URL is provided, the bot downloads the web thumbnail image and sends the status report as a **Photo Card with Caption** (so the thumbnail displays at the top header of the status). That thumbnail is also applied as the cover image of the uploaded Telegram video!
* **100% FloodWait Protected & Zero Memory Overflow (512MB RAM Koyeb Guard):**
  Built-in Pyrogram rate-limit handlers combined with **Automatic Disk Cleanup** and **Python Garbage Collection (`gc.collect()`)** ensure your Koyeb container or local disk never fills up or crashes.

---

## 🎭 Special Guide: How to Record Ticket Shows & Private Streams (Stripchat, Chaturbate, OTT, etc.)

When recording adult live streams or ticketed/private OTT shows, understand how authentication works:

### **1. Free / Public Live Streams**
* Simply pass the profile or live stream link:
  ```
  /record simran_live https://stripchatgirls.com/Kaur_Simran_01
  ```
* **What happens:** The bot automatically normalizes the domain to `stripchat.com`, invokes `yt-dlp`'s `StripchatIE` extractor, grabs the live `.m3u8` playlist and stream thumbnail, displays the thumbnail header in Telegram, and starts recording.

### **2. Ticket Shows / Private Rooms / Password-Protected Streams**
A public profile link **cannot** access a ticket show unless authenticated. You have two professional ways to record Ticket Shows:

* **Method A: Direct Tokenized `.m3u8` Link (100% Recommended & Foolproof)**
  1. Open your browser (Chrome/Firefox), log in, and enter the Ticket Show room.
  2. Press **F12** to open Developer Tools → **Network Tab** → filter by `m3u8`.
  3. Copy the **Direct Token HLS URL** (e.g., `https://edge-hls.stripchat.com/hls/.../master.m3u8?token=xxxxx`).
  4. Send it to the bot with `Referer` and `User-Agent`:
     ```
     /record kaur_ticket https://edge-hls.stripchat.com/hls/12345/master.m3u8?token=xxxx | Referer: https://stripchat.com/ | User-Agent: Mozilla/5.0...
     ```

* **Method B: Passing Session Cookie via Pipe Syntax**
  * Pass your logged-in browser cookies so `yt-dlp` and `FFmpeg` can authenticate:
    ```
    /record kaur_ticket https://stripchat.com/Kaur_Simran_01 | Cookie: session_id=xxxx; auth_token=yyyy | User-Agent: Mozilla/5.0...
    ```

---

## 🚀 Features Breakdown (Level 1, Level 2 & Level 3)

### **Level 1: Core Pro Features**
1. **🔐 Admin / Owner Authorization:**
   * Protects your bot from unauthorized public use.
   * `.env` variables `OWNER_ID` and `SUDO_USERS` restrict `/record`, `/stop`, and management commands to trusted administrators.
   * Dynamic admin commands: `/addsudo <id>`, `/rmsudo <id>`, and `/sudolist`.
2. **🧹 Automatic Disk Cleanup & Memory Release:**
   * Immediately deletes recorded `.mp4` files, split `.mp4` segments, and `.jpg` thumbnails from `recordings/` and `splits/` after upload completion or job cancellation.
   * Calls `gc.collect()` to release RAM back to Linux OS on 512MB RAM servers.
3. **🎬 Telegram Video Streaming & Auto Thumbnail:**
   * Uses `ffprobe` / `ffmpeg` to extract video duration and dimensions.
   * Automatically displays the web thumbnail in the status message header and applies it (or a custom screenshot at 5s) to `send_video(supports_streaming=True)`.
4. **🔘 Interactive Inline Keyboard Buttons:**
   * Every active recording includes smart control buttons: `[🛑 Stop]`, `[📊 Refresh]`, and `[🗑 Cancel & Delete]`.

### **Level 2: Advanced Stream Recording Features**
5. **🌐 Custom Headers / Referer / Cookie / User-Agent Support:**
   * Bypasses 403 Forbidden DRM or protected stream servers by passing HTTP headers to both `yt-dlp` and `FFmpeg`.
   * PIPE syntax: `/record <name> <url> | Referer: https://example.com | Cookie: ... | User-Agent: ...`
6. **⏱️ Timed / Scheduled Recording (Auto-Stop Timer):**
   * Specify a recording duration limit in seconds (`s`), minutes (`m`), or hours (`h`).
   * *Example:* `/record my_match 90m https://example.com/live.m3u8` automatically stops recording after 90 minutes and uploads the video.
7. **🎛️ Quality / Resolution Selection:**
   * Inspect available stream qualities using `/qualities <url>`.
   * Record at specific quality tiers: `| q=best`, `| q=1080p`, `| q=720p`, `| q=480p`, `| q=360p`, or `| q=audio` (audio-only `.m4a`/`.mp3` extraction).
8. **🚦 Job Queue System (512MB RAM Concurrency Control):**
   * Default `MAX_CONCURRENT_JOBS=1` configured for 512MB RAM Koyeb servers.
   * When concurrency slots are full, additional recording requests are placed in an SQLite queue and start automatically when a running job finishes.

### **Level 3: Ultra / Enterprise Level Features**
9. **💎 Telegram Premium 4GB Upload Support:**
   * Configure `STRING_SESSION` (Pyrogram Userbot string session) in `.env`.
   * When enabled, the bot automatically switches the part limit from **1.9 GB** (standard Bot API) to **3.9 GB** (Telegram Premium) and uploads via the Premium account.
10. **📊 System Stats & Server Diagnostics Panel:**
    * Commands `/stats` or `/server` provide real-time CPU usage, RAM memory consumption, Free Disk space, Python/OS info, Bot & Server uptime, and queue diagnostics with a `[🔄 Refresh Stats]` button.
11. **💾 SQLite Persistence & Container Auto-Recovery:**
    * Saves all active recordings, sudo users, and queue state in `recorder.db`.
    * **Container Restart Recovery:** If your Koyeb container or server restarts while a job is finishing, the bot checks on startup for completed `.mp4` files and automatically resumes uploading them to Telegram.

---

## 📜 Complete Command Reference

| Command | Usage | Description |
| :--- | :--- | :--- |
| `/record` | `/record <name> <url> [time] [\| Referer: ... \| q=720p]` | Start a recording or queue it if server is busy |
| `/qualities` | `/qualities <url>` | Inspect available resolutions and stream formats |
| `/status` | `/status` | Display active recordings with interactive control buttons |
| `/queue` | `/queue` | Display waiting jobs in the concurrency queue |
| `/stop` | `/stop <job_name>` | Gracefully stop an active recording and upload immediately |
| `/stats` | `/stats` (or `/server`) | View server CPU, RAM, Disk, and bot uptime diagnostics |
| `/mode` | `/mode <video\|document>` | Toggle default Telegram upload format |
| `/addsudo` | `/addsudo <user_id>` | Add an authorized sudo administrator (Owner only) |
| `/rmsudo` | `/rmsudo <user_id>` | Remove an authorized sudo administrator (Owner only) |
| `/sudolist` | `/sudolist` | Display all authorized admin / sudo user IDs |

---

## 🛠️ Deployment Instructions

### 1. Koyeb Deployment (GitHub Integration)
1. Push this repository to your GitHub account.
2. In [Koyeb Dashboard](https://app.koyeb.com):
   * **Create Service** → **GitHub** → Select your repo.
   * **Builder:** Dockerfile
   * **Port:** `8080`, **Protocol:** `HTTP`, **Path:** `/health`
3. Configure **Environment Variables** in Koyeb:
   ```env
   API_ID=your_api_id
   API_HASH=your_api_hash
   BOT_TOKEN=your_bot_token
   OWNER_ID=your_telegram_id
   SUDO_USERS=id1,id2
   PORT=8080
   MAX_CONCURRENT_JOBS=1
   DEFAULT_UPLOAD_MODE=video
   # Optional: STRING_SESSION=your_userbot_session_for_4gb
   ```
4. Deploy! Koyeb's `/health` check on port `8080` keeps the bot running 24/7.

---

### 2. Local Docker / Docker Compose Deployment
```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit credentials in .env
nano .env

# 3. Build & Run with Docker Compose
docker-compose up --build -d
```
