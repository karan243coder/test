"""
media_utils.py - Super Advanced V11 (MOUFLON-FIX) URL & Stream Resolution Engine

THE AD PROBLEM (20s / 661.6KB) — ROOT CAUSE & FIX
---------------------------------------------------
Stripchat's HLS serves a placeholder AD playlist (VOD, 6 x 4s chunks = ~20s,
~661KB, `#EXT-X-MOUFLON-ADVERT` + `cpa/v2`) whenever a variant playlist is
fetched WITHOUT the `psch`/`pkey` params that the master playlist advertises.

The REAL live stream is only served when:
  1. variant URL gets `?psch=v2&pkey=<pkey>` (pkey from master)
  2. segment URIs (`#EXT-X-MOUFLON:URI:`) are decrypted with the matching
     `pdkey` (SHA256-XOR), because plain `media.mp4` placeholders 404.

V11 resolver therefore:
  - NEVER hands ffmpeg a variant without psch/pkey
  - auto-syncs public pkey->pdkey pairs (mouflon.chantrail.com by default,
    or MOUFLON_KEYS / MOUFLON_SYNC_URL)
  - serves the decoded live playlist through a local proxy
    (/mouflon_proxy) so ffmpeg fetches REAL segments only
  - rejects AD playlists explicitly ("Model private/ticket show")
"""

import os
import re
import json
import time
import base64
import hashlib
import itertools
import asyncio
import logging
import urllib.request
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse, unquote

logger = logging.getLogger(__name__)

RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

PORT = int(os.getenv("PORT", "8080") or 8080)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

STRIPCHAT_MIRRORS = (
    "stripchatgirls", "stripchatglobal", "stripchateu", "stripchateurope",
    "stripchat-girls", "stripchatlive", "cam-stripchat",
)
STRIPCHAT_HOST_RE = re.compile(
    r"https?://(?:www\.)?(?:" + "|".join(STRIPCHAT_MIRRORS) + r")\.com/",
    re.IGNORECASE,
)

EDGE_HOSTS = ["doppiocdn.com", "doppiocdn1.com", "doppiocdn.media",
              "doppiocdn.net", "doppiocdn.org", "doppiocdn.live"]

MOUFLON_SYNC_URL = os.getenv("MOUFLON_SYNC_URL", "https://mouflon.chantrail.com").strip()
KEY_SYNC_TTL = int(os.getenv("MOUFLON_KEY_TTL", "600") or 600)

# ------------------------------------------------------------
#  URL CLEANING & NORMALIZATION
# ------------------------------------------------------------

def extract_urls_from_text(text: str) -> List[str]:
    """Pull http(s):// tokens out of messy pasted text (markdown junk etc)."""
    found = re.findall(r"https?://[^\s<>\"']+", text or "")
    urls = []
    for u in found:
        u = u.strip()
        for sep in ("](", ")(", "]]", "))", "](http", ")(http"):
            i = u.find(sep)
            if i != -1:
                u = u[:i]
        m = re.search(r"\(https?://", u)
        if m:
            u = u[:m.start()]
        u = u.rstrip("])}")
        u = u.rstrip(".,;!?")
        u = u.split("|")[0].strip()
        if u.startswith(("http://", "https://")):
            urls.append(u)
    return urls


def normalize_stream_url(url: str) -> str:
    url = (url or "").strip()
    url = url.replace("]]", "").replace("))", "")
    url = STRIPCHAT_HOST_RE.sub("https://stripchat.com/", url)
    url = re.sub(r"https?://(?:www\.)?vr\.stripchat\.com/(?:cam/)?",
                 "https://stripchat.com/", url, flags=re.IGNORECASE)
    url = re.sub(r"https?://(?:www\.)?m\.stripchat\.com/",
                 "https://stripchat.com/", url, flags=re.IGNORECASE)
    return url


def is_stripchat_url(url: str) -> bool:
    return "stripchat.com/" in (url or "").lower()


def extract_username_from_url(url: str) -> str:
    try:
        path = urlparse(url).path.rstrip("/")
        username = unquote(path.split("/")[-1]) if path else ""
        return username.strip()
    except Exception:
        return ""


