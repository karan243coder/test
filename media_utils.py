"""
media_utils.py - FIXED Level 1 & Level 2 Media & Stream Handling Utilities (512MB RAM Koyeb Optimized)
Fixed bugs:
  - Old extractor used hardcoded edge-hls.doppiocdn.org + fake token ?playlistType=standard which returns empty playlist -> 0 bytes
  - New extractor follows yt-dlp latest logic: parse window.__PRELOADED_STATE__ from webpage, get fallbackDomains + hlsStreamHost
  - Validates HLS content contains #EXT-X-STREAM-INF / #EXTINF before returning
  - Adds proper Referer header for stripchat
  - Supports both doppiocdn.com / .live / .org / edge domains
  - Falls back to API v2 only if webpage method fails, with validation
  - Improved thumbnail download
"""

import os
import re
import json
import time
import asyncio
import logging
import gc
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)


def clean_url_punctuation(url: str) -> str:
    url_clean = url.strip()
    while url_clean.endswith(".") or url_clean.endswith(",") or url_clean.endswith(";") or url_clean.endswith(")") or url_clean.endswith("]") or url_clean.endswith("!") or url_clean.endswith("?"):
        url_clean = url_clean[:-1]
    # strip trailing ellipsis ...
    if url_clean.endswith("..."):
        url_clean = url_clean[:-3]
    # also handle unicode ellipsis
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
    # vr.stripchat support -> keep as stripchat.com for extraction
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
    # If it looks like a direct HLS mp4 link with token, treat as direct
    # But profile pages should NOT be considered direct
    if "stripchat.com/" in url_lower and not url_lower.endswith(".m3u8") and ".m3u8" not in url_lower:
        return False
    if any(x in url_lower for x in [".m3u8", ".mp4", ".m4a", ".ts", ".mpd", "master", "playlist", "chunk", "hls", "live"]):
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
            req = urllib.request.Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": "https://stripchat.com/"})
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
    """Emulate yt-dlp lowercase_escape: convert \\xNN to \\u00NN and decode"""
    try:
        # Convert \xFF to \u00FF for json compatibility
        s = re.sub(r'\\x([0-9a-fA-F]{2})', r'\\u00\1', s)
        # Now json.loads the string if it's quoted? Actually yt-dlp does json parsing after
        return s
    except Exception:
        return s


def _extract_preloaded_state(html: str) -> Optional[Dict[str, Any]]:
    """Extract window.__PRELOADED_STATE__ JSON from HTML, similar to yt-dlp"""
    # Pattern 1: window.__PRELOADED_STATE__ = {...}
    patterns = [
        r'<script[^>]*>\s*window\.__PRELOADED_STATE__\s*=\s*({.+?})\s*</script>',
        r'window\.__PRELOADED_STATE__\s*=\s*({.+?});?\s*</script>',
        r'window\.__PRELOADED_STATE__\s*=\s*({.*})\s*',
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.DOTALL)
        if m:
            json_str = m.group(1)
            try:
                json_str = _lowercase_escape(json_str)
                data = json.loads(json_str)
                return data
            except Exception as e:
                # Try to handle trailing JS, cut at last }
                try:
                    # Find balanced braces? Simplify: try to load with cleanup
                    # Sometimes ends with </script> remnants
                    json_str = json_str.split("</script>")[0]
                    data = json.loads(json_str)
                    return data
                except Exception:
                    logger.debug(f"Failed to parse __PRELOADED_STATE__ json: {e}")
                    continue
    return None


