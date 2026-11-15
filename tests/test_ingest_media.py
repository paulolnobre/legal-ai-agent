"""Archiving an attachment (spec "Publishing, and erasure", "Archiving").

`ingest_media` always makes a new private document of the person, with the
reading or transcript as its text and the original sealed as every original
is, and never looks at another document. In the panel worker its network
half (embeddings, the bucket) runs before the publication guard and only
the database writes run under it, after the rank is re-read on the locked
staff row: an erasure never waits on the network, and an archive the guard
refuses leaves no document and no stored original behind.
"""
import asyncio
import contextlib
import hashlib
import io
import re
from types import SimpleNamespace
from typing import LiteralString

import pytest
from PIL import Image

from core import document_crypto, legal_agent
from core.config import settings
from core.handlers import legal_message
from core.integrations import object_store
from core.models import StaffMember
from core.repositories import panel_uploads, staff
from core.services import (
    channels,
    document_storage,
    ingestion,
    media,
    panel_intake,
    work_hours,
)

PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
ANA = StaffMember(id=5, chat_id="500", name="Ana", role="estagiario")
READING = "Contrato de locação, R$ 2.000,00."
_REFUSED = "Arquivar pelo chat fica com advogados e administradores."


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


async def _rows(conn, statement: LiteralString, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        rows = await cur.fetchall()
    await conn.commit()
    return rows


async def _execute(conn, statement: LiteralString, params=()) -> None:
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
    await conn.commit()


@pytest.fixture
def storage(monkeypatch):
    """Originals go to a bucket kept in a dict; embeddings are counted and
    can be paused or made to do something while they run."""
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())
    monkeypatch.setattr(object_store, "configured", lambda: True)
    objects: dict[str, bytes] = {}
    embedding = SimpleNamespace(calls=0, reached=None, gate=None, during=None, result=True)

    async def put(key, data):
        objects[key] = data

    async def get(key):
        return objects[key]

    async def remove(key):
        objects.pop(key, None)

    async def embeddings(texts):
        embedding.calls += 1
        if embedding.during is not None:
            await embedding.during()
        if embedding.reached is not None:
            embedding.reached.set()
        if embedding.gate is not None:
            await embedding.gate.wait()
        return [[0.1] * settings.embed_dim for _ in texts] if embedding.result else None

    monkeypatch.setattr(object_store, "put_object", put)
    monkeypatch.setattr(object_store, "get_object", get)
    monkeypatch.setattr(object_store, "delete_object", remove)
    monkeypatch.setattr(ingestion, "embed_texts", embeddings)
    return SimpleNamespace(objects=objects, embedding=embedding)


# ── ingest_media itself ──────────────────────────────────────────────────────

def test_a_photo_and_an_audio_become_new_private_documents_even_with_the_same_text(
        db, storage, monkeypatch):
    text = "O mesmo texto da reunião."

    async def embeddings(texts):
        # One vector per chunk, told apart by its first value.
        return [[0.25 + index] + [0.0] * (settings.embed_dim - 1)
                for index, _text in enumerate(texts)]

    monkeypatch.setattr(ingestion, "embed_texts", embeddings)

    async def body(conn):
        # Already in the archive with exactly this text, as `ingest_bytes`
        # would have stored it: deduplication would have found it.
        await _execute(conn, "INSERT INTO documents (id, title, full_text, content_sha256, "
                          "uploaded_by, visibility) VALUES (900, 'Ata', %s, %s, 1, 'firm')",
                    (text, hashlib.sha256(text.encode()).hexdigest()))
        ids = []
        for data, media_type in [(_png(), "image/png"), (b"OggS reuniao", "audio/ogg")]:
            ids.append(await ingestion.ingest_media(
                conn, data=data, media_type=media_type, text=text, title="Anexo", staff=RUI))
            await conn.commit()
        documents = await _rows(
            conn, "SELECT id, uploaded_by, visibility, full_text, source_size_bytes, "
                  "source_storage_key, source_wrapped_key, source_sha256 FROM documents "
                  "WHERE id <> 900 ORDER BY id")
        originals = [await document_storage.read(key, wrapped, raw_sha256=digest)
                     for *_rest, key, wrapped, digest in documents]
        chunks = await _rows(conn, "SELECT document_id, chunk_index, content, "
                                   "embedding::text FROM document_chunks "
                                   "ORDER BY document_id, chunk_index")
        audit = await _rows(conn, "SELECT action, staff_id, document_ids FROM "
                                  "document_access_log ORDER BY id")
        untouched = await _rows(conn, "SELECT uploaded_by, visibility, source_storage_key "
                                      "FROM documents WHERE id = 900")
        return ids, documents, originals, chunks, audit, untouched

    ids, documents, originals, chunks, audit, untouched = db.run(body)

    assert len(set(ids)) == 2 and 900 not in ids
    assert [row[:5] for row in documents] == [
        (ids[0], RUI.id, "private", text, len(_png())),
        (ids[1], RUI.id, "private", text, len(b"OggS reuniao")),
    ]
    assert originals == [_png(), b"OggS reuniao"]
    assert all(plain not in sealed for plain, sealed in zip(
        originals, storage.objects.values(), strict=True)), "sealed, not stored in clear"
    vector = "[0.25" + ",0" * (settings.embed_dim - 1) + "]"
    assert chunks == [(ids[0], 0, text, vector), (ids[1], 0, text, vector)], (
        "the reading is indexed with the vectors computed for it")
    assert audit == [("ingest_media", RUI.id, [ids[0]]), ("ingest_media", RUI.id, [ids[1]])]
    assert untouched == [(1, "firm", None)], "the other document is not touched"