def is_direct_media_url(url: str) -> bool:
    low = url.lower()
    if any(x in low for x in [".m3u8", ".mp4", ".m4a", ".ts", ".mpd", ".aac", ".mkv"]):
        return True
    if any(low.startswith(p) for p in ["rtmp://", "srt://", "rtsp://"]):
        return True
    return False


def auto_generate_job_name(url: str, username: str = "") -> str:
    if username:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", username)[:35] or "stream"
    try:
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        parts = [p for p in url_clean.split("/") if p]
        if parts:
            candidate = parts[-1]
            if candidate.lower() in ["m3u8", "master", "playlist", "index", "live", "stream", "chunk", "hls"]:
                if len(parts) >= 2:
                    candidate = f"{parts[-2]}_{candidate}"
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", candidate).strip("_")
            if clean_name and len(clean_name) >= 2:
                return clean_name[:35]
    except Exception:
        pass
    return f"stream_{int(time.time()) % 100000}"


def parse_record_command(text: str) -> Tuple[Optional[str], Optional[str], int, Dict[str, str], str]:
    parts = (text or "").split(" ", 1)
    if len(parts) < 2:
        return None, None, 0, {}, "best"

    raw = parts[1].strip()
    sections = [s.strip() for s in raw.split("|")]
    main = sections[0]

    quality = "best"
    headers: Dict[str, str] = {}
    for sec in sections[1:]:
        low = sec.lower()
        if low.startswith("q=") or low.startswith("quality="):
            quality = sec.split("=", 1)[1].strip().lower() or "best"
        elif ":" in sec:
            k, v = sec.split(":", 1)
            headers[k.strip()] = v.strip()

    urls = extract_urls_from_text(main)
    if not urls:
        return None, None, 0, headers, quality
    url = normalize_stream_url(urls[0])

    duration_limit = 0
    m = re.search(r"(?<!\S)(\d+)\s*([smh]?)(?!\S)", main)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "h":
            duration_limit = val * 3600
        elif unit == "m":
            duration_limit = val * 60
        else:
            duration_limit = val

    job_name = ""
    username = extract_username_from_url(url) if is_stripchat_url(url) else ""
    for tok in main.split():
        if tok.lower().startswith(("http://", "https://", "rtmp://", "srt://", "rtsp://")):
            break
        if re.match(r"^\d+[smh]?$", tok.lower()):
            continue
        if tok and not job_name:
            job_name = tok.strip()
    if not job_name:
        job_name = auto_generate_job_name(url, username)
    job_name = re.sub(r"[^a-zA-Z0-9_-]", "_", job_name)[:35]

    return job_name, url, duration_limit, headers, quality


# ------------------------------------------------------------
#  HTTP helpers (urllib — proven to work with Stripchat CDNs)
# ------------------------------------------------------------

def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 12) -> str:
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def http_get_bytes(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> bytes:
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def stripchat_headers(username: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "User-Agent": DEFAULT_UA,
        "Referer": f"https://stripchat.com/{username}",
        "Accept": "*/*",
    }
    if extra:
        h.update(extra)
    return h


# ------------------------------------------------------------
#  FAST STRIPCHAT STATUS API (cheap pre-filter)
# ------------------------------------------------------------

def _api_fetch_sync(username: str) -> Dict[str, Any]:
    api_url = f"https://stripchat.com/api/front/v2/models/username/{username}/cam"
    req_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://stripchat.com/{username}",
    }
    req = urllib.request.Request(api_url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


async def check_stripchat_status(username: str) -> Dict[str, Any]:
    """
    NOTE: API 'isLive' can stay True during private shows and its `show`
    field is unreliable, so this is only a cheap pre-filter. The final
    decision always comes from the HLS probe (resolve_stripchat).
    """
    if not username:
        return {"status": "unknown", "error": "Invalid username"}
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _api_fetch_sync, username)
        cam = data.get("cam", {}) or {}
        user = (data.get("user", {}) or {}).get("user", {}) or {}
        model_id = str(cam.get("streamName") or user.get("id") or "")
        thumb_url = user.get("previewUrl") or user.get("avatarUrl") or ""
        display_name = user.get("displayName") or user.get("username") or username

        if not model_id and not user.get("username"):
            return {"status": "not_found", "model_id": "", "thumb_url": thumb_url,
                    "display_name": display_name, "error": "Model 404 not found"}

        is_avail = bool(cam.get("isCamAvailable"))
        is_active = bool(cam.get("isCamActive"))
        show = cam.get("show")
        private_mode = cam.get("privateMode") or ""

        if show or private_mode:
            return {"status": "private", "model_id": model_id, "thumb_url": thumb_url,
                    "display_name": display_name, "error": "Private / ticket show"}
        if is_avail and is_active:
            return {"status": "public", "model_id": model_id, "thumb_url": thumb_url,
                    "display_name": display_name, "error": None}
        if is_active:
            return {"status": "private", "model_id": model_id, "thumb_url": thumb_url,
                    "display_name": display_name, "error": "Private / ticket show"}
        return {"status": "offline", "model_id": model_id, "thumb_url": thumb_url,
                "display_name": display_name, "error": "Model offline"}
    except Exception as e:
        err = str(e)
        if "404" in err:
            return {"status": "not_found", "model_id": "", "thumb_url": "",
                    "display_name": username, "error": "Model 404 not found"}
        logger.debug(f"API status check failed for {username}: {e}")
        return {"status": "unknown", "model_id": "", "thumb_url": "",
                "display_name": username, "error": "API unreachable"}


