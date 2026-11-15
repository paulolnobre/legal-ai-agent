"""Voice transcription via OpenAI Whisper, async.

Both bots accept voice: the client bot so a lead can talk instead of typing,
the legal assistant so staff can dictate a question or a clause on the move.
Audio bytes are never persisted — only the transcript continues down the
pipeline, so a voice note leaves the same trace a typed message would.
"""
import io
import logging
import time

import httpx

from core.config import OPENAI_KEY
from core.metrics import AUDIO_SECONDS, observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"

# Whisper picks its decoder from the filename extension, so a Telegram `audio`
# upload (mp3, m4a) must not be announced as the .ogg of a voice note.
_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/oga": "oga",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}
_DEFAULT_MIME = "audio/ogg"  # what Telegram sends for a voice note

# On silence (a mic that never opened, a note recorded muted) Whisper does not
# return an empty string: it hallucinates the caption boilerplate of its training
# videos. Those transcripts are noise and must be treated as "nothing was said",
# otherwise the assistant answers a question nobody asked.
_SILENCE_ARTIFACTS = (
    "amara.org",
    "legendas pela comunidade",
    "legendado pela comunidade",
    "subtitles by the",
    "subtitulos realizados por la comunidad",
    "inscreva-se no canal",
    "obrigado por assistir",
    "ate o proximo video",
)
_STRIP_ACCENTS = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
                              "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC")


def is_silence_artifact(text: str | None) -> bool:
    """True when a transcript is Whisper's boilerplate for an audio with no speech."""
    normalized = (text or "").translate(_STRIP_ACCENTS).casefold()
    normalized = "".join(ch if ch.isalnum() or ch == "." else " " for ch in normalized)
    normalized = " ".join(normalized.split()).strip(" .")
    if len(normalized) < 2:
        return True
    return any(artifact in normalized for artifact in _SILENCE_ARTIFACTS)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


def normalize_mime(mime: str | None) -> str:
    """Bare MIME type, without the parameters Telegram sometimes appends."""
    return (mime or "").split(";")[0].strip().lower() or _DEFAULT_MIME


def filename_for(mime: str | None) -> str:
    """Filename Whisper should see for this MIME type."""
    return f"audio.{_EXTENSIONS.get(normalize_mime(mime), 'ogg')}"


def _trace_inputs(inputs: dict) -> dict:
    audio_bytes = inputs.get("audio_bytes") or b""
    return {
        "audio_bytes": len(audio_bytes),
        "mime": inputs.get("mime"),
        "prompt_present": bool(inputs.get("prompt")),
        "content_redacted": True,
    }


def _trace_output(output: str | None) -> dict:
    return {"transcribed": output is not None, "characters": len(output or "")}


@traced(
    name="openai.transcription",
    run_type="tool",
    process_inputs=_trace_inputs,
    process_outputs=_trace_output,
)
async def transcribe(
    audio_bytes: bytes, mime: str | None = None, prompt: str | None = None,
    timeout: float = 60, seconds: float = 0.0,
) -> str | None:
    """Transcribe Portuguese speech. Returns None when transcription failed.

    `prompt` is a vocabulary hint: proper nouns and jargon the model would
    otherwise spell by ear (Whisper reads at most 224 tokens of it).
    `timeout` is the client's 60 s unless the caller knows better: an hour
    of audio passes 600. `seconds` is the audio's length, counted for the cost
    dashboard once the provider answers (it bills what it was sent, so a
    recording of silence counts too); left out, nothing is counted.
    """
    if not OPENAI_KEY:
        logger.error("OPENAI_API_KEY not configured, cannot transcribe audio")
        return None
    mime = normalize_mime(mime)
    data = {"model": "whisper-1", "language": "pt"}
    if prompt:
        data["prompt"] = prompt
    started = time.perf_counter()
    try:
        resp = await _http().post(
            _WHISPER_URL,
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            files={"file": (filename_for(mime), io.BytesIO(audio_bytes), mime)},
            data=data,
            timeout=timeout,
        )
        if resp.status_code == 200:
            if seconds > 0:
                AUDIO_SECONDS.inc(seconds)
            text = resp.json().get("text", "").strip() or None
            if is_silence_artifact(text):
                logger.info("Discarded empty/silent transcription artifact")
                text = None
            observe_external("openai_transcription", "success", time.perf_counter() - started)
            return text
        observe_external("openai_transcription", "error", time.perf_counter() - started)
        logger.error("Whisper API returned status %s", resp.status_code)
    except httpx.HTTPError as e:
        observe_external("openai_transcription", "error", time.perf_counter() - started)
        logger.error("Whisper transcription failed: %s", e)
    return None


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
