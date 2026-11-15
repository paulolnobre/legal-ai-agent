"""Taking a PJe password without the chat ever seeing it.

A PJe password is the most dangerous secret this system holds: a Microsoft
token reads somebody's files, this one acts as the lawyer in court. Three
properties carry the whole design, and the tests below try to break each.

The chat never carries the credential, only a link. The link works once and
briefly, and what the database holds cannot be used as one. The password is
sealed with its own key, so a leaked Microsoft key does not open it.
"""
import asyncio
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from core.config import settings
from core.crypto import SealError
from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.repositories import pje_credentials
from core.web import pje_enrolment

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_LAWYER = StaffMember(id=7, chat_id="700", name="Bia", role="advogado")


@pytest.fixture
def keys(monkeypatch):
    """Real Fernet keys, so the sealing is exercised rather than stubbed."""
    pje, microsoft = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "pje_cred_key", pje)
    monkeypatch.setattr(settings, "ms_token_key", microsoft)
    return SimpleNamespace(pje=pje, microsoft=microsoft)


# ── the secret at rest ───────────────────────────────────────────────────────


def test_the_password_is_encrypted_before_it_is_stored(keys):
    box = pje_credentials.sealer().seal("senha-do-pje")

    assert b"senha-do-pje" not in box
    assert pje_credentials.sealer().unseal(box) == "senha-do-pje"


def test_the_microsoft_key_does_not_open_a_pje_password(keys, monkeypatch):
    """The whole reason the keys are separate: one leaking must not open the
    other, because what they buy is not comparable."""
    box = pje_credentials.sealer().seal("senha-do-pje")

    monkeypatch.setattr(settings, "pje_cred_key", keys.microsoft)

    with pytest.raises(SealError, match="enrol again"):
        pje_credentials.sealer().unseal(box)


def test_without_a_key_the_password_is_refused_rather_than_stored_bare(monkeypatch):
    """Both the missing key and an invalid one fail closed, so asserting only
    that something was raised proves little. What separates them is the
    message: a missing key is an operator mistake with a fix, and the error
    has to carry that fix or somebody reads "invalid" and goes looking for a
    corrupted value that does not exist."""
    monkeypatch.setattr(settings, "pje_cred_key", None)

    with pytest.raises(SealError, match="PJE_CRED_KEY is not set") as raised:
        pje_credentials.sealer().seal("senha")

    assert "Fernet.generate_key" in str(raised.value), "the error says what to do"


def test_a_rotated_key_is_picked_up_rather_than_cached(keys, monkeypatch):
    """The sealer is built per call. A cached one is a second place the key
    lives, which is how a rotated key works in one path and fails in another."""
    fresh = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "pje_cred_key", fresh)

    box = pje_credentials.sealer().seal("nova")

    assert Fernet(fresh.encode()).decrypt(box) == b"nova"


# ── the link ─────────────────────────────────────────────────────────────────


def test_what_is_stored_for_a_link_cannot_be_used_as_one():
    """The raw token exists only in the Telegram message, so a leaked database
    does not hand over a live enrolment link."""
    token = "um-token-qualquer"

    digest = pje_credentials.token_digest(token)

    assert digest != token and len(digest) == 64
    assert token not in digest


def test_the_same_token_always_digests_the_same_way():
    assert pje_credentials.token_digest("t") == pje_credentials.token_digest("t")


# ── the page ─────────────────────────────────────────────────────────────────


@pytest.fixture
def page(monkeypatch):
    state = SimpleNamespace(peeked=None, spent=None, saved=[], usable=7)

    async def peek_enrolment(_conn, token):
        state.peeked = token
        return state.usable

    async def spend_enrolment(_conn, token):
        state.spent = token
        taken, state.usable = state.usable, None
        return taken

    async def save_credential(_conn, staff_id, *, cpf, password, court):
        state.saved.append((staff_id, cpf, password, court))

    class _Pool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    return "conn"

                async def __aexit__(self, *_args):
                    return False
            return _Ctx()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(settings, "pje_enabled", True)
    monkeypatch.setattr(pje_enrolment, "peek_enrolment", peek_enrolment)
    monkeypatch.setattr(pje_enrolment, "spend_enrolment", spend_enrolment)
    monkeypatch.setattr(pje_enrolment, "save_credential", save_credential)
    monkeypatch.setattr(pje_enrolment, "get_pool", get_pool)
    return state