def test_an_intern_archives_nothing_and_pays_for_nothing(db, storage):
    async def body(conn):
        with pytest.raises(PermissionError):
            await ingestion.ingest_media(conn, data=_png(), media_type="image/png",
                                         text="Documento", title="Foto", staff=ANA)
        return await _rows(conn, "SELECT count(*) FROM documents")

    assert db.run(body) == [(0,)]
    assert storage.embedding.calls == 0 and storage.objects == {}


@pytest.mark.parametrize("media_type, size", [
    ("image/heic", 10), ("text/html", 10),
    ("image/png", 10 * 1024 * 1024 + 1), ("audio/ogg", 100 * 1024 * 1024 + 1),
])
def test_a_type_or_size_outside_the_media_limits_is_refused(storage, media_type, size):
    with pytest.raises(ValueError):
        asyncio.run(ingestion.prepare_media(data=b"x" * size, media_type=media_type,
                                            text="texto", staff=RUI))
    assert storage.embedding.calls == 0 and storage.objects == {}


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """No bucket and no key: originals on the local volume, where names were
    once given by content."""
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "doc_master_key", None)
    monkeypatch.setattr(object_store, "configured", lambda: False)

    async def embeddings(texts):
        return [[0.1] * settings.embed_dim for _ in texts]

    monkeypatch.setattr(ingestion, "embed_texts", embeddings)
    return tmp_path


def test_a_refused_archive_never_takes_the_original_of_an_identical_one(db, volume):
    """Two people archive the same photo at the same time (Codex review of
    M.3/M.4). Rui's preparation stores it first; Paulo's prepares the same
    bytes and is published; Rui's is then refused under the guard and taken
    back. Under a name given by content both would have pointed at one file,
    and taking Rui's back would have left Paulo's document without it."""
    data = _png()

    async def body(conn):
        other = await db.connect()
        try:
            refused = await ingestion.prepare_media(
                data=data, media_type="image/png", text=READING, staff=RUI)
            kept = await ingestion.prepare_media(
                data=data, media_type="image/png", text=READING, staff=PAULO)
            async with other.transaction():
                document_id = await ingestion.write_media(other, kept, title="Foto",
                                                          staff=PAULO)
            await ingestion.discard_media(refused)
        finally:
            await other.close()
        [(key, wrapped, digest)] = await _rows(
            conn, "SELECT source_storage_key, source_wrapped_key, source_sha256 "
                  "FROM documents WHERE id = %s", (document_id,))
        return refused.stored.key, key, await document_storage.read(
            key, wrapped, raw_sha256=digest)

    refused_key, kept_key, original = db.run(body)

    assert refused_key != kept_key, "each preparation has a file of its own"
    assert original == data, "the published document's original is still there"
    assert not (volume / refused_key).exists(), "the refused one was taken back"


