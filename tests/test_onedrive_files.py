"""Reading one lawyer's own drive, and nobody else's.

The firm's Microsoft accounts are unrelated to each other, so the whole
safety of this rests on one property: every call spends the token of the
person who asked. The tests below try to break that, and the second thing
they try to break is where the file lands, because a contract pulled from
somebody's personal OneDrive into the firm's shelf is the same leak the
folders were built to prevent, arriving by a new door.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core.config import settings
from core.handlers import legal_message
from core.services import channels
from core.integrations import microsoft
from core.models import StaffMember
from core.services import microsoft_files

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")


def _item(name="Contrato Acme.docx", size=120_000, item_id="01ABC"):
    return microsoft.DriveItem(item_id=item_id, name=name, size=size,
                               modified="2026-09-20", web_url="https://onedrive/x")


# ── whose token is spent ────────────────────────────────────────────────────


@pytest.fixture
def account(monkeypatch):
    state = SimpleNamespace(
        tokens={6: "refresh-lia", 9: "refresh-ana"}, spent=[], saved=[], touched=[],
        rotate=False, declined=False, listed=[], downloaded=[], ingested=[],
    )

    async def get_refresh_token(_conn, staff_id):
        return state.tokens.get(staff_id)

    async def access_token_from(refresh_token):
        state.spent.append(refresh_token)
        if state.declined:
            raise microsoft.SignInDeclined("invalid_grant")
        new = f"rotated-{refresh_token}" if state.rotate else refresh_token
        return microsoft.Tokens(access_token=f"access-for-{refresh_token}", refresh_token=new)

    async def save_account(_conn, staff_id, *, account_name, refresh_token):
        state.saved.append((staff_id, account_name, refresh_token))

    async def touch(_conn, staff_id):
        state.touched.append(staff_id)

    async def whoami(_access_token):
        return "lia@escritorio.adv.br"

    async def list_files(access_token, query="", limit=10):
        state.listed.append((access_token, query, limit))
        return [_item()]

    async def download_file(access_token, item_id, *, max_bytes):
        state.downloaded.append((access_token, item_id, max_bytes))
        return b"conteudo do contrato"

    async def ingest_document(_conn, data, **kwargs):
        state.ingested.append((data, kwargs))
        return SimpleNamespace(document_id=31, chunk_count=4, text="...")

    monkeypatch.setattr(microsoft_files, "get_refresh_token", get_refresh_token)
    monkeypatch.setattr(microsoft_files, "save_account", save_account)
    monkeypatch.setattr(microsoft_files, "touch", touch)
    monkeypatch.setattr(microsoft_files, "ingest_document", ingest_document)
    monkeypatch.setattr(microsoft_files.microsoft, "access_token_from", access_token_from)
    monkeypatch.setattr(microsoft_files.microsoft, "whoami", whoami)
    monkeypatch.setattr(microsoft_files.microsoft, "list_files", list_files)
    monkeypatch.setattr(microsoft_files.microsoft, "download_file", download_file)
    return state


def test_each_person_spends_their_own_token(account):
    asyncio.run(microsoft_files.list_files("conn", 6))
    asyncio.run(microsoft_files.list_files("conn", 9))

    assert account.spent == ["refresh-lia", "refresh-ana"]
    assert account.listed[0][0] == "access-for-refresh-lia"
    assert account.listed[1][0] == "access-for-refresh-ana"


def test_somebody_who_never_connected_is_told_so_and_nothing_is_called(account):
    with pytest.raises(microsoft_files.NotConnected):
        asyncio.run(microsoft_files.list_files("conn", 404))

    assert account.spent == [] and account.listed == []


def test_a_rotated_refresh_token_is_stored_back(account):
    """Microsoft may hand out a new one every time. Ignoring it means the
    connection works for hours and then dies for no visible reason."""
    account.rotate = True

    asyncio.run(microsoft_files.list_files("conn", 6))

    assert account.saved == [(6, "lia@escritorio.adv.br", "rotated-refresh-lia")]


def test_an_unchanged_token_is_not_rewritten(account):
    asyncio.run(microsoft_files.list_files("conn", 6))

    assert account.saved == []
    assert account.touched == [6], "the connection is still marked as used"


def test_a_revoked_connection_reads_as_not_connected(account):
    """Revoked, expired, or the password changed: all the same to the lawyer,
    who has to reconnect either way."""
    account.declined = True

    with pytest.raises(microsoft_files.NotConnected):
        asyncio.run(microsoft_files.list_files("conn", 6))


# ── where the file lands ────────────────────────────────────────────────────


def test_a_file_from_somebody_drive_lands_in_their_own_folder(account):
    result = asyncio.run(microsoft_files.import_file("conn", 6, _item()))

    _data, kwargs = account.ingested[0]
    assert kwargs["visibility"] == "private", "their drive, their document"
    assert kwargs["uploaded_by"] == 6
    assert kwargs["filename"] == "Contrato Acme.docx"
    assert result.document_id == 31


def test_a_file_above_the_limit_is_refused_before_it_is_downloaded(account):
    huge = _item(size=(settings.max_doc_mb + 1) * 1024 * 1024)

    with pytest.raises(ValueError, match="limite"):
        asyncio.run(microsoft_files.import_file("conn", 6, huge))

    assert account.downloaded == [], "the claim is checked before the bytes are fetched"


def test_the_download_is_also_capped_at_the_ingestion_limit(account):
    """The listing's size is a claim; the response is the fact."""
    asyncio.run(microsoft_files.import_file("conn", 6, _item()))

    _token, _item_id, max_bytes = account.downloaded[0]
    assert max_bytes == settings.max_doc_mb * 1024 * 1024