# ------------------------------------------------------------
#  MOUFLON KEYS (auto-sync + env/file)
# ------------------------------------------------------------

_keys_cache: Dict[str, Any] = {"data": None, "ts": 0.0}


def sync_mouflon_keys(force: bool = False) -> Dict[str, str]:
    """Merge file keys + env keys + remote public sync. Cached KEY_SYNC_TTL sec."""
    global _keys_cache
    now = time.time()
    if not force and _keys_cache["data"] is not None and (now - _keys_cache["ts"]) < KEY_SYNC_TTL:
        return _keys_cache["data"]

    merged: Dict[str, str] = {}

    # 1) local files
    for fname in ("stripchat_mouflon_keys.json", "mouflon_keys.json"):
        if os.path.exists(fname):
            try:
                with open(fname, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged.update({k: v for k, v in data.items()
                                   if isinstance(v, str) and len(k) >= 4 and len(v) >= 4})
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, str) and ":" in item:
                            k, v = item.split(":", 1)
                            merged[k.strip()] = v.strip()
            except Exception as e:
                logger.debug(f"Failed to load {fname}: {e}")

    # 2) env var
    env_keys = os.getenv("MOUFLON_KEYS", "").strip()
    if env_keys:
        try:
            if env_keys.startswith("{"):
                d = json.loads(env_keys)
                if isinstance(d, dict):
                    merged.update(d)
            else:
                for pair in env_keys.split(","):
                    if ":" in pair:
                        k, v = pair.split(":", 1)
                        merged[k.strip()] = v.strip()
        except Exception as e:
            logger.debug(f"MOUFLON_KEYS env parse fail: {e}")

    # 3) remote public sync (community key pool)
    if MOUFLON_SYNC_URL:
        try:
            text = http_get(MOUFLON_SYNC_URL, timeout=10)
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("keys"), dict):
                merged.update({k: v for k, v in data["keys"].items() if isinstance(v, str)})
                logger.info(f"Mouflon keys synced from {MOUFLON_SYNC_URL}: {len(data['keys'])} keys (total {len(merged)})")
        except Exception as e:
            logger.warning(f"Mouflon key sync failed from {MOUFLON_SYNC_URL}: {e}")

    _keys_cache = {"data": merged, "ts": now}
    if not merged:
        logger.warning("No MOUFLON keys available - encrypted streams will fail. Set MOUFLON_KEYS or MOUFLON_SYNC_URL.")
    return merged


def get_mouflon_keys(force: bool = False) -> Dict[str, str]:
    return sync_mouflon_keys(force=force)


# ------------------------------------------------------------
#  MOUFLON DECRYPTION (verified algorithm, works with public key pool)
# ------------------------------------------------------------