def test_a_document_found_already_written_never_takes_the_others_original(db, volume):
    """The same race on the document path (Task M.6), which deduplicates by
    text as Telegram does: two people send the same file; both prepare it;
    Paulo's is written first; Rui's then finds that document and is not
    kept, so its stored original is taken back. Paulo's must still open."""
    data = "Ata da reunião de condomínio.".encode()

    async def body(conn):
        other = await db.connect()
        try:
            late = await ingestion.prepare_document(conn, data, filename="ata.txt")
            await conn.commit()
            first = await ingestion.prepare_document(other, data, filename="ata.txt")
            written, kept_first = await ingestion.write_document(
                other, first, uploaded_by=PAULO.id)
            await other.commit()
            found, kept_late = await ingestion.write_document(conn, late, uploaded_by=RUI.id)
            await conn.commit()
            await ingestion.discard_document(late)
        finally:
            await other.close()
        [(key, wrapped, digest)] = await _rows(
            conn, "SELECT source_storage_key, source_wrapped_key, source_sha256 "
                  "FROM documents WHERE id = %s", (written.document_id,))
        original = await document_storage.read(key, wrapped, raw_sha256=digest)
        count = await _rows(conn, "SELECT count(*) FROM documents")
        assert late.stored is not None
        return (kept_first, kept_late, found.document_id == written.document_id,
                found.chunk_count, original, count, (volume / late.stored.key).exists())

    kept_first, kept_late, same, chunks, original, count, late_left = db.run(body)

    assert kept_first and not kept_late and same and chunks == 0 and count == [(1,)]
    assert original == data, "the written document's original is still there"
    assert not late_left, "the one not kept was taken back"


# ── in the panel worker ──────────────────────────────────────────────────────

@pytest.fixture
def world(db, monkeypatch, storage):
    """The panel worker on this run's database: Telegram spied, the queue in
    a list, the photo read by a stub, and the answer by a stub that records
    its question and says one line."""
    queued: list[dict] = []
    questions: list[str] = []
    telegram: list[str] = []
    invalidated: list[bool] = []

    async def invalidate_answers():
        invalidated.append(True)
        return 0

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

    def spy(name):
        async def fake(*_args, **_kwargs):
            telegram.append(name)
            return 1
        return fake

    class _Pool:
        def connection(self, timeout=None):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                    await conn.commit()
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    async def serve_message(conn, update, msg, chat_id, staff_member, text, *_rest,
                            **_kwargs):
        questions.append(text)
        await legal_message._send(chat_id, "resposta")

    async def complete_with_image(task, prompt, image_jpeg, *, max_tokens):
        return READING

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, spy(name))
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)
    monkeypatch.setattr(legal_message, "get_pool", get_pool)
    monkeypatch.setattr(legal_agent, "get_pool", get_pool)
    monkeypatch.setattr(legal_message, "_serve_message", serve_message)
    monkeypatch.setattr(media.llm, "complete_with_image", complete_with_image)
    monkeypatch.setattr(legal_message.cache, "invalidate_answers", invalidate_answers)
    return SimpleNamespace(queued=queued, questions=questions, telegram=telegram,
                           invalidated=invalidated)


async def _send_archived(conn, world, who: StaffMember, text: str = "qual o valor?") -> dict:
    """A photo sent with "Guardar também no arquivo". Stored straight in the
    repository, as a forged request past the upload route's own check would
    leave it."""
    upload_id = await panel_uploads.store_upload(
        conn, who.id, kind="photo", media_type="image/png", data=_png(), archive=True)
    assert upload_id is not None
    intake = await panel_intake.submit_message(conn, who, text, upload_id=upload_id)
    assert intake.outcome == "queued", intake
    return world.queued[-1]


async def _outcome(conn, who: StaffMember):
    turns = await _rows(conn, "SELECT speaker, text FROM conversation_turns "
                              "WHERE staff_id = %s ORDER BY id", (who.id,))
    documents = await _rows(conn, "SELECT id, uploaded_by, visibility, full_text, title "
                                  "FROM documents ORDER BY id")
    uploads = await _rows(conn, "SELECT count(*) FROM panel_uploads")
    return turns, documents, uploads[0][0]


def test_a_lawyers_photo_is_archived_said_and_still_answered(db, world, storage):
    async def body(conn):
        await legal_agent._handle_update(await _send_archived(conn, world, RUI))
        return await _outcome(conn, RUI)

    turns, documents, uploads = db.run(body)

    [(document_id, owner, visibility, text, title)] = documents
    assert (owner, visibility, text) == (RUI.id, "private", READING)
    assert re.fullmatch(r"Foto de \d\d/\d\d/\d{4} \d\d:\d\d", title)
    assert turns == [
        ("person", "qual o valor?"),
        ("bot", f"Leitura da foto:\n{READING}"),
        ("bot", f"Guardado no arquivo, na sua pasta pessoal: {title} "
                f"(documento {document_id})."),
        ("bot", "resposta"),
    ]
    assert uploads == 0 and len(storage.objects) == 1
    assert world.questions == ["qual o valor?"]
    assert world.invalidated == [True], "the person's cached answers predate it"


