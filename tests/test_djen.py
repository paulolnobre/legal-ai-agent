"""The DJEN: what was published about a process, read from the CNJ by number.

DataJud says what moved in a process; the DJEN carries what was published
about it, usually with the full text of the decision behind the intimation. It
is the same authority as DataJud, so, unlike the diary search, it needs no
exception to where a process number may go, and it does not wait for DataJud to
confirm the process: a recent case DataJud has not indexed yet can already have
publications.

Two things here would cost a lawyer a deadline if they went wrong quietly: the
newest publication being missed because the API returned another order, and a
date being read as the publication date when it is the date made available.
"""
import asyncio
import logging
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from core.config import settings
from core.integrations import djen
from core.models import StaffMember
from core.services.legal_assistant import external_research, sources
from core.services.legal_assistant.state import LegalState

NUMBER = "0815616-14.2025.8.14.0301"
DIGITS = "08156161420258140301"


def _item(day: str, *, number: str = DIGITS, text: str | None = None, kind: str = "Intimação"):
    return {
        "numero_processo": number,
        "numeroprocessocommascara": NUMBER if number == DIGITS else number,
        "siglaTribunal": "TJPA",
        "tipoComunicacao": kind,
        "tipoDocumento": "Sentença",
        "nomeOrgao": "1ª Vara do Juizado Especial da Fazenda Pública de Belém",
        "nomeClasse": "PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL",
        "data_disponibilizacao": day,
        "link": "https://pje.tjpa.jus.br/pje/Processo/ConsultaDocumento/listView.seam?x=1",
        "texto": text if text is not None else (
            "PODER JUDICIÁRIO DO ESTADO DO PARÁ Fones: (91) 3239-5453 Travessa "
            "Quintino SENTENÇA Vistos etc. Julgo procedente o pedido."
        ),
    }


@pytest.fixture
def api(monkeypatch):
    """The DJEN's HTTP answer, recorded rather than called."""
    state = SimpleNamespace(items=[], status=200, error=None, requests=[])

    class _Response:
        def __init__(self):
            self.status_code = state.status

        def json(self):
            return {"status": "success", "count": len(state.items), "items": state.items}

    class _Client:
        async def get(self, url, params=None):
            state.requests.append((url, dict(params or {})))
            if state.error:
                raise state.error
            return _Response()

    monkeypatch.setattr(djen, "_http", lambda: _Client())
    monkeypatch.setattr(settings, "djen_enabled", True)
    return state


# ── the request ─────────────────────────────────────────────────────────────

def test_the_number_goes_to_the_cnj_as_digits(api):
    asyncio.run(djen.communications(NUMBER))

    url, params = api.requests[0]
    assert url.endswith("/api/v1/comunicacao")
    assert params["numeroProcesso"] == DIGITS
    assert params["itensPorPagina"] >= settings.djen_max_items


# ── what comes back ─────────────────────────────────────────────────────────

def test_the_newest_publication_comes_first_whatever_order_the_api_used(api):
    """The newest one is where a deadline runs from. The API's order is not
    documented, so the choice is made here, from a wider page."""
    api.items = [_item("2026-01-10"), _item("2026-09-11"), _item("2025-12-01"), _item("2026-05-02")]

    found = asyncio.run(djen.communications(NUMBER, limit=2))

    assert [c.made_available for c in found] == [date(2026, 9, 11), date(2026, 5, 2)]


def test_a_publication_about_another_case_is_dropped(api):
    api.items = [_item("2026-09-11"), _item("2026-09-10", number="09999991120228140301")]

    found = asyncio.run(djen.communications(NUMBER))

    assert [c.process_number for c in found] == [NUMBER]


@pytest.mark.parametrize(
    ("status", "error", "refused"),
    [
        (403, None, True),
        (401, None, True),
        (503, None, False),
        (200, httpx.ConnectError("down"), False),
    ],
)
def test_a_failure_is_a_djen_error_that_never_carries_the_number(api, status, error, refused):
    """A refusal is told apart from an outage: from outside Brazil the DJEN's
    CloudFront answers 403, and that is not the DJEN being down."""
    api.status, api.error = status, error

    with pytest.raises(djen.DjenError) as raised:
        asyncio.run(djen.communications(NUMBER))
    assert isinstance(raised.value, djen.DjenRefused) is refused
    assert DIGITS not in str(raised.value) and NUMBER not in str(raised.value)