def decrypt_segment_url(encoded_url: str, pdkey: str) -> str:
    """
    Segment URL like .../6406_335_wMiYLgb+3RVx9gCVyDJexS_1785604431.mp4
    -> token before the timestamp is reversed, base64-decoded, XOR'ed with
    SHA256(pdkey) -> real path replaces the token.
    """
    if not pdkey or not encoded_url:
        return encoded_url
    m = re.search(r"_([^_]+)_(\d+(?:_part\d+)?)\.mp4(?:[?#].*)?$", encoded_url)
    if not m:
        return encoded_url
    token = m.group(1)
    reversed_tok = token[::-1]
    reversed_tok += "=" * ((4 - len(reversed_tok) % 4) % 4)
    try:
        raw = base64.b64decode(reversed_tok)
    except Exception:
        return encoded_url
    kb = hashlib.sha256(pdkey.encode("utf-8")).digest()
    dec = bytes(a ^ b for a, b in zip(raw, itertools.cycle(kb)))
    dec_str = dec.decode("utf-8", errors="ignore")
    if not dec_str:
        return encoded_url
    return encoded_url.replace(token, dec_str)


def decode_mouflon_live_playlist(content: str, pdkey: str) -> str:
    """
    Rewrite a LIVE playlist: each `media.mp4` placeholder segment line gets
    replaced by the decrypted real CDN URL from its #EXT-X-MOUFLON:URI tag.
    """
    if "#EXT-X-MOUFLON:URI:" not in content:
        return content
    out_lines: List[str] = []
    pending_url: Optional[str] = None
    for line in content.splitlines():
        if line.startswith("#EXT-X-MOUFLON:URI:"):
            uri = line[len("#EXT-X-MOUFLON:URI:"):].strip()
            pending_url = decrypt_segment_url(uri, pdkey)
            # keep the MOUFLON tag out of the served playlist (ffmpeg ignores
            # unknown tags, but a clean playlist is safer)
            continue
        if pending_url and "media.mp4" in line:
            out_lines.append(pending_url)
            pending_url = None
            continue
        if line.strip().startswith("#EXT-X-MOUFLON:"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


# ------------------------------------------------------------
#  HLS PLAYLIST CLASSIFIERS (AD vs LIVE)
# ------------------------------------------------------------

def _is_ad_playlist(content: str) -> bool:
    """The 20s/661KB placeholder: MOUFLON-ADVERT + cpa/v2 + ENDLIST."""
    if not content or "#EXTM3U" not in content:
        return True
    if "#EXT-X-MOUFLON-ADVERT" in content:
        return True
    if "#EXT-X-ENDLIST" in content and "cpa/v2" in content:
        return True
    return False


def _is_live_playlist(content: str) -> bool:
    if not content or "#EXTM3U" not in content:
        return False
    if _is_ad_playlist(content):
        return False
    return "#EXT-X-MEDIA-SEQUENCE" in content and "#EXT-X-ENDLIST" not in content


def _pick_best_variant(master_content: str) -> Optional[str]:
    """Pick the variant with the highest BANDWIDTH from a master playlist."""
    lines = master_content.splitlines()
    best_url = None
    best_bw = -1
    pending_bw = -1
    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending_bw = int(m.group(1)) if m else -1
        elif line and not line.startswith("#"):
            if pending_bw > best_bw:
                best_bw = pending_bw
                best_url = line
            pending_bw = -1
    if best_url:
        return best_url
    # fallback: any http line
    for line in lines:
        line = line.strip()
        if line.startswith("http") and ".m3u8" in line:
            return line
    return None


# ------------------------------------------------------------
#  STRIPCHAT HLS RESOLVER (AD-PROOF)
# ------------------------------------------------------------

def _fetch_master_sync(model_id: str, username: str) -> Optional[Dict[str, str]]:
    """Race all edge hosts for the master playlist. First valid wins."""
    from concurrent.futures import ThreadPoolExecutor

    def try_host(host: str):
        url = f"https://edge-hls.{host}/hls/{model_id}/master/{model_id}_auto.m3u8"
        try:
            content = http_get(url, stripchat_headers(username), timeout=8)
            if "#EXTM3U" in content and ("#EXT-X-STREAM-INF" in content or "#EXTINF" in content):
                return {"host": host, "url": url, "content": content}
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=len(EDGE_HOSTS)) as ex:
        results = list(ex.map(try_host, EDGE_HOSTS))
    for r in results:
        if r:
            return r
    return None


