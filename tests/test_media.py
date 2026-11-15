"""Reading an attachment in the worker: the photo, the audio, their limits
and what they cost.

The decoders are the real ones: Pillow throughout, and `ffmpeg`/`ffprobe`
from PATH where a test needs them (skipped, saying so, when they are not
installed). The model and Whisper are stubs, so what reaches them is what
is asserted.
"""
import asyncio
import io
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageFile

from core import legal_agent
from core.config import settings
from core.integrations import llm
from core.services import budget, media

_HAS_FFMPEG = bool(shutil.which(media.FFMPEG) and shutil.which(media.FFPROBE)
                   and shutil.which(media.PRLIMIT))
needs_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG,
                                  reason="ffmpeg, ffprobe or prlimit is not installed")


def _media_files() -> list[str]:
    directory = media.media_dir()
    return sorted(os.listdir(directory)) if directory.exists() else []


# ── photo ────────────────────────────────────────────────────────────────────


@pytest.fixture
def model(monkeypatch):
    """The vision call, recorded."""
    calls: list[dict] = []

    async def complete_with_image(task, prompt, image_jpeg, *, max_tokens):
        calls.append({"task": task, "prompt": prompt, "image": image_jpeg,
                      "max_tokens": max_tokens})
        return "Contrato de locação entre A e B."

    monkeypatch.setattr(media.llm, "complete_with_image", complete_with_image)
    return calls


def test_a_50_megapixel_png_is_refused_before_it_is_decoded(model, monkeypatch):
    """Under Pillow's own ceiling (about 89 MP), so only ours stops it."""
    out = io.BytesIO()
    Image.new("1", (10_000, 5_000)).save(out, "PNG")
    loads: list[str] = []
    real_load = ImageFile.ImageFile.load

    def load(self):
        loads.append("load")
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", load)

    with pytest.raises(media.MediaError) as refused:
        asyncio.run(media.read_photo(out.getvalue(), "o que é isto?"))

    assert refused.value.message == "A foto passa de 40 megapixels. Envie uma foto menor."
    assert loads == [], "not a pixel decoded"
    assert model == []


def test_a_jpeg_reaches_the_model_without_exif_and_at_most_2048_px(model):
    exif = Image.Exif()
    exif[0x010F] = "Câmera do Rui"  # Make
    exif[0x8825] = {2: (22.0, 54.0, 30.0)}  # GPS latitude
    source = io.BytesIO()
    Image.new("RGB", (3_000, 1_200), (200, 30, 30)).save(
        source, "JPEG", exif=exif.tobytes(), comment=b"segredo no comentario")
    assert b"Exif" in source.getvalue() and b"segredo" in source.getvalue()

    reading = asyncio.run(media.read_photo(source.getvalue(), "qual o valor?"))

    assert reading == "Contrato de locação entre A e B."
    [call] = model
    sent = call["image"]
    assert sent.startswith(b"\xff\xd8\xff")
    assert b"Exif" not in sent and b"segredo" not in sent and "Rui".encode() not in sent
    with Image.open(io.BytesIO(sent)) as image:
        assert image.format == "JPEG"
        assert image.size == (2_048, 819)
        assert not image.getexif()
        assert "comment" not in image.info and "icc_profile" not in image.info
    assert "qual o valor?" in call["prompt"]
    assert _media_files() == [], "a photo never touches the disk"


def test_a_file_that_is_not_an_image_is_unreadable(model):
    with pytest.raises(media.MediaError) as refused:
        asyncio.run(media.read_photo(b"%PDF-1.7 not a photo", "o que é isto?"))
    assert refused.value.message == media.UNREADABLE
    assert model == []


def test_a_photo_the_model_did_not_read_is_unreadable(monkeypatch):
    async def nothing(*_args, **_kwargs):
        raise llm.VisionUnavailable("no reading")

    monkeypatch.setattr(media.llm, "complete_with_image", nothing)
    out = io.BytesIO()
    Image.new("RGB", (40, 40)).save(out, "PNG")
    with pytest.raises(media.MediaError) as refused:
        asyncio.run(media.read_photo(out.getvalue(), "o que é isto?"))
    assert refused.value.message == media.UNREADABLE


