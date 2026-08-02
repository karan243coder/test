"""Resolve only publicly playable streams through yt-dlp.

No cookies, browser automation, paywall/private-room bypass, or anti-bot bypass
is implemented here. If the platform does not make playback publicly available,
the bot returns an error instead of attempting to evade access controls.
"""
from __future__ import annotations
import asyncio, json

class PublicStreamUnavailable(RuntimeError): pass

async def resolve_public_stream(url: str) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--no-playlist", "--dump-single-json", "--no-warnings", "--skip-download", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill(); await proc.communicate(); raise PublicStreamUnavailable("Public stream resolver timed out.")
    if proc.returncode != 0:
        detail = err.decode("utf-8", "ignore").strip().splitlines()[-1:] or ["Public playback is unavailable."]
        raise PublicStreamUnavailable(detail[0][:300])
    try: info = json.loads(out.decode("utf-8"))
    except json.JSONDecodeError as exc: raise PublicStreamUnavailable("Resolver returned invalid metadata.") from exc
    stream_url = info.get("url")
    if not stream_url: raise PublicStreamUnavailable("No public playable media URL was provided by the platform.")
    title = str(info.get("title") or info.get("uploader") or "live_recording")
    return stream_url, title
