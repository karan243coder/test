"""
media_utils.py - V9 FINAL - Fixes 20 sec AD (661kb) issue
Root Cause:
- Stripchat returns AD VOD (MOUFLON-ADVERT + cpa/v2 + ENDLIST) when variant URL fetched without psch/pkey
- Live requires ?psch=v2&pkey=... query to get live playlist (MEDIA-SEQUENCE)
- Live playlist still has encrypted segment URIs (MOUFLON:URI) that need pdkey to decode
- Without pdkey, segments return 404 -> ffmpeg stops after 1s or records only ad

Fixes in V9:
1. Extract PSCH/PKEY from master playlist (regex #EXT-X-MOUFLON:PSCH:...)
2. Append ?psch=&pkey= to variant URLs to get LIVE not AD
3. Detect AD playlist (MOUFLON-ADVERT + cpa/v2 + ENDLIST) and skip it
4. Load mouflon keys from stripchat_mouflon_keys.json or MOUFLON_KEYS env var
5. If keys available, decode live playlist (XOR+SHA256) and serve via local proxy for ffmpeg
6. If no keys, return proper error: "Ad detected, need mouflon keys" instead of silently downloading ad
"""

import os
import re
import json
import time
import asyncio
import logging
import gc
import base64
import hashlib
import itertools
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)

# Global mouflon keys cache
_mouflon_keys: Optional[Dict[str, str]] = None
_cached_hash_keys: Dict[str, bytes] = {}


def load_mouflon_keys() -> Dict[str, str]:
    global _mouflon_keys
    if _mouflon_keys is not None:
        return _mouflon_keys
    _mouflon_keys = {}
    # Try file
    possible_files = ["stripchat_mouflon_keys.json", "mouflon_keys.json", "keys.json"]
    for fname in possible_files:
        if os.path.exists(fname):
            try:
                with open(fname, "r") as f:
                    data = json.load(f)
                    # Support both {"pkey":"pdkey"} and {"keys": [...]} or list
                    if isinstance(data, dict):
                        # Check if has "keys" array
                        if "keys" in data and isinstance(data["keys"], list):
                            for item in data["keys"]:
                                if isinstance(item, str) and ":" in item:
                                    k,v = item.split(":",1)
                                    _mouflon_keys[k.strip()] = v.strip()
                                elif isinstance(item, dict):
                                    _mouflon_keys.update(item)
                        else:
                            # Assume dict is pkey->pdkey
                            for k,v in data.items():
                                if isinstance(v, str) and len(k)>=8 and len(v)>=8:
                                    _mouflon_keys[k] = v
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, str) and ":" in item:
                                k,v = item.split(":",1)
                                _mouflon_keys[k.strip()] = v.strip()
                logger.info(f"Loaded {len(_mouflon_keys)} mouflon keys from {fname}")
            except Exception as e:
                logger.debug(f"Failed to load {fname}: {e}")

    # Try env var MOUFLON_KEYS (JSON string or pkey:pdkey comma separated)
    env_keys = os.getenv("MOUFLON_KEYS", "").strip()
    if env_keys:
        try:
            # Try JSON
            if env_keys.startswith("{"):
                d = json.loads(env_keys)
                if isinstance(d, dict):
                    _mouflon_keys.update(d)
            else:
                # Comma separated pkey:pdkey
                for pair in env_keys.split(","):
                    if ":" in pair:
                        k,v = pair.split(":",1)
                        _mouflon_keys[k.strip()] = v.strip()
            logger.info(f"Loaded mouflon keys from env, total now {len(_mouflon_keys)}")
        except Exception as e:
            logger.debug(f"Failed to parse MOUFLON_KEYS env: {e}")

    # Hardcoded fallback example (old key, may not work for current v2 but better than nothing)
    # User should provide current keys via file or env
    if not _mouflon_keys:
        logger.warning("No mouflon keys found! Live HLS will download only AD (20sec 661kb). Provide keys via stripchat_mouflon_keys.json or MOUFLON_KEYS env var. See https://github.com/ChanTrail/StripchatRecorder for how to get keys")

    return _mouflon_keys


def _decode_mouflon_b64(encrypted_b64: str, key: str) -> str:
    """XOR decrypt as per StreaMonitor"""
    try:
        # Pad base64
        padded = encrypted_b64 + "=" * ((4 - len(encrypted_b64) % 4) % 4)
        encrypted_data = base64.b64decode(padded)
    except Exception:
        return ""
    if key not in _cached_hash_keys:
        _cached_hash_keys[key] = hashlib.sha256(key.encode("utf-8")).digest()
    hash_bytes = _cached_hash_keys[key]
    decoded = bytes(a ^ b for a, b in zip(encrypted_data, itertools.cycle(hash_bytes)))
    try:
        return decoded.decode("utf-8")
    except:
        return decoded.decode("latin-1", errors="ignore")