# ── the vision call ──────────────────────────────────────────────────────────


class _Meter:
    def __init__(self):
        self.reserved: list[tuple[int, int]] = []
        self.settled: list[tuple[int, int]] = []

    async def reserve(self, worst_input, worst_output):
        self.reserved.append((worst_input, worst_output))
        return "reservation"

    async def settle(self, reservation, input_tokens, output_tokens):
        self.settled.append((input_tokens, output_tokens))


def test_complete_with_image_sends_the_image_block_and_meters_it(monkeypatch):
    meter = _Meter()
    sent: list[dict] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": " lido "}}],
                    "usage": {"prompt_tokens": 1_200, "completion_tokens": 30}}

    class Client:
        is_closed = False

        async def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(llm, "OPENAI_KEY", "test-key")
    monkeypatch.setattr(llm, "_client", Client())
    monkeypatch.setattr(llm.budget, "reserve", meter.reserve)
    monkeypatch.setattr(llm.budget, "settle", meter.settle)
    monkeypatch.setattr(settings, "llm_complex_provider", "gemini")

    text = asyncio.run(llm.complete_with_image("complex", "leia", b"\xff\xd8\xffJPEG",
                                               max_tokens=500))

    assert text == "lido"
    [payload] = sent
    assert payload["model"] == llm._model_for("complex", "openai"), "always OpenAI"
    [message] = payload["messages"]
    assert message["content"][0] == {"type": "text", "text": "leia"}
    image = message["content"][1]["image_url"]["url"]
    assert image == "data:image/jpeg;base64,/9j/SlBFRw=="
    assert meter.reserved == [(settings.budget_worst_input_complex
                               + settings.budget_worst_input_image, 500)]
    assert meter.settled == [(1_200, 30)]


def test_complete_with_image_without_a_reading_raises_and_returns_the_reservation(
        monkeypatch):
    meter = _Meter()
    monkeypatch.setattr(llm, "OPENAI_KEY", "")
    monkeypatch.setattr(llm.budget, "reserve", meter.reserve)
    monkeypatch.setattr(llm.budget, "settle", meter.settle)

    with pytest.raises(llm.VisionUnavailable):
        asyncio.run(llm.complete_with_image("complex", "leia", b"jpeg", max_tokens=10))
    assert meter.settled == [(0, 0)]


# ── audio: ffmpeg ────────────────────────────────────────────────────────────