def _collect_hls_hosts(data: Dict[str, Any]) -> List[str]:
    """Collect all possible hls hosts from preloaded state, yt-dlp logic"""
    hosts = []
    # Traverse possible paths
    # Path: config.data.hlsStreamHost
    try:
        h = data.get("config", {}).get("data", {}).get("hlsStreamHost")
        if h and isinstance(h, str):
            hosts.append(h)
    except: pass
    # Path: config.data.features.featuresV2.hlsFallback.fallbackDomains
    try:
        domains = data.get("config", {}).get("data", {}).get("features", {}).get("featuresV2", {}).get("hlsFallback", {}).get("fallbackDomains")
        if isinstance(domains, list):
            hosts.extend([x for x in domains if isinstance(x, str)])
    except: pass
    # Path: config.data.hlsFallback.fallbackDomains
    try:
        domains = data.get("config", {}).get("data", {}).get("hlsFallback", {}).get("fallbackDomains")
        if isinstance(domains, list):
            hosts.extend([x for x in domains if isinstance(x, str)])
    except: pass
    # Path: configV3.static.hlsStreamHost
    try:
        h = data.get("configV3", {}).get("static", {}).get("hlsStreamHost")
        if h and isinstance(h, str):
            hosts.append(h)
    except: pass
    # Path: configV3.static.features.hlsFallback.fallbackDomains
    try:
        domains = data.get("configV3", {}).get("static", {}).get("features", {}).get("hlsFallback", {}).get("fallbackDomains")
        if isinstance(domains, list):
            hosts.extend([x for x in domains if isinstance(x, str)])
    except: pass
    # Path: configV3.static.features.featuresV2.hlsFallback.fallbackDomains
    try:
        domains = data.get("configV3", {}).get("static", {}).get("features", {}).get("featuresV2", {}).get("hlsFallback", {}).get("fallbackDomains")
        if isinstance(domains, list):
            hosts.extend([x for x in domains if isinstance(x, str)])
    except: pass

    # Deduplicate, keep order
    seen = set()
    uniq = []
    for h in hosts:
        if h not in seen and h:
            seen.add(h)
            uniq.append(h)
    # Add common defaults if empty
    if not uniq:
        uniq = ["doppiocdn.com", "doppiocdn.live"]
    return uniq


def _validate_hls_content(content: str) -> bool:
    """Check if m3u8 content is a valid live playlist"""
    if not content or "#EXTM3U" not in content:
        return False
    # Must have at least one stream or segment
    if "#EXT-X-STREAM-INF" in content or "#EXTINF" in content or "EXT-X-MEDIA" in content:
        return True
    return False


