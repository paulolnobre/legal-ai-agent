"""
test_audio.py
Pure-logic tests for the voice path: MIME → Whisper filename, the no-API-key
fallback, and the legal handler's transcription flow with the network stubbed
out (no DB, no network, no API key).
Run: python3 core/test_audio.py
"""
import asyncio
from typing import Any

from core.handlers import legal_message
from core.services import channels
from core.services import audio


def test_filename_follows_mime():
    assert audio.filename_for("audio/ogg") == "audio.ogg"
    assert audio.filename_for("audio/mpeg") == "audio.mp3"
    assert audio.filename_for("audio/x-m4a") == "audio.m4a"
    print("  OK Whisper filename follows the MIME type")


def test_filename_tolerates_dirty_mime():
    # Telegram may append parameters, and an `audio` upload may carry none.
    assert audio.filename_for("audio/OGG; codecs=opus") == "audio.ogg"
    assert audio.filename_for(None) == "audio.ogg"
    assert audio.filename_for("application/octet-stream") == "audio.ogg"
    print("  OK unknown or parameterized MIME falls back to .ogg")


def test_transcribe_without_key_is_a_no_op():
    """CI runs without an API key: the call must fail closed, not raise."""
    if audio.OPENAI_KEY:
        print("  SKIP OPENAI_API_KEY is configured in this environment")
        return
    assert asyncio.run(audio.transcribe(b"not-audio")) is None
    print("  OK transcription without an API key returns None")


# ── Legal handler flow ───────────────────────────────────────────────────────

def _run_voice(
    voice: dict[str, Any],
    *,
    downloaded: bytes | None = b"bytes",
    transcript: str | None = "texto",
) -> tuple[str, list[str], dict[str, Any]]:
    """Run _voice_to_text with Telegram and Whisper replaced by stubs."""
    sent: list[str] = []
    calls: dict[str, Any] = {}

    async def fake_send(chat_id, text):
        sent.append(text)

    async def fake_typing(chat_id, base_url=None):
        pass

    async def fake_download(file_id, token=None):
        calls["token"] = token
        return downloaded

    async def fake_transcribe(data, mime=None, prompt=None, seconds=0.0):
        calls["mime"] = mime
        calls["prompt"] = prompt
        calls["seconds"] = seconds
        return transcript

    original = (legal_message._send, channels.send_typing,
                legal_message.download_voice, legal_message.transcribe)
    legal_message._send = fake_send
    channels.send_typing = fake_typing
    legal_message.download_voice = fake_download
    legal_message.transcribe = fake_transcribe
    try:
        text = asyncio.run(legal_message._voice_to_text(1, voice))
    finally:
        (legal_message._send, channels.send_typing,
         legal_message.download_voice, legal_message.transcribe) = original
    return text, sent, calls


def test_voice_is_transcribed_with_the_legal_vocabulary():
    text, sent, calls = _run_voice({"file_id": "abc", "duration": 8, "mime_type": "audio/ogg"})
    assert text == "texto"
    assert not sent, "a successful transcription should not warn the sender"
    assert calls["mime"] == "audio/ogg"
    assert "jurisprudência" in (calls["prompt"] or ""), "Whisper needs the vocabulary hint"
    print("  OK voice note transcribed with the legal vocabulary hint")


def test_the_voice_notes_length_goes_with_the_request_for_the_cost_counter():
    _, _, calls = _run_voice({"file_id": "abc", "duration": 8})
    assert calls["seconds"] == 8


def test_voice_uses_the_internal_bot_token():
    """The file endpoint carries the token: the public bot's would 404 here."""
    _, _, calls = _run_voice({"file_id": "abc", "duration": 8})
    assert calls["token"] == legal_message.LEGAL_TELEGRAM_TOKEN
    print("  OK download uses the internal bot's token")


def test_long_audio_is_refused_before_download():
    duration = legal_message.settings.max_voice_seconds + 1
    text, sent, calls = _run_voice({"file_id": "abc", "duration": duration})
    assert text == ""
    assert "token" not in calls, "over-long audio must not be downloaded"
    assert sent and "longo" in sent[0]
    print("  OK over-long audio refused before any download")


def test_failures_answer_instead_of_going_silent():
    text, sent, _ = _run_voice({"file_id": "abc", "duration": 5}, downloaded=None)
    assert text == "" and sent, "a failed download must be reported to the sender"
    text, sent, _ = _run_voice({"file_id": "abc", "duration": 5}, transcript="   ")
    assert text == "" and sent, "an empty transcript must be reported to the sender"
    print("  OK download and transcription failures answer the sender")


def test_silence_artifacts_are_treated_as_nothing_said():
    """Whisper answers silence with caption boilerplate, not an empty string."""
    assert audio.is_silence_artifact("Legendas pela comunidade Amara.org")
    assert audio.is_silence_artifact("  ")
    assert audio.is_silence_artifact("Obrigado por assistir!")
    assert not audio.is_silence_artifact("Preciso revisar o contrato de locação")
    assert not audio.is_silence_artifact("NS-A7B2988C")
    print("  OK silent-audio boilerplate is recognized as no speech")


def test_silent_audio_answers_that_it_was_not_understood():
    text, sent, _ = _run_voice(
        {"file_id": "abc", "duration": 5}, transcript="Legendas pela comunidade Amara.org"
    )
    assert text == "", "caption boilerplate must not be answered as a question"
    assert sent and "Não entendi" in sent[0]
    print("  OK silent audio gets a 'não entendi' answer instead of an answer to noise")


if __name__ == "__main__":
    print("\n-- Voice transcription tests --")
    tests = [
        test_filename_follows_mime,
        test_filename_tolerates_dirty_mime,
        test_transcribe_without_key_is_a_no_op,
        test_voice_is_transcribed_with_the_legal_vocabulary,
        test_voice_uses_the_internal_bot_token,
        test_long_audio_is_refused_before_download,
        test_failures_answer_instead_of_going_silent,
        test_silence_artifacts_are_treated_as_nothing_said,
        test_silent_audio_answers_that_it_was_not_understood,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{'OK all passed' if not failed else f'{failed} failure(s)'}\n")


# ── The cost counter ─────────────────────────────────────────────────────────


def _counted() -> float:
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value("legal_audio_seconds_total") or 0.0


class _Reply:
    def __init__(self, status, text=""):
        self.status_code = status
        self._text = text

    def json(self):
        return {"text": self._text}


def _transcribe_with(monkeypatch, reply, **kwargs):
    class _Client:
        async def post(self, *args, **kw):
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(audio, "OPENAI_KEY", "k")
    monkeypatch.setattr(audio, "_http", lambda: _Client())
    before = _counted()
    asyncio.run(audio.transcribe(b"x", mime="audio/ogg", **kwargs))
    return _counted() - before


def test_an_answered_request_is_counted_even_when_it_heard_only_silence(monkeypatch):
    """The provider bills the audio it was sent, whatever came back."""
    assert _transcribe_with(monkeypatch, _Reply(200, "olá"), seconds=12.5) == 12.5
    assert _transcribe_with(monkeypatch, _Reply(200, ""), seconds=30) == 30


def test_a_failed_request_and_an_unlabelled_one_are_not_counted(monkeypatch):
    import httpx
    assert _transcribe_with(monkeypatch, _Reply(500), seconds=30) == 0
    assert _transcribe_with(monkeypatch, httpx.ConnectError("down"), seconds=30) == 0
    assert _transcribe_with(monkeypatch, _Reply(200, "olá")) == 0