def _post(body: str, token: str = "tok"):
    """A real Starlette request, so the body is read the way production reads
    it rather than through a double that agrees with whatever we wrote."""
    payload = body.encode()
    sent = False

    async def receive():
        nonlocal sent
        sent, more = True, not sent
        return {"type": "http.request", "body": payload if more else b"",
                "more_body": False}

    request = Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": f"/pje/{token}", "raw_path": b"", "query_string": b"",
        "root_path": "", "scheme": "https", "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
    }, receive)
    return asyncio.run(pje_enrolment.submit(request, token))


def test_the_form_is_shown_for_a_usable_link(page):
    response = asyncio.run(pje_enrolment.show_form("tok"))

    assert response.status_code == 200
    assert b"senha" in bytes(response.body).lower()


def test_looking_at_the_form_does_not_spend_the_link(page):
    asyncio.run(pje_enrolment.show_form("tok"))

    assert page.spent is None, "only posting spends it"


def test_a_credential_is_stored_for_whoever_the_link_belongs_to(page):
    _post("cpf=12345678901&senha=minha-senha")

    assert page.saved == [(7, "12345678901", "minha-senha", settings.pje_court)]


def test_a_link_works_once(page):
    _post("cpf=12345678901&senha=primeira")
    response = _post("cpf=12345678901&senha=segunda")

    assert len(page.saved) == 1, "the second post must not store anything"
    assert response.status_code == 404


def test_a_failed_submission_still_burns_the_link(page):
    """A link that survives a failed attempt is a link that can be retried."""
    _post("cpf=123&senha=curto-demais")

    assert page.spent == "tok"
    assert page.saved == []


def test_every_reason_a_link_fails_reads_the_same(page):
    """Telling expired from used from never-existed tells a stranger which
    tokens are real."""
    page.usable = None

    opened = asyncio.run(pje_enrolment.show_form("tok"))
    posted = _post("cpf=12345678901&senha=x")

    assert opened.body == posted.body
    assert opened.status_code == posted.status_code == 404


def test_a_cpf_that_is_not_eleven_digits_is_refused(page):
    response = _post("cpf=123&senha=alguma")

    assert response.status_code == 400 and page.saved == []


def test_an_empty_password_is_refused(page):
    response = _post("cpf=12345678901&senha=")

    assert response.status_code == 400 and page.saved == []


def test_punctuation_in_the_cpf_is_accepted_and_normalised(page):
    _post("cpf=123.456.789-01&senha=minha-senha")

    assert page.saved[0][1] == "12345678901"


def test_an_oversized_body_is_not_parsed(page):
    """Starlette buffers the body before this sees it, so the cap is what keeps
    a public endpoint from being made to read megabytes."""
    huge = "cpf=12345678901&senha=" + "x" * (pje_enrolment._MAX_BODY + 1)

    response = _post(huge)

    assert page.saved == [] and response.status_code == 400


def test_the_page_never_reaches_out_to_anywhere(page):
    """Anything fetched from elsewhere is another party watching a password
    being typed."""
    body = bytes(asyncio.run(pje_enrolment.show_form("tok")).body).decode()

    assert "http://" not in body and "https://" not in body
    assert "<script" not in body.lower()


