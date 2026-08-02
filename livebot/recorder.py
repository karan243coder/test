from __future__ import annotations
import asyncio, os, re, shutil, signal, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from pyrogram import Client, enums
from pyrogram.types import Message
from .progress import LiveProgress
from .resolver import resolve_public_stream, PublicStreamUnavailable
from .urls import platform_name


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w .-]+", "_", value, flags=re.UNICODE).strip("._ ")
    return (value or "live_recording")[:80]

@dataclass
class RecordingJob:
    job_id: str; user_id: int; source_url: str; platform: str; max_seconds: int; work_dir: Path
    progress_message: Message; title: str = "live"; process: asyncio.subprocess.Process | None = None
    stopped: bool = False; started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    progress: LiveProgress | None = None

class RecordingManager:
    def __init__(self, settings):
        self.settings = settings; self.jobs: dict[int, RecordingJob] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_recordings)
        self._lock = asyncio.Lock()

    async def start(self, app: Client, requester: Message, progress_message: Message, source_url: str, minutes: int, owner_id: int | None = None) -> RecordingJob:
        """Start a job owned by requester/owner_id; progress_message is the bot dashboard."""
        user_id = int(owner_id if owner_id is not None else requester.from_user.id)
        async with self._lock:
            if user_id in self.jobs: raise RuntimeError("You already have an active recording. Use /livestatus or /livestop.")
            job_id = uuid.uuid4().hex[:12]; work = self.settings.recordings_dir / str(user_id) / job_id; work.mkdir(parents=True)
            job = RecordingJob(job_id, user_id, source_url, platform_name(source_url), minutes * 60, work, progress_message)
            self.jobs[user_id] = job
        asyncio.create_task(self._run(app, job))
        return job

    async def status(self, user_id: int) -> RecordingJob | None: return self.jobs.get(user_id)

    async def stop(self, user_id: int) -> bool:
        job = self.jobs.get(user_id)
        if not job: return False
        job.stopped = True
        if job.process and job.process.returncode is None:
            try: os.killpg(job.process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        return True

    async def _disk_ok(self) -> bool:
        return shutil.disk_usage(self.settings.recordings_dir).free >= self.settings.max_disk_gb * 1024**3

    async def _edit(self, job: RecordingJob, force=False):
        try: await job.progress_message.edit_text(job.progress.render(), parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
        except Exception: pass

    async def _run(self, app: Client, job: RecordingJob):
        output = job.work_dir / "recording.mkv"
        try:
            async with self.semaphore:
                if not await self._disk_ok(): raise RuntimeError("Not enough free disk space to start safely.")
                job.progress = LiveProgress(job.platform, "resolving…", job.started_at)
                await self._edit(job)
                stream_url, title = await resolve_public_stream(job.source_url)
                job.title = safe_name(title); job.progress.title = job.title; job.progress.status = "Recording public live stream"
                await self._edit(job)
                # Matroska is resilient if a live stream ends unexpectedly. It is remuxed to MP4 after capture.
                cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10", "-i", stream_url, "-t", str(job.max_seconds), "-map", "0", "-c", "copy", "-f", "matroska", str(output)]
                job.process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True)
                last_update = 0.0
                while job.process.returncode is None:
                    await asyncio.sleep(1)
                    if not await self._disk_ok():
                        job.stopped = True; os.killpg(job.process.pid, signal.SIGTERM); job.progress.status = "Stopped: disk safety limit"; break
                    job.progress.bytes_written = output.stat().st_size if output.exists() else 0
                    if time.monotonic() - last_update >= self.settings.progress_interval:
                        await self._edit(job); last_update = time.monotonic()
                _, err = await job.process.communicate()
                if not output.exists() or output.stat().st_size < 1024:
                    if job.stopped: raise RuntimeError("Recording stopped before media data was saved.")
                    raise RuntimeError((err.decode("utf-8", "ignore")[-500:] or "Live stream produced no recording."))
                job.progress.status = "Finalizing MP4"; await self._edit(job)
                final = job.work_dir / f"{job.title}.mp4"
                remux = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(output), "-c", "copy", "-movflags", "+faststart", str(final), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                await remux.communicate()
                if remux.returncode != 0 or not final.exists(): final = output
                job.progress.status = "Uploading to Telegram"; job.progress.bytes_written = final.stat().st_size; await self._edit(job)
                await app.send_document(job.progress_message.chat.id, str(final), caption=f"✅ <b>Live recording complete</b>\n🎥 {job.platform}\n📺 <code>{job.title}</code>", parse_mode=enums.ParseMode.HTML)
                job.progress.status = "Completed"; await self._edit(job)
        except PublicStreamUnavailable as exc:
            job.progress = job.progress or LiveProgress(job.platform, "live", job.started_at); job.progress.status = "Public stream unavailable"; await self._edit(job)
            await app.send_message(job.progress_message.chat.id, f"❌ Public live stream could not be resolved.\n<code>{str(exc)}</code>\n\nThis bot does not bypass logins, private/paid rooms, or bot protection.", parse_mode=enums.ParseMode.HTML)
        except Exception as exc:
            job.progress = job.progress or LiveProgress(job.platform, "live", job.started_at); job.progress.status = "Failed"; await self._edit(job)
            await app.send_message(job.progress_message.chat.id, f"❌ Recording failed: <code>{str(exc)[:500]}</code>", parse_mode=enums.ParseMode.HTML)
        finally:
            self.jobs.pop(job.user_id, None)
            # Successful upload is durable in Telegram; remove local temporary recording.
            shutil.rmtree(job.work_dir, ignore_errors=True)
