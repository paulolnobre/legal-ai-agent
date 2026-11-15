"""Reading the firm's document bucket into the archive.

The bucket is a place other people write to, so everything from it is untrusted:
a name is never a path, an extension is checked against the same allowlist a
Telegram attachment must pass, and a size is refused before the bytes are
fetched rather than after. The tests here are mostly about what the import
refuses, because the happy path is one line and the refusals are the product.
"""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.config import settings
from core.integrations import inbox_bucket, object_store
from core.handlers import legal_message
from core.services import inbox_import
from core.services.ingestion import DOCUMENT_EXTENSIONS


@dataclass
class _Recorded:
    file_id: str
    file_name: str
    outcome: str
    document_id: int | None
    detail: str | None


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "inbox_enabled", True)
    monkeypatch.setattr(settings, "max_doc_mb", 20)
    monkeypatch.setattr(settings, "inbox_max_files_per_run", 25)


def _file(name, *, file_id=None, size=1024):
    return inbox_bucket.BucketFile(
        file_id=file_id or f"id-{name}",
        key=f"entrada/{name}",
        name=name,
        size=size,
        uploaded_at=datetime(2026, 9, 9, tzinfo=UTC),
    )


@pytest.fixture
def bucket(monkeypatch):
    """The bucket, the ledger and ingestion, all recorded rather than real."""
    state = SimpleNamespace(
        files=[],
        downloaded=[],
        ingested=[],
        recorded=[],
        known=set(),
        # What ingestion was actually asked to store. Separate from `ingested`
        # so the classification is asserted on rather than swallowed by
        # **_kwargs, which is what hid it from every test in this file.
        classified=[],
        ingest_result=(7, 3),
        download_error=None,
        ingest_error=None,
    )

    async def list_files(limit=None):
        return state.files

    async def download(key, *, max_bytes):
        if state.download_error:
            raise inbox_bucket.InboxError(state.download_error)
        state.downloaded.append(key)
        return b"conteudo"

    async def known_file_ids(_conn, file_ids):
        return {fid for fid in file_ids if fid in state.known}

    async def record(_conn, *, file_id, file_name, outcome,
                     document_id=None, detail=None, commit=True):
        state.recorded.append(_Recorded(file_id, file_name, outcome, document_id, detail))

    async def ingest(_conn, data, *, filename, uploaded_by,
                     doc_type=None, matter=None, **_kwargs):
        if state.ingest_error:
            raise ValueError(state.ingest_error)
        state.ingested.append((filename, uploaded_by))
        state.classified.append((filename, matter, doc_type))
        return state.ingest_result

    monkeypatch.setattr(inbox_import.inbox_bucket, "list_files", list_files)
    monkeypatch.setattr(inbox_import.inbox_bucket, "download", download)
    monkeypatch.setattr(inbox_import.inbox, "known_file_ids", known_file_ids)
    monkeypatch.setattr(inbox_import.inbox, "record", record)
    monkeypatch.setattr(inbox_import, "ingest_bytes", ingest)
    return state


def _run(state=None):
    return asyncio.run(inbox_import.import_new_documents("connection", staff_id=6))


# ── the happy path ──────────────────────────────────────────────────────────

def test_a_new_file_is_ingested_and_written_down(bucket):
    bucket.files = [_file("contrato-acme.pdf")]

    report = _run()

    assert bucket.ingested == [("contrato-acme.pdf", 6)]
    # A file at the root of the bucket has no folder to be classified by, which
    # is correct and has to stay visible rather than becoming a guess.
    assert report.ingested == [("contrato-acme.pdf", None, None)]
    assert bucket.recorded[0].outcome == "ingested"
    assert bucket.recorded[0].document_id == 7


def test_a_file_in_a_folder_is_ingested_under_its_own_name(bucket):
    """Folders in a bucket are a naming convention, not a structure. The name is
    handed to the API verbatim and only the last segment titles the document,
    because a path is not a name and neither is ever resolved on disk."""
    bucket.files = [_file("2026/setembro/contrato-acme.pdf")]

    _run()

    assert bucket.downloaded == ["entrada/2026/setembro/contrato-acme.pdf"]
    assert bucket.ingested == [("contrato-acme.pdf", 6)]