def _extract_psch_pkey_from_master(master_content: str) -> List[Tuple[str, str]]:
    """Extract list of (psch, pkey) from master m3u8"""
    pattern = r'#EXT-X-MOUFLON:PSCH:([^:]+):([^\s\n]+)'
    matches = re.findall(pattern, master_content)
    # matches are list of (psch, pkey)
    return matches


def _is_ad_playlist(content: str) -> bool:
    """Detect if playlist is AD VOD (20 sec, 661kb) not live"""
    if not content or "#EXTM3U" not in content:
        return True
    # AD has MOUFLON-ADVERT and cpa/v2 and ENDLIST and PLAYLIST-TYPE:VOD
    if "MOUFLON-ADVERT" in content and "cpa/v2" in content and "#EXT-X-ENDLIST" in content:
        # Count segments - ad has 6 segments
        if content.count("chunk_") >= 5 and "PLAYLIST-TYPE:VOD" in content:
            return True
    # Also check if it's VOD with ENDLIST and small
    if "#EXT-X-ENDLIST" in content and "TARGETDURATION:4" in content and len(content) < 2000:
        # Likely ad VOD
        if "MOUFLON-ADVERT" in content:
            return True
    return False


def _is_live_playlist(content: str) -> bool:
    """Check if playlist is live (not ad)"""
    if not content or "#EXTM3U" not in content:
        return False
    if _is_ad_playlist(content):
        return False
    # Live has MEDIA-SEQUENCE and no ENDLIST, or has MOUFLON:URI
    if "#EXT-X-MEDIA-SEQUENCE" in content and "#EXT-X-ENDLIST" not in content:
        return True
    if "#EXT-X-MOUFLON:URI:" in content:
        return True
    if "#EXTINF" in content and "#EXT-X-ENDLIST" not in content:
        return True
    return False


def _decode_m3u8_content(content: str, pkey: str, pdkey: str) -> str:
    """
    Decode mouflon encrypted m3u8 content using pdkey
    Logic from StreaMonitor:
    - For v2: URI line contains encoded part second last _ separated, reversed
    - Decode and replace media.mp4 placeholder
    """
    if not content:
        return content
    # Determine PSCH version from content or use provided
    # For simplicity, assume v2 if URI present
    mouflon_file_attr = None
    if "#EXT-X-MOUFLON:URI:" in content:
        mouflon_file_attr = "#EXT-X-MOUFLON:URI:"
    elif "#EXT-X-MOUFLON:FILE:" in content:
        mouflon_file_attr = "#EXT-X-MOUFLON:FILE:"
    else:
        # No mouflon encryption, return as is
        return content

    decoded = ""
    lines = content.splitlines()
    last_decoded_file = None

    for line in lines:
        if line.startswith(mouflon_file_attr):
            # Extract encrypted part
            if mouflon_file_attr == "#EXT-X-MOUFLON:URI:":
                # v2: uri = line[len(attr):], encoded_part = uri.split('_')[-2], decoded = decode(reverse(encoded_part), pdkey)
                uri = line[len(mouflon_file_attr):].strip()
                try:
                    parts = uri.split('_')
                    if len(parts) >= 2:
                        encoded_part = parts[-2]
                        reversed_enc = encoded_part[::-1]
                        decoded_part = _decode_mouflon_b64(reversed_enc, pdkey)
                        # Now replace encoded_part with decoded_part in uri and take path after 4th slash
                        # uri.replace(encoded_part, decoded_part).split('/', maxsplit=4)[4]
                        new_uri = uri.replace(encoded_part, decoded_part)
                        # Split by / maxsplit 4
                        split_parts = new_uri.split('/', 4)
                        if len(split_parts) >= 5:
                            last_decoded_file = split_parts[4]
                        else:
                            last_decoded_file = new_uri.split('/')[-1]
                    else:
                        last_decoded_file = None
                except Exception as e:
                    logger.debug(f"Failed to decode URI {uri}: {e}")
                    last_decoded_file = None
            else:  # v1
                try:
                    last_decoded_file = _decode_mouflon_b64(line[len(mouflon_file_attr):].strip(), pdkey)
                except:
                    last_decoded_file = None
        elif line.endswith("media.mp4") and last_decoded_file:
            # Replace media.mp4 with decoded file
            decoded += line.replace("media.mp4", last_decoded_file) + "\n"
            last_decoded_file = None
        else:
            decoded += line + "\n"

    return decoded


