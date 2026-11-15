"""Client for the public DataJud API maintained by Brazil's CNJ.

The public API exposes process metadata and movements, not the full text of
decisions. Searches are sent only to the court alias derived from the CNJ
process number; querying every index is slow and unnecessary.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from core.config import settings
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_CNJ_NUMBER = re.compile(
    r"(?<!\d)(\d{7})[-.\s]?(\d{2})[.\s]?(\d{4})[.\s]?([1-9])"
    r"[.\s]?(\d{2})[.\s]?(\d{4})(?!\d)"
)

# The TR segment used by the state, electoral and state-military courts.
_STATE_CODES = {
    "01": "ac",
    "02": "al",
    "03": "ap",
    "04": "am",
    "05": "ba",
    "06": "ce",
    "07": "dft",
    "08": "es",
    "09": "go",
    "10": "ma",
    "11": "mt",
    "12": "ms",
    "13": "mg",
    "14": "pa",
    "15": "pb",
    "16": "pr",
    "17": "pe",
    "18": "pi",
    "19": "rj",
    "20": "rn",
    "21": "rs",
    "22": "ro",
    "23": "rr",
    "24": "sc",
    "25": "se",
    "26": "sp",
    "27": "to",
}

_SOURCE_FIELDS = [
    "numeroProcesso",
    "tribunal",
    "grau",
    "nivelSigilo",
    "dataAjuizamento",
    "dataHoraUltimaAtualizacao",
    "classe",
    "assuntos",
    "orgaoJulgador",
    "formato",
    "sistema",
    "movimentos",
]

_client: httpx.AsyncClient | None = None

# Deliberately impossible-looking and fixed: a canary must never disclose which
# cases the firm is handling. It still has the shape needed to address TJSP.
CANARY_PROCESS_NUMBER = "0000000-00.1900.8.26.0000"


class DataJudError(RuntimeError):
    """A safe failure raised by the DataJud integration."""


@dataclass(frozen=True)
class DataJudLookup:
    process_number: str
    court_alias: str
    endpoint: str
    records: tuple[dict[str, Any], ...]
    consulted_at: datetime


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(settings.datajud_timeout_seconds, connect=10.0)
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


def normalize_process_number(value: str) -> str:
    """Return a 20-digit CNJ number, accepting the usual punctuation."""
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 20:
        raise ValueError("the CNJ process number must contain 20 digits")
    return digits


def has_valid_check_digits(value: str) -> bool:
    """Whether a CNJ number's two check digits match the rest of it.

    Resolução CNJ 65/2008: the digits are 98 minus the remainder of the other
    eighteen digits, times 100, divided by 97. A number mistyped by one digit
    fails this, which matters wherever a number is kept rather than looked up
    once: a watched process that is somebody else's case sends somebody
    else's intimations, and nothing about them looks wrong.
    """
    digits = normalize_process_number(value)
    sequence, check, rest = digits[:7], digits[7:9], digits[9:]
    return int(check) == 98 - (int(sequence + rest) * 100) % 97


def format_process_number(value: str) -> str:
    """Write a CNJ number the way it is cited: NNNNNNN-DD.AAAA.J.TR.OOOO.

    `normalize_process_number` strips the punctuation because that is the form
    an API takes. A person reads the punctuated one, and a citation carrying
    twenty bare digits is not a citation anybody can check.
    """
    d = normalize_process_number(value)
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def redact_process_numbers(text: str, placeholder: str = "[processo]") -> str:
    """Remove CNJ numbers from text bound for a third party.

    A process number identifies a client's case. DataJud is authoritative for
    it and is asked directly, so sending the same number to a general search
    engine buys nothing and tells an outside service what the firm is working
    on.
    """
    return _CNJ_NUMBER.sub(placeholder, text)


def extract_process_numbers(text: str, limit: int | None = None) -> list[str]:
    """Extract unique CNJ numbers from natural-language text."""
    maximum = limit if limit is not None else settings.datajud_max_processes
    found: list[str] = []
    for match in _CNJ_NUMBER.finditer(text):
        number = "".join(match.groups())
        if number not in found:
            found.append(number)
        if len(found) >= maximum:
            break
    return found


def court_alias(process_number: str) -> str:
    """Resolve the DataJud court alias from the J.TR fields of a CNJ number."""
    number = normalize_process_number(process_number)
    justice = number[13]
    court = number[14:16]

    if justice == "3" and court == "00":
        return "stj"
    if justice == "4" and court in {"01", "02", "03", "04", "05", "06"}:
        return f"trf{int(court)}"
    if justice == "5":
        if court == "00":
            return "tst"
        if 1 <= int(court) <= 24:
            return f"trt{int(court)}"
    if justice == "6":
        if court == "00":
            return "tse"
        state = _STATE_CODES.get(court)
        if state:
            return f"tre-{state}"
    if justice == "7" and court == "00":
        return "stm"
    if justice == "8":
        state = _STATE_CODES.get(court)
        if state:
            return f"tj{state}"
    if justice == "9":
        state = _STATE_CODES.get(court)
        if state in {"mg", "rs", "sp"}:
            return f"tjm{state}"

    raise ValueError("court not supported by the DataJud Public API")


def _authorization_header() -> str:
    key = (settings.datajud_api_key or "").strip()
    if not key:
        raise DataJudError("the DataJud public API key is not configured")
    return key if key.lower().startswith("apikey ") else f"APIKey {key}"


def _trace_lookup_inputs(_: dict) -> dict:
    return {"process_number_redacted": True}


def _trace_lookup_output(output: DataJudLookup) -> dict:
    return {"court": output.court_alias, "records": len(output.records)}


@traced(
    name="datajud.lookup",
    run_type="tool",
    process_inputs=_trace_lookup_inputs,
    process_outputs=_trace_lookup_output,
)
async def lookup_process(process_number: str) -> DataJudLookup:
    """Fetch public metadata for one CNJ process number."""
    number = normalize_process_number(process_number)
    alias = court_alias(number)
    base_url = settings.datajud_base_url.rstrip("/")
    endpoint = f"{base_url}/api_publica_{alias}/_search"
    payload = {
        "size": settings.datajud_max_processes,
        "_source": _SOURCE_FIELDS,
        "query": {"match": {"numeroProcesso": number}},
    }
    started = time.perf_counter()

    try:
        response = await _http().post(
            endpoint,
            headers={
                "Authorization": _authorization_header(),
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except httpx.HTTPError as exc:
        observe_external("datajud", "error", time.perf_counter() - started)
        raise DataJudError("the DataJud API did not respond") from exc

    if response.status_code in {401, 403}:
        observe_external("datajud", "error", time.perf_counter() - started)
        raise DataJudError("the DataJud API rejected the configured public key")
    if response.status_code != 200:
        observe_external("datajud", "error", time.perf_counter() - started)
        raise DataJudError(f"the DataJud API returned HTTP {response.status_code}")

    try:
        body = response.json()
        hits = body.get("hits", {}).get("hits", [])
        records = tuple(
            hit["_source"] for hit in hits
            if isinstance(hit, dict) and isinstance(hit.get("_source"), dict)
        )
    except (TypeError, ValueError, AttributeError) as exc:
        observe_external("datajud", "error", time.perf_counter() - started)
        raise DataJudError("the DataJud API returned an invalid response") from exc

    observe_external("datajud", "success", time.perf_counter() - started)
    return DataJudLookup(
        process_number=number,
        court_alias=alias,
        endpoint=endpoint,
        records=records,
        consulted_at=datetime.now(UTC),
    )


async def canary() -> None:
    """Check DataJud with a fixed synthetic process number and discard the result."""
    await lookup_process(CANARY_PROCESS_NUMBER)


def _named_values(items: Any) -> str:
    if not isinstance(items, list):
        return "not provided"
    names = [str(item.get("nome")) for item in items if isinstance(item, dict) and item.get("nome")]
    return ", ".join(names) or "not provided"


def format_lookup(lookup: DataJudLookup) -> str:
    """Render bounded DataJud records as reference material for the LLM."""
    lines = [
        "Official source: DataJud/CNJ Public API",
        f"Consulted at: {lookup.consulted_at.isoformat()}",
        f"Endpoint: {lookup.endpoint}",
    ]
    if not lookup.records:
        lines.append(f"No public record found for {lookup.process_number}.")
        return "\n".join(lines)

    for index, record in enumerate(lookup.records, start=1):
        case_class = record.get("classe") or {}
        adjudicating_body = record.get("orgaoJulgador") or {}
        lines.extend([
            "",
            f"Record {index}",
            f"Number: {record.get('numeroProcesso') or lookup.process_number}",
            f"Court/level: {record.get('tribunal', 'not provided')} / "
            f"{record.get('grau', 'not provided')}",
            f"Case class: {case_class.get('nome', 'not provided')}",
            f"Adjudicating body: {adjudicating_body.get('nome', 'not provided')}",
            f"Filing date: {record.get('dataAjuizamento', 'not provided')}",
            "Last update: "
            f"{record.get('dataHoraUltimaAtualizacao', 'not provided')}",
            f"Subjects: {_named_values(record.get('assuntos'))}",
            f"Reported secrecy level: {record.get('nivelSigilo', 'not provided')}",
            "Most recent movements:",
        ])
        movements = record.get("movimentos")
        if not isinstance(movements, list) or not movements:
            lines.append("- no public movement returned")
            continue
        recent = sorted(
            (movement for movement in movements if isinstance(movement, dict)),
            key=lambda movement: str(movement.get("dataHora", "")),
            reverse=True,
        )[:settings.datajud_max_movements]
        lines.extend(
            f"- {movement.get('dataHora', 'date not provided')}: "
            f"{movement.get('nome', 'movement without description')}"
            for movement in recent
        )

    lines.extend([
        "",
        "Source limitation: public DataJud provides metadata and movements; it "
        "does not necessarily provide the full text of decisions or filings.",
    ])
    return "\n".join(lines)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _date(value: Any) -> str:
    """dd/mm/aaaa, from the DataJud's ISO ("2025-03-18T10:12:00.000Z") or
    compact ("20250318101200") timestamps; anything else as it came."""
    text = str(value if value is not None else "-")
    # The whole value has to be one of the two shapes, and a real day: a
    # number that merely starts with eight digits is not a date.
    match = (re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T[\d:.]+Z?)?", text)
             or re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:\d{6})?", text))
    if not match:
        return text
    try:
        day = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return text
    return f"{day:%d/%m/%Y}"


def format_for_staff(lookup: DataJudLookup) -> str:
    """The process as a lawyer reads it, with where to open it for real."""
    from core.integrations.process_links import describe

    if not lookup.records:
        return (
            f"Não há registro público no DataJud para o processo "
            f"{lookup.process_number} (índice {lookup.court_alias.upper()}). "
            "Processos em segredo de justiça e os muito recentes não aparecem.\n\n"
            + describe(lookup.process_number)
        )

    lines: list[str] = []
    for record in lookup.records:
        case_class = record.get("classe") or {}
        body = record.get("orgaoJulgador") or {}
        lines.extend([
            f"⚖️ Processo {record.get('numeroProcesso') or lookup.process_number}",
            f"Tribunal/grau: {record.get('tribunal', '-')} / {record.get('grau', '-')}",
            f"Classe: {case_class.get('nome', '-')}",
            f"Órgão julgador: {body.get('nome', '-')}",
            f"Ajuizamento: {_date(record.get('dataAjuizamento', '-'))}",
            f"Última atualização: {_date(record.get('dataHoraUltimaAtualizacao', '-'))}",
            f"Assuntos: {_named_values(record.get('assuntos'))}",
            f"Sistema: {record.get('sistema', {}).get('nome', '-') if isinstance(record.get('sistema'), dict) else record.get('sistema', '-')}",
            f"Nível de sigilo informado: {record.get('nivelSigilo', '-')}",
            "",
            "Movimentações mais recentes:",
        ])
        movements = record.get("movimentos")
        if not isinstance(movements, list) or not movements:
            lines.append("- nenhuma movimentação pública retornada")
        else:
            recent = sorted(
                (movement for movement in movements if isinstance(movement, dict)),
                key=lambda movement: str(movement.get("dataHora", "")),
                reverse=True,
            )[: settings.datajud_max_movements]
            lines.extend(
                f"- {_date(movement.get('dataHora', '-'))}: "
                f"{movement.get('nome', 'movimento sem descrição')}"
                for movement in recent
            )
        lines.append("")

    lines.append(describe(lookup.process_number))
    lines.append("")
    lines.append(
        "O DataJud traz metadados e movimentações públicas, não o inteiro teor "
        "das peças. Para o conteúdo, abra o processo no sistema do tribunal."
    )
    return "\n".join(lines)