def test_what_was_read_before_is_not_read_again(bucket):
    """Ingestion deduplicates, but only after the bytes have arrived and been
    parsed, which is the expensive half."""
    bucket.files = [_file("ja-lido.pdf", file_id="known-1")]
    bucket.known = {"known-1"}

    report = _run()

    assert bucket.downloaded == []
    assert report.already_known == 1
    assert report.touched == 0


def test_a_file_already_in_the_archive_under_another_name_is_named_as_such(bucket):
    """Zero chunks is how ingestion reports a sha256 it has already stored."""
    bucket.files = [_file("copia.pdf")]
    bucket.ingest_result = (7, 0)

    report = _run()

    assert report.duplicates == ["copia.pdf"]
    assert bucket.recorded[0].outcome == "duplicate"


# ── what it refuses ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "planilha.xlsx", "programa.exe", "arquivo.zip", "sem_extensao",
])
def test_a_format_the_archive_does_not_accept_is_skipped(bucket, name):
    """The same allowlist a Telegram attachment must pass. The bucket is a
    delivery route, not a second door with a weaker lock."""
    bucket.files = [_file(name)]

    report = _run()

    assert bucket.downloaded == [], "an unsupported file is never even fetched"
    assert [n for n, _ in report.skipped] == [name]


def test_a_skip_for_format_is_not_remembered(bucket):
    """Otherwise widening the allowlist could never reach what it widened for.

    Real: three .html statutes were listed, skipped and written down. Accepting
    .html the next day would have left them skipped forever, because the ledger
    said they had been dealt with. A skip for format or size is a judgement
    about the rules, not about the file, and it costs nothing to make again.

    The example is a spreadsheet rather than a statute because .html is now on
    the allowlist, which is exactly the change this test exists for.
    """
    bucket.files = [_file("planilha.xlsx")]

    _run()

    assert bucket.recorded == [], "nothing about a format skip is worth keeping"


def test_the_bootstrap_clears_skips_an_older_version_recorded():
    """The rule changed, so the rows that contradict it go.

    Without this the fix would not reach the files it was written for: they are
    already in the ledger, marked as dealt with, from before the allowlist was
    widened.
    """
    import inspect

    from core.repositories import inbox

    source = inspect.getsource(inbox.ensure_inbox_schema)

    assert "DELETE FROM inbox_imports WHERE outcome = 'skipped'" in source


def test_a_failure_after_downloading_is_remembered(bucket):
    """The asymmetry: this one cost a download and will fail identically."""
    bucket.files = [_file("corrompido.pdf")]
    bucket.ingest_error = "no text could be extracted"

    _run()

    assert [r.outcome for r in bucket.recorded] == ["failed"]


def test_the_allowlist_is_the_one_ingestion_enforces():
    """Two copies of an allowlist drift, and the drift is invisible until the
    day one of them lets something through."""
    for extension in DOCUMENT_EXTENSIONS:
        assert inbox_import._unsupported(f"documento{extension}") is None


def test_a_file_over_the_size_limit_is_refused_before_it_is_fetched(bucket):
    bucket.files = [_file("enorme.pdf", size=21 * 1024 * 1024)]

    report = _run()

    assert bucket.downloaded == []
    assert "acima do limite" in report.skipped[0][1]


def test_a_download_that_fails_does_not_stop_the_run(bucket):
    bucket.files = [_file("primeiro.pdf"), _file("segundo.pdf")]
    bucket.download_error = "o arquivo não está mais no balde"

    report = _run()

    assert len(report.failed) == 2


def test_a_download_failure_is_not_held_against_the_file(bucket):
    """It says nothing about the file, and it may be our fault.

    Real: a comma in a file name was sent unencoded and Backblaze refused every
    name containing one. Writing that down would have made those files
    permanently unreachable by the fix written for them.
    """
    bucket.files = [_file("LEI No 8.245, DE 1991.html")]
    bucket.download_error = "O armazenamento recusou a operação (HTTP 400)"

    _run()

    assert bucket.recorded == []