def clean_url_punctuation(url: str) -> str:
    url_clean = url.strip()
    while url_clean.endswith((".", ",", ";", ")", "]", "!", "?", "'", '"')):
        url_clean = url_clean[:-1]
    if url_clean.endswith("..."):
        url_clean = url_clean[:-3]
    url_clean = url_clean.rstrip(".")
    return url_clean


def normalize_stream_url(url: str) -> str:
    url_clean = clean_url_punctuation(url)
    url_clean = re.sub(
        r"https?://(?:www\.)?(?:stripchatgirls|stripchatglobal|stripchateu|stripchateurope|stripchat-girls|stripchatlive|cam-stripchat)\.com/",
        "https://stripchat.com/",
        url_clean,
        flags=re.IGNORECASE
    )
    url_clean = re.sub(
        r"https?://(?:www\.)?vr\.stripchat\.com/(?:cam/)?",
        "https://stripchat.com/",
        url_clean,
        flags=re.IGNORECASE
    )
    return url_clean


def is_protected_platform_url(url: str) -> bool:
    url_lower = (url or "").lower()
    return (
        "stripchat.com/" in url_lower
        or "stripchatgirls.com/" in url_lower
        or "doppiocdn" in url_lower
        or ("edge-hls." in url_lower and "/hls/" in url_lower)
    )


def is_valid_hls_playlist_text(content: str) -> bool:
    if not content or "#EXTM3U" not in content:
        return False
    media_lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        media_lines.append(line)
    if not media_lines:
        return False
    if "media.mp4" in content or "#EXT-X-MOUFLON:" in content:
        return False
    valid_exts = (".m3u8", ".ts", ".m4s", ".mp4", ".aac", ".m4a")
    return any(any(ext in line for ext in valid_exts) for line in media_lines)


def auto_generate_job_name(url: str) -> str:
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
                return clean_name[:30]
    except Exception as e:
        logger.debug(f"Auto job name error: {e}")
    return f"stream_{int(time.time()) % 100000}"


def parse_record_command(text: str) -> Tuple[Optional[str], Optional[str], int, Dict[str, str], str]:
    parts = text.split(" ", 1)
    if len(parts) < 2:
        return None, None, 0, {}, "best"
    raw_args = parts[1].strip()
    sections = [x.strip() for x in raw_args.split("|")]
    main_section = sections[0]
    headers = {}
    quality = "best"
    for sec in sections[1:]:
        sec_lower = sec.lower()
        if sec_lower.startswith("q=") or sec_lower.startswith("quality="):
            quality = sec.split("=", 1)[1].strip().lower()
        elif ":" in sec:
            k, v = sec.split(":", 1)
            headers[k.strip()] = v.strip()
    tokens = main_section.split()
    if not tokens:
        return None, None, 0, headers, quality
    job_name = ""
    url = ""
    duration_limit = 0
    for tok in tokens:
        tok_lower = tok.lower()
        if any(tok_lower.startswith(prefix) for prefix in ["http://", "https://", "rtmp://", "srt://", "rtsp://"]):
            url = normalize_stream_url(tok)
            if not job_name:
                job_name = auto_generate_job_name(url)
        elif re.match(r"^\d+[smh]?$", tok_lower):
            val = int(re.sub(r"[smh]", "", tok_lower))
            if tok_lower.endswith("m"):
                duration_limit = val * 60
            elif tok_lower.endswith("h"):
                duration_limit = val * 3600
            else:
                duration_limit = val
        else:
            if not url and "." in tok:
                url = normalize_stream_url(tok)
                if not job_name:
                    job_name = auto_generate_job_name(url)
            elif not job_name or job_name == auto_generate_job_name(url):
                job_name = tok
    if not job_name and url:
        job_name = auto_generate_job_name(url)
    return job_name, url, duration_limit, headers, quality


def is_explicit_direct_link(url: str) -> bool:
    url_lower = url.lower()
    if is_protected_platform_url(url_lower):
        return False
    if any(x in url_lower for x in [".m3u8", ".mp4", ".m4a", ".ts", ".mpd"]):
        return True
    if any(url_lower.startswith(proto) for proto in ["rtmp://", "srt://", "rtsp://"]):
        return True
    return False


async def download_web_thumbnail(thumbnail_url: str, job_name: str) -> Optional[str]:
    if not thumbnail_url or not thumbnail_url.startswith("http"):
        return None
    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_web_thumb.jpg")
    try:
        import urllib.request
        def _dl():
            req = urllib.request.Request(thumbnail_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://stripchat.com/"})
            with urllib.request.urlopen(req, timeout=12) as resp, open(thumb_path, "wb") as f:
                f.write(resp.read())
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _dl)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            logger.info(f"Downloaded web thumbnail to {thumb_path}")
            return thumb_path
    except Exception as e:
        logger.debug(f"Web thumbnail download failed for {thumbnail_url}: {e}")
    return None