# ── the integration's own edges ─────────────────────────────────────────────


def _stub_graph(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(microsoft, "_http", lambda: client)


def test_folders_and_notebooks_are_not_offered_as_documents(monkeypatch):
    _stub_graph(monkeypatch, lambda request: httpx.Response(200, json={"value": [
        {"id": "1", "name": "Contratos", "folder": {"childCount": 3}},
        {"id": "2", "name": "Peticao.docx", "size": 1000, "file": {"mimeType": "x"},
         "lastModifiedDateTime": "2026-09-20T10:00:00Z"},
    ]}))

    found = asyncio.run(microsoft.list_files("token"))

    assert [item.name for item in found] == ["Peticao.docx"]


def test_a_quote_in_the_search_does_not_end_the_query(monkeypatch):
    asked = {}

    def handler(request):
        asked["url"] = str(request.url)
        return httpx.Response(200, json={"value": []})

    _stub_graph(monkeypatch, handler)
    asyncio.run(microsoft.list_files("token", query="contrato d'água"))

    assert "d''" in asked["url"] or "d%27%27" in asked["url"]


def test_an_expired_connection_is_told_apart_from_a_graph_outage(monkeypatch):
    _stub_graph(monkeypatch, lambda request: httpx.Response(401, json={"error": "unauthorized"}))

    with pytest.raises(microsoft.SignInDeclined):
        asyncio.run(microsoft.list_files("stale-token"))


def test_a_file_larger_than_the_cap_is_refused_rather_than_truncated(monkeypatch):
    _stub_graph(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 2048))

    with pytest.raises(microsoft.MicrosoftError, match="limite"):
        asyncio.run(microsoft.download_file("token", "01ABC", max_bytes=1024))


# ── the command ─────────────────────────────────────────────────────────────


def _command(monkeypatch, text, *, items=None, imported=None, error=None):
    legal_message._LAST_DRIVE_LISTING.clear()
    sent: list[str] = []
    imports: list = []

    async def get_staff(_conn, chat_id):
        return _LAWYER

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def list_files(_conn, _staff_id, query="", limit=10):
        if error:
            raise error
        return items if items is not None else []

    async def import_file(_conn, staff_id, item):
        imports.append((staff_id, item))
        if error:
            raise error
        return imported or SimpleNamespace(document_id=31, chunk_count=4)

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message.microsoft_files, "list_files", list_files)
    monkeypatch.setattr(legal_message.microsoft_files, "import_file", import_file)

    def run(message):
        update = {"update_id": 1, "message": {
            "chat": {"id": 600, "type": "private"}, "from": {"id": 600}, "text": message,
        }}
        asyncio.run(legal_message.process_legal_message("connection", update))

    run(text)
    return sent, imports, run


def test_the_listing_is_numbered_and_says_where_a_file_would_land(monkeypatch):
    sent, _imports, _run = _command(monkeypatch, "/onedrive contrato", items=[
        _item("Contrato Acme.docx"), _item("Contrato Beta.pdf", size=2_000_000),
    ])

    assert "1. Contrato Acme.docx" in sent[-1]
    assert "2. Contrato Beta.pdf (1.9 MB" in sent[-1]
    assert "pasta pessoal" in sent[-1]


def test_bringing_a_file_uses_the_number_from_the_last_listing(monkeypatch):
    sent, imports, run = _command(monkeypatch, "/onedrive", items=[
        _item("Primeiro.docx", item_id="A"), _item("Segundo.docx", item_id="B"),
    ])

    run("/onedrive trazer 2")

    assert imports[0][0] == 6
    assert imports[0][1].item_id == "B"
    assert "[31]" in sent[-1] and "/compartilhar 31" in sent[-1]


def test_a_number_nobody_listed_is_refused(monkeypatch):
    _sent, imports, run = _command(monkeypatch, "/onedrive", items=[_item()])

    run("/onedrive trazer 9")

    assert imports == []


def test_asking_to_bring_before_listing_says_to_list(monkeypatch):
    sent, imports, _run = _command(monkeypatch, "/onedrive trazer 1")

    assert imports == []
    assert "/onedrive primeiro" in sent[-1].casefold()


def test_somebody_who_never_connected_is_pointed_at_the_connect_command(monkeypatch):
    sent, _imports, _run = _command(
        monkeypatch, "/onedrive", error=microsoft_files.NotConnected("none"),
    )

    assert "/microsoft" in sent[-1]


def test_the_menu_offers_it_in_the_firms_words():
    menu = {entry["command"]: entry["description"] for entry in legal_message.command_menu()}
    assert menu["onedrive"] == "lista os seus arquivos"
