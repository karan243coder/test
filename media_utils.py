"""
media_utils.py - V6 FIXED - API-FIRST Stripchat extractor
Why previous fix still showed private for Busty_priya69?
- Webpage __PRELOADED_STATE__ keeps old show object even after private ended.
- API v2 says show:null, isCamAvailable:true -> actually public.
So now:
  1. API v2 is PRIMARY for online/offline/private truth
  2. Webpage is only for fallbackDomains + hlsStreamHost extraction + thumbnail
  3. Private detection checks endedAt timestamp (ignore if ended >60s ago) + isShowAvailable + privateMode
  4. Validates HLS playlist contains #EXT-X-STREAM-INF
"""

import os
import re
import json
import time
import asyncio
import logging
import gc
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)


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
    # profile pages are NOT direct even though they have stripchat.com
    if "stripchat.com/" in url_lower and ".m3u8" not in url_lower:
        # might still be direct if doppiocdn
        if "doppiocdn" not in url_lower and "edge-hls" not in url_lower:
            return False
    if any(x in url_lower for x in [".m3u8", ".mp4", ".m4a", ".ts", ".mpd"]):
        # ensure it's not just a webpage url
        if ".m3u8" in url_lower or url_lower.startswith("https://edge-hls") or "doppiocdn" in url_lower:
            return True
        # generic mp4 might still be direct if not stripchat profile
        if "stripchat.com/" not in url_lower:
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
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
    # dedup
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
    """
    Determine if show dict represents ACTIVE private show, not history.
    Logic:
    - If isShowAvailable True -> active
    - If privateMode non-empty -> active
    - If show has endedAt in past >90s -> not active
    - If show isDeleted True -> not active
    """
    if not show or not isinstance(show, dict):
        return False
    # If explicitly marked deleted, not active
    if show.get("isDeleted"):
        return False
    # Check endedAt
    ended_at = show.get("endedAt")
    if ended_at:
        try:
            # ISO format like 2026-07-31T17:51:41Z
            dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            # If ended more than 90 seconds ago, it's history
            if (now - dt).total_seconds() > 90:
                return False
        except Exception as e:
            logger.debug(f"endedAt parse error {ended_at}: {e}")
            pass
    # If isShowAvailable True, definitely active private
    if isShowAvailable:
        return True
    # If privateMode non-empty like "p2p", "private", "group"
    if privateMode and privateMode != "":
        return True
    # If show exists and has no endedAt and not deleted, and isShowAvailable is None? 
    # We treat as active only if isShowAvailable is not explicitly False?
    # In Busty case, isShowAvailable=False and endedAt in past -> should be inactive
    # For safety, if isShowAvailable is False, treat as inactive
    if isShowAvailable is False:
        return False
    # Fallback: if show has no endedAt and isShowAvailable not False, treat as active
    if not ended_at:
        return True
    return False


def _extract_stripchat_api_sync(username: str, headers: Dict[str, str]) -> Tuple[Optional[str], str, Optional[str], Optional[str], bool]:
    """
    API-first extractor: calls /api/front/v2/models/username/{username}/cam
    Returns: (model_id_str_or_none, username, thumb_url, error_or_none, is_live_bool)
    """
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

            # Determine status
            if is_avail and is_active:
                # Check private
                if show is not None:
                    # show not null means currently in private/group/ticket
                    if isinstance(show, dict):
                        mode = show.get("mode", "private")
                        return None, username, thumb_url, f"🔒 **Model is in a Private / {mode.upper()} Show.**\nCurrently in {mode} mode, public stream unavailable. Try again later or use direct .m3u8 token if you have access.", False
                    else:
                        return None, username, thumb_url, "🔒 **Model is in a Private / Ticket Show.**", False
                if private_mode and private_mode != "":
                    return None, username, thumb_url, f"🔒 **Model is in Private Mode: {private_mode}.**", False
                # Public live
                return model_id, username, thumb_url, None, True
            else:
                # Not available
                if is_active:
                    # isCamActive true but not available -> likely private
                    return None, username, thumb_url, "🔒 **Model is in a Private / Ticket Show.**\nPublic profile link cannot record private shows without session cookies or direct token `.m3u8` link.", False
                else:
                    return None, username, thumb_url, "💤 **Model is currently OFFLINE or not broadcasting.**", False
    except Exception as e:
        # Check 404
        err_str = str(e)
        if "404" in err_str or "Not Found" in err_str:
            return None, username, None, "❌ **404 Not Found:** Model page does not exist.", False
        logger.debug(f"API v2 fetch error for {username}: {e}")
        return None, username, None, None, False  # signal to fallback