def _fetch_variant_sync(variant_url: str, pkey: str, username: str) -> Tuple[Optional[str], str]:
    """Fetch variant with psch/pkey. Returns (content, kind) kind in live/ad/err."""
    sep = "&" if "?" in variant_url else "?"
    vurl = f"{variant_url}{sep}psch=v2&pkey={urllib.parse.quote(pkey, safe='')}"
    try:
        content = http_get(vurl, stripchat_headers(username), timeout=10)
    except Exception as e:
        return None, f"err:{str(e)[:80]}"
    if _is_ad_playlist(content):
        return content, "ad"
    if _is_live_playlist(content):
        return content, "live"
    return content, "unknown"


async def resolve_stripchat(username: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    AD-PROOF Stripchat resolver. Returns:
      {"url": proxy|direct URL or None, "title", "thumb_path",
       "error", "status": public|private|offline|not_found|unknown}
    """
    headers = headers or {}

    fast = await check_stripchat_status(username)
    status0 = fast.get("status", "unknown")
    display = fast.get("display_name", username)
    thumb_url = fast.get("thumb_url", "")
    model_id = fast.get("model_id", "")

    if status0 == "not_found" or not model_id:
        return {"url": None, "title": display, "thumb_path": None,
                "error": "❌ 404 — Model exist nahi karta. Username check karo.", "status": "not_found"}

    # ---- master playlist (race edge hosts) ----
    loop = asyncio.get_event_loop()
    master = await loop.run_in_executor(None, _fetch_master_sync, model_id, username)
    if not master:
        # API says active but no master -> private/offline
        if status0 == "offline":
            return {"url": None, "title": display, "thumb_path": None,
                    "error": "💤 Model abhi OFFLINE hai.", "status": "offline"}
        return {"url": None, "title": display, "thumb_path": None,
                "error": "🔒 Master playlist nahi mila — model PRIVATE show mein hai ya offline.",
                "status": "private"}

    pkeys = re.findall(r"#EXT-X-MOUFLON:PSCH:v2:([^\s\n]+)", master["content"])
    variant = _pick_best_variant(master["content"])
    if not variant:
        return {"url": None, "title": display, "thumb_path": None,
                "error": "⚠️ Master playlist mein koi variant nahi mila.", "status": "unknown"}

    # ---- try each pkey (prefer ones we have a pdkey for) ----
    keys = get_mouflon_keys()
    if pkeys:
        ordered = [p for p in pkeys if p in keys] + [p for p in pkeys if p not in keys]
    else:
        ordered = [""]

    def _make_proxy_url(variant_url: str, pkey: str) -> str:
        sep = "&" if "?" in variant_url else "?"
        vurl = f"{variant_url}{sep}psch=v2&pkey={urllib.parse.quote(pkey, safe='')}"
        return (f"http://127.0.0.1:{PORT}/mouflon_proxy"
                f"?url={urllib.parse.quote(vurl, safe='')}&username={urllib.parse.quote(username, safe='')}")

    ad_seen = False
    for pkey in ordered:
        content, kind = await loop.run_in_executor(
            None, _fetch_variant_sync, variant, pkey, username)
        if kind == "ad":
            ad_seen = True
            continue
        if kind == "live" and content:
            if "#EXT-X-MOUFLON:URI:" in content:
                pdkey = keys.get(pkey, "")
                if not pdkey:
                    # force one re-sync (keys rotate), then retry once
                    keys = get_mouflon_keys(force=True)
                    pdkey = keys.get(pkey, "")
                if not pdkey:
                    continue  # try next pkey
                # serve decoded playlist through local proxy
                proxy_url = _make_proxy_url(variant, pkey)
                logger.info(f"[{username}] LIVE + MOUFLON -> proxy (pkey {pkey})")
                thumb_path = await download_thumbnail(thumb_url, username) if thumb_url else None
                return {"url": proxy_url, "title": display, "thumb_path": thumb_path,
                        "error": None, "status": "public"}
            # plain live playlist, no encryption
            logger.info(f"[{username}] LIVE (plain) -> direct URL")
            thumb_path = await download_thumbnail(thumb_url, username) if thumb_url else None
            return {"url": variant, "title": display, "thumb_path": thumb_path,
                    "error": None, "status": "public"}

    # ---- nothing worked ----
    thumb_path = None
    if thumb_url:
        thumb_path = await download_thumbnail(thumb_url, username)

    if ad_seen:
        return {"url": None, "title": display, "thumb_path": thumb_path,
                "error": ("🔒 **Model PRIVATE / TICKET show mein hai** (Stripchat AD placeholder mila). "
                          "Public room kholne par record ho jayega."),
                "status": "private"}
    return {"url": None, "title": display, "thumb_path": thumb_path,
            "error": "⚠️ Live stream resolve nahi hui — kuch der baad dobara try karo.",
            "status": "unknown"}


# ------------------------------------------------------------
#  GENERIC (yt-dlp) RESOLVER FOR OTHER PLATFORMS
# ------------------------------------------------------------

def _ytdlp_sync(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    import yt_dlp
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": headers or {"User-Agent": DEFAULT_UA},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {"info": info}
    except Exception as e:
        return {"error": e}


async def ytdlp_extract(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ytdlp_sync, url, headers or {})


def _pick_best_format(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    formats = info.get("formats") or []
    if not formats:
        return None
    if len(formats) == 1:
        return formats[0]
    vids = [f for f in formats if f.get("vcodec") not in ("none", None) and f.get("height")]
    if vids:
        return max(vids, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return formats[-1]


def classify_extraction_error(err: Exception) -> str:
    msg = str(err)
    low = msg.lower()
    if "private show" in low or "ticket show" in low or "private mode" in low:
        return "🔒 Model abhi PRIVATE / TICKET show mein hai."
    if "not currently live" in low or "user not live" in low or "is not live" in low:
        return "💤 Model abhi OFFLINE hai."
    if "404" in low or "not found" in low:
        return "❌ 404 — Model exist nahi karta."
    if "unable to download webpage" in low:
        return "⚠️ Webpage fetch fail (anti-bot/network). Dobara try karo."
    if "unable to extract stream host" in low:
        return "⚠️ HLS host extract nahi hua. 15s baad retry karo."
    return f"⚠️ Extraction Error: `{msg[:150]}`"


async def download_thumbnail(thumb_url: str, job_name: str) -> Optional[str]:
    if not thumb_url or not thumb_url.startswith("http"):
        return None
    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_thumb.jpg")
    try:
        def _dl():
            req = urllib.request.Request(thumb_url, headers={
                "User-Agent": DEFAULT_UA, "Referer": "https://stripchat.com/"})
            with urllib.request.urlopen(req, timeout=15) as resp, open(thumb_path, "wb") as f:
                f.write(resp.read())
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _dl)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            return thumb_path
    except Exception as e:
        logger.debug(f"Thumbnail download failed: {e}")
    return None


async def resolve_stream_url(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Universal resolver -> {"url", "title", "thumb_path", "error", "status"}"""
    headers = headers or {}
    url = normalize_stream_url(url)

    if is_stripchat_url(url):
        username = extract_username_from_url(url)
        if not username:
            return {"url": None, "title": "", "thumb_path": None,
                    "error": "❌ Invalid Stripchat link. Format: https://stripchat.com/Username", "status": "unknown"}
        return await resolve_stripchat(username, headers)

    if is_direct_media_url(url):
        return {"url": url, "title": auto_generate_job_name(url),
                "thumb_path": None, "error": None, "status": "direct"}

    result = await ytdlp_extract(url, headers)
    if result.get("error"):
        return {"url": url, "title": auto_generate_job_name(url), "thumb_path": None,
                "error": classify_extraction_error(result["error"]), "status": "unknown"}
    info = result.get("info") or {}
    fmt = _pick_best_format(info)
    if not fmt or not fmt.get("url"):
        return {"url": url, "title": auto_generate_job_name(url), "thumb_path": None,
                "error": "⚠️ Is link se koi playable stream nahi mila.", "status": "unknown"}
    title = info.get("title") or auto_generate_job_name(url)
    thumb_path = None
    if info.get("thumbnail"):
        thumb_path = await download_thumbnail(info["thumbnail"], auto_generate_job_name(url))
    return {"url": fmt["url"], "title": title, "thumb_path": thumb_path,
            "error": None, "status": "public"}
