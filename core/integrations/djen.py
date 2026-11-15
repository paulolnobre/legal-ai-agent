"""What was published about a process in the DJEN, the CNJ's national diary.

DataJud says what moved in a process. The DJEN carries what was published about
it: the intimation, and usually the full text of the decision or order behind
it. The courts publish there (the TJPA's intimations do), and the CNJ exposes it
through a public API that answers by process number with no key, measured on
2026-09-11.

Sending the number here is the same act as sending it to DataJud: the CNJ is the
authority for the case, not a third party learning what the firm is working on.
So unlike the diary search in dje.py, this needs no exception to the rule that
a process number never reaches a search engine.

A publication's date in the DJEN is the date it was made available. The legal
publication date is the first business day after it, and a deadline starts on
the first business day after that (Lei 11.419/2006, art. 4º, §§ 3º e 4º). The
rule is said wherever a date is shown, because counting from the availability
date is the kind of error that costs a deadline.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import httpx

from core.config import settings
from core.integrations import datajud
from core.metrics import observe_external

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

DATE_RULE = (
    "A data mostrada é a de disponibilização no diário. Considera-se publicada no "
    "primeiro dia útil seguinte, e o prazo começa no primeiro dia útil seguinte a "
    "essa publicação (Lei 11.419/2006, art. 4º, §§ 3º e 4º)."
)


# Where a person reads the same publications by process number, when the API
# does not answer this server.
PUBLIC_SITE = "https://comunica.pje.jus.br/"


class DjenError(RuntimeError):
    """A safe failure reading the DJEN. Never carries the process number."""


class DjenRefused(DjenError):
    """The DJEN answered, and refused.

    Its API sits behind a CloudFront distribution that refuses requests from
    outside Brazil: measured on 2026-09-11, HTTP 403 from the VPS in the United
    States and 200 from Brazil, over IPv4 and IPv6 alike. That is not an outage,
    and trying again later does not help, so it is told apart.
    """


@dataclass(frozen=True)
class Communication:
    """One publication about a process, as the DJEN describes it."""

    process_number: str
    court: str
    kind: str
    document_type: str
    body: str
    case_class: str
    made_available: date | None
    text: str
    link: str | None
    # The DJEN's own id, stable across queries: how a publication already
    # announced is told apart from a new one.
    publication_id: int | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(settings.djen_timeout_seconds, connect=10.0)
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


async def communications(process_number: str, *, limit: int | None = None) -> list[Communication]:
    """The publications about this process, newest first.

    A wider page is asked for than is returned: the API does not document its
    order, and the newest publication is the one a deadline runs from, so the
    choice of which to keep is made here. Only items the DJEN says are about
    this very number are kept; one about another case is dropped, not trusted.
    """
    digits = datajud.normalize_process_number(process_number)
    wanted = max(1, limit or settings.djen_max_items)
    url = f"{settings.djen_base_url.rstrip('/')}/api/v1/comunicacao"
    params = {
        "numeroProcesso": digits,
        "itensPorPagina": max(wanted, settings.djen_fetch_items),
        "pagina": 1,
    }
    started = time.perf_counter()
    try:
        response = await _http().get(url, params=params)
    except httpx.HTTPError as exc:
        observe_external("djen", "error", time.perf_counter() - started)
        raise DjenError("the DJEN did not respond") from exc
    if response.status_code in (401, 403):
        observe_external("djen", "error", time.perf_counter() - started)
        raise DjenRefused(f"the DJEN refused the request (HTTP {response.status_code})")
    if response.status_code != 200:
        observe_external("djen", "error", time.perf_counter() - started)
        raise DjenError(f"the DJEN returned HTTP {response.status_code}")
    try:
        body = response.json()
        items = body.get("items") or []
    except (ValueError, AttributeError) as exc:
        observe_external("djen", "error", time.perf_counter() - started)
        raise DjenError("the DJEN returned an invalid response") from exc

    found = [
        _communication(item)
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and _digits(item.get("numero_processo")) == digits
    ]
    found.sort(key=lambda c: c.made_available or date.min, reverse=True)
    observe_external("djen", "success", time.perf_counter() - started)
    logger.info("DJEN lookup returned %d publication(s)", len(found))
    return found[:wanted]


def _communication(item: dict) -> Communication:
    digits = _digits(item.get("numero_processo"))
    try:
        fallback = datajud.format_process_number(digits)
    except ValueError:
        fallback = digits
    raw_id = _digits(item.get("id"))
    publication_id = int(raw_id) if raw_id else None
    return Communication(
        process_number=_clean(item.get("numeroprocessocommascara")) or fallback,
        court=_clean(item.get("siglaTribunal")),
        kind=_clean(item.get("tipoComunicacao")),
        document_type=_clean(item.get("tipoDocumento")),
        body=_clean(item.get("nomeOrgao")),
        case_class=_clean(item.get("nomeClasse")),
        made_available=_date(
            item.get("data_disponibilizacao") or item.get("datadisponibilizacao")
        ),
        text=str(item.get("texto") or "").strip(),
        link=_clean(item.get("link")) or None,
        publication_id=publication_id,
    )


def _addressed_to(item: dict, oab_number: str, oab_uf: str) -> bool:
    """Whether the DJEN lists this lawyer among the ones the item notifies."""
    for entry in item.get("destinatarioadvogados") or []:
        if not isinstance(entry, dict):
            continue
        lawyer = entry.get("advogado") or {}
        if (_digits(lawyer.get("numero_oab")) == oab_number
                and _clean(lawyer.get("uf_oab")).upper() == oab_uf):
            return True
    return False


async def communications_for_lawyer(oab_number: str, oab_uf: str, *, since: date,
                                    until: date) -> list[Communication]:
    """Every publication made available in the window that notifies this lawyer.

    One query per page, across every court that publishes in the DJEN, which
    is what makes following a lawyer worth more than following a list of
    processes: a case nobody remembered to register is still in it. Pages are
    read until one comes back short, up to `djen_watch_max_pages`; past that
    the rest is logged, never silently assumed empty.

    An item is kept only when the DJEN itself names this OAB among the lawyers
    it notifies, the same distrust `communications` applies to the process
    number.
    """
    number, uf = _digits(oab_number), oab_uf.strip().upper()
    url = f"{settings.djen_base_url.rstrip('/')}/api/v1/comunicacao"
    page_size = 100
    found: list[Communication] = []
    for page in range(1, settings.djen_watch_max_pages + 1):
        params = {
            "numeroOab": number,
            "ufOab": uf,
            "dataDisponibilizacaoInicio": since.isoformat(),
            "dataDisponibilizacaoFim": until.isoformat(),
            "itensPorPagina": page_size,
            "pagina": page,
        }
        started = time.perf_counter()
        try:
            response = await _http().get(url, params=params)
        except httpx.HTTPError as exc:
            observe_external("djen", "error", time.perf_counter() - started)
            raise DjenError("the DJEN did not respond") from exc
        if response.status_code in (401, 403):
            observe_external("djen", "error", time.perf_counter() - started)
            raise DjenRefused(f"the DJEN refused the request (HTTP {response.status_code})")
        if response.status_code != 200:
            observe_external("djen", "error", time.perf_counter() - started)
            raise DjenError(f"the DJEN returned HTTP {response.status_code}")
        try:
            items = response.json().get("items") or []
        except (ValueError, AttributeError) as exc:
            observe_external("djen", "error", time.perf_counter() - started)
            raise DjenError("the DJEN returned an invalid response") from exc
        observe_external("djen", "success", time.perf_counter() - started)
        if not isinstance(items, list):
            break
        found += [
            _communication(item) for item in items
            if isinstance(item, dict) and _addressed_to(item, number, uf)
        ]
        if len(items) < page_size:
            break
    else:
        logger.warning(
            "DJEN lawyer query stopped at %d pages; later publications were not read",
            settings.djen_watch_max_pages,
        )
    found.sort(key=lambda c: c.made_available or date.min, reverse=True)
    return found


def excerpt(c: Communication, size: int = 280) -> str:
    """The part of a publication worth a glance; see `_excerpt`."""
    return _excerpt(c, size)


def _excerpt(c: Communication, size: int = 280) -> str:
    """The part of the text worth a glance, not the court's letterhead.

    Publications open with the court's name, address and phone numbers. The
    document's own heading ("SENTENÇA", "DECISÃO") is where it starts saying
    something, so the excerpt starts there when it can be found.
    """
    text = _clean(c.text)
    start = text.upper().find(c.document_type.upper()) if c.document_type else -1
    snippet = text[start:] if start > 0 else text
    return snippet[:size] + ("..." if len(snippet) > size else "")


def format_for_staff(process_number: str, comms: list[Communication]) -> str:
    """The publications as a lawyer reads them in /processo."""
    number = datajud.format_process_number(process_number)
    if not comms:
        return f"📰 DJEN (CNJ): nenhuma publicação encontrada para o processo {number}."
    lines = ["📰 Publicações no DJEN (CNJ), mais recentes primeiro:"]
    for c in comms:
        when = f"{c.made_available:%d/%m/%Y}" if c.made_available else "data não informada"
        what = ", ".join(part for part in (c.kind, c.document_type) if part)
        lines.append(f"• {when}: {what}")
        if c.body:
            lines.append(f"  {c.body}")
        excerpt = _excerpt(c)
        if excerpt:
            lines.append(f"  {excerpt}")
        if c.link:
            lines.append(f"  {c.link}")
    lines.extend(["", DATE_RULE])
    return "\n".join(lines)


def format_for_model(c: Communication) -> str:
    """One publication for the model: what it is, then the text as published."""
    cap = max(500, settings.djen_max_chars)
    when = (
        f"Disponibilizada no DJEN em {c.made_available:%d/%m/%Y}"
        if c.made_available else "Data de disponibilização não informada"
    )
    lines = [
        f"Processo: {c.process_number}",
        f"Tribunal: {c.court}",
        f"Órgão: {c.body}",
        f"Classe: {c.case_class}",
        f"Comunicação: {c.kind}; documento: {c.document_type}",
        when,
    ]
    if c.link:
        lines.append(f"Documento no sistema do tribunal: {c.link}")
    lines.extend(["", "Texto publicado:", c.text[:cap]])
    if len(c.text) > cap:
        lines.append("[Texto truncado no limite configurado.]")
    return "\n".join(lines)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = [
    "DATE_RULE", "PUBLIC_SITE", "Communication", "DjenError", "DjenRefused", "close",
    "communications", "communications_for_lawyer", "excerpt", "format_for_model",
    "format_for_staff",
]
