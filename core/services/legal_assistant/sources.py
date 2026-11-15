"""What an answer was built on — collected by the agents, shown on request.

Every answer that touches the corpus or a public source carries its sources
here. The agents only collect them; the handler decides what to show, because
dumping a bibliography under every reply buries the answer. The default is a
one-line attribution plus an offer, and the full list — each entry explaining
what is in that document or at that link — is sent when the lawyer asks.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SourceKind = Literal["corpus", "web", "datajud", "djen"]

_DETAIL_MAX = 300

_KIND_LABEL = {
    "corpus": "acervo do escritório",
    "web": "fonte pública oficial",
    "datajud": "DataJud/CNJ",
    "djen": "DJEN/CNJ",
}


class Source(BaseModel):
    """One document or page an answer was built on."""

    kind: SourceKind
    title: str
    reference: str = ""  # document id in the corpus, or the URL of the page
    detail: str = ""     # what is in it — the passage used, or the page's content

    def line(self) -> str:
        head = f"{self.title}"
        if self.reference:
            head += f": {self.reference}"
        detail = " ".join(self.detail.split())[:_DETAIL_MAX]
        return f"{head}\n  {detail}" if detail else head


def dedupe(sources: list[Source]) -> list[Source]:
    """Keep the first occurrence of each document or URL, order preserved."""
    seen: set[tuple[str, str]] = set()
    unique: list[Source] = []
    for source in sources:
        key = (source.kind, source.reference or source.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(source)
    return unique


_KIND_COUNT = {
    "djen": ("1 publicação do DJEN/CNJ", "{n} publicações do DJEN/CNJ"),
    "corpus": ("1 documento do acervo", "{n} documentos do acervo"),
    "web": ("1 fonte pública oficial", "{n} fontes públicas oficiais"),
    "datajud": ("1 processo consultado no DataJud/CNJ",
                "{n} processos consultados no DataJud/CNJ"),
}


def summary(sources: list[Source]) -> str:
    """"2 documentos do acervo e 1 fonte pública oficial" — what backs the answer."""
    counts: dict[str, int] = {}
    for source in dedupe(sources):
        counts[source.kind] = counts.get(source.kind, 0) + 1

    parts = [
        (_KIND_COUNT[kind][0] if total == 1 else _KIND_COUNT[kind][1].format(n=total))
        for kind in ("corpus", "web", "datajud", "djen")
        if (total := counts.get(kind, 0))
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " e " + parts[-1]


def offer(sources: list[Source]) -> str:
    """The line appended to an answer: what it rests on, and the offer to detail."""
    described = summary(sources)
    if not described:
        return ""
    return (
        f"\n\nBaseei esta resposta em {described}. "
        "Quer que eu liste as fontes, com o que há em cada uma?"
    )


def listing(sources: list[Source]) -> str:
    """The full bibliography, each entry explaining what is in it."""
    unique = dedupe(sources)
    if not unique:
        return "Esta resposta não teve fontes registradas."
    lines = [f"[{n}] {source.line()}" for n, source in enumerate(unique, start=1)]
    return "Fontes desta resposta:\n" + "\n".join(lines)


# ── Builders, so every agent describes a source the same way ─────────────────

def from_corpus_hit(hit) -> Source:
    """A retrieved passage: the lawyer needs the document and the excerpt used."""
    reference = f"documento [{hit.document_id}] do acervo"
    if getattr(hit, "matter", None):
        reference += f", caso {hit.matter}"
    return Source(
        kind="corpus",
        title=hit.title + (f" ({hit.doc_type})" if getattr(hit, "doc_type", None) else ""),
        reference=reference,
        detail=f"Trecho consultado: “{(hit.content or '').strip()}”",
    )


def from_document(document_id, title, doc_type=None, detail="") -> Source:
    """A whole stored document, as used by a summary or a revision."""
    return Source(
        kind="corpus",
        title=title + (f" ({doc_type})" if doc_type else ""),
        reference=f"documento [{document_id}] do acervo",
        detail=detail or "Documento lido integralmente para produzir esta resposta.",
    )


def from_web_result(result) -> Source:
    """A vetted official page: the URL plus what the search index shows of it."""
    detail = " ".join((result.snippet or "").split())
    if getattr(result, "date", None):
        detail = f"{detail} (data indicada pela fonte: {result.date})".strip()
    return Source(
        kind="web",
        title=result.title,
        reference=result.url,
        detail=detail or "Página oficial retornada pela busca restrita.",
    )


def from_datajud(number, endpoint, detail="") -> Source:
    return Source(
        kind="datajud",
        title=f"Processo {number}",
        reference=endpoint,
        detail=detail or (
            "Metadados e movimentações públicas do processo, conforme a API "
            "Pública do DataJud/CNJ. Não é o inteiro teor da decisão."
        ),
    )


def from_djen(communication) -> Source:
    """A publication read from the CNJ's national diary.

    The reference is the document in the court's own system when the DJEN
    gives one, which is where a lawyer opens the full piece.
    """
    when = (
        f"{communication.made_available:%d/%m/%Y}"
        if communication.made_available else "data não informada"
    )
    what = ", ".join(p for p in (communication.kind, communication.document_type) if p)
    return Source(
        kind="djen",
        title=f"{communication.court} {what} de {when}, processo {communication.process_number}",
        # Not every publication carries a link, and the field is text.
        reference=communication.link or "",
        detail=(
            f"Publicação no DJEN/CNJ, disponibilizada em {when}"
            + (f", {communication.body}" if communication.body else "")
            + ". A publicação considera-se feita no primeiro dia útil seguinte."
        ),
    )