# ── how it is shown ─────────────────────────────────────────────────────────

def test_every_date_shown_to_a_lawyer_carries_the_publication_rule():
    """The DJEN date is when it was made available. Counting a deadline from it
    is one business day early, which is the error this line exists to prevent."""
    found = [djen.Communication(
        NUMBER, "TJPA", "Intimação", "Sentença", "1ª Vara", "Classe",
        date(2026, 9, 11), _item("2026-09-11")["texto"], "https://pje.tjpa.jus.br/x",
    )]

    shown = djen.format_for_staff(NUMBER, found)

    assert "11/09/2026" in shown
    assert "Intimação, Sentença" in shown
    assert "Lei 11.419/2006" in shown and "primeiro dia útil seguinte" in shown
    assert "https://pje.tjpa.jus.br/x" in shown


def test_the_excerpt_starts_where_the_document_does_not_at_the_letterhead():
    found = [djen.Communication(
        NUMBER, "TJPA", "Intimação", "Sentença", "", "", date(2026, 9, 11),
        _item("2026-09-11")["texto"], None,
    )]

    shown = djen.format_for_staff(NUMBER, found)

    assert "SENTENÇA Vistos etc." in shown
    assert "Fones" not in shown, "the court's phone numbers are not the publication"


def test_nothing_published_is_said_plainly():
    assert "nenhuma publicação" in djen.format_for_staff(NUMBER, [])


def test_a_long_text_is_cut_for_the_model_and_says_so(monkeypatch):
    monkeypatch.setattr(settings, "djen_max_chars", 600)
    long_text = "SENTENÇA " + "fundamentação " * 200
    found = djen.Communication(NUMBER, "TJPA", "Intimação", "Sentença", "", "", None, long_text, None)

    block = djen.format_for_model(found)

    assert "[Texto truncado no limite configurado.]" in block
    assert "Data de disponibilização não informada" in block


def test_a_publication_is_its_own_kind_of_source():
    found = djen.Communication(NUMBER, "TJPA", "Intimação", "Sentença", "", "", date(2026, 9, 11), "x", None)

    source = sources.from_djen(found)

    assert source.kind == "djen"
    assert "publicação do DJEN/CNJ" in sources.summary([source])


def test_the_suite_never_reaches_the_live_djen_by_default():
    """conftest turns it off; a test about the DJEN turns it on and fakes it."""
    assert settings.djen_enabled is False


# ── the research pipeline ───────────────────────────────────────────────────

@pytest.fixture
def pipeline(monkeypatch):
    state = SimpleNamespace(asked=[], found=[], error=None, system="", content=[])

    async def communications(number):
        state.asked.append(number)
        if state.error:
            raise state.error
        return state.found

    async def nothing(_text):
        return [], [], [], []

    async def no_web(_query, _provider=None):
        return []

    async def no_pages(_sources):
        return [], [], []

    async def complete(system, content, **_kwargs):
        state.system, state.content = system, content
        return "resposta"

    async def log_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "djen_enabled", True)
    monkeypatch.setattr(external_research.djen, "communications", communications)
    monkeypatch.setattr(external_research, "_datajud_context", nothing)
    monkeypatch.setattr(external_research.websearch, "search", no_web)
    monkeypatch.setattr(external_research, "_read_official_pages", no_pages)
    monkeypatch.setattr(external_research, "complete", complete)
    monkeypatch.setattr(external_research, "log_access", log_access)
    return state


def _ask(question):
    return asyncio.run(external_research.research(LegalState(
        conn=None, question=question, query=question, task="external_research",
    )))


def test_the_djen_is_read_even_when_datajud_found_nothing(pipeline):
    """A recent case can be published before DataJud indexes it. The DJEN is the
    same authority, so it does not wait for DataJud's confirmation."""
    pipeline.found = [djen.Communication(
        NUMBER, "TJPA", "Intimação", "Sentença", "1ª Vara", "Classe",
        date(2026, 9, 11), "SENTENÇA Julgo procedente.", "https://pje.tjpa.jus.br/x",
    )]

    result = _ask(f"quais as publicações do processo {NUMBER}?")

    assert pipeline.asked == [DIGITS]
    titles = [block.get("title", "") for block in pipeline.content]
    assert any(t.startswith("DJEN/CNJ: Intimação de 11/09/2026") for t in titles)
    assert "11.419/2006" in pipeline.system, "the model is told how the date is read"
    assert "Lei 11.419/2006" in result["answer"]
    assert any(source.kind == "djen" for source in result["sources"])


