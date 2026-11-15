import asyncio
from datetime import UTC, datetime

from core.config import settings
from core.integrations import datajud
from core.services.legal_assistant import orchestrator
from core.services.legal_assistant.state import LegalState


def test_extracts_formatted_and_plain_cnj_numbers_once():
    text = (
        "Consulte 0000832-35.2018.4.01.3202 e depois "
        "00008323520184013202, que é o mesmo processo."
    )
    assert datajud.extract_process_numbers(text) == ["00008323520184013202"]


def test_resolves_datajud_alias_from_cnj_number():
    assert datajud.court_alias("0000832-35.2018.4.01.3202") == "trf1"
    assert datajud.court_alias("0000001-23.2026.5.02.0001") == "trt2"
    assert datajud.court_alias("0000001-23.2026.8.26.0100") == "tjsp"
    assert datajud.court_alias("0000001-23.2026.6.20.0001") == "tre-rn"


def test_lookup_uses_public_key_header_and_specific_court(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "hits": {
                    "hits": [{"_source": {"numeroProcesso": "00008323520184013202"}}]
                }
            }

    class Client:
        is_closed = False

        async def post(self, endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(settings, "datajud_api_key", "public-test-key")
    monkeypatch.setattr(datajud, "_client", Client())

    result = asyncio.run(datajud.lookup_process("0000832-35.2018.4.01.3202"))

    assert captured["endpoint"].endswith("/api_publica_trf1/_search")
    assert captured["headers"]["Authorization"] == "APIKey public-test-key"
    assert captured["json"]["query"]["match"]["numeroProcesso"] == result.process_number
    assert len(result.records) == 1


def test_formats_recent_movements_with_source_limit(monkeypatch):
    monkeypatch.setattr(settings, "datajud_max_movements", 1)
    lookup = datajud.DataJudLookup(
        process_number="00008323520184013202",
        court_alias="trf1",
        endpoint="https://example.test/api_publica_trf1/_search",
        records=({
            "numeroProcesso": "00008323520184013202",
            "tribunal": "TRF1",
            "grau": "JE",
            "classe": {"nome": "Procedimento do Juizado Especial Cível"},
            "orgaoJulgador": {"nome": "JEF"},
            "assuntos": [{"nome": "Concessão"}],
            "movimentos": [
                {"nome": "Distribuição", "dataHora": "2018-10-30T14:06:24Z"},
                {"nome": "Sentença", "dataHora": "2024-01-10T12:00:00Z"},
            ],
        },),
        consulted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    rendered = datajud.format_lookup(lookup)

    assert "Official source: DataJud/CNJ Public API" in rendered
    assert "Sentença" in rendered
    assert "Distribuição" not in rendered
    assert "does not necessarily provide the full text" in rendered


def test_cnj_number_corrects_internal_route_to_external(monkeypatch):
    async def classify(*args, **kwargs):
        return {"task": "internal_research", "query": "consultar processo"}

    monkeypatch.setattr(orchestrator, "complete_json", classify)
    state = LegalState(
        conn=None,
        question="Consulte o processo 0000832-35.2018.4.01.3202",
    )

    result = asyncio.run(orchestrator.orchestrate(state))

    assert result["task"] == "external_research"
    assert result["routed_by"] == "rule:datajud-process-number"
