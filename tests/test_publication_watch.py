"""A new intimation reaches the lawyer it is for, once, without anybody asking.

The failure this exists to prevent is silence: an intimation made available on
a Friday, nobody looking until Wednesday, and a deadline already running. So
the tests below care most about the two ways it could go silent (a publication
wrongly treated as already told, a notice recorded that never went out) and
the one way it could go wrong loudly (somebody else's case, from a mistyped
number, arriving as if it were the firm's).
"""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest

from core.config import settings
from core.handlers import legal_message
from core.integrations import datajud, djen
from core.models import StaffMember
from core.repositories.publications import Watch
from core.services import publication_watch
from core.services.authorization import decide

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

# Real numbers, as the DJEN returned them for the TJPA on 2026-09-18.
_REAL_CNJ = ["08250468720258140301", "08025251720268140301", "09027481220258140301"]


# ── the process number ──────────────────────────────────────────────────────


def test_real_process_numbers_pass_the_check_digits():
    for number in _REAL_CNJ:
        assert datajud.has_valid_check_digits(number)


def test_one_mistyped_digit_is_caught():
    number = _REAL_CNJ[0]
    for position in (0, 8, 12, 19):
        wrong = number[:position] + str((int(number[position]) + 1) % 10) + number[position + 1:]
        assert not datajud.has_valid_check_digits(wrong), wrong


# ── the OAB ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("written", [
    ["12345", "PA"], ["12345/PA"], ["PA", "12345"], ["12.345-pa"], ["012345", "pa"],
])
def test_an_oab_is_read_however_it_is_written(written):
    assert legal_message._parse_oab(written) == ("12345", "PA")


@pytest.mark.parametrize("written", [
    ["12345"], ["12345", "XX"], ["12345", "PA", "SP"], ["12345678", "PA"], ["PA"],
])
def test_an_oab_that_cannot_be_read_is_not_guessed(written):
    assert legal_message._parse_oab(written) is None


# ── asking the DJEN for a lawyer ────────────────────────────────────────────


def _item(pub_id: int, oab: str = "12345", uf: str = "PA", day: str = "2026-09-18") -> dict:
    return {
        "id": pub_id,
        "numero_processo": _REAL_CNJ[0],
        "siglaTribunal": "TJPA",
        "tipoComunicacao": "Intimação",
        "tipoDocumento": "Decisão",
        "nomeOrgao": "1ª Vara Cível de Belém",
        "data_disponibilizacao": day,
        "texto": "DECISÃO Defiro o pedido.",
        "destinatarioadvogados": [{"advogado": {"numero_oab": oab, "uf_oab": uf}}],
    }


def _stub_djen(monkeypatch, pages: list[list[dict]], seen: list[dict] | None = None,
               status: int = 200):
    def handler(request):
        params = dict(request.url.params)
        if seen is not None:
            seen.append(params)
        if status != 200:
            return httpx.Response(status)
        page = int(params["pagina"])
        items = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(200, json={"items": items})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(djen, "_http", lambda: client)


def _lawyer_query():
    return asyncio.run(djen.communications_for_lawyer(
        "12345", "PA", since=date(2026, 9, 15), until=date(2026, 9, 18),
    ))


def test_every_page_is_read_until_a_short_one(monkeypatch):
    seen: list[dict] = []
    full = [_item(i) for i in range(100)]
    _stub_djen(monkeypatch, [full, [_item(500)]], seen)

    found = _lawyer_query()

    assert len(found) == 101
    assert [p["pagina"] for p in seen] == ["1", "2"]
    assert seen[0]["numeroOab"] == "12345" and seen[0]["ufOab"] == "PA"
    assert seen[0]["dataDisponibilizacaoInicio"] == "2026-09-15"


def test_the_page_ceiling_is_logged_not_silently_treated_as_the_end(monkeypatch, caplog):
    monkeypatch.setattr(settings, "djen_watch_max_pages", 2)
    full = [_item(i) for i in range(100)]
    _stub_djen(monkeypatch, [full, full, full])

    with caplog.at_level("WARNING"):
        _lawyer_query()

    assert "later publications were not read" in caplog.text


def test_an_item_the_djen_does_not_address_to_this_lawyer_is_dropped(monkeypatch):
    _stub_djen(monkeypatch, [[_item(1), _item(2, oab="99999"), _item(3, uf="SP")]])

    assert [c.publication_id for c in _lawyer_query()] == [1]


def test_a_refusal_is_told_apart_from_an_outage(monkeypatch):
    _stub_djen(monkeypatch, [], status=403)
    with pytest.raises(djen.DjenRefused):
        _lawyer_query()


# ── deciding what is new, and for whom ──────────────────────────────────────


