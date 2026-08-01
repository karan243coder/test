# 🤖 SUPER ADVANCED TELEGRAM RECORDER BOT V11 (AD-FIX)

24/7 **Telegram stream recorder bot** — link paste karo, record karega, video Telegram pe bhej dega.
**Stripchat, Chaturbate, YouTube, m3u8, OTT — sab kuch**.

> ⚠️ **Important note:** Recording public streams is allowed only for content you
> have the right to record (public broadcasts / your own streams). Private & ticket
> shows require explicit participation, and recording them without the model's
> consent violates platform ToS. This bot **automatically refuses** to record
> private/ticket shows (AD placeholder detected → blocked).

---

## 🚫 The 20-second AD problem — FIXED (V11)

**Kya hota tha:** Stripchat ne bot ko 20-sec ka `661.6 KB` AD placeholder de diya tha.

**Asli wajah (root cause):** Stripchat ki HLS variant playlist **bina `psch`/`pkey` params**
ke sirf AD VOD deti hai (`#EXT-X-MOUFLON-ADVERT` + `cpa/v2` + 6×4s chunks = exactly
20s / 661KB). Real live stream ke liye:
1. master playlist se `pkey` nikalna padta hai
2. variant URL pe `?psch=v2&pkey=...` lagana padta hai
3. segment URIs `#EXT-X-MOUFLON:URI:` ko matching `pdkey` se decrypt karna padta hai
   (SHA256-XOR) — warna segments 404 dete hain

**V11 ka fix (live test karke prove kiya — real 536KB fMP4 segment download hua):**
- Har variant psch/pkey ke saath fetch hota hai — AD playlist **kabhi** ffmpeg tak nahi pahunchti
- `pkey→pdkey` keys **auto-sync** hoti hain public pool se (`MOUFLON_SYNC_URL`, default
  `https://mouflon.chantrail.com`) + apne `MOUFLON_KEYS` bhi laga sakte ho
- Decrypted playlist local proxy (`/mouflon_proxy`) se serve hoti hai → ffmpeg sirf REAL
  segments download karta hai
- Post-record **AD Guard**: agar 12-28s aur <1.5MB ka file bana → AD samajh kar DELETE +
  alert + auto-retry (AD kabhi chat pe upload nahi hota)
- Private/ticket show → clean error: "🔒 Model PRIVATE show mein hai"

---

## 🔥 Features

| Feature | Status |
|---|---|
| AD-proof Stripchat resolution (MOUFLON decrypt + psch/pkey) | ✅ NEW (V11) |
| Auto key sync + `/keys` `/resynckeys` | ✅ NEW (V11) |
| Post-record AD Guard (20s/661KB discard) | ✅ NEW (V11) |
| 24/7 Auto-Watchlist — `/watch <link>` → live hote hi auto-record + auto-send | ✅ |
| Status transition notifications (offline/private/live) | ✅ |
| Auto-restart on stream drop (HLS expiry) | ✅ |
| Video cover thumbnail + photo status cards | ✅ |
| Auto-split uploads >1.9GB (bot) / >3.9GB (premium userbot) | ✅ |
| Queue system, crash recovery, sudo management, Koyeb health server | ✅ |

---

## 🚀 Setup

### 1. Credentials
- **API_ID / API_HASH** → [my.telegram.org](https://my.telegram.org) (App api tools)
- **BOT_TOKEN** → [@BotFather](https://t.me/BotFather) se
- **OWNER_ID** → [@userinfobot](https://t.me/userinfobot) se apna numeric ID
- *(Optional)* **STRING_SESSION** → premium account ki (4GB uploads). [Generate](https://colab.research.google.com/github/SpEcHiDe/PyroGramSession/blob/master/GenerateStringSession.ipynb) — `pyrogram` session

### 2. Install (local)
```bash
git clone https://github.com/karan243coder/test.git
cd test
cp .env.example .env        # edit karke values daalo
pip install -r requirements.txt
sudo apt install ffmpeg     # agar nahi hai
python main.py
```

### 3. Deploy (Koyeb / Docker)
```bash
docker compose up -d --build
```
Koyeb pe: repo import karo → `main` branch → **Buildpack = Dockerfile**, port `8080`,
env vars `.env` jaisi set karo. Health check: `https://<app>.koyeb.app/health`

---

## 📖 Commands

```
/record <name> <url> [duration]     # manual record
   /record kritika https://stripchatgirls.com/Cute_Kritika69 30m
   /record test https://site.com/stream.m3u8 1h | q=best
   (ya bas URL paste karo — auto record)

/watch <link ya username>           # 24/7 auto-record watchlist
/unwatch <username>
/watchlist                          # list + pause/remove buttons
/watchinterval 120                  # check interval (min 60s)
/check <username>                   # abhi online/private/offline?
/stop <name>   /status   /queue
/stats          # CPU/RAM/disk + total recordings
/keys /resynckeys  # MOUFLON keys status + force sync (AD-proof)
/mode video|document
/clean          # leftover files delete
/addsudo <id>  /rmsudo <id>  /sudolist
```

### Example — Cute_Kritika69
```
/watch https://stripchatgirls.com/Cute_Kritika69
```
Bot har `WATCH_INTERVAL` (default 180s) pe check karega. Model public room mein
online hote hi: **record → thumbnail cover ke saath video → telegram pe send**.

---

## ⚙️ Environment Variables (`.env`)

| Var | Default | Meaning |
|---|---|---|
| `API_ID` / `API_HASH` / `BOT_TOKEN` | — | Telegram credentials (required) |
| `OWNER_ID` | 0 | Owner user id (0 = open for all) |
| `SUDO_USERS` | — | Extra authorized ids, comma separated |
| `STRING_SESSION` | — | Premium userbot session → 4GB uploads |
| `PORT` | 8080 | Koyeb health server port |
| `MAX_CONCURRENT_JOBS` | 1 | Parallel recordings |
| `DEFAULT_UPLOAD_MODE` | video | video / document |
| `WATCH_INTERVAL` | 180 | Watchlist check interval (sec, min 60) |
| `WATCH_COOLDOWN` | 300 | Min gap between two auto-recordings |
| `MAX_AUTO_RESTARTS` | 3 | Auto-restart tries on stream drop |
| `MOUFLON_SYNC_URL` | mouflon.chantrail.com | Public key pool (auto-sync) |
| `MOUFLON_KEY_TTL` | 600 | Key cache time (sec) |
| `MOUFLON_KEYS` | — | Apne keys: `pkey:pdkey,pkey:pdkey` (optional) |
| `KEEP_FAILED_LOGS` | 1 | Keep ffmpeg logs on failure |

---

## 🧠 Technical notes

- **Resolver flow (Stripchat):** fast status API (cheap pre-filter) → master
  playlist (6 edge hosts race) → `psch/pkey` extraction → variant + psch/pkey
  → AD-vs-LIVE classifier → MOUFLON segment decryption (SHA256-XOR with pdkey)
  → decoded playlist via local proxy → ffmpeg fetches REAL segments only.
- **MOUFLON decode:** `decrypt_segment_url()` — token reverse → base64 →
  XOR with SHA256(pdkey) → real CDN path (algorithm verified against
  ChanTrail/StripchatRecorder; live-tested: real 536KB fMP4 segment 200 OK).
- **Other platforms (YouTube, m3u8 pages, OTT):** yt-dlp.
- **Why it broke before:** custom extractor was never wired into `main.py`
  (ffmpeg got the profile *webpage*), the proxy endpoint was a 410 stub, and
  variants were fetched without `psch/pkey` → 20s/661KB AD was recorded.