def test_the_page_refuses_to_be_framed_or_cached(page):
    response = asyncio.run(pje_enrolment.show_form("tok"))

    assert response.headers["X-Frame-Options"] == "DENY"
    assert "no-store" in response.headers["Cache-Control"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_the_token_in_the_form_action_is_escaped(page):
    """The token comes from the URL, which is somebody else's input."""
    response = asyncio.run(pje_enrolment.show_form('"><script>alert(1)</script>'))

    assert "<script>alert" not in bytes(response.body).decode()


def test_nothing_is_served_while_the_feature_is_off(page, monkeypatch):
    monkeypatch.setattr(settings, "pje_enabled", False)

    assert asyncio.run(pje_enrolment.show_form("tok")).status_code == 404
    assert _post("cpf=12345678901&senha=x").status_code == 404
    assert page.saved == []


# ── the command ──────────────────────────────────────────────────────────────


@pytest.fixture
def command(monkeypatch):
    state = SimpleNamespace(sent=[], previews=[], credential=None, opened=[],
                            forgotten=False)

    async def get_staff(_conn, _chat_id):
        return _LAWYER

    async def send(_chat_id, message, reply_markup=None, *, preview=True):
        state.sent.append(message)
        state.previews.append(preview)
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def get_credential(_conn, _staff_id):
        return state.credential

    async def open_enrolment(_conn, staff_id, *, ttl_seconds):
        state.opened.append((staff_id, ttl_seconds))
        return "token-gerado"

    async def forget_credential(_conn, _staff_id):
        return state.forgotten

    monkeypatch.setattr(settings, "pje_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "https://agente.exemplo.br")
    monkeypatch.setattr(settings, "web_base_url", None)
    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message.pje_credentials, "get_credential", get_credential)
    monkeypatch.setattr(legal_message.pje_credentials, "open_enrolment", open_enrolment)
    monkeypatch.setattr(
        legal_message.pje_credentials, "forget_credential", forget_credential,
    )
    return state


def _command(text: str):
    update = {"update_id": 1, "message": {
        "chat": {"id": 700, "type": "private"}, "from": {"id": 700}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


def test_the_command_sends_a_link_and_never_asks_for_the_password(command):
    _command("/pje")

    assert "https://agente.exemplo.br/pje/token-gerado" in command.sent[-1]
    assert "senha" in command.sent[-1].casefold()
    assert "nunca aqui no chat" in command.sent[-1]


def test_the_link_is_sent_without_a_preview(command):
    """With a preview on, Telegram's servers fetch the link before the
    person does. The GET does not spend it, but a one-time link is no
    business of anyone else's."""
    _command("/pje")

    assert command.previews[-1] is False


def test_the_link_says_how_long_it_lasts(command):
    _command("/pje")

    assert "minutos" in command.sent[-1]


def test_somebody_already_enrolled_sees_a_masked_cpf(command):
    from datetime import datetime
    command.credential = pje_credentials.Credential(
        staff_id=7, cpf="12345678901", court="TJPA", enrolled_at=datetime.now(),
    )

    _command("/pje")

    assert "12345678901" not in command.sent[-1], "the CPF is not echoed whole"
    assert "123." in command.sent[-1] and "-01" in command.sent[-1]
    assert command.opened == [], "an existing credential is not silently replaced"


def test_asking_to_enrol_again_mints_a_new_link(command):
    from datetime import datetime
    command.credential = pje_credentials.Credential(
        staff_id=7, cpf="12345678901", court="TJPA", enrolled_at=datetime.now(),
    )

    _command("/pje cadastrar")

    assert command.opened, "the person asked to replace it"


def test_removing_says_to_change_the_password_at_the_pje(command):
    """Deleting our copy does not invalidate a password that was already used."""
    command.forgotten = True

    _command("/pje remover")

    assert "troque a senha" in command.sent[-1].casefold()


def test_without_a_public_address_the_link_is_not_invented(command, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", None)

    _command("/pje")

    assert command.opened == []
    assert "administrador" in command.sent[-1]


def test_enrolling_is_open_to_every_authorized_rank():
    from core.services import authorization

    for role in ("estagiario", "advogado", "admin", "owner"):
        staff = StaffMember(id=1, chat_id="1", name="x", role=role)
        assert authorization.decide(staff, "pje.connect").allowed, role