def _comm(pub_id: int, day: date) -> djen.Communication:
    return djen.Communication(
        process_number="0825046-87.2025.8.14.0301", court="TJPA", kind="Intimação",
        document_type="Decisão", body="1ª Vara Cível", case_class="Procedimento Comum",
        made_available=day, text="DECISÃO Defiro.", link=None, publication_id=pub_id,
    )


def _watch(staff_id: int, kind: Literal["oab", "process"], key: str, since: date) -> Watch:
    return Watch(staff_id=staff_id, chat_id=str(staff_id * 100), kind=kind, key=key,
                 since=datetime(since.year, since.month, since.day, 12, tzinfo=timezone.utc))


@pytest.fixture
def djen_answers(monkeypatch):
    answers = SimpleNamespace(by_oab={}, by_process={}, refuse=False)

    async def for_lawyer(number, uf, *, since, until):
        if answers.refuse:
            raise djen.DjenRefused("HTTP 403")
        return answers.by_oab.get(f"{number}/{uf}", [])

    async def for_process(number, *, limit=None):
        return answers.by_process.get(number, [])

    monkeypatch.setattr(publication_watch.djen, "communications_for_lawyer", for_lawyer)
    monkeypatch.setattr(publication_watch.djen, "communications", for_process)
    return answers


def _collect(watches, today=date(2026, 9, 21)):
    return asyncio.run(publication_watch._collect(watches, today))


def test_nothing_from_before_somebody_started_following_is_sent(djen_answers):
    djen_answers.by_oab["12345/PA"] = [_comm(1, date(2026, 9, 19)), _comm(2, date(2026, 9, 21))]

    found = _collect([_watch(4, "oab", "12345/PA", since=date(2026, 9, 20))])

    assert list(found) == [2], "following an OAB does not unload last week into the chat"


def test_the_same_intimation_reaches_two_people_by_two_routes(djen_answers):
    same = _comm(7, date(2026, 9, 21))
    djen_answers.by_oab["12345/PA"] = [same]
    djen_answers.by_process[_REAL_CNJ[0]] = [same]

    found = _collect([
        _watch(4, "oab", "12345/PA", since=date(2026, 9, 1)),
        _watch(6, "process", _REAL_CNJ[0], since=date(2026, 9, 1)),
    ])

    assert found[7].reasons == {4: "OAB 12345/PA", 6: "processo acompanhado"}


def test_a_refusal_ends_the_pass_rather_than_being_skipped(djen_answers):
    djen_answers.refuse = True
    with pytest.raises(djen.DjenRefused):
        _collect([_watch(4, "oab", "12345/PA", since=date(2026, 9, 1))])


# ── one pass, end to end ────────────────────────────────────────────────────


@pytest.fixture
def pass_state(monkeypatch, djen_answers):
    state = SimpleNamespace(
        watches=[_watch(4, "oab", "12345/PA", since=date(2026, 9, 1))],
        told=set(), recorded=[], sent=[], deliver=True,
    )
    djen_answers.by_oab["12345/PA"] = [_comm(1, date(2026, 9, 21)), _comm(2, date(2026, 9, 20))]

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def watches(_conn):
        return state.watches

    async def noticed(_conn, ids):
        return {pair for pair in state.told if pair[0] in ids}

    async def record(_conn, staff_id, ids):
        state.recorded.append((staff_id, sorted(ids)))

    async def purge(_conn, *, keep_days):
        return 0

    async def send(chat_id, text, parse_mode="Markdown", **_kwargs):
        state.sent.append((chat_id, text, parse_mode))
        return 1 if state.deliver else None

    monkeypatch.setattr(publication_watch, "get_pool", pool)
    monkeypatch.setattr(publication_watch, "list_active_watches", watches)
    monkeypatch.setattr(publication_watch, "noticed", noticed)
    monkeypatch.setattr(publication_watch, "record_notices", record)
    monkeypatch.setattr(publication_watch, "purge_old_notices", purge)
    monkeypatch.setattr(publication_watch.telegram, "send_message", send)
    monkeypatch.setattr(
        publication_watch, "local_now",
        lambda now=None: (now or datetime(2026, 9, 21, 15, tzinfo=timezone.utc)),
    )
    return state


def test_new_publications_are_sent_and_then_remembered(pass_state):
    delivered = asyncio.run(publication_watch.check_once())

    assert delivered == 2
    chat, text, parse_mode = pass_state.sent[0]
    assert chat == "400"
    assert "2 publicações novas no DJEN" in text
    assert parse_mode is None, "court text is not Markdown and must not be read as it"
    assert djen.DATE_RULE in text, "the availability date is not the publication date"
    assert pass_state.recorded == [(4, [1, 2])]


def test_what_was_already_told_is_not_told_again(pass_state):
    pass_state.told = {(1, 4)}

    asyncio.run(publication_watch.check_once())

    assert pass_state.recorded == [(4, [2])]
    assert "Publicação nova no DJEN" in pass_state.sent[0][1]