def _fetch_hosts_from_webpage_sync(username: str, headers: Dict[str, str]) -> List[str]:
    """Fetch webpage and extract fallbackDomains + hlsStreamHost"""
    import urllib.request
    try:
        url = f"https://stripchat.com/{username}"
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
    """Try each host to find valid HLS playlist"""
    import urllib.request
    ua = headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    for host in hosts:
        # Normalize host to edge-hls.{host}
        if host.startswith("edge-hls."):
            base = f"https://{host}"
        else:
            # Remove possible prefix b- etc
            clean_host = host.replace("b-", "").replace("edge-hls.", "")
            base = f"https://edge-hls.{clean_host}"
        # Main URL without query (yt-dlp style)
        candidates = [
            f"{base}/hls/{model_id}/master/{model_id}_auto.m3u8",
            f"{base}/hls/{model_id}/master/{model_id}.m3u8",
            f"{base}/hls/{model_id}/{model_id}.m3u8",
            f"{base}/hls/{model_id}/master/{model_id}_auto.m3u8?playlistType=standard",
        ]
        for hls_url in candidates:
            try:
                req = urllib.request.Request(hls_url, headers={
                    "User-Agent": ua,
                    "Referer": f"https://stripchat.com/{username}",
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req, timeout=7) as r:
                    content = r.read().decode("utf-8", errors="ignore")
                    if _validate_hls_content(content):
                        logger.info(f"Found working HLS for {username} on {host}: {hls_url}")
                        return hls_url
                    else:
                        logger.debug(f"Host {host} returned invalid playlist ({len(content)} chars)")
            except Exception as e:
                logger.debug(f"HLS try failed {hls_url}: {e}")
                continue
    return None


def _extract_stripchat_custom_sync(url: str, headers: Dict[str, str]) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    """
    NEW API-FIRST logic
    1. Extract username
    2. Call API v2 for truth about live/private/offline
    3. If public live, get model_id from API, then fetch webpage for hosts
    4. Try each host for valid HLS
    5. If API says offline/private, return error
    6. If API fails, fallback to webpage method with improved private check
    """
    import urllib.request, json

    username = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    if not username:
        return None, "", None, "❌ Invalid username"

    # Step 1: API truth
    model_id, api_uname, thumb_api, api_err, is_live_api = _extract_stripchat_api_sync(username, headers)

    if api_err:
        # API says offline/private
        return None, username, thumb_api, api_err

    if model_id and is_live_api:
        # Public live according to API, now find working HLS
        hosts = _fetch_hosts_from_webpage_sync(username, headers)
        # Ensure at least includes common
        extra_hosts = ["doppiocdn.com", "doppiocdn.live", "doppiocdn.net", "doppiocdn.media"]
        for eh in extra_hosts:
            if eh not in hosts:
                hosts.append(eh)
        working_hls = _try_find_working_hls(model_id, username, hosts, headers)
        if working_hls:
            return working_hls, username, thumb_api, None
        else:
            # API says live but no HLS found, maybe try webpage extractor as fallback
            logger.info(f"API says live but no HLS on hosts {hosts}, trying webpage extractor fallback")
            # continue to webpage fallback below

    # Step 2: Webpage fallback with improved logic
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
        # If model_obj still empty, try data['viewCamBase']['model']
        if not model_obj.get("id"):
            try:
                model_obj = data.get("viewCamBase", {}).get("model", {})
            except:
                pass
        model_id_web = model_obj.get("id") or model_obj.get("streamName") or (data.get("user", {}).get("user", {}).get("id"))
        thumb_url = model_obj.get("previewUrl") or model_obj.get("avatarUrl") or thumb_api

        # Improved private check
        if _is_show_currently_active(show, is_show_available, private_mode):
            mode = show.get("mode", "private") if isinstance(show, dict) else "private"
            return None, username, thumb_url, f"🔒 **Model is in a Private / {mode.upper()} Show.**\nPublic stream unavailable."

        # Offline check
        is_live = model_obj.get("isLive")
        if is_live is False:
            return None, username, thumb_url, "💤 **Model is currently OFFLINE or not broadcasting.**"
        if is_cam_available is False and is_cam_active is False:
            # Check if model status public but cam not available
            # Could be offline
            if not model_id and not model_id_web:
                return None, username, thumb_url, "💤 **Model is currently OFFLINE or not broadcasting.**"

        # If we have model id from webpage
        final_model_id = str(model_id_web or model_id or "")
        if not final_model_id:
            return None, username, thumb_url, "❌ Model ID missing"

        hosts = _collect_hls_hosts(data)
        working_hls = _try_find_working_hls(final_model_id, username, hosts, req_headers)
        if working_hls:
            return working_hls, username, thumb_url, None
        else:
            return None, username, thumb_url, "❌ Model appears live but no working HLS edge found (retry in 15s) - may be temporary CDN issue"

    except Exception as e:
        logger.debug(f"Webpage fallback error for {username}: {e}")
        # If we had API model_id earlier but HLS failed, try with API hosts
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
        matches = re.findall(
            r"https?://[^\s\"'<>]+?\.m3u8(?:[^\s\"'<>]*)?",
            html_content,
            flags=re.IGNORECASE
        )
        if matches:
            valid = []
            for m in matches:
                m_clean = m.replace("\\u0026", "&").replace("\\/", "/")
                if not any(bad in m_clean.lower() for bad in ["_blurred", "preview", "thumb", "sample"]):
                    valid.append(m_clean)
            if valid:
                for v in valid:
                    if "_auto.m3u8" in v or "master" in v or "playlist" in v:
                        logger.info(f"ProStreamFinder discovered master/auto HLS link: {v[:85]}...")
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
        return "🔒 **Model is in a Private / Ticket Show.**\nPublic profile link cannot record private shows without session cookies or direct token `.m3u8` link."
    if any(x in err_lower for x in ["offline", "not broadcasting", "is not live"]):
        return "💤 **Model is currently OFFLINE or not broadcasting.**"
    if any(x in err_lower for x in ["403", "forbidden", "unauthorized", "401"]):
        return "🚫 **403 Forbidden / Access Denied.**"
    if any(x in err_lower for x in ["404", "not found"]):
        return "❌ **404 Not Found:** Stream or model page does not exist."
    return f"⚠️ **Stream Extraction Error:** `{error_str[:150]}`"


async def resolve_stream_url(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[str, str, Optional[str], Dict[str, str], Optional[str]]:
    combined_headers = {}
    if headers:
        combined_headers.update(headers)

    normalized = normalize_stream_url(url)

    if is_explicit_direct_link(normalized):
        if "stripchat" in normalized.lower() or "doppiocdn" in normalized.lower():
            if "Referer" not in combined_headers:
                combined_headers["Referer"] = "https://stripchat.com/"
        return normalized, "", None, combined_headers, None

    if "stripchat.com" in normalized.lower():
        try:
            logger.info(f"Running API-FIRST StripchatExtractor on: {normalized}")
            loop = asyncio.get_event_loop()
            hls_url, uname, thumb_url, strip_err = await loop.run_in_executor(None, _extract_stripchat_custom_sync, normalized, combined_headers)
            web_thumb_path = None
            if thumb_url:
                web_thumb_path = await download_web_thumbnail(thumb_url, auto_generate_job_name(normalized))
            if "Referer" not in combined_headers:
                combined_headers["Referer"] = f"https://stripchat.com/{uname}" if uname else "https://stripchat.com/"
            if hls_url:
                logger.info(f"Extractor SUCCESS: {hls_url[:80]}...")
                return hls_url, uname, web_thumb_path, combined_headers, None
            elif strip_err:
                logger.info(f"Extractor says: {strip_err}")
                return normalized, uname, web_thumb_path, combined_headers, strip_err
        except Exception as e:
            logger.debug(f"Extractor exception: {e}")

    # Pro finder
    try:
        loop = asyncio.get_event_loop()
        discovered_m3u8, page_title = await loop.run_in_executor(None, _extract_direct_hls_from_webpage_sync, normalized, combined_headers)
        if discovered_m3u8:
            logger.info(f"ProStreamFinder resolved: {discovered_m3u8[:80]}...")
            return discovered_m3u8, page_title, None, combined_headers, None
    except Exception as e:
        logger.debug(f"ProStreamFinder exception: {e}")

    # yt-dlp
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
        {"id": "best", "label": "🌟 Best Quality (Default)", "desc": "Highest available"},
        {"id": "1080p", "label": "📺 1080p Full HD", "desc": "Max height 1080px"},
        {"id": "720p", "label": "🖥️ 720p HD", "desc": "Max height 720px"},
        {"id": "480p", "label": "📱 480p SD", "desc": "Max height 480px"},
        {"id": "360p", "label": "📶 360p Low", "desc": "Data saver"},
        {"id": "audio", "label": "🎵 Audio Only", "desc": "Audio only"},
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
