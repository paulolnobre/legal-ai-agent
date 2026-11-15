"""Reading what was attached in the panel's chat: a photo, a voice note, an
audio file. Runs in the worker, inside the request's media stage.

Everything here is untrusted input (lesson 21):

- A photo is opened lazily by Pillow and refused over its pixel ceiling
  before anything is decoded; Pillow's own bomb warning is an error. It is
  converted in memory to a JPEG of at most 2048 px on the long side, built
  from the pixels alone, so no EXIF, comment or profile reaches the model.
  A photo never touches the disk.
- Audio is decoded by `ffmpeg` with fixed arguments, only the demuxers the
  upload's magic bytes allow and only the `file` protocol (a playlist cannot
  make it open anything else), under `prlimit` (CPU time, 1 GB of address
  space, output size), in its own process group, which is killed and reaped
  on timeout or cancellation. It is re-encoded to mono 16 kHz Opus at
  24 kb/s with `-t 3601`, and the duration that counts is the one of that
  output, which we produced; a file whose metadata claims a minute and
  decodes past the hour is refused before Whisper, and the bill is reserved
  from that same figure.
- The decrypted audio sits only in one container-local directory, as
  `<random>.bin` (mode 0600, directory 0700), deleted after use and on
  failure or cancellation. A worker killed abruptly skips that, so
  `sweep_media_dir` empties the directory at every worker start, before any
  request is taken.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import secrets
import shutil
import signal
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from core.config import settings
from core.integrations import llm
from core.services import audio, budget

logger = logging.getLogger(__name__)

# Pillow warns, and decodes anyway, between its own ceiling and twice it.
# Here a warning of that kind is a refusal, for every image in the process.
warnings.filterwarnings("error", category=Image.DecompressionBombWarning)

# The binaries, by name on PATH; tests point them elsewhere.
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
PRLIMIT = "prlimit"

# Demuxers for what `assistant.sniff` lets in (MP4/M4A, MP3, WAV, Ogg, WebM,
# AAC), and nothing that reads other files or URLs (HLS, concat, ...).
_DEMUXERS = "mov,mp3,wav,ogg,matroska,aac"
_CPU_SECONDS = 600
_ADDRESS_SPACE = 1024 * 1024 * 1024
_OUTPUT_BYTES = 64 * 1024 * 1024
# Whisper takes at most 25 MB; an hour of our output is about 11 MB.
_WHISPER_MAX_BYTES = 25 * 1024 * 1024
_READING_MAX_TOKENS = 2_000

# What the person reads (the spec's "Errors").
UNREADABLE = ("Não consegui ler esse anexo agora. Tente de novo; se continuar, "
              "mande por texto.")
AUDIO_TOO_LONG = "O áudio passa de 1 hora."
# The Telegram path's copy for a voice note over its length.
VOICE_TOO_LONG = "O áudio é muito longo. Pode mandar um trecho menor ou escrever?"
# A photo over the pixel ceiling (implementation copy, accepted into the
# spec's Errors).
PHOTO_TOO_LARGE = "A foto passa de 40 megapixels. Envie uma foto menor."

_READING_PROMPT = (
    "Você lê imagens enviadas por advogados de um escritório brasileiro. "
    "Transcreva fielmente todo o texto legível da imagem, mantendo nomes, números, "
    "datas e valores como estão, e descreva em poucas linhas o que mais for "
    "relevante nela. Não responda à pergunta abaixo: ela só indica o que observar "
    "com mais atenção.\n\nPergunta: {question}"
)


class MediaError(Exception):
    """An attachment that could not be read or is refused. `message` is what
    the person reads."""

    def __init__(self, message: str = UNREADABLE):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Encoded:
    """Our own re-encoding of an audio file: where it is, how long it lasts
    and its size. The caller discards it."""

    path: Path
    seconds: float
    size: int

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Transcript:
    """Whisper's text (None when nothing came back) and the duration of the
    audio it was given."""

    text: str | None
    seconds: float

    @property
    def minutes(self) -> int:
        return max(1, math.ceil(self.seconds / 60))


# ── photo ────────────────────────────────────────────────────────────────────


def _to_jpeg(data: bytes) -> bytes:
    """The photo as a clean JPEG: refused over the pixel ceiling before a
    pixel is decoded, then at most `panel_photo_max_side` on its long side,
    upright, from the pixels alone (no EXIF, comment or ICC profile)."""
    side = settings.panel_photo_max_side
    try:
        with Image.open(io.BytesIO(data)) as image:  # lazy: the header only
            width, height = image.size
            if width * height > settings.panel_photo_max_pixels:
                raise MediaError(PHOTO_TOO_LARGE)
            if image.format not in ("JPEG", "PNG"):
                raise MediaError(UNREADABLE)
            image.draft("RGB", (side, side))  # a JPEG decodes at a smaller scale
            upright = ImageOps.exif_transpose(image).convert("RGB")
            upright.thumbnail((side, side))
            clean = Image.new("RGB", upright.size)
            clean.paste(upright)
            out = io.BytesIO()
            clean.save(out, "JPEG", quality=85)
            return out.getvalue()
    except MediaError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaError(PHOTO_TOO_LARGE) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise MediaError(UNREADABLE) from exc


async def read_photo(data: bytes, question: str) -> str:
    """What the model reads in the photo, guided by the question. Raises
    `MediaError` (refused or unread) or `budget.BudgetExceeded`."""
    jpeg = await asyncio.to_thread(_to_jpeg, data)
    try:
        return await llm.complete_with_image(
            "complex", _READING_PROMPT.format(question=question), jpeg,
            max_tokens=_READING_MAX_TOKENS)
    except llm.VisionUnavailable as exc:
        raise MediaError(UNREADABLE) from exc


# ── audio ────────────────────────────────────────────────────────────────────


def media_dir() -> Path:
    return Path(settings.panel_media_dir)


def _ready_dir() -> Path:
    directory = media_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def sweep_media_dir() -> int:
    """Empty the media directory: whatever is in it was left by a worker that
    died mid-read. Called at worker start, before any request. Returns how
    many entries it removed."""
    directory = _ready_dir()
    removed = 0
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            os.unlink(entry.path)
        removed += 1
    if removed:
        logger.warning("Removed %s files a stopped worker left in the media directory",
                       removed)
    return removed


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


async def _write_private_complete(path: Path, data: bytes) -> None:
    """Join a disk writer before cleanup, including repeated cancellation.

    Cancelling to_thread cannot stop its thread. The caller may unlink only
    after the writer can no longer create or change the plaintext file.
    """
    writer = asyncio.create_task(asyncio.to_thread(_write_private, path, data))
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        while not writer.done():
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Observe failures without replacing the original cancellation.
        if writer.done() and not writer.cancelled():
            writer.exception()
        raise


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _run(args: list[str]) -> tuple[int, bytes]:
    """Run one decoder under its resource limits, in its own process group,
    within the audio stage's wall clock. The group is killed and reaped on
    timeout or cancellation. Returns (exit code, stdout)."""
    process = await asyncio.create_subprocess_exec(
        PRLIMIT, f"--cpu={_CPU_SECONDS}", f"--as={_ADDRESS_SPACE}",
        f"--fsize={_OUTPUT_BYTES}", "--", *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    try:
        async with asyncio.timeout(settings.panel_media_audio_seconds):
            stdout, _ = await process.communicate()
    except BaseException:
        # The group, so whatever it started dies with it; killed before the
        # leader is reaped, so its id cannot have been reused.
        _kill_group(process)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=5)
        except TimeoutError:
            logger.error("A killed decoder was not reaped within 5 s")
        raise
    return process.returncode or 0, stdout


# The largest Ogg page: a 27-byte header, 255 lacing values, 255 x 255 bytes.
_OGG_PAGE_MAX = 27 + 255 + 255 * 255
_OPUS_RATE = 48_000


def _opus_samples(path: Path) -> int:
    """Playable samples (at 48 kHz) in our own Ogg Opus output, read from the
    stream as RFC 7845 section 4 defines them: the last page's granule
    position minus the OpusHead pre-skip. ffprobe's duration counts the
    pre-skip (60 s of speech reads 60.0065 s), and how it offsets the start
    has varied between versions; the granule is what the encoder wrote."""
    with path.open("rb") as handle:
        head = handle.read(_OGG_PAGE_MAX)
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - _OGG_PAGE_MAX))
        tail = handle.read()
    # The first page holds the OpusHead packet alone.
    if len(head) < 28 or not head.startswith(b"OggS"):
        raise MediaError(UNREADABLE)
    body = 27 + head[26]
    if head[body:body + 8] != b"OpusHead" or len(head) < body + 19:
        raise MediaError(UNREADABLE)
    serial = head[14:18]
    pre_skip = int.from_bytes(head[body + 10:body + 12], "little")
    # The last page: the one that ends exactly where the file ends, marked
    # end of stream, of the same stream. "OggS" inside a packet cannot pass
    # all three.
    at = len(tail)
    while (at := tail.rfind(b"OggS", 0, at)) >= 0:
        page = tail[at:]
        if len(page) < 27 or page[4] != 0 or len(page) < 27 + page[26]:
            continue
        lacing = page[27:27 + page[26]]
        if (27 + len(lacing) + sum(lacing) == len(page) and page[5] & 0x04
                and page[14:18] == serial):
            granule = int.from_bytes(page[6:14], "little", signed=True)
            return granule - pre_skip
    raise MediaError(UNREADABLE)


async def _duration(path: Path) -> float:
    samples = await asyncio.to_thread(_opus_samples, path)
    if samples <= 0:
        raise MediaError(UNREADABLE)
    return samples / _OPUS_RATE


async def reencode_audio(data: bytes) -> Encoded:
    """Decode the audio and re-encode it as mono 16 kHz Opus at 24 kb/s, at
    most `panel_audio_max_seconds` + 1 s of it. Its duration is read from this
    output. Raises `MediaError` when ffmpeg cannot read it."""
    directory = _ready_dir()
    stem = secrets.token_hex(16)
    source, target = directory / f"{stem}.bin", directory / f"{stem}.ogg"
    try:
        await _write_private_complete(source, data)
        # Created here, 0600, and only overwritten by ffmpeg.
        await _write_private_complete(target, b"")
        code, _ = await _run([
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-format_whitelist", _DEMUXERS, "-protocol_whitelist", "file",
            "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", "asetpts=N/SR/TB",
            "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
            "-t", str(int(settings.panel_audio_max_seconds) + 1),
            "-f", "ogg", "-y", str(target),
        ])
        if code != 0:
            raise MediaError(UNREADABLE)
        seconds = await _duration(target)
        size = target.stat().st_size
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        source.unlink(missing_ok=True)
    return Encoded(path=target, seconds=seconds, size=size)


async def transcribe_audio(data: bytes, *, voice: bool, prompt: str | None) -> Transcript:
    """Re-encode, check the length of our output, reserve the audio budget
    from it, then ask Whisper. A voice note is held to the Telegram path's
    length, an audio file to the hour.

    The reservation is settled only when Whisper answered; a timeout or a
    lost connection keeps it, since the audio may have been billed. Raises
    `MediaError` and `budget.BudgetExceeded`, both before Whisper is called.
    """
    encoded = await reencode_audio(data)
    try:
        limit = settings.max_voice_seconds if voice else settings.panel_audio_max_seconds
        if encoded.seconds > limit:
            raise MediaError(VOICE_TOO_LONG if voice else AUDIO_TOO_LONG)
        if encoded.size > _WHISPER_MAX_BYTES:
            raise MediaError(UNREADABLE)
        payload = await asyncio.to_thread(encoded.path.read_bytes)
    finally:
        encoded.discard()
    reservation = await budget.reserve_audio(encoded.seconds)
    timeout = 60.0 if voice else settings.panel_transcribe_audio_seconds
    text = await audio.transcribe(payload, mime="audio/ogg", prompt=prompt, timeout=timeout,
                                  seconds=encoded.seconds)
    if text is not None:
        await budget.settle_audio(reservation, encoded.seconds)
    return Transcript(text=text, seconds=encoded.seconds)


__all__ = ["AUDIO_TOO_LONG", "Encoded", "MediaError", "PHOTO_TOO_LARGE", "Transcript",
           "UNREADABLE", "VOICE_TOO_LONG", "media_dir", "read_photo", "reencode_audio",
           "sweep_media_dir", "transcribe_audio"]