def test_a_notice_that_did_not_go_out_is_not_recorded(pass_state):
    """Recorded, it would never be tried again: the silence this exists to end."""
    pass_state.deliver = False

    delivered = asyncio.run(publication_watch.check_once())

    assert delivered == 0
    assert pass_state.recorded == []


def test_nobody_following_anything_costs_no_query(pass_state, djen_answers):
    pass_state.watches = []
    djen_answers.refuse = True  # would raise if anything were asked

    assert asyncio.run(publication_watch.check_once()) == 0


def test_many_publications_are_split_under_telegrams_limit():
    entries = [publication_watch._entry(_comm(i, date(2026, 9, 21)), "OAB 12345/PA")
               for i in range(60)]

    messages = publication_watch.messages_for(entries)

    assert len(messages) > 1
    assert all(len(message) <= 4096 for message in messages)
    assert all(djen.DATE_RULE in message for message in messages)
    assert sum(message.count("• 21/09/2026") for message in messages) == 60


# ── the commands ────────────────────────────────────────────────────────────

_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")


def test_every_rank_may_follow_the_djen():
    assert decide(_INTERN, "publications.watch").allowed


def _command(monkeypatch, text, **stubs):
    legal_message._last_deliveries.clear()
    sent: list[str] = []
    calls: list[tuple] = []

    async def get_staff(_conn, chat_id):
        return _INTERN

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    def recorder(name, result):
        async def call(*args, **kwargs):
            calls.append((name, args[1:], kwargs))
            return result
        return call

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    for name in ("watch_oab", "unwatch_oab", "watch_process", "unwatch_process"):
        monkeypatch.setattr(legal_message, name, recorder(name, stubs.get(name, True)))

    async def own(_conn, _staff_id):
        return stubs.get("own", ([], []))

    monkeypatch.setattr(legal_message, "list_own_watches", own)

    async def recent(*_args, **_kwargs):
        return stubs.get("recent", [])

    monkeypatch.setattr(legal_message.djen, "communications_for_lawyer", recent)
    update = {"update_id": 1, "message": {
        "chat": {"id": 400, "type": "private"}, "from": {"id": 400}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))
    return sent, calls


def test_following_an_oab_says_what_the_last_week_held(monkeypatch):
    sent, calls = _command(monkeypatch, "/oab 12.345/pa",
                           recent=[_comm(1, date(2026, 9, 18))] * 3)

    assert calls == [("watch_oab", (4, "12345", "PA"), {})]
    assert "OAB 12345/PA" in sent[-1]
    assert "3 intimação(ões)" in sent[-1]


def test_a_quiet_week_is_said_so_a_typo_shows_itself(monkeypatch):
    sent, _calls = _command(monkeypatch, "/oab 12345 PA", recent=[])
    assert "confira o número" in sent[-1]


def test_a_number_with_wrong_check_digits_is_never_followed(monkeypatch):
    wrong = _REAL_CNJ[0][:8] + str((int(_REAL_CNJ[0][8]) + 1) % 10) + _REAL_CNJ[0][9:]

    sent, calls = _command(monkeypatch, f"/acompanhar {wrong}")

    assert calls == []
    assert "dígitos verificadores" in sent[-1]


def test_a_process_is_followed_by_its_digits(monkeypatch):
    sent, calls = _command(monkeypatch, "/acompanhar 0825046-87.2025.8.14.0301")

    assert calls == [("watch_process", (4, _REAL_CNJ[0]), {})]
    assert "0825046-87.2025.8.14.0301" in sent[-1]


def test_unfollowing_tells_a_process_from_an_oab(monkeypatch):
    _sent, calls = _command(monkeypatch, "/desacompanhar 0825046-87.2025.8.14.0301")
    assert calls == [("unwatch_process", (4, _REAL_CNJ[0]), {})]

    _sent, calls = _command(monkeypatch, "/desacompanhar 12345 PA")
    assert calls == [("unwatch_oab", (4, "12345", "PA"), {})]


def test_asking_with_nothing_lists_what_is_followed(monkeypatch):
    sent, calls = _command(monkeypatch, "/acompanhar",
                           own=(["12345/PA"], [_REAL_CNJ[0]]))

    assert calls == []
    assert "OAB 12345/PA" in sent[-1]
    assert "Processo 0825046-87.2025.8.14.0301" in sent[-1]


def test_the_menu_offers_all_three_in_the_firms_words():
    menu = {entry["command"]: entry["description"] for entry in legal_message.command_menu()}
    assert menu["oab"] == "aviso aqui toda intimação nova no DJEN para essa OAB"
    assert menu["acompanhar"] == "aviso aqui toda publicação nova desse processo"
    assert menu["desacompanhar"] == "para de avisar"