def _lowercase_escape(s: str) -> str:
    try:
        s = re.sub(r'\\x([0-9a-fA-F]{2})', r'\\u00\1', s)
        return s
    except:
        return s


def _extract_preloaded_state(html: str) -> Optional[Dict[str, Any]]:
    patterns = [
        r'<script[^>]*>\s*window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*</script>',
        r'window\.__PRELOADED_STATE__\s*=\s*({.+?});?\s*</script>',
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.DOTALL)
        if m:
            json_str = m.group(1)
            try:
                json_str = _lowercase_escape(json_str)
                data = json.loads(json_str)
                return data
            except:
                try:
                    json_str = json_str.split("</script>")[0]
                    data = json.loads(json_str)
                    return data
                except Exception as e:
                    logger.debug(f"Failed to parse preloaded json: {e}")
                    continue
    return None


def _collect_hls_hosts(data: Dict[str, Any]) -> List[str]:
    hosts = []
    paths_to_try = [
        lambda d: d.get("config", {}).get("data", {}).get("hlsStreamHost"),
        lambda d: d.get("configV3", {}).get("static", {}).get("hlsStreamHost"),
        lambda d: d.get("configV3", {}).get("static", {}).get("hosts", {}).get("hlsFallback", {}).get("hlsStreamHost"),
    ]
    list_paths = [
        lambda d: d.get("config", {}).get("data", {}).get("features", {}).get("featuresV2", {}).get("hlsFallback", {}).get("fallbackDomains"),
        lambda d: d.get("config", {}).get("data", {}).get("hlsFallback", {}).get("fallbackDomains"),
        lambda d: d.get("configV3", {}).get("static", {}).get("features", {}).get("hlsFallback", {}).get("fallbackDomains"),
        lambda d: d.get("configV3", {}).get("static", {}).get("features", {}).get("featuresV2", {}).get("hlsFallback", {}).get("fallbackDomains"),
        lambda d: d.get("configV3", {}).get("static", {}).get("hosts", {}).get("hlsFallback", {}).get("fallbackDomains"),
    ]
    for fn in paths_to_try:
        try:
            h = fn(data)
            if h and isinstance(h, str):
                hosts.append(h)
        except: pass
    for fn in list_paths:
        try:
            doms = fn(data)
            if isinstance(doms, list):
                hosts.extend([x for x in doms if isinstance(x, str)])
        except: pass
    seen = set()
    uniq = []
    for h in hosts:
        if h and h not in seen:
            seen.add(h)
            uniq.append(h)
    if not uniq:
        uniq = ["doppiocdn.com", "doppiocdn.live", "doppiocdn.net", "doppiocdn.media"]
    return uniq


def _validate_hls_content(content: str) -> bool:
    if not content or "#EXTM3U" not in content:
        return False
    if "#EXT-X-STREAM-INF" in content or "#EXTINF" in content or "EXT-X-MEDIA" in content:
        return True
    return False


def _is_show_currently_active(show: Dict[str, Any], isShowAvailable: bool, privateMode: str) -> bool:
    if not show or not isinstance(show, dict):
        return False
    if show.get("isDeleted"):
        return False
    ended_at = show.get("endedAt")
    if ended_at:
        try:
            dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if (now - dt).total_seconds() > 90:
                return False
        except:
            pass
    if isShowAvailable:
        return True
    if privateMode and privateMode != "":
        return True
    if isShowAvailable is False:
        return False
    if not ended_at:
        return True
    return False


