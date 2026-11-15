"""Production smoke checks for audio, Telegram download, OCR, and ingestion."""
from __future__ import annotations

import asyncio
import io
import os
import sys
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core.config import LEGAL_TELEGRAM_TOKEN, OPENAI_KEY, settings  # noqa: E402
from core.integrations import embeddings, telegram  # noqa: E402
from core.repositories.connection import connect_async  # noqa: E402
from core.services import audio  # noqa: E402
from core.services.ingestion import ingest_bytes  # noqa: E402

_TTS_URL = "https://api.openai.com/v1/audio/speech"
_TEST_PHRASE = "Este é um teste de áudio do assistente jurídico Demo e Associados."
_OCR_TEXT = (
    "CONTRATO DE TESTE OPERACIONAL\n"
    "CLAUSULA PRIMEIRA: Este documento valida OCR, embeddings e persistencia.\n"
    "O conteudo e temporario e sera removido automaticamente apos a verificacao."
)


async def check_audio() -> None:
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            _TTS_URL,
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={
                "model": "tts-1",
                "voice": "alloy",
                "input": _TEST_PHRASE,
                "response_format": "mp3",
            },
        )
    if response.status_code != 200 or not response.content:
        raise RuntimeError(f"TTS smoke request failed with HTTP {response.status_code}.")

    transcript = await audio.transcribe(
        response.content,
        mime="audio/mpeg",
        prompt="Demo e Associados, assistente jurídico",
    )
    normalized = (transcript or "").casefold()
    recognized = "teste" in normalized and "assistente" in normalized
    if not recognized:
        raise RuntimeError("The generated audio did not produce the expected transcript.")
    print(f"audio_transcription=ok characters={len(transcript or '')}")


async def _telegram_get(method: str, **params: Any) -> dict[str, Any]:
    if not LEGAL_TELEGRAM_TOKEN:
        raise RuntimeError("LEGAL_TELEGRAM_BOT_TOKEN is not configured.")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"https://api.telegram.org/bot{LEGAL_TELEGRAM_TOKEN}/{method}",
            params=params,
        )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram rejected {method}.")
    return body.get("result") or {}


async def check_telegram_download() -> None:
    bot = await _telegram_get("getMe")
    photos = await _telegram_get("getUserProfilePhotos", user_id=bot["id"], limit=1)
    photo_sets = photos.get("photos") or []
    if not photo_sets or not photo_sets[0]:
        print("telegram_download=not_tested reason=no_bot_profile_photo")
        return
    file_id = photo_sets[0][-1]["file_id"]
    payload = await telegram.download_file(file_id, max_bytes=5 * 1024 * 1024)
    if not payload:
        raise RuntimeError("Telegram returned a file id but the agent could not download it.")
    print(f"telegram_download=ok bytes={len(payload)}")


def _ocr_fixture() -> bytes:
    image = Image.new("RGB", (1800, 560), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        font = ImageFont.truetype(font_path, 46)
    except OSError:
        font = ImageFont.load_default()
    draw.multiline_text((60, 70), _OCR_TEXT, fill="black", font=font, spacing=30)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


async def check_ocr_embedding_storage() -> None:
    connection = await connect_async()
    document_id: int | None = None
    try:
        document_id, chunks = await ingest_bytes(
            connection,
            _ocr_fixture(),
            filename="capability-smoke-ocr.png",
            title="__capability_smoke_ocr_embedding__",
            doc_type="smoke-test",
            matter="capability-check",
        )
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT d.full_text, count(c.id), min(vector_dims(c.embedding))
                FROM documents d
                JOIN document_chunks c ON c.document_id = d.id
                WHERE d.id = %s
                GROUP BY d.id
                """,
                (document_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("The smoke document was not persisted.")
        extracted_text, stored_chunks, dimensions = row
        normalized = str(extracted_text).casefold()
        if "contrato" not in normalized or "clausula" not in normalized:
            raise RuntimeError("OCR did not extract the expected legal text.")
        if stored_chunks != chunks or dimensions != settings.embed_dim:
            raise RuntimeError("Stored chunks or embedding dimensions are inconsistent.")
        print(
            "ocr_ingestion=ok "
            f"text_characters={len(str(extracted_text))} "
            f"chunks={stored_chunks} embedding_dimensions={dimensions}"
        )
    finally:
        if document_id is not None:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    DELETE FROM document_access_log
                    WHERE action = 'ingest' AND %s = ANY(document_ids)
                    """,
                    (document_id,),
                )
                await cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))
            await connection.commit()
        await connection.close()


async def main() -> None:
    await check_telegram_download()
    await check_audio()
    await check_ocr_embedding_storage()
    await audio.close()
    await embeddings.close()
    await telegram.close()


if __name__ == "__main__":
    asyncio.run(main())