def test_the_switch_turns_the_djen_off(pipeline, monkeypatch):
    monkeypatch.setattr(settings, "djen_enabled", False)

    _ask(f"quais as publicações do processo {NUMBER}?")

    assert pipeline.asked == []


def test_a_djen_failure_is_a_limitation_not_a_failed_answer(pipeline):
    pipeline.error = djen.DjenError("the DJEN did not respond")

    result = _ask(f"quais as publicações do processo {NUMBER}?")

    assert result["answer"].startswith("resposta")
    assert any("DJEN" in limitation and "falhou" in limitation for limitation in result["limitations"])


def test_a_refusal_reaches_the_answer_as_a_refusal_with_where_to_read(pipeline, caplog):
    pipeline.error = djen.DjenRefused("the DJEN refused the request (HTTP 403)")

    with caplog.at_level(logging.WARNING):
        result = _ask(f"quais as publicações do processo {NUMBER}?")

    limitation = next(item for item in result["limitations"] if item.startswith("DJEN"))
    assert "recusou" in limitation
    assert djen.PUBLIC_SITE in limitation
    assert "HTTP 403" in caplog.text
    assert DIGITS not in caplog.text and NUMBER not in caplog.text


# ── /processo ───────────────────────────────────────────────────────────────

@pytest.fixture
def process_command(monkeypatch):
    from core.handlers import legal_message
    from core.services import channels
    from core.integrations import datajud

    state = SimpleNamespace(sent=[], djen_error=None, datajud_error=False)

    async def lookup(number):
        if state.datajud_error:
            raise datajud.DataJudError("the DataJud API did not respond")
        return datajud.DataJudLookup(
            process_number=DIGITS, court_alias="tjpa",
            endpoint="https://api-publica.datajud.cnj.jus.br/api_publica_tjpa/_search",
            records=(), consulted_at=__import__("datetime").datetime.now(),
        )

    async def communications(number):
        if state.djen_error:
            raise state.djen_error
        return [djen.Communication(
            NUMBER, "TJPA", "Intimação", "Sentença", "1ª Vara", "Classe",
            date(2026, 9, 11), "SENTENÇA Julgo procedente.", None,
        )]

    async def send(_chat_id, text, reply_markup=None, **_kwargs):
        state.sent.append(text)

    async def quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "djen_enabled", True)
    monkeypatch.setattr(legal_message.datajud, "lookup_process", lookup)
    monkeypatch.setattr(legal_message.djen, "communications", communications)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", quiet)
    monkeypatch.setattr(legal_message, "log_access", quiet)
    staff = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
    return state, lambda: asyncio.run(legal_message._report_process(None, 600, staff, NUMBER))


def test_process_lists_what_the_djen_published(process_command):
    state, run = process_command

    run()

    assert "Publicações no DJEN" in state.sent[-1]
    assert "11/09/2026" in state.sent[-1]


def test_process_still_answers_when_the_djen_is_down(process_command):
    state, run = process_command
    state.djen_error = djen.DjenError("the DJEN did not respond")

    run()

    assert "O DJEN não respondeu agora" in state.sent[-1]
    assert djen.PUBLIC_SITE in state.sent[-1]


def test_process_says_the_djen_refused_and_where_to_read_it(process_command, caplog):
    """Measured on 2026-09-11: the DJEN answered 403 to the VPS in the United
    States. Calling that "not now" sent a lawyer to try again for nothing."""
    state, run = process_command
    state.djen_error = djen.DjenRefused("the DJEN refused the request (HTTP 403)")

    with caplog.at_level(logging.WARNING):
        run()

    assert "O DJEN recusou a consulta" in state.sent[-1]
    assert djen.PUBLIC_SITE in state.sent[-1]
    assert "HTTP 403" in caplog.text
    assert DIGITS not in caplog.text and NUMBER not in caplog.text


def test_process_still_shows_publications_when_datajud_is_down(process_command):
    state, run = process_command
    state.datajud_error = True

    run()

    assert "Não consegui consultar o DataJud agora" in state.sent[-1]
    assert "Publicações no DJEN" in state.sent[-1]