def _extract_stripchat_api_sync(username: str, headers: Dict[str, str]) -> Tuple[Optional[str], str, Optional[str], Optional[str], bool]:
    import urllib.request, json
    api_url = f"https://stripchat.com/api/front/v2/models/username/{username}/cam"
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://stripchat.com/{username}",
    }
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(api_url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            cam = data.get("cam", {})
            user_obj = data.get("user", {}).get("user", {})
            is_avail = cam.get("isCamAvailable", False)
            is_active = cam.get("isCamActive", False)
            show = cam.get("show")
            private_mode = cam.get("privateMode", "")
            thumb_url = user_obj.get("previewUrl") or user_obj.get("avatarUrl")
            model_id = str(cam.get("streamName") or user_obj.get("id") or "")
            status = user_obj.get("status", "")
            is_live_api = user_obj.get("isLive", False)

            logger.info(f"API v2 for {username}: avail={is_avail} active={is_active} show={show} privateMode={private_mode} status={status} isLive={is_live_api} model_id={model_id}")

            if not model_id:
                return None, username, thumb_url, "❌ Model ID missing in API", False

            if is_avail and is_active:
                if show is not None:
                    if isinstance(show, dict):
                        mode = show.get("mode", "private")
                        return None, username, thumb_url, f"🔒 **Model is in a Private / {mode.upper()} Show.** Currently in {mode} mode.", False
                    else:
                        return None, username, thumb_url, "🔒 **Model is in a Private / Ticket Show.**", False
                if private_mode and private_mode != "":
                    return None, username, thumb_url, f"🔒 **Model is in Private Mode: {private_mode}.**", False
                return model_id, username, thumb_url, None, True
            else:
                if is_active:
                    return None, username, thumb_url, "🔒 **Model is in a Private / Ticket Show.**", False
                else:
                    return None, username, thumb_url, "💤 **Model is currently OFFLINE.**", False
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "Not Found" in err_str:
            return None, username, None, "❌ **404 Not Found:** Model does not exist.", False
        logger.debug(f"API v2 fetch error for {username}: {e}")
        return None, username, None, None, False


def _fetch_hosts_from_webpage_sync(username: str, headers: Dict[str, str]) -> List[str]:
    import urllib.request
    try:
        url = f"https://stripchat.com/{username}"
        req_headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://stripchat.com/",
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        data = _extract_preloaded_state(html)
        if data:
            hosts = _collect_hls_hosts(data)
            logger.info(f"Webpage hosts for {username}: {hosts}")
            return hosts
    except Exception as e:
        logger.debug(f"Failed to fetch hosts from webpage for {username}: {e}")
    return ["doppiocdn.com", "doppiocdn.live", "doppiocdn.net", "doppiocdn.media"]


def _try_find_working_hls(model_id: str, username: str, hosts: List[str], headers: Dict[str, str]) -> Optional[str]:
    """
    V9: Now extracts PSCH/PKEY from master and appends to variant to get LIVE not AD
    Also detects AD playlist and skips it
    """
    import urllib.request
    ua = headers.get("User-Agent", "Mozilla/5.0")
    mouflon_keys = load_mouflon_keys()

    for host in hosts:
        if host.startswith("edge-hls."):
            base = f"https://{host}"
        else:
            clean_host = host.replace("b-", "").replace("edge-hls.", "")
            base = f"https://edge-hls.{clean_host}"

        candidates = [
            f"{base}/hls/{model_id}/master/{model_id}_auto.m3u8",
            f"{base}/hls/{model_id}/master/{model_id}.m3u8",
        ]

        for master_url in candidates:
            try:
                # Fetch master
                req_master = urllib.request.Request(master_url, headers={
                    "User-Agent": ua,
                    "Referer": f"https://stripchat.com/{username}",
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req_master, timeout=7) as r:
                    master_content = r.read().decode("utf-8", errors="ignore")

                if not _validate_hls_content(master_content):
                    continue

                # Extract PSCH/PKEY pairs from master
                psch_pkey_list = _extract_psch_pkey_from_master(master_content)
                logger.info(f"Master {master_url} has PSCH/PKEY: {psch_pkey_list[:3]}")

                # Extract variant URLs from master
                variant_urls = re.findall(r"https://[^\s]+\.m3u8[^\s]*", master_content)
                # Also try to find relative variant URLs
                # For each variant, try with psch/pkey appended to get LIVE not AD
                for variant_url in variant_urls:
                    # First try without psch/pkey (might be ad)
                    # Then try with each psch/pkey to get live
                    tries = [variant_url]  # first without
                    for psch, pkey in psch_pkey_list:
                        sep = "&" if "?" in variant_url else "?"
                        tries.append(f"{variant_url}{sep}psch={psch}&pkey={pkey}")

                    for try_url in tries:
                        try:
                            req_var = urllib.request.Request(try_url, headers={
                                "User-Agent": ua,
                                "Referer": f"https://stripchat.com/{username}",
                                "Accept": "*/*",
                            })
                            with urllib.request.urlopen(req_var, timeout=7) as rv:
                                var_content = rv.read().decode("utf-8", errors="ignore")

                            # Check if AD
                            if _is_ad_playlist(var_content):
                                logger.debug(f"Variant {try_url} is AD VOD (20sec), skipping")
                                continue

                            # Check if live
                            if _is_live_playlist(var_content):
                                logger.info(f"Found LIVE HLS for {username} on {host}: {try_url} (AD check passed)")
                                # If we have mouflon keys for this pkey, return via local proxy that will decode
                                mouflon_keys = load_mouflon_keys()
                                # Extract pkey from try_url
                                m = re.search(r'pkey=([^&]+)', try_url)
                                cur_pkey = m.group(1) if m else (pkey if 'pkey' in locals() else "")
                                cur_psch = psch if 'psch' in locals() else "v2"
                                # Check if we have pdkey for this pkey
                                if cur_pkey and cur_pkey in mouflon_keys:
                                    import os, urllib.parse
                                    port = os.getenv("PORT", "8080")
                                    # URL-encode the variant url for safe query param passing
                                    encoded_url = urllib.parse.quote(try_url, safe='')
                                    proxy_url = f"http://127.0.0.1:{port}/mouflon_proxy?url={encoded_url}&pkey={cur_pkey}&psch={cur_psch}&username={username}"
                                    logger.info(f"Returning proxy URL for {username} with pdkey available: {proxy_url[:200]}...")
                                    return proxy_url
                                else:
                                    # No pdkey for this pkey, try to find any available key as fallback (some players do this)
                                    # Check if we have any keys at all, if yes, use first one as fallback to try decoding
                                    if mouflon_keys:
                                        # Pick a key that starts with zokee or first available
                                        fallback_pkey = None
                                        for k in mouflon_keys.keys():
                                            if k.lower().startswith('zokee'):
                                                fallback_pkey = k
                                                break
                                        if not fallback_pkey:
                                            fallback_pkey = next(iter(mouflon_keys.keys()))
                                        import os, urllib.parse
                                        port = os.getenv("PORT", "8080")
                                        encoded_url = urllib.parse.quote(try_url, safe='')
                                        proxy_url = f"http://127.0.0.1:{port}/mouflon_proxy?url={encoded_url}&pkey={fallback_pkey}&psch={cur_psch}&username={username}"
                                        logger.warning(f"Found LIVE HLS for {username} but no pdkey for pkey {cur_pkey}, using fallback pkey {fallback_pkey} proxy: {proxy_url[:200]}")
                                        return proxy_url
                                    logger.warning(f"Found LIVE HLS for {username} but no pdkey for pkey {cur_pkey} (have {len(mouflon_keys)} keys, need {cur_pkey}). Returning direct URL which will likely fail with 404 for media.mp4. Provide keys via stripchat_mouflon_keys.json")
                                    return try_url
                            else:
                                logger.debug(f"Variant {try_url} not live, len {len(var_content)}")
                        except Exception as e:
                            logger.debug(f"Variant fetch failed {try_url}: {e}")
                            continue

            except Exception as e:
                logger.debug(f"Master fetch failed {master_url}: {e}")
                continue

    return None


def _extract_stripchat_custom_sync(url: str, headers: Dict[str, str]) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    import urllib.request, json

    username = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    if not username:
        return None, "", None, "❌ Invalid username"

    model_id, api_uname, thumb_api, api_err, is_live_api = _extract_stripchat_api_sync(username, headers)

    if api_err:
        return None, username, thumb_api, api_err

    if model_id and is_live_api:
        hosts = _fetch_hosts_from_webpage_sync(username, headers)
        extra_hosts = ["doppiocdn.com", "doppiocdn.live", "doppiocdn.net", "doppiocdn.media"]
        for eh in extra_hosts:
            if eh not in hosts:
                hosts.append(eh)
        working_hls = _try_find_working_hls(model_id, username, hosts, headers)
        if working_hls:
            return working_hls, username, thumb_api, None
        else:
            logger.info(f"API says live but no HLS found, trying webpage fallback")

    # Webpage fallback
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://stripchat.com/",
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(f"https://stripchat.com/{username}", headers=req_headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        data = _extract_preloaded_state(html)
        if not data:
            return None, username, thumb_api, "💤 **Model OFFLINE or webpage parse failed**"

        view_cam = data.get("viewCam", {})
        show = view_cam.get("show")
        is_show_available = view_cam.get("isShowAvailable")
        private_mode = view_cam.get("privateMode", "")
        is_cam_available = view_cam.get("isCamAvailable")
        is_cam_active = view_cam.get("isCamActive")
        model_obj = view_cam.get("model", {}) or view_cam.get("viewCamBase", {}).get("model", {})
        if not model_obj.get("id"):
            try:
                model_obj = data.get("viewCamBase", {}).get("model", {})
            except:
                pass
        model_id_web = model_obj.get("id") or model_obj.get("streamName") or (data.get("user", {}).get("user", {}).get("id"))
        thumb_url = model_obj.get("previewUrl") or model_obj.get("avatarUrl") or thumb_api

        if _is_show_currently_active(show, is_show_available, private_mode):
            mode = show.get("mode", "private") if isinstance(show, dict) else "private"
            return None, username, thumb_url, f"🔒 **Model is in a Private / {mode.upper()} Show.**"

        is_live = model_obj.get("isLive")
        if is_live is False:
            return None, username, thumb_url, "💤 **Model is currently OFFLINE.**"
        if is_cam_available is False and is_cam_active is False:
            if not model_id and not model_id_web:
                return None, username, thumb_url, "💤 **Model is currently OFFLINE.**"

        final_model_id = str(model_id_web or model_id or "")
        if not final_model_id:
            return None, username, thumb_url, "❌ Model ID missing"

        hosts = _collect_hls_hosts(data)
        working_hls = _try_find_working_hls(final_model_id, username, hosts, req_headers)
        if working_hls:
            return working_hls, username, thumb_url, None
        else:
            # Check if we detected AD only
            mouflon_keys = load_mouflon_keys()
            if not mouflon_keys:
                return None, username, thumb_url, "⚠️ **AD Detected (20sec 661kb):** Stripchat now requires Mouflon decryption keys (pkey:pdkey). Bot fetched only AD VOD. Please provide keys via `stripchat_mouflon_keys.json` or `MOUFLON_KEYS` env var. See https://github.com/ChanTrail/StripchatRecorder for how to get keys. Without keys, live recording not possible."
            return None, username, thumb_url, "❌ Model appears live but no working HLS edge found (retry in 15s)"

    except Exception as e:
        logger.debug(f"Webpage fallback error for {username}: {e}")
        if model_id:
            hosts = ["doppiocdn.com", "doppiocdn.live", "doppiocdn.net"]
            working = _try_find_working_hls(model_id, username, hosts, headers)
            if working:
                return working, username, thumb_api, None
        return None, username, thumb_api, None


def _extract_direct_hls_from_webpage_sync(url: str, headers: Dict[str, str]) -> Tuple[Optional[str], str]:
    import urllib.request
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if headers:
            for k, v in headers.items():
                req_headers[k] = v
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            html_content = resp.read().decode("utf-8", errors="ignore")
        title_match = re.search(r"<title>(.*?)</title>", html_content, flags=re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else ""
        matches = re.findall(r"https?://[^\s\"'<>]+?\.m3u8(?:[^\s\"'<>]*)?", html_content, flags=re.IGNORECASE)
        if matches:
            valid = []
            for m in matches:
                m_clean = m.replace("\\u0026", "&").replace("\\/", "/")
                if not any(bad in m_clean.lower() for bad in ["_blurred", "preview", "thumb", "sample"]):
                    valid.append(m_clean)
            if valid:
                for v in valid:
                    if "_auto.m3u8" in v or "master" in v or "playlist" in v:
                        return v, title
                return valid[0], title
    except Exception as e:
        logger.debug(f"ProStreamFinder fallback: {e}")
    return None, ""


def _extract_ytdlp_sync(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "http_headers": headers or {},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as e:
        logger.debug(f"ytdlp sync error: {e}")
        return {}


def classify_extraction_error(error_str: str) -> str:
    err_lower = error_str.lower()
    if any(x in err_lower for x in ["private show", "ticket show", "private room", "password", "requires authentication", "members only"]):
        return "🔒 **Model is in a Private / Ticket Show.**"
    if any(x in err_lower for x in ["offline", "not broadcasting", "is not live"]):
        return "💤 **Model is currently OFFLINE.**"
    if any(x in err_lower for x in ["403", "forbidden", "unauthorized", "401"]):
        return "🚫 **403 Forbidden.**"
    if any(x in err_lower for x in ["404", "not found"]):
        return "❌ **404 Not Found.**"
    return f"⚠️ **Stream Extraction Error:** `{error_str[:150]}`"


async def resolve_stream_url(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[str, str, Optional[str], Dict[str, str], Optional[str]]:
    combined_headers = {}
    if headers:
        combined_headers.update(headers)

    normalized = normalize_stream_url(url)

    if is_protected_platform_url(normalized):
        return normalized, "", None, combined_headers, (
            "⚠️ **Unsupported Protected Source:** This bot will not proxy, decrypt, or bypass platform-protected streams. "
            "Use a source URL that is directly playable by FFmpeg and that you are authorized to record."
        )

    if is_explicit_direct_link(normalized):
        return normalized, "", None, combined_headers, None

    try:
        loop = asyncio.get_event_loop()
        discovered_m3u8, page_title = await loop.run_in_executor(None, _extract_direct_hls_from_webpage_sync, normalized, combined_headers)
        if discovered_m3u8:
            return discovered_m3u8, page_title, None, combined_headers, None
    except Exception as e:
        logger.debug(f"ProStreamFinder exception: {e}")

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _extract_ytdlp_sync, normalized, combined_headers)
        title = data.get("title", "")
        extracted_url = data.get("url", "")
        thumbnail_url = data.get("thumbnail", "")
        yt_headers = data.get("http_headers", {})
        if yt_headers and isinstance(yt_headers, dict):
            for k, v in yt_headers.items():
                if k not in combined_headers:
                    combined_headers[k] = v
        web_thumb_path = None
        if thumbnail_url:
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", title[:15]) or auto_generate_job_name(normalized)
            web_thumb_path = await download_web_thumbnail(thumbnail_url, clean_name)
        if extracted_url:
            return extracted_url, title, web_thumb_path, combined_headers, None
    except Exception as e:
        logger.warning(f"yt-dlp extraction failed for {normalized}: {e}")
        return normalized, "", None, combined_headers, classify_extraction_error(str(e))

    return normalized, "", None, combined_headers, None


async def get_stream_qualities(url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    return [
        {"id": "best", "label": "🌟 Best Quality", "desc": "Highest"},
        {"id": "720p", "label": "📺 720p", "desc": "720p"},
        {"id": "480p", "label": "📱 480p", "desc": "480p"},
        {"id": "360p", "label": "📶 360p", "desc": "Low"},
        {"id": "audio", "label": "🎵 Audio Only", "desc": "Audio"},
    ]


async def get_video_metadata(file_path: str) -> Dict[str, int]:
    metadata = {"duration": 0, "width": 0, "height": 0}
    if not os.path.exists(file_path):
        return metadata
    cmd = ["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,duration:format=duration","-of","json",file_path]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            if streams:
                s0 = streams[0]
                metadata["width"] = int(s0.get("width", 0) or 0)
                metadata["height"] = int(s0.get("height", 0) or 0)
                dur_str = s0.get("duration", "0")
                if dur_str and dur_str != "N/A":
                    metadata["duration"] = int(float(dur_str))
            if metadata["duration"] == 0:
                fmt = data.get("format", {})
                fmt_dur = fmt.get("duration", "0")
                if fmt_dur and fmt_dur != "N/A":
                    metadata["duration"] = int(float(fmt_dur))
    except Exception as e:
        logger.debug(f"ffprobe failed: {e}")
    if metadata["duration"] == 0 or metadata["width"] == 0:
        try:
            proc = await asyncio.create_subprocess_exec("ffmpeg","-i",file_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            output_str = stderr.decode("utf-8", errors="ignore")
            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output_str)
            if dur_match and metadata["duration"] == 0:
                h,m,s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
                metadata["duration"] = int(h*3600+m*60+s)
            dim_match = re.search(r",\s*(\d{3,4})x(\d{3,4})\s*(?:\[|,)", output_str)
            if dim_match and metadata["width"] == 0:
                metadata["width"] = int(dim_match.group(1))
                metadata["height"] = int(dim_match.group(2))
        except Exception as e:
            logger.debug(f"ffmpeg fallback failed: {e}")
    return metadata


async def generate_thumbnail(file_path: str, job_name: str, duration: int = 0, web_thumb_path: Optional[str] = None) -> Optional[str]:
    if web_thumb_path and os.path.exists(web_thumb_path) and os.path.getsize(web_thumb_path) > 500:
        return web_thumb_path
    if not os.path.exists(file_path):
        return None
    seek_sec = 5 if duration >= 5 else 1
    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_thumb.jpg")
    if os.path.exists(thumb_path):
        try: os.remove(thumb_path)
        except: pass
    cmd = ["ffmpeg","-y","-ss",str(seek_sec),"-i",file_path,"-vframes","1","-q:v","2",thumb_path]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.wait()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail error for {job_name}: {e}")
    return None


def cleanup_job_files(job_name: str, file_path: Optional[str] = None):
    removed_count = 0
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            removed_count += 1
        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {e}")
    ffmpeg_log = os.path.join(RECORDINGS_DIR, f"{job_name}_ffmpeg.log")
    if os.path.exists(ffmpeg_log):
        try: os.remove(ffmpeg_log); removed_count+=1
        except: pass
    for root_dir in [RECORDINGS_DIR, "."]:
        try:
            for f in os.listdir(root_dir):
                if f.startswith(f"{job_name}_") and f.endswith(".jpg"):
                    p=os.path.join(root_dir,f)
                    if os.path.exists(p):
                        os.remove(p); removed_count+=1
        except Exception as e:
            logger.debug(f"Thumb cleanup error: {e}")
    try:
        for f in os.listdir(SPLITS_DIR):
            if f.startswith(job_name):
                p=os.path.join(SPLITS_DIR,f)
                if os.path.exists(p):
                    os.remove(p); removed_count+=1
    except: pass
    try:
        for f in os.listdir(RECORDINGS_DIR):
            if f.startswith(f"{job_name}_") and any(f.endswith(ext) for ext in [".mp4",".ts",".mkv",".mp3",".m4a",".jpg",".log"]):
                p=os.path.join(RECORDINGS_DIR,f)
                if os.path.exists(p):
                    os.remove(p); removed_count+=1
    except: pass
    gc.collect()
    logger.info(f"Auto-cleanup for '{job_name}' — {removed_count} files removed.")
