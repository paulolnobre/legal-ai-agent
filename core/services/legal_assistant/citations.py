"""Checking, without a model, that what an answer cites is in what it read.

A fabricated process number or a súmula that does not exist is the one failure
in this system a lawyer cannot catch by reading the answer: it looks exactly
like a real citation, and it is copied into a petition. Asking the model
whether it invented something does not help, because the same process that
produced the invention produces the reassurance. So this is arithmetic on
strings, and nothing here calls a model.

**The evidence is the text the model actually saw.** Not the sources list
shown to the lawyer, whose `detail` is an excerpt trimmed for display: a
citation present in the page body but absent from a 300-character snippet
would be reported as invented. Each agent records the blocks it put in front
of the model, and this reads those.

**One extractor, run on both sides.** The answer's citations and the
evidence's citations come out of `find`, and the comparison is between the
keys it produces. A second, differently written matcher for the evidence side
is how a checker starts reporting formatting differences as fabrications:
`art. 5º` in the answer against `Art. 5o` in the source. Both sides collapse
to one key, so that difference cannot exist.

**A guard that cries wolf is worse than no guard**, because the firm learns to
ignore it. Every ambiguity here therefore resolves toward not accusing: an
answer that names a law the source did not name is supported by the article
number alone, and a citation form this module cannot parse confidently is not
extracted rather than extracted and doubted.

Two things are deliberately not checked. That the cited thing says what the
answer claims it says is legal judgment, and no regular expression has it.
That an article of the right number belongs to the right law is only caught
when both sides name the law the same way, for the reason above. What this
does answer, and the firm can rely on, is whether the thing cited appears in
the material at all.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from core.integrations.datajud import has_valid_check_digits, normalize_process_number

# A CNJ number, written in full or as twenty bare digits: a lawyer writes it
# either way and so does a source, and both collapse to the same key.
_PROCESS = re.compile(r"\b\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}\b")

# "Súmula 331", "Súmula Vinculante n 13", "sumula n. 7 do STJ". Note `n[o.]?`
# rather than `nº`: folding has already turned the ordinal indicator into "o".
_SUMULA = re.compile(
    r"\bsumula\s+(vinculante\s+)?(?:n[o.]?\s*)?(\d{1,4})\b"
    r"(?:[^.;\n]{0,40}?\b(stf|stj|tst|tse|stm|cnj)\b)?"
)

# "art. 5o", "artigo 1.228", "art. 20-a". The law it belongs to is looked for
# separately, in the text that follows, because folding it into one expression
# makes the law group optional and lazy, and an optional lazy group at the end
# matches empty every time. That was a real bug in the first version here.
_ARTIGO = re.compile(
    r"\bart(?:igo)?s?\.?\s*(\d{1,4}(?:\.\d{3})?)\s*(?:[o°](?![a-z]))?(?:-([a-z]))?"
)

# What a law is called, in the window right after an article citation.
_LAW_AFTER = re.compile(
    r"^[^.;\n]{0,50}?\b(?:"
    r"(?P<named>lei|decreto|mp|medida provisoria)\b[^.;\n]{0,18}?"
    r"(?P<number>\d{1,3}(?:\.\d{3})*)"
    r"|(?P<charter>constituicao|cf|carta magna)\b"
    r"|(?P<code>codigo\s+[a-z]+|cc|cpc|cpp|clt|ctn|cdc|ctb|ca)\b"
    r")"
)

# Abbreviations a source and an answer use interchangeably, each mapped to the
# key both collapse to and to the name the firm reads. Anything not here keeps
# its own spelling, which at worst costs the law half of a key and falls back
# to matching on the article number alone. The preposition belongs to the
# display form because it changes with the name ("do Código Civil" but "da
# CLT"), and a guard that writes bad Portuguese is one the firm trusts less.
_CODES = {
    "cc": ("codigocivil", "do Código Civil"),
    "codigo civil": ("codigocivil", "do Código Civil"),
    "cpc": ("codigoprocessocivil", "do Código de Processo Civil"),
    "codigo de processo civil": ("codigoprocessocivil", "do Código de Processo Civil"),
    "cpp": ("codigoprocessopenal", "do Código de Processo Penal"),
    "codigo de processo penal": ("codigoprocessopenal", "do Código de Processo Penal"),
    "clt": ("clt", "da CLT"),
    "ctn": ("codigotributario", "do Código Tributário Nacional"),
    "codigo tributario": ("codigotributario", "do Código Tributário Nacional"),
    "cdc": ("codigoconsumidor", "do Código de Defesa do Consumidor"),
    "ctb": ("codigotransito", "do Código de Trânsito Brasileiro"),
    "codigo penal": ("codigopenal", "do Código Penal"),
}


@dataclass(frozen=True)
class Citation:
    """One thing an answer cites, and the key two spellings of it share."""

    kind: str     # "processo", "súmula", "súmula vinculante" or "artigo"
    raw: str      # rebuilt for a person to read, not the matched span
    key: str      # normalised, for the comparison

    def described(self) -> str:
        """How this reads in the sentence the firm gets."""
        return self.raw


def _fold(text: str) -> str:
    """Lowercase, unaccented, single-spaced: the form both sides collapse to."""
    stripped = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in stripped if not unicodedata.combining(c))
    return " ".join(without_accents.lower().split())


def _law_after(folded: str, start: int) -> tuple[str, str]:
    """The law an article citation names next, as (key part, readable part).

    Empty when the citation names none, which is common and not a defect: the
    article number alone is then what gets compared.
    """
    match = _LAW_AFTER.match(folded[start:start + 70])
    if not match:
        return "", ""
    if match.group("named"):
        number = match.group("number").replace(".", "")
        kind = "lei" if match.group("named") == "lei" else match.group("named")
        return f"{kind}{number}", f" da {kind.capitalize()} {match.group('number')}"
    if match.group("charter"):
        return "cf", " da Constituição"
    code = " ".join(match.group("code").split())
    key, display = _CODES.get(code, (code.replace(" ", ""), f"do {code.title()}"))
    return key, f" {display}"


def find(text: str) -> list[Citation]:
    """Every process number, súmula and article cited in this text.

    Order is preserved and repeats are kept, so a caller that wants the first
    occurrence of something can have it.
    """
    if not text:
        return []
    folded = _fold(text)
    found: list[Citation] = []

    for match in _PROCESS.finditer(folded):
        digits = normalize_process_number(match.group(0))
        if len(digits) == 20:
            found.append(Citation("processo", f"processo {match.group(0)}", digits))

    for match in _SUMULA.finditer(folded):
        binding, number, court = match.group(1), match.group(2), match.group(3)
        kind = "súmula vinculante" if binding else "súmula"
        readable = f"Súmula Vinculante {int(number)}" if binding else f"Súmula {int(number)}"
        if court:
            readable += f" do {court.upper()}"
        # The court joins the key only when the text names it: a source saying
        # "Súmula 331" and an answer saying "Súmula 331 do TST" would otherwise
        # never meet, and the more specific side would be called a fabrication.
        found.append(Citation(kind, readable, f"{kind}:{int(number)}:{court or ''}"))

    for match in _ARTIGO.finditer(folded):
        number, suffix = match.group(1), match.group(2)
        article = number.replace(".", "") + (f"-{suffix.upper()}" if suffix else "")
        law_key, law_text = _law_after(folded, match.end())
        # Brazilian legal writing ordinals the first nine articles and no more,
        # so the message reads back the way a lawyer would have written it.
        shown = f"{number}º" if number.isdigit() and int(number) <= 9 else number
        found.append(Citation(
            "artigo", f"art. {shown}{f'-{suffix.upper()}' if suffix else ''}{law_text}",
            f"artigo:{article}:{law_key}",
        ))

    return found


def keys(text: str) -> set[str]:
    """The comparable form of everything cited in this text."""
    return {citation.key for citation in find(text)}


def _supports(cited: Citation, available: set[str]) -> bool:
    """Whether the evidence carries this citation.

    A citation that names its law or its court is also satisfied by evidence
    naming the same thing without that qualifier: the answer being more
    specific than the source it read is not an invention. The reverse does not
    hold, and it is the direction that keeps this from accusing wrongly.
    """
    if cited.key in available:
        return True
    head, _, qualifier = cited.key.rpartition(":")
    return bool(qualifier) and f"{head}:" in available


def unsupported(answer: str, evidence: list[str]) -> list[Citation]:
    """What the answer cites that the material it read does not contain.

    Deduplicated by key, so one invented number cited three times is reported
    once instead of shouting three times about the same thing.
    """
    available: set[str] = set()
    for block in evidence:
        available |= keys(block)

    missing: list[Citation] = []
    seen: set[str] = set()
    for citation in find(answer):
        if citation.key in seen or _supports(citation, available):
            continue
        seen.add(citation.key)
        missing.append(citation)
    return missing


def malformed_process_numbers(text: str) -> list[Citation]:
    """Cited process numbers whose CNJ check digits do not match.

    Independent of the evidence, and worth its own answer: a number that fails
    Resolução 65/2008 is wrong even when it does appear in the source, and an
    invented one almost always fails it.
    """
    seen: set[str] = set()
    bad: list[Citation] = []
    for citation in find(text):
        if citation.kind != "processo" or citation.key in seen:
            continue
        seen.add(citation.key)
        if not has_valid_check_digits(citation.key):
            bad.append(citation)
    return bad


__all__ = [
    "Citation", "find", "keys", "malformed_process_numbers", "unsupported",
]