def _wav(seconds: float, *, rate: int = 44_100, channels: int = 2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as sound:
        sound.setnchannels(channels)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        frames = int(seconds * rate)
        sound.writeframes(b"".join(
            struct.pack("<h", int(8_000 * ((i // 50) % 2 * 2 - 1))) * channels
            for i in range(frames)))
    return out.getvalue()


def _opus_head(data: bytes) -> tuple[int, int]:
    """(channels, input sample rate) from the OpusHead packet."""
    at = data.index(b"OpusHead")
    return data[at + 9], struct.unpack("<I", data[at + 12:at + 16])[0]


def _probe(path: Path, entries: str) -> str:
    return subprocess.run(
        [media.FFPROBE, "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True, timeout=30).stdout.strip()


@needs_ffmpeg
def test_real_ffmpeg_gives_opus_mono_16khz_in_a_private_file():
    encoded = asyncio.run(media.reencode_audio(_wav(2.0)))
    try:
        data = encoded.path.read_bytes()
        assert _probe(encoded.path, "stream=codec_name") == "opus"
        assert _opus_head(data) == (1, 16_000)
        assert 1.9 < encoded.seconds < 2.2
        assert encoded.size == len(data)
        assert encoded.path.parent == media.media_dir()
        assert oct(encoded.path.stat().st_mode & 0o777) == "0o600"
        assert oct(media.media_dir().stat().st_mode & 0o777) == "0o700"
        assert _media_files() == [encoded.path.name], "the decrypted input is gone"
    finally:
        encoded.discard()
    assert _media_files() == []


@needs_ffmpeg
def test_a_file_ffmpeg_cannot_read_is_unreadable_and_leaves_nothing():
    with pytest.raises(media.MediaError) as refused:
        asyncio.run(media.reencode_audio(b"OggS" + b"\x00" * 500))
    assert refused.value.message == media.UNREADABLE
    assert _media_files() == []


@needs_ffmpeg
def test_a_playlist_cannot_make_ffmpeg_open_another_file(tmp_path):
    secret = tmp_path / "secret.wav"
    secret.write_bytes(_wav(1.0))
    playlist = f"#EXTM3U\n#EXTINF:1,\nfile://{secret}\n".encode()
    with pytest.raises(media.MediaError):
        asyncio.run(media.reencode_audio(playlist))


@pytest.fixture
def whisper(monkeypatch):
    calls: list[dict] = []
    replies: list[Any] = ["o contrato vence em março"]

    async def transcribe(audio_bytes, mime=None, prompt=None, timeout=60, seconds=0.0):
        calls.append({"bytes": audio_bytes, "mime": mime, "prompt": prompt,
                      "timeout": timeout, "seconds": seconds})
        reply: Any = replies[0]
        if asyncio.iscoroutine(reply):
            return await reply
        return reply

    monkeypatch.setattr(media.audio, "transcribe", transcribe)
    return calls, replies


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, int] = {}

    async def incrby(self, key, amount):
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    async def expire(self, key, ttl):
        return True

    def audio(self) -> list[int]:
        return [value for key, value in sorted(self.values.items()) if key.endswith(":audio")]


@pytest.fixture
def meter(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(budget, "_redis", lambda: fake)
    monkeypatch.setattr(settings, "budget_enabled", True)
    return fake


def _chained_ogg(tmp_path: Path, *, long_seconds: int, short_seconds: int) -> bytes:
    """Two Ogg streams back to back: ffprobe reads the duration of the last
    one only, while decoding goes through both."""
    parts = []
    for name, seconds in (("long", long_seconds), ("short", short_seconds)):
        path = tmp_path / f"{name}.ogg"
        subprocess.run([media.FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"sine=duration={seconds}", "-c:a", "libopus", str(path)],
                       check=True, timeout=60)
        parts.append(path.read_bytes())
    return b"".join(parts)


@needs_ffmpeg
def test_a_file_whose_metadata_lies_is_refused_past_the_limit_before_whisper(
        tmp_path, monkeypatch, whisper, meter):
    """The limit is three seconds here so the file can be small; the rule
    is the hour's: the length that counts is our own output's."""
    monkeypatch.setattr(settings, "panel_audio_max_seconds", 3)
    data = _chained_ogg(tmp_path, long_seconds=6, short_seconds=1)
    lying = tmp_path / "lying.ogg"
    lying.write_bytes(data)
    assert float(_probe(lying, "format=duration")) < 2, "its metadata claims about 1 s"

    with pytest.raises(media.MediaError) as refused:
        asyncio.run(media.transcribe_audio(data, voice=False, prompt=None))

    assert refused.value.message == media.AUDIO_TOO_LONG
    assert whisper[0] == [], "Whisper never called"
    assert meter.audio() == [], "nothing reserved"
    assert _media_files() == []


@needs_ffmpeg
def test_an_audio_file_inside_the_limit_goes_to_whisper_as_our_opus(whisper, meter):
    transcript = asyncio.run(media.transcribe_audio(_wav(2.0), voice=False, prompt="dica"))

    assert transcript.text == "o contrato vence em março"
    assert 1.9 < transcript.seconds < 2.2 and transcript.minutes == 1
    [call] = whisper[0]
    assert call["mime"] == "audio/ogg" and call["prompt"] == "dica"
    assert call["timeout"] == settings.panel_transcribe_audio_seconds == 600
    assert _opus_head(call["bytes"]) == (1, 16_000)
    assert _media_files() == []


def _hanging_ffmpeg(tmp_path: Path) -> tuple[Path, Path]:
    """A decoder that never ends and starts a child of its own."""
    pidfile = tmp_path / "pid"
    script = tmp_path / "ffmpeg"
    script.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nsleep 300 &\nsleep 300\n")
    script.chmod(0o755)
    return script, pidfile


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone(pgid: int) -> bool:
    for _ in range(100):
        if not _group_alive(pgid):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.skipif(not shutil.which(media.PRLIMIT), reason="prlimit is not installed")
@pytest.mark.parametrize("how", ["own deadline", "stage cancelled"])
def test_a_hung_ffmpeg_is_killed_with_its_group_and_leaves_nothing(tmp_path, monkeypatch,
                                                                   how):
    script, pidfile = _hanging_ffmpeg(tmp_path)
    monkeypatch.setattr(media, "FFMPEG", str(script))
    if how == "own deadline":
        monkeypatch.setattr(settings, "panel_media_audio_seconds", 0.5)

    async def body():
        reading = media.reencode_audio(_wav(0.5))
        with pytest.raises(TimeoutError):
            if how == "own deadline":
                await reading
            else:
                await asyncio.wait_for(reading, timeout=0.5)
        pgid = int(pidfile.read_text())
        return pgid, await _gone(pgid)

    async def bounded():
        # A process left alive holds the pipe open: fail, do not hang.
        return await asyncio.wait_for(body(), timeout=15)

    pgid, gone = asyncio.run(bounded())

    assert gone, f"process group {pgid} still has processes"
    assert _media_files() == []


def test_the_sweep_empties_the_media_directory_and_follows_no_link(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours")
    directory = media.media_dir()
    directory.mkdir(mode=0o700, parents=True)
    (directory / "abc.bin").write_bytes(b"decrypted audio")
    (directory / "abc.ogg").write_bytes(b"opus")
    (directory / "leftover").mkdir()
    (directory / "leftover" / "x.bin").write_bytes(b"x")
    (directory / "link").symlink_to(outside)

    assert media.sweep_media_dir() == 4

    assert _media_files() == []
    assert outside.read_text() == "not ours"


def test_the_worker_sweeps_before_it_takes_any_request(monkeypatch):
    directory = media.media_dir()
    directory.mkdir(mode=0o700, parents=True)
    (directory / "left-by-a-killed-worker.bin").write_bytes(b"decrypted audio")
    seen: list[list[str]] = []

    class Stop(Exception):
        pass

    async def recover_processing():
        seen.append(_media_files())
        raise Stop

    monkeypatch.setattr(legal_agent.update_queue, "recover_processing", recover_processing)

    with pytest.raises(Stop):
        asyncio.run(legal_agent._run_worker_session(asyncio.Event()))

    assert seen == [[]], "empty before the queue is touched"


# ── audio: the budget ────────────────────────────────────────────────────────


@pytest.fixture
def encoded(monkeypatch, tmp_path):
    """Our re-encoding, stubbed to a known duration."""
    seconds = [61.2]

    async def reencode(_data):
        directory = media.media_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "fixed.ogg"
        path.write_bytes(b"OggS opus")
        return media.Encoded(path=path, seconds=seconds[0], size=9)

    monkeypatch.setattr(media, "reencode_audio", reencode)
    return seconds


def test_the_audio_budget_is_reserved_from_our_output_rounded_to_the_minute(
        encoded, whisper, meter):
    transcript = asyncio.run(media.transcribe_audio(b"claims one second", voice=False,
                                                    prompt=None))

    assert transcript.text == "o contrato vence em março"
    assert meter.audio() == [120, 120], "61.2 s of our output, as two minutes, day and month"
    assert _media_files() == []


def test_the_length_whisper_bills_goes_with_the_request(encoded, whisper, meter):
    calls, _replies = whisper
    asyncio.run(media.transcribe_audio(b"claims one second", voice=False, prompt=None))
    assert [c["seconds"] for c in calls] == [61.2], "our own output's length, for the cost counter"


def test_the_audio_reservation_is_kept_when_whisper_times_out(encoded, whisper, meter):
    calls, replies = whisper

    async def hang():
        await asyncio.sleep(30)

    replies[0] = hang()

    async def body():
        await asyncio.wait_for(media.transcribe_audio(b"x", voice=False, prompt=None),
                               timeout=0.3)

    with pytest.raises(TimeoutError):
        asyncio.run(body())

    assert len(calls) == 1
    assert meter.audio() == [120, 120], "may have been billed: kept"


def test_the_audio_reservation_is_kept_when_nothing_comes_back(encoded, whisper, meter):
    whisper[1][0] = None
    transcript = asyncio.run(media.transcribe_audio(b"x", voice=False, prompt=None))
    assert transcript.text is None
    assert meter.audio() == [120, 120]


def test_over_the_audio_budget_nothing_is_sent_and_nothing_is_kept(
        encoded, whisper, meter, monkeypatch):
    monkeypatch.setattr(settings, "budget_day_audio_seconds", 60)
    with pytest.raises(budget.BudgetExceeded) as refused:
        asyncio.run(media.transcribe_audio(b"x", voice=False, prompt=None))
    assert refused.value.window == "day"
    assert whisper[0] == []
    assert set(meter.audio()) == {0}, "every counter it touched given back"


def test_a_voice_note_is_held_to_the_telegram_length(encoded, whisper, meter):
    encoded[0] = settings.max_voice_seconds + 1
    with pytest.raises(media.MediaError) as refused:
        asyncio.run(media.transcribe_audio(b"x", voice=True, prompt=None))
    assert refused.value.message == media.VOICE_TOO_LONG
    assert whisper[0] == []


def test_a_voice_note_keeps_the_client_timeout(encoded, whisper, meter):
    asyncio.run(media.transcribe_audio(b"x", voice=True, prompt=None))
    assert whisper[0][0]["timeout"] == 60


def test_settle_audio_returns_what_was_not_used(meter):
    async def body():
        reservation = await budget.reserve_audio(300)
        await budget.settle_audio(reservation, 61)
        return reservation

    reservation = asyncio.run(body())
    assert reservation is not None and reservation.audio_seconds == 300
    assert meter.audio() == [120, 120]


@pytest.mark.parametrize('delayed_suffix', ['.bin', '.ogg'])
def test_cancellation_before_a_private_write_opens_leaves_no_plaintext(monkeypatch, delayed_suffix):
    import threading

    started, release = threading.Event(), threading.Event()
    original = media._write_private

    def delayed(path, data):
        if path.suffix == delayed_suffix:
            started.set()
            assert release.wait(5)
        original(path, data)

    monkeypatch.setattr(media, '_write_private', delayed)

    async def scenario():
        task = asyncio.create_task(media.reencode_audio(b'private audio'))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Wait for any abandoned thread too: the old implementation leaves
        # its file only after the request's cleanup has already finished.
        await asyncio.to_thread(lambda: None)

    asyncio.run(scenario())
    assert _media_files() == []


def _recording(seconds: int, container: str) -> bytes:
    """A real recording of exactly `seconds`, made by ffmpeg: a WAV, or an
    Ogg Opus like a phone's voice note (small enough for the hour)."""
    encoding = (["-c:a", "libopus", "-b:a", "6k", "-f", "ogg"] if container == "ogg"
                else ["-f", "wav"])
    return subprocess.run(
        [media.FFMPEG, "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:sample_rate=16000", "-t", str(seconds), "-ac", "1",
         *encoding, "pipe:1"],
        capture_output=True, check=True, timeout=60).stdout


@needs_ffmpeg
@pytest.mark.parametrize("seconds, voice, container", [
    (60, True, "wav"), (300, True, "wav"), (60, False, "wav"), (300, False, "ogg"),
])
def test_a_recording_of_whole_minutes_counts_as_those_minutes(
        seconds, voice, container, whisper, meter, monkeypatch):
    """ffprobe reads 60 s of our Opus as 60.0065 s: the encoder's pre-skip.
    Counted that way, a 300 s voice note and an hour of audio would be
    refused, and every whole minute would be billed one minute more. The
    audio file's limit is its own length here: the hour's rule, at a length
    a test can afford (a real hour takes 40 s to encode)."""
    if not voice:
        monkeypatch.setattr(settings, "panel_audio_max_seconds", seconds)
    transcript = asyncio.run(media.transcribe_audio(_recording(seconds, container),
                                                    voice=voice, prompt=None))

    assert transcript.seconds == seconds, "the playable length, to the sample"
    assert len(whisper[0]) == 1, "inside the limit: sent to Whisper"
    assert meter.audio() == [seconds, seconds], "reserved and settled at that, day and month"
    assert _media_files() == []
