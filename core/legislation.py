"""What counts as legislation: documents of common use that belong to no client.

Laws, codes, decrees, súmulas and constitutions serve the whole office, in the
firm's folder and in a personal one alike, so the explorer files them by kind
under "Legislação" rather than under a client. Decided with the firm's owner
on 2026-10-13: one subfolder per kind, and the sphere (federal, estadual,
municipal) as a label on the card, not another level of folders.

A document's kind is its free-text `doc_type`, so it is matched loosely and
the same way everywhere: case and accents folded, spaces collapsed, and a kind
counts when the type is that word or starts with it ("lei complementar",
"decreto-lei", "súmula vinculante"). The database, the pages and the browser
script all read this one module, so the three cannot disagree.
"""
from __future__ import annotations

import re
import unicodedata

# (address word, the name on the tile, what a folded type starts with)
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("constituicao", "Constituição", ("constituicao",)),
    ("leis", "Leis", ("lei",)),
    ("codigos", "Códigos", ("codigo",)),
    ("decretos", "Decretos e MPs", ("decreto", "medida provisoria")),
    ("sumulas", "Súmulas", ("sumula",)),
)
GROUP_NAMES = {key: name for key, name, _starts in GROUPS}
PREFIXES: tuple[str, ...] = tuple(p for _key, _name, starts in GROUPS for p in starts)

SPHERES = ("federal", "estadual", "municipal")

# Offered when a type is typed, so the usual names are one pick away.
TYPE_SUGGESTIONS = ("constituição", "lei", "lei complementar", "código", "decreto",
                    "decreto-lei", "medida provisória", "súmula", "súmula vinculante")

# The one definition of folding, spelled the same in three languages: Python
# here, SQL in `group_sql`, JavaScript in static/explorer.js (foldType). The
# text is decomposed (NFD), these combining marks are removed, it is
# lowercased, these spaces become one plain space, and plain spaces at the
# ends go. Explicit ranges rather than each language's idea of "accent" or
# "space", because those ideas differ, and a type the database files under
# "Códigos" must be one the page and the script call a code too.
MARKS = "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
SPACES = " \t\n\r\f\v\u00a0"
_MARKS = re.compile(f"[{MARKS}]")
_SPACES = re.compile(f"[{SPACES}]+")


def collapse(text: str) -> str:
    """These spaces to one plain space, plain spaces off the ends: the spacing
    rule alone, which tidying typed names shares with folding."""
    return _SPACES.sub(" ", text).strip(" ")


def fold(text: str | None) -> str:
    """Decomposed, unmarked, lowercase, single spaces: how a type is compared."""
    return collapse(_MARKS.sub("", unicodedata.normalize("NFD", text or "")).lower())


def group_of(doc_type: str | None) -> str | None:
    """The legislation subfolder a type belongs to, or None for anything else."""
    folded = fold(doc_type)
    for key, _name, starts in GROUPS:
        for start in starts:
            if folded == start or folded.startswith((start + " ", start + "-")):
                return key
    return None


def group_sql(column: str = "d.doc_type") -> str:
    """`group_of` as SQL: the subfolder key, or NULL.

    Built only from the constants above, never from input. The `%%` are
    psycopg's escape for a literal `%`; every statement using this passes
    parameters, so they reach the database as `%`.
    """
    # The same ranges as escapes Postgres reads itself ("\\uXXXX" in an E''
    # string), written out from MARKS and SPACES so they cannot drift apart.
    def escaped(chars: str) -> str:
        return "".join(ch if ch == "-" else f"\\\\u{ord(ch):04x}" for ch in chars)

    folded = (f"btrim(regexp_replace(lower(regexp_replace(normalize({column}, NFD), "
              f"E'[{escaped(MARKS)}]', '', 'g')), E'[{escaped(SPACES)}]+', ' ', 'g'), ' ')")
    branches = []
    for key, _name, starts in GROUPS:
        tests = " OR ".join(
            f"{folded} = '{start}' OR {folded} LIKE '{start} %%' OR {folded} LIKE '{start}-%%'"
            for start in starts
        )
        branches.append(f"WHEN {tests} THEN '{key}'")
    return f"(CASE {' '.join(branches)} END)"


__all__ = ["GROUPS", "GROUP_NAMES", "MARKS", "PREFIXES", "SPACES", "SPHERES",
           "TYPE_SUGGESTIONS", "collapse", "fold",
           "group_of", "group_sql"]