def test_a_file_that_cannot_be_parsed_is_not_downloaded_again(bucket):
    """It would fail identically every run, and re-fetching it nightly forever
    is the kind of waste nobody notices."""
    bucket.files = [_file("corrompido.pdf")]
    bucket.ingest_error = "no text could be extracted"

    report = _run()

    assert report.failed and report.failed[0][0] == "corrompido.pdf"
    assert bucket.recorded[0].outcome == "failed"
    assert bucket.recorded[0].detail


def test_one_run_reads_at_most_the_configured_number_of_files(bucket, monkeypatch):
    """A folder somebody just synchronised must not become one unbounded run."""
    monkeypatch.setattr(settings, "inbox_max_files_per_run", 3)
    bucket.files = [_file(f"documento-{n}.pdf") for n in range(10)]

    report = _run()

    assert report.touched == 3
    assert len(bucket.ingested) == 3


def test_the_import_is_refused_while_the_bucket_is_switched_off(bucket, monkeypatch):
    monkeypatch.setattr(settings, "inbox_enabled", False)

    with pytest.raises(inbox_bucket.InboxError, match="desligada"):
        _run()


def test_nothing_new_does_not_claim_everything_is_in_the_archive():
    """"Already known" is not "already in the archive".

    Real, from the firm's chat: three of four files in the bucket had been
    refused, and the report answered "os 4 arquivos do balde já estavam no
    acervo". The owner went looking in /docs for a document that was never
    ingested and could not find it, which is the exact confusion a report is
    supposed to prevent.
    """
    from core.handlers.legal_message import _render_import_report

    report = inbox_import.ImportReport(listed=4, already_known=4)
    message = _render_import_report(report)

    assert "já estavam no acervo" not in message
    assert "avaliados" in message
    assert "/docs" in message, "it has to say where the truth is"


# ── the client's own boundaries ─────────────────────────────────────────────

_ENDPOINT = "https://br-se1.magaluobjects.com"
_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "object_store_endpoint", _ENDPOINT)
    monkeypatch.setattr(settings, "object_store_region", "br-se1")
    monkeypatch.setattr(settings, "object_store_bucket", "demo-firm-object")
    monkeypatch.setattr(settings, "object_store_access_key_id", "key")
    monkeypatch.setattr(settings, "object_store_secret_access_key", "secret")
    monkeypatch.setattr(settings, "inbox_bucket", None)
    monkeypatch.setattr(settings, "inbox_prefix", "entrada/")


def _serve(monkeypatch, handler):
    """Answer the object store's requests from `handler`, recording them."""
    import httpx

    asked = []

    def record(request):
        asked.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(object_store, "_http", lambda: client)
    return asked