def _extract_stripchat_from_webpage_sync(url: str, headers: Dict[str, str]) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    """
    FIXED Custom Stripchat extractor based on yt-dlp latest:
    1. Fetch webpage HTML
    2. Extract __PRELOADED_STATE__
    3. Check isLive / private show
    4. Extract model_id and hosts, then try each HLS URL
    Returns: (hls_url, username, thumb_url, error)
    """
    import urllib.request
    try:
        username = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
        if not username:
            return None, "", None, "❌ Invalid username"

        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://stripchat.com/",
        }
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        page_title = title_match.group(1).strip() if title_match else username

        data = _extract_preloaded_state(html)
        if not data:
            logger.debug("No __PRELOADED_STATE__ found, falling back to API")
            return None, username, None, None  # Let API fallback handle

        # Check private show
        view_cam = data.get("viewCam", {})
        show = view_cam.get("show")
        if isinstance(show, dict) and show:
            # Private / ticket show
            return None, username, None, "🔒 **Model is in a Private / Ticket Show.**\nPublic profile link cannot record private shows without session cookies or direct token `.m3u8` link."

        model_obj = view_cam.get("model", {})
        is_live = model_obj.get("isLive")
        if is_live is False:
            return None, username, None, "💤 **Model is currently OFFLINE or not broadcasting.**"

        model_id = model_obj.get("id")
        if not model_id:
            # Try alternative location user.user.id
            try:
                model_id = data.get("user", {}).get("user", {}).get("id")
            except:
                pass
        if not model_id:
            return None, username, None, "❌ Model ID missing in webpage config"

        # Thumbnail
        thumb_url = None
        try:
            user_obj = data.get("user", {}).get("user", {}) or model_obj
            thumb_url = user_obj.get("previewUrl") or user_obj.get("avatarUrl") or model_obj.get("previewUrl")
        except:
            pass

        hosts = _collect_hls_hosts(data)
        logger.info(f"Stripchat webpage config: model_id={model_id}, hosts={hosts}, isLive={is_live}")

        # Try each host
        for host in hosts:
            # Ensure host formatting: edge-hls.{host}
            # If host already starts with edge-hls, use directly
            if host.startswith("edge-hls."):
                base = f"https://{host}"
            else:
                base = f"https://edge-hls.{host}"
            # yt-dlp latest format without query
            hls_url = f"{base}/hls/{model_id}/master/{model_id}_auto.m3u8"
            try:
                req_hls = urllib.request.Request(hls_url, headers={
                    "User-Agent": req_headers["User-Agent"],
                    "Referer": f"https://stripchat.com/{username}",
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req_hls, timeout=8) as r_hls:
                    content = r_hls.read().decode("utf-8", errors="ignore")
                    if _validate_hls_content(content):
                        logger.info(f"FixedExtractor discovered valid CDN node ({host}): {hls_url}")
                        return hls_url, username, thumb_url, None
                    else:
                        logger.debug(f"Host {host} returned invalid playlist: {content[:200]}")
            except Exception as e:
                logger.debug(f"Host {host} failed: {e}")
                continue

            # Also try with ?playlistType=standard as fallback? No, try without
            # Try old b-hls format: https://b-{num}.{host}/hls/{id}/{id}.m3u8 - not needed but fallback
            # Try with query param lowLatency
            for playlist_type in ["", "?playlistType=standard", "?playlistType=lowLatency"]:
                hls_try = f"{base}/hls/{model_id}/master/{model_id}_auto.m3u8{playlist_type}"
                if hls_try == hls_url:
                    continue
                try:
                    req_hls = urllib.request.Request(hls_try, headers={
                        "User-Agent": req_headers["User-Agent"],
                        "Referer": f"https://stripchat.com/{username}",
                    })
                    with urllib.request.urlopen(req_hls, timeout=6) as r_hls:
                        content = r_hls.read().decode("utf-8", errors="ignore")
                        if _validate_hls_content(content):
                            logger.info(f"FixedExtractor fallback playlist type ({playlist_type}) worked: {hls_try}")
                            return hls_try, username, thumb_url, None
                except:
                    continue

        return None, username, thumb_url, "❌ Model appears live but no working HLS edge found (try again in 10s)"

    except Exception as e:
        logger.debug(f"Fixed webpage extractor error: {e}")
        return None, "", None, None


def _extract_stripchat_custom_sync(url: str, headers: Dict[str, str]) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    """
    Wrapper that tries webpage method first, then old API v2 fallback with validation
    """
    # 1. Try fixed webpage method (yt-dlp style) - PRIMARY
    hls, uname, thumb, err = _extract_stripchat_from_webpage_sync(url, headers)
    if hls:
        return hls, uname, thumb, None
    if err:
        # If explicit offline/private error, return it
        return None, uname, thumb, err

    # 2. Fallback to old API v2 (legacy)
    import urllib.request
    try:
        username = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
        api_url = f"https://stripchat.com/api/front/v2/models/username/{username}/cam"
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://stripchat.com/{username}",
        }
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(api_url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            cam = data.get("cam", {})
            user_obj = data.get("user", {}).get("user", {})
            is_avail = cam.get("isCamAvailable", False)
            is_active = cam.get("isCamActive", False)
            thumb_url = user_obj.get("previewUrl") or user_obj.get("avatarUrl")

            if is_avail:
                model_id = str(cam.get("streamName") or user_obj.get("id") or "")
                if not model_id:
                    return None, username, thumb_url, "❌ Model ID missing in Stripchat API response"

                # Try multiple hosts with validation
                servers = [
                    "doppiocdn.com",
                    "doppiocdn.live",
                    "doppiocdn.org",
                    "edge-hls.doppiocdn.com",
                    "edge-hls.doppiocdn.live",
                ]
                for s in servers:
                    if s.startswith("edge-hls."):
                        hls_url = f"https://{s}/hls/{model_id}/master/{model_id}_auto.m3u8"
                    else:
                        hls_url = f"https://edge-hls.{s}/hls/{model_id}/master/{model_id}_auto.m3u8"
                    try:
                        req_hls = urllib.request.Request(hls_url, headers={"User-Agent": req_headers["User-Agent"], "Referer": f"https://stripchat.com/{username}"})
                        with urllib.request.urlopen(req_hls, timeout=5) as r_hls:
                            content = r_hls.read().decode("utf-8", errors="ignore")
                            if _validate_hls_content(content):
                                logger.info(f"Legacy API fallback discovered active CDN ({s}): {hls_url}")
                                return hls_url, username, thumb_url, None
                    except Exception:
                        pass
                return None, username, thumb_url, "❌ Model is marked live, but all edge CDN nodes returned unreachable"
            else:
                if is_active:
                    return None, username, thumb_url, "🔒 **Model is in a Private / Ticket Show.**\nPublic profile link cannot record private shows without session cookies or direct token `.m3u8` link."
                else:
                    return None, username, thumb_url, "💤 **Model is currently OFFLINE or not broadcasting.**"
    except Exception as e:
        logger.debug(f"CustomStripchatExtractor fallback error: {e}")

    return None, "", None, None


def _extract_direct_hls_from_webpage_sync(url: str, headers: Dict[str, str]) -> Tuple[Optional[str], str]:
    import urllib.request
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
                logger.info(f"ProStreamFinder discovered valid HLS link: {valid[0][:85]}...")
                return valid[0], title
    except Exception as e:
        logger.debug(f"ProStreamFinder HTML check fallback: {e}")
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
        return "🚫 **403 Forbidden / Access Denied.**\nStream server rejected access. Provide direct token `.m3u8` link or Referer/Cookie headers."
    if any(x in err_lower for x in ["404", "not found"]):
        return "❌ **404 Not Found:** Stream or model page does not exist."
    return f"⚠️ **Stream Extraction Error:** `{error_str[:150]}`"


async def resolve_stream_url(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[str, str, Optional[str], Dict[str, str], Optional[str]]:
    combined_headers = {}
    if headers:
        combined_headers.update(headers)

    normalized = normalize_stream_url(url)

    # 0. explicit direct
    if is_explicit_direct_link(normalized):
        # For direct m3u8, add referer if stripchat
        if "stripchat" in normalized or "doppiocdn" in normalized:
            if "Referer" not in combined_headers:
                combined_headers["Referer"] = "https://stripchat.com/"
        return normalized, "", None, combined_headers, None

    # 1. Custom Stripchat extractor
    if "stripchat.com" in normalized.lower():
        try:
            logger.info(f"Running FixedStripchatExtractor on: {normalized}")
            loop = asyncio.get_event_loop()
            hls_url, uname, thumb_url, strip_err = await loop.run_in_executor(None, _extract_stripchat_custom_sync, normalized, combined_headers)
            web_thumb_path = None
            if thumb_url:
                web_thumb_path = await download_web_thumbnail(thumb_url, auto_generate_job_name(normalized))
            # Ensure Referer header for ffmpeg
            if "Referer" not in combined_headers:
                combined_headers["Referer"] = f"https://stripchat.com/{uname}" if uname else "https://stripchat.com/"
            if hls_url:
                logger.info(f"FixedExtractor resolved live stream: {hls_url[:80]}...")
                return hls_url, uname, web_thumb_path, combined_headers, None
            elif strip_err:
                logger.info(f"FixedExtractor status alert: {strip_err}")
                return normalized, uname, web_thumb_path, combined_headers, strip_err
        except Exception as e:
            logger.debug(f"FixedExtractor check exception: {e}")

    # 2. Pro Direct Stream Finder
    try:
        logger.info(f"Running ProStreamFinder on: {normalized}")
        loop = asyncio.get_event_loop()
        discovered_m3u8, page_title = await loop.run_in_executor(None, _extract_direct_hls_from_webpage_sync, normalized, combined_headers)
        if discovered_m3u8:
            logger.info(f"ProStreamFinder resolved direct HLS link: {discovered_m3u8[:80]}...")
            return discovered_m3u8, page_title, None, combined_headers, None
    except Exception as e:
        logger.debug(f"ProStreamFinder check exception: {e}")

    # 3. yt-dlp fallback
    try:
        logger.info(f"Resolving URL via Python yt-dlp library: {normalized}")
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
            logger.info(f"Successfully resolved stream URL via Python yt-dlp: {title}")
            return extracted_url, title, web_thumb_path, combined_headers, None
    except ImportError:
        logger.debug("Python yt_dlp module not installed; falling back to CLI subprocess...")
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"Python yt_dlp extraction failed for {normalized}: {err_msg}")
        if not is_explicit_direct_link(normalized):
            return normalized, "", None, combined_headers, classify_extraction_error(err_msg)

    # 4. CLI yt-dlp
    try:
        import json
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout", "15",
        ]
        if combined_headers.get("User-Agent"):
            cmd.extend(["--user-agent", combined_headers["User-Agent"]])
        if combined_headers.get("Referer"):
            cmd.extend(["--referer", combined_headers["Referer"]])
        cmd.append(normalized)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
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
                logger.info(f"Successfully resolved stream URL via yt-dlp CLI: {title}")
                return extracted_url, title, web_thumb_path, combined_headers, None
        else:
            err_output = stderr.decode("utf-8", errors="ignore")
            if not is_explicit_direct_link(normalized) and err_output:
                return normalized, "", None, combined_headers, classify_extraction_error(err_output)
    except Exception as e:
        logger.debug(f"yt-dlp CLI extraction fallback error for {normalized}: {e}")

    return normalized, "", None, combined_headers, None