def test_an_interns_archive_request_archives_nothing_and_the_upload_is_deleted(
        db, world, storage):
    async def body(conn):
        await legal_agent._handle_update(await _send_archived(conn, world, ANA))
        return await _outcome(conn, ANA)

    turns, documents, uploads = db.run(body)

    assert documents == [] and uploads == 0
    assert turns[1:] == [("bot", f"Leitura da foto:\n{READING}"), ("bot", _REFUSED),
                         ("bot", "resposta")]
    assert storage.embedding.calls == 0 and storage.objects == {}
    assert world.invalidated == []


def test_a_rank_lost_while_the_archive_is_prepared_is_refused_under_the_guard(
        db, world, storage):
    """The rank that decides is the one re-read on the locked staff row, not
    the one seen before the embeddings: demoted while they run, nothing is
    archived and the original already stored is taken back."""
    async def demote():
        other = await db.connect()
        try:
            async with other.cursor() as cur:
                await cur.execute("UPDATE staff SET role = 'estagiario' WHERE id = %s",
                                  (RUI.id,))
            await other.commit()
        finally:
            await other.close()

    storage.embedding.during = demote

    async def body(conn):
        await legal_agent._handle_update(await _send_archived(conn, world, RUI))
        return await _outcome(conn, RUI)

    turns, documents, uploads = db.run(body)

    assert storage.embedding.calls == 1, "the preparation ran"
    assert documents == [] and uploads == 0
    assert storage.objects == {}, "the stored original was taken back"
    assert turns[1:] == [("bot", f"Leitura da foto:\n{READING}"), ("bot", _REFUSED),
                         ("bot", "resposta")]


def test_an_archive_that_cannot_be_prepared_is_said_and_the_answer_still_comes(
        db, world, storage):
    storage.embedding.result = False

    async def body(conn):
        await legal_agent._handle_update(await _send_archived(conn, world, RUI))
        return await _outcome(conn, RUI)

    turns, documents, uploads = db.run(body)

    assert documents == [] and uploads == 0 and storage.objects == {}
    assert turns[1:] == [
        ("bot", f"Leitura da foto:\n{READING}"),
        ("bot", "Não consegui guardar o anexo no arquivo agora. "
                "Para guardar, anexe de novo e marque a opção."),
        ("bot", "resposta"),
    ]


def test_an_erasure_while_the_embeddings_run_does_not_wait_and_nothing_is_archived(
        db, world, storage):
    """The embedding call is paused. The erasure must finish while it is
    (no lock is held across the network), and the archive that follows is
    refused by the guard and leaves no document and no stored original."""
    storage.embedding.reached, storage.embedding.gate = asyncio.Event(), asyncio.Event()

    async def body(conn):
        update = await _send_archived(conn, world, RUI)
        eraser = await db.connect()
        try:
            serving = asyncio.create_task(legal_agent._handle_update(update))
            await asyncio.wait_for(storage.embedding.reached.wait(), timeout=10)
            erased = await asyncio.wait_for(
                staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id), timeout=5)
            storage.embedding.gate.set()
            await asyncio.wait_for(serving, timeout=10)
        finally:
            storage.embedding.gate.set()
            await eraser.close()
        return erased.status, await _outcome(conn, RUI)

    outcome, (turns, documents, uploads) = db.run(body)

    assert outcome == "erased", "the erasure did not wait for the embeddings"
    assert turns == [] and documents == [] and uploads == 0
    assert storage.objects == {}
    assert world.questions == [] and world.telegram == []


def test_an_archive_past_the_media_deadline_is_said_and_the_answer_still_comes(
        db, world, storage, monkeypatch):
    """Codex review of M.3/M.4: the archive's preparation runs inside the
    media stage's deadline. Paused past it, the archive is said to have
    failed, nothing is archived, and the reading and the answer go out."""
    monkeypatch.setattr(settings, "panel_media_photo_seconds", 1.0)
    storage.embedding.gate = asyncio.Event()  # never opened

    async def body(conn):
        update = await _send_archived(conn, world, RUI)
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(legal_agent._handle_update(update), timeout=15)
        took = asyncio.get_running_loop().time() - started
        status = await _rows(conn, "SELECT status FROM panel_requests")
        return took, status, await _outcome(conn, RUI)

    took, status, (turns, documents, uploads) = db.run(body)

    assert took < 5, "the preparation was not cut at the stage's deadline"
    assert storage.embedding.calls == 1, "the preparation was under way"
    assert documents == [] and uploads == 0 and storage.objects == {}
    assert turns[1:] == [
        ("bot", f"Leitura da foto:\n{READING}"),
        ("bot", "Não consegui guardar o anexo no arquivo agora. "
                "Para guardar, anexe de novo e marque a opção."),
        ("bot", "resposta"),
    ]
    assert world.questions == ["qual o valor?"] and status == [("done",)]
