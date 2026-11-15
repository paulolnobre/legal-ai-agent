"""Read what a lawyer cites from the acórdão the pipeline opened.

The external research already fetches an inteiro teor and hands its text to the
model. That is enough to answer a question and not enough to cite one: a
citation needs the process number, the relator and the date the case was judged,
and those are exactly the fields a language model will supply from context when
they are not in front of it. A relator invented from a plausible pattern reads
like a citation and is a fabrication a lawyer would sign.

So they are read here, deterministically, and passed to the model as fields that
were verified. Anything not found stays `None`. A field this module guesses is
worse than a field it leaves empty, because the empty one is visibly missing.

The ementa is read the same way, for a stronger reason. A lawyer asked for the
court's decision to be transcribed, and a transcription a model writes from
memory is a paraphrase that looks like a quotation. So it is cut out of the
document between its own heading and the next section, with the page footers a
PDF inserts mid-text removed, and handed over as the only text the answer may
put in quotes. The court is read too, from the CNJ number first, then from the
name in the header, then from the page's address.

Two mistakes this is shaped to avoid:

Only the header is searched. An acórdão quotes precedents, each with its own
relator and its own date, and those quotations sit in the body. Scanning the
whole document finds the last relator mentioned rather than the one who wrote
it, and nothing about the result looks wrong.

The judgment date and the publication date are different fields and are kept
apart. "DJe 15/03/2024" is when the decision was published in the official
gazette; "Brasília, 12 de março de 2024" is when it was judged. Reporting the
first as the second is the kind of error the other side notices.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from core.integrations import datajud

# How much of the document counts as the header. Measured against real inteiro
# teor PDFs from the STJ and the STF: the block carrying the class, the number,
# the relator and the parties runs to roughly a page, and the ementa starts
# after it. Generous enough for a long autuação, short enough that the first
# quoted precedent does not fit.
_HEADER_CHARS = 4000

_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

# "RECURSO ESPECIAL Nº 1.234.567 - SP", "RE 123.456", "AgInt no AREsp 987.654".
# The class matters: a bare number cites nothing, and the number alone is not
# unique across classes.
_CLASS_NUMBER = re.compile(
    r"\b((?:AgInt|AgRg|EDcl|EMBARGOS DE DECLARA\w+|"
    r"REsp|RECURSO ESPECIAL|RE|RECURSO EXTRAORDIN\w+|"
    r"AREsp|AGRAVO EM RECURSO ESPECIAL|HC|HABEAS CORPUS|"
    r"MS|MANDADO DE SEGURAN\w+|ADI|ADPF|RMS|CC|IAC|IRDR)"
    r"(?:\s+n?o?s?\.?º?)?\s+[\d.]{3,})"
    # The originating state is part of the citation: "REsp 1.234.567/SP" is
    # what gets written down, and the number alone is ambiguous across states.
    r"(?:\s*[-/]\s*([A-Z]{2})\b)?",
    re.IGNORECASE,
)

# "RELATOR : MINISTRO FULANO", "Relatora: Min. Beltrana", "Relator(a): ...".
# The colon or the run of spaces is what separates the label from the name in
# the fixed-width headers these documents use.
_RELATOR = re.compile(
    r"\bRELATOR(?:A)?(?:\(A\))?\s*:?\s+([^\n\r]{3,80})",
    re.IGNORECASE,
)

# A name ends where the next labelled field begins. These are the labels that
# follow the relator in a real header.
_NEXT_LABEL = re.compile(
    r"\b(RECORRENTE|RECORRIDO|AGRAVANTE|AGRAVADO|IMPETRANTE|IMPETRADO|"
    r"REQUERENTE|REQUERIDO|EMBARGANTE|EMBARGADO|PACIENTE|ADVOGAD|"
    r"PROCURADOR|INTERESSAD|ORIGEM|EMENTA|RELATOR)",
    re.IGNORECASE,
)

_JUDGED = re.compile(
    # "Sessão Ordinária realizada em 13 de março de 2018" is how a TJPA acórdão
    # of the older layout states its own session; measured on a real one, which
    # the other alternatives missed.
    r"(?:sess[aã]o\s+(?:ordin[aá]ria\s+|extraordin[aá]ria\s+|virtual\s+)?realizada\s+em|"
    r"julgad[oa]\s+em|data\s+do\s+julgamento|julgamento\s*:|"
    r"em\s+sess[aã]o\s+de|Bras[ií]lia[^\n,]{0,20},)\s*"
    r"([0-3]?\d[\s/.-]+(?:de\s+)?[\wç]+[\s/.-]+(?:de\s+)?\d{4})",
    re.IGNORECASE,
)

_PUBLISHED = re.compile(
    r"\b(?:DJe|DJ|D\.J\.e\.|Data\s+da\s+Publica[çc][aã]o)[\s:]*"
    r"([0-3]?\d[\s/.-]+(?:de\s+)?[\wç]+[\s/.-]+(?:de\s+)?\d{4})",
    re.IGNORECASE,
)


# How far into the document the ementa may start. It follows the autuação,
# which in the PJe layout carries a table of parties and documents first:
# measured at about 1,400 characters on a real TJPA acórdão, and earlier on the
# older layout. An EMENTA heading deep in the body belongs to a quoted precedent.
_EMENTA_WINDOW = 8000
_EMENTA_MAX_CHARS = 3000

# The ementa's own heading, in capitals at the start of a line. The PJe layout
# lists "Ementa Ementa" in its table of documents above the real one, so neither
# the first occurrence of the word nor a case-insensitive match will do.
_EMENTA_START = re.compile(r"(?m)^[ \t]*EMENTA\b[ \t]*[:.\-–]?[ \t]*")

# Where it ends: the next section of the acórdão. The older TJPA layout prints
# "A  C  Ó  R  D  Ã  O" with its letters spaced out.
_EMENTA_END = re.compile(
    r"(?m)^[ \t]*(?:A\s*C\s*Ó\s*R\s*D\s*Ã\s*O|ACÓRDÃO|RELATÓRIO|VOTO|VISTOS|Vistos|"
    r"ACORDAM|Acordam|CERTIDÃO)\b"
)

# Page furniture a PDF inserts in the middle of the text when the ementa crosses
# a page: the footer (page stamp, signature line, document number and its link)
# and the running header the next page opens with. Measured on a real TJPA
# acórdão, whose ementa came out with "TRIBUNAL DE JUSTIÇA DO ESTADO DO PARÁ"
# between its fourth and fifth items. Transcribed literally, either would put
# words in the court's mouth.
#
# The header is matched in capitals only, which is how the page prints it. A
# mixed-case line naming a court inside an ementa is the ementa's own sentence
# broken where the PDF broke it, and dropping it would change the quotation.
_PAGE_FOOTER = re.compile(
    r"^(?:(?i:Num\.\s*\d+\s*-\s*Pág\.\s*\d+|Assinado eletronicamente por:.*|"
    r"Número do documento:.*|https?://\S+|\S*listView\.seam\?x=\S*|"
    r"Página\s+\d+\s+de\s+\d+)|\d+|"
    r"PODER JUDICIÁRIO|TRIBUNAL DE JUSTIÇA DO ESTADO D[OAE] [A-ZÁÂÃÉÊÍÓÔÕÚÇ ]+)$"
)

# State names as a TJ writes them in its header, folded, longest first so that
# "mato grosso do sul" is not read as "mato grosso".
_STATES = sorted({
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
    "mato grosso do sul": "MS", "mato grosso": "MT", "minas gerais": "MG",
    "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE",
    "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
    "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
}.items(), key=lambda item: -len(item[0]))

_COURT_HOST = re.compile(
    r"(?:^|\.)(stj|stf|tst|tse|stm|tjdft|tj[a-z]{2}|trf\d|trt\d{1,2})\.jus\.br$"
)
_COURT_TOKEN = re.compile(r"\b(TJDFT|TJ[A-Z]{2}|TRF\d|TRT\d{1,2})\b")


@dataclass(frozen=True)
class AcordaoMetadata:
    """What was actually read out of the header. Never what was inferred."""

    process_number: str | None = None
    relator: str | None = None
    judged_on: date | None = None
    published_on: date | None = None
    court: str | None = None
    ementa: str | None = None
    ementa_truncated: bool = False

    def is_empty(self) -> bool:
        """Whether nothing marks this page as a decision.

        The court and the ementa do not count. A court read from the address
        would make every page of a court's portal a decision, and a bill on a
        legislative site carries an EMENTA heading too.
        """
        return not any(
            (self.process_number, self.relator, self.judged_on, self.published_on)
        )

    def citation(self) -> str:
        """The fields that identify the decision, without its ementa."""
        lines = []
        if self.court:
            lines.append(f"Tribunal: {self.court}")
        if self.process_number:
            lines.append(f"Processo: {self.process_number}")
        if self.relator:
            lines.append(f"Relator(a): {self.relator}")
        if self.judged_on:
            lines.append(f"Data do julgamento: {self.judged_on:%d/%m/%Y}")
        if self.published_on:
            lines.append(f"Data da publicação (DJe): {self.published_on:%d/%m/%Y}")
        return "\n".join(lines)

    def describe(self) -> str:
        """The fields in words, for the block the model reads.

        Each line says the field was read from the document, because the whole
        purpose of this module is that the model treats these as given rather
        than as something to complete.
        """
        lines = [self.citation()] if self.citation() else []
        if self.ementa:
            lines.append("Ementa (transcrição literal do documento):")
            lines.append(self.ementa)
            if self.ementa_truncated:
                lines.append(
                    "[Ementa possivelmente incompleta: o fim dela não foi localizado "
                    "no texto lido.]"
                )
        return "\n".join(lines)


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _parse_date(raw: str) -> date | None:
    """Read "12/03/2024" and "12 de março de 2024", and nothing invented."""
    cleaned = _fold(raw).replace(" de ", " ").strip()
    parts = [p for p in re.split(r"[\s/.-]+", cleaned) if p]
    if len(parts) != 3:
        return None
    day, month, year = parts
    if not day.isdigit() or not year.isdigit():
        return None
    number = int(month) if month.isdigit() else _MONTHS.get(month)
    if not number:
        return None
    try:
        return date(int(year), number, int(day))
    except ValueError:
        # A malformed header must not become a plausible wrong date.
        return None


def _relator_in(header: str) -> str | None:
    match = _RELATOR.search(header)
    if not match:
        return None
    candidate = match.group(1).strip()
    # The fixed-width headers put the next field on the same line often enough
    # that the name has to be cut where the next label starts.
    boundary = _NEXT_LABEL.search(candidate)
    if boundary:
        candidate = candidate[: boundary.start()]
    candidate = candidate.strip(" \t:-–.,;")
    # Two or more runs of spaces are column separators, not part of a name.
    candidate = re.sub(r"\s{2,}.*$", "", candidate).strip()
    return candidate or None


def _process_number_in(header: str) -> str | None:
    """A CNJ number when the document carries one, else the class and number.

    CNJ first because it is unique and machine-checkable; the class form is what
    the STJ and the STF actually print at the top, and a citation without the
    class ("1.234.567") identifies nothing.
    """
    cnj = datajud.extract_process_numbers(header, limit=1)
    if cnj:
        # Punctuated, not the bare digits: this goes into a citation a person
        # has to be able to check against a court's own search form.
        return datajud.format_process_number(cnj[0])
    match = _CLASS_NUMBER.search(header)
    if match:
        number = " ".join(match.group(1).split())
        state = match.group(2)
        return f"{number}/{state.upper()}" if state else number
    return None


def _court_in(header: str, url: str | None) -> str | None:
    """The court, from the most reliable evidence the document offers.

    A CNJ number encodes its court and cannot be misread. A name written out in
    the header comes next, then a sigla printed there, and the page's address
    last, because a court's portal also hosts other courts' decisions it quotes.
    """
    numbers = datajud.extract_process_numbers(header, limit=1)
    if numbers:
        try:
            return datajud.court_alias(numbers[0]).upper()
        except ValueError:
            pass
    folded = _fold(header)
    if "superior tribunal de justica" in folded:
        return "STJ"
    if "supremo tribunal federal" in folded:
        return "STF"
    if "tribunal de justica do distrito federal" in folded:
        return "TJDFT"
    marker = re.search(r"tribunal de justica do estado d[aeo]\s+", folded)
    if marker:
        after = folded[marker.end():]
        for name, uf in _STATES:
            if re.match(rf"{name}(?![a-z])", after):
                return f"TJ{uf}"
    federal = re.search(r"tribunal regional federal da (\d)a regiao", folded)
    if federal:
        return f"TRF{federal.group(1)}"
    token = _COURT_TOKEN.search(header)
    if token:
        return token.group(1)
    if url:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        match = _COURT_HOST.search(host)
        if match:
            return match.group(1).upper()
    return None


def _ementa_in(text: str) -> tuple[str | None, bool]:
    """The ementa as printed, and whether its end was actually found.

    Lines are joined with single spaces, because a PDF breaks them wherever the
    page did; the words and their order are the document's own.
    """
    start = _EMENTA_START.search(text[:_EMENTA_WINDOW])
    if not start:
        return None, False
    rest = text[start.end():]
    end = _EMENTA_END.search(rest)
    raw = rest[: end.start()] if end else rest
    kept = [
        line.strip() for line in raw.splitlines()
        if line.strip() and not _PAGE_FOOTER.match(line.strip())
    ]
    ementa = " ".join(" ".join(kept).split())
    if not ementa:
        return None, False
    truncated = end is None or len(ementa) > _EMENTA_MAX_CHARS
    return ementa[:_EMENTA_MAX_CHARS].rstrip(), truncated


def extract(
    text: str, *, url: str | None = None, header_chars: int = _HEADER_CHARS,
) -> AcordaoMetadata:
    """Read the citable fields from the head of a decision.

    `header_chars` is an argument so a caller with a short document does not
    have to pad it, and so the boundary is testable rather than implicit.
    """
    if not text:
        return AcordaoMetadata()
    header = text[:header_chars]

    judged = _JUDGED.search(header)
    published = _PUBLISHED.search(header)
    ementa, truncated = _ementa_in(text)
    return AcordaoMetadata(
        process_number=_process_number_in(header),
        relator=_relator_in(header),
        judged_on=_parse_date(judged.group(1)) if judged else None,
        published_on=_parse_date(published.group(1)) if published else None,
        court=_court_in(header, url),
        ementa=ementa,
        ementa_truncated=truncated,
    )


__all__ = ["AcordaoMetadata", "extract"]