async def get_stream_qualities(url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    qualities = [
        {"id": "best", "label": "🌟 Best Quality (Default)", "desc": "Highest available video & audio"},
        {"id": "1080p", "label": "📺 1080p Full HD", "desc": "Max height 1080px"},
        {"id": "720p", "label": "🖥️ 720p HD", "desc": "Max height 720px"},
        {"id": "480p", "label": "📱 480p SD", "desc": "Max height 480px"},
        {"id": "360p", "label": "📶 360p Low", "desc": "Data saver mode"},
        {"id": "audio", "label": "🎵 Audio Only (MP3)", "desc": "Extract audio stream only"},
    ]
    return qualities


async def get_video_metadata(file_path: str) -> Dict[str, int]:
    metadata = {"duration": 0, "width": 0, "height": 0}
    if not os.path.exists(file_path):
        return metadata
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json",
        file_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
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
        logger.debug(f"ffprobe metadata check failed: {e}")
    if metadata["duration"] == 0 or metadata["width"] == 0:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            output_str = stderr.decode("utf-8", errors="ignore")
            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output_str)
            if dur_match and metadata["duration"] == 0:
                h = int(dur_match.group(1))
                m = int(dur_match.group(2))
                s = float(dur_match.group(3))
                metadata["duration"] = int(h * 3600 + m * 60 + s)
            dim_match = re.search(r",\s*(\d{3,4})x(\d{3,4})\s*(?:\[|,)", output_str)
            if dim_match and metadata["width"] == 0:
                metadata["width"] = int(dim_match.group(1))
                metadata["height"] = int(dim_match.group(2))
        except Exception as e:
            logger.debug(f"ffmpeg metadata fallback failed: {e}")
    return metadata


