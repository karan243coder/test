from __future__ import annotations
import asyncio, logging, secrets, time
from dataclasses import dataclass
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from .config import Settings
from .urls import extract_supported_url, platform_name
from .recorder import RecordingManager
from .health import start_health_server
from .resolver import resolve_public_stream, PublicStreamUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = Settings.from_env()
manager = RecordingManager(settings)
app = Client("public_live_recorder", api_id=settings.api_id, api_hash=settings.api_hash, bot_token=settings.bot_token, workers=8)

@dataclass
class Preview:
    owner_id: int
    url: str
    platform: str
    created: float
    title: str = "Room URL received"
    availability: str = "Not checked yet"

previews: dict[str, Preview] = {}
_PREVIEW_TTL = 15 * 60


def cleanup_previews() -> None:
    now = time.monotonic()
    for token, item in list(previews.items()):
        if now - item.created > _PREVIEW_TTL:
            previews.pop(token, None)


def keyboard(token: str, active: bool = False) -> InlineKeyboardMarkup:
    if active:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Live Status", callback_data=f"lr:status:{token}"), InlineKeyboardButton("⏹ Stop & Upload", callback_data=f"lr:stop:{token}")],
            [InlineKeyboardButton("✖ Close", callback_data=f"lr:close:{token}")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Start 15 min", callback_data=f"lr:start:{token}:15"), InlineKeyboardButton("🔴 Start 30 min", callback_data=f"lr:start:{token}:30")],
        [InlineKeyboardButton("🔴 Start 60 min", callback_data=f"lr:start:{token}:60"), InlineKeyboardButton("🔴 Start maximum", callback_data=f"lr:start:{token}:0")],
        [InlineKeyboardButton("🔄 Check public stream", callback_data=f"lr:refresh:{token}"), InlineKeyboardButton("✖ Close", callback_data=f"lr:close:{token}")],
    ])


def preview_text(item: Preview) -> str:
    return (
        "<b>🎥 LIVE RECORDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Platform:</b> {item.platform}\n"
        f"<b>Room:</b> <code>{item.title[:90]}</code>\n"
        f"<b>Public playback:</b> {item.availability}\n\n"
        "Choose how long to record. The recording will be finalized and uploaded automatically.\n\n"
        "<i>Only publicly playable, authorized streams are supported. Private, paid, account-only and access-restricted rooms are not bypassed.</i>"
    )


@app.on_message(filters.private & filters.text & ~filters.via_bot)
async def link_handler(_: Client, message: Message):
    url = extract_supported_url(message.text or "")
    if not url:
        return
    cleanup_previews()
    token = secrets.token_urlsafe(7).replace("-", "a").replace("_", "b")[:10]
    item = Preview(owner_id=message.from_user.id, url=url, platform=platform_name(url), created=time.monotonic())
    previews[token] = item
    await message.reply_text(preview_text(item), parse_mode="html", reply_markup=keyboard(token), disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^lr:(start|refresh|status|stop|close):"))
async def controls(_: Client, query: CallbackQuery):
    parts = (query.data or "").split(":")
    action, token = parts[1], parts[2]
    item = previews.get(token)
    if not item or time.monotonic() - item.created > _PREVIEW_TTL:
        previews.pop(token, None)
        return await query.answer("This panel expired. Send the room link again.", show_alert=True)
    if query.from_user.id != item.owner_id:
        return await query.answer("Only the user who sent this link can control this recording.", show_alert=True)

    if action == "close":
        previews.pop(token, None)
        await query.answer("Closed")
        try: await query.message.delete()
        except Exception: pass
        return

    if action == "refresh":
        await query.answer("Checking public playback…")
        item.availability = "Checking…"
        await query.message.edit_text(preview_text(item), parse_mode="html", reply_markup=keyboard(token), disable_web_page_preview=True)
        try:
            _, title = await resolve_public_stream(item.url)
            item.title = title
            item.availability = "✅ Public playback resolved"
        except PublicStreamUnavailable:
            item.availability = "⚠️ Public playback unavailable right now"
        await query.message.edit_text(preview_text(item), parse_mode="html", reply_markup=keyboard(token), disable_web_page_preview=True)
        return

    if action == "start":
        if len(parts) != 4:
            return await query.answer("Invalid duration", show_alert=True)
        try: minutes = int(parts[3])
        except ValueError: return await query.answer("Invalid duration", show_alert=True)
        minutes = settings.max_record_minutes if minutes == 0 else min(max(1, minutes), settings.max_record_minutes)
        await query.answer("Recorder is starting…")
        try:
            await manager.start(app, query.message, query.message, item.url, minutes, owner_id=item.owner_id)
        except RuntimeError as exc:
            return await query.answer(str(exc), show_alert=True)
        item.availability = f"🔴 Recording requested — {minutes} min limit"
        await query.message.edit_text(preview_text(item), parse_mode="html", reply_markup=keyboard(token, active=True), disable_web_page_preview=True)
        return

    job = await manager.status(item.owner_id)
    if action == "status":
        if not job: return await query.answer("No active recording.", show_alert=True)
        await query.answer("Status updated")
        return await query.message.edit_text(job.progress.render() if job.progress else "🔎 Resolving public stream…", parse_mode="html", reply_markup=keyboard(token, active=True))
    if action == "stop":
        if not await manager.stop(item.owner_id): return await query.answer("No active recording.", show_alert=True)
        await query.answer("Stopping safely; final upload will follow.")
        return await query.message.edit_text("⏹ <b>Stop requested</b>\n\nFinalizing the saved recording. It will upload automatically when ready.", parse_mode="html", reply_markup=keyboard(token, active=True))

if __name__ == "__main__":
    # Koyeb requires a listener on $PORT; start it before Pyrogram connects.
    start_health_server()
    app.run()