def _listing(*keys, truncated=False, token=None, etag="abc"):
    import httpx

    contents = "".join(
        f"<Contents><Key>{k}</Key><Size>10</Size><ETag>&quot;{etag}&quot;</ETag>"
        f"<LastModified>2026-09-28T22:36:54.475Z</LastModified></Contents>"
        for k in keys
    )
    more = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    body = (f'<ListBucketResult xmlns="{_NS}">{contents}'
            f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{more}</ListBucketResult>")
    return httpx.Response(200, content=body.encode())


def test_the_inbox_refuses_to_work_without_a_key(monkeypatch, configured):
    """A half-configured integration that silently does nothing is worse than
    one that says it is not configured."""
    monkeypatch.setattr(settings, "object_store_secret_access_key", None)

    with pytest.raises(inbox_bucket.InboxError, match="não está configurado"):
        asyncio.run(inbox_bucket.list_files())


def test_an_empty_prefix_on_the_documents_bucket_is_refused(monkeypatch, configured):
    """The encrypted originals live at that bucket's root with real extensions.
    Pointed at the whole bucket, the importer would download ciphertext and
    try to read it as a document."""
    monkeypatch.setattr(settings, "inbox_prefix", "")

    with pytest.raises(inbox_bucket.InboxError, match="pasta própria"):
        asyncio.run(inbox_bucket.list_files())


def test_an_empty_prefix_is_fine_on_a_bucket_of_its_own(monkeypatch, configured):
    monkeypatch.setattr(settings, "inbox_prefix", "")
    monkeypatch.setattr(settings, "inbox_bucket", "demo-firm-inbox")
    asked = _serve(monkeypatch, lambda _r: _listing("a.pdf"))

    files = asyncio.run(inbox_bucket.list_files())

    assert [f.name for f in files] == ["a.pdf"]
    assert asked[0].url.path == "/demo-firm-inbox"


@pytest.mark.parametrize("name,expected", [
    # The one that broke on Backblaze, where a comma went out unencoded.
    ("LEI No 8.245, DE 1991.html", "LEI%20No%208.245%2C%20DE%201991.html"),
    ("CONSTITUIÇÃO DE 1988.html", "CONSTITUI%C3%87%C3%83O%20DE%201988.html"),
    # A folder separator is a real path separator in this URL and stays.
    ("2026/setembro/contrato (v2).pdf", "2026/setembro/contrato%20%28v2%29.pdf"),
])
def test_the_download_url_encodes_the_name_as_it_was_signed(
    monkeypatch, configured, name, expected,
):
    """The path that is signed and the path that is sent are one string, so a
    name cannot be encoded one way for the signature and another on the wire."""
    import httpx

    asked = _serve(monkeypatch, lambda _r: httpx.Response(200, content=b"conteudo"))

    asyncio.run(inbox_bucket.download(f"entrada/{name}", max_bytes=1024))

    assert asked[0].url.raw_path.decode() == f"/demo-firm-object/entrada/{expected}"


def test_the_listing_asks_only_for_the_inbox_folder(monkeypatch, configured):
    asked = _serve(monkeypatch, lambda _r: _listing())

    asyncio.run(inbox_bucket.list_files())

    assert asked[0].url.params["prefix"] == "entrada/"
    assert asked[0].url.params["list-type"] == "2"


def test_a_folder_placeholder_is_not_a_document(monkeypatch, configured):
    """A folder created in a web console is an empty object ending in "/"."""
    _serve(monkeypatch, lambda _r: _listing("entrada/2026/", "entrada/2026/a.pdf"))

    files = asyncio.run(inbox_bucket.list_files())

    assert [(f.key, f.name) for f in files] == [("entrada/2026/a.pdf", "2026/a.pdf")]


def test_a_long_listing_is_followed_page_by_page(monkeypatch, configured):
    def pages(request):
        if "continuation-token" in request.url.params:
            return _listing("entrada/b.pdf")
        return _listing("entrada/a.pdf", truncated=True, token="next page")

    asked = _serve(monkeypatch, pages)

    files = asyncio.run(inbox_bucket.list_files())

    assert [f.name for f in files] == ["a.pdf", "b.pdf"]
    assert asked[1].url.params["continuation-token"] == "next page"


def test_a_corrected_file_under_the_same_name_is_a_new_file(monkeypatch, configured):
    """The ledger remembers name and content together. Keying by name alone
    would silently skip the correction."""
    _serve(monkeypatch, lambda _r: _listing("entrada/contrato.pdf", etag="v1"))
    first = asyncio.run(inbox_bucket.list_files())[0].file_id
    _serve(monkeypatch, lambda _r: _listing("entrada/contrato.pdf", etag="v2"))
    second = asyncio.run(inbox_bucket.list_files())[0].file_id

    assert first != second
    assert len(first) <= 200, "it has to fit the ledger's key column"


def test_a_file_that_grew_past_the_limit_is_refused_not_truncated(monkeypatch, configured):
    import httpx

    _serve(monkeypatch, lambda _r: httpx.Response(200, content=b"x" * 2048))

    with pytest.raises(inbox_bucket.InboxError, match="limite"):
        asyncio.run(inbox_bucket.download("entrada/a.pdf", max_bytes=1024))


def test_a_file_that_disappeared_says_so(monkeypatch, configured):
    import httpx

    _serve(monkeypatch, lambda _r: httpx.Response(404, content=b"<Error/>"))

    with pytest.raises(inbox_bucket.InboxError, match="não está mais no balde"):
        asyncio.run(inbox_bucket.download("entrada/a.pdf", max_bytes=1024))


# ── The folders are the classification the firm already keeps ───────────────
# The bucket is synchronised from the firm's Drive, so its folder tree is a
# classification somebody maintains and keeps current. Throwing it away asks
# for the same information twice, and the second time does not happen: every
# document imported before this landed with no type and no matter at all.

@pytest.mark.parametrize("path, matter, doc_type", [
    ("Clientes/Acme Ltda/Contratos/aditivo.pdf", "Acme Ltda", "contrato"),
    ("Drive/Compartilhado/Banco Beltrano S.A./Acórdãos/x.pdf",
     "Banco Beltrano S.A.", "acórdão"),
    ("Acme Ltda/x.pdf", "Acme Ltda", None),
    ("Contratos/x.pdf", None, "contrato"),
    ("x.pdf", None, None),
])
def test_the_folder_tree_classifies_what_it_names(path, matter, doc_type):
    assert inbox_import.classify_from_path(path) == (matter, doc_type)


def test_the_deepest_folder_wins_because_it_is_the_specific_one():
    """"Clientes/Acme/Processo 123" is filed under the process, not the client.

    The deeper folder is the one somebody chose to create, and it is the more
    precise answer. Taking the shallower one would file every document of a
    client under the client and lose the case.
    """
    matter, doc_type = inbox_import.classify_from_path(
        "Clientes/Acme Ltda/Processo 123/Petições/inicial.pdf"
    )
    assert matter == "Processo 123"
    assert doc_type == "petição"


def test_a_drawer_is_never_read_as_a_client():
    """Somebody who keeps everything under "Clientes/" named no client Clientes.

    Importing one would put the whole archive under a single matter, and the
    filter that matter exists for would stop separating anything.
    """
    for drawer in ("Clientes", "Documentos", "Drive", "Inbox", "Geral"):
        assert inbox_import.classify_from_path(f"{drawer}/x.pdf") == (None, None)


def test_a_folder_that_names_no_type_is_never_guessed_into_one():
    """A folder called "2024" or "Ana" is not a document type. Guessing one
    would put a wrong label on every file underneath it."""
    matter, doc_type = inbox_import.classify_from_path("Clientes/Acme/2024/x.pdf")

    assert doc_type is None
    assert matter == "2024"


def test_a_second_type_folder_is_not_mistaken_for_a_client():
    """In "Contratos/Aditivos/x.pdf" neither folder names anybody."""
    assert inbox_import.classify_from_path("Contratos/Aditivos/x.pdf") == (
        None, "aditivo",
    )


def test_the_type_map_is_the_one_the_chat_already_uses():
    """Two copies of this table become two vocabularies for one archive.

    The catalogue filters on the same values a caption writes and a folder
    names, so "Contratos", "contratos" and "tipo: contrato" have to agree.
    """
    from core.services.legal_assistant.intents import normalize_doc_type

    assert normalize_doc_type("Contratos") == "contrato"
    assert inbox_import.classify_from_path("Contratos/x.pdf")[1] == (
        normalize_doc_type("contratos")
    )


def test_the_classification_actually_reaches_ingestion(bucket):
    """The seam. classify_from_path can be perfect and reach nothing.

    Until this test the stub swallowed doc_type and matter in **kwargs, so the
    import could have gone on storing every bucket document unclassified with
    the whole suite green.
    """
    bucket.files = [_file("Clientes/Acme Ltda/Contratos/aditivo.pdf")]

    report = _run()

    assert bucket.classified == [("aditivo.pdf", "Acme Ltda", "contrato")]
    assert report.ingested == [
        ("Clientes/Acme Ltda/Contratos/aditivo.pdf", "Acme Ltda", "contrato"),
    ]


def test_the_report_shows_what_each_file_was_filed_as(bucket):
    """Showing it is what makes the folder convention discoverable: somebody
    who reads "sem classificação" learns why without being told."""
    classified = legal_message._imported_line(
        ("Clientes/Acme/Contratos/x.pdf", "Acme", "contrato")
    )
    bare = legal_message._imported_line(("solto.pdf", None, None))

    assert "contrato" in classified and "Acme" in classified
    assert "sem classificação" in bare