async def generate_thumbnail(file_path: str, job_name: str, duration: int = 0, web_thumb_path: Optional[str] = None) -> Optional[str]:
    if web_thumb_path and os.path.exists(web_thumb_path) and os.path.getsize(web_thumb_path) > 500:
        return web_thumb_path
    if not os.path.exists(file_path):
        return None
    seek_sec = 5 if duration >= 5 else 1
    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_thumb.jpg")
    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except:
            pass
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_sec),
        "-i", file_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            logger.info(f"Thumbnail generated: {thumb_path}")
            return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail generation error for {job_name}: {e}")
    return None


def cleanup_job_files(job_name: str, file_path: Optional[str] = None):
    removed_count = 0
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            removed_count += 1
            logger.info(f"Cleaned up main file: {file_path}")
        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {e}")
    # cleanup ffmpeg log
    ffmpeg_log = os.path.join(RECORDINGS_DIR, f"{job_name}_ffmpeg.log")
    if os.path.exists(ffmpeg_log):
        try:
            os.remove(ffmpeg_log)
            removed_count += 1
        except: pass
    for root_dir in [RECORDINGS_DIR, "."]:
        try:
            for f in os.listdir(root_dir):
                if f.startswith(f"{job_name}_") and f.endswith(".jpg"):
                    p = os.path.join(root_dir, f)
                    if os.path.exists(p):
                        os.remove(p)
                        removed_count += 1
        except Exception as e:
            logger.debug(f"Thumb cleanup error: {e}")
    try:
        for f in os.listdir(SPLITS_DIR):
            if f.startswith(job_name):
                p = os.path.join(SPLITS_DIR, f)
                if os.path.exists(p):
                    os.remove(p)
                    removed_count += 1
    except Exception as e:
        logger.debug(f"Splits cleanup error: {e}")
    try:
        for f in os.listdir(RECORDINGS_DIR):
            if f.startswith(f"{job_name}_") and any(f.endswith(ext) for ext in [".mp4", ".ts", ".mkv", ".mp3", ".m4a", ".jpg", ".log"]):
                p = os.path.join(RECORDINGS_DIR, f)
                if os.path.exists(p):
                    os.remove(p)
                    removed_count += 1
    except Exception as e:
        logger.debug(f"Remaining recordings cleanup error: {e}")
    gc.collect()
    logger.info(f"Auto-cleanup completed for job '{job_name}' — {removed_count} files removed. Memory reclaimed.")
