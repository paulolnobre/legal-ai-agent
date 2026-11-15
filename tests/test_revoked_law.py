"""Revoked law is not law, and it must not reach the model as if it were.

The Planalto keeps every superseded article on the page, struck through, beside
the one in force. Read as plain text the strike vanished and the two stood side
by side, equally authoritative. Measured on 2026-09-21: the firm's own copy of
the Constitution, ingested from the Planalto, carried the 12% real-interest cap
of art. 192, removed by EC 40/2003, as an ordinary searchable passage; and a
live read of the tenancy law gave the model both the revoked "duas vezes nos
doze meses" and the current "24 meses" for the same rule.

The fixtures below are the shapes that page actually uses: <strike> around a
whole paragraph, and an inline style for headings and, in the tenancy law, one
entire article.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.integrations.web_fetch import html_to_text
from core.services import ingestion

# Lei 8.245, art. 62, parágrafo único, as the Planalto serves it: the 1991
# wording struck through, the 2009 wording after it.
_TENANCY = """
<p>Parágrafo único. <strike>Não se admitirá a emenda da mora se o locatário já
houver utilizado essa faculdade por duas vezes nos doze meses imediatamente
anteriores à propositura da ação.</strike></p>
<p>Parágrafo único. Não se admitirá a emenda da mora se o locatário já houver
utilizado essa faculdade nos 24 (vinte e quatro) meses imediatamente anteriores
à propositura da ação. (Redação dada pela Lei nº 12.112, de 2009)</p>
"""

# Constituição, art. 192, as it stood in the firm's archive.
_CONSTITUTION = """
<p>Art. 192. O sistema financeiro nacional, estruturado de forma a promover o
desenvolvimento equilibrado do País e a servir aos interesses da coletividade.</p>
<p><strike>§ 3º As taxas de juros reais, nelas incluídas comissões e quaisquer
outras remunerações direta ou indiretamente referidas à concessão de crédito,
não poderão ser superiores a doze por cento ao ano.</strike></p>
"""


# Real pages are long, and HTML ingestion refuses anything short enough to be a
# site's error or bot-check page, so the archive fixtures carry a body of
# ordinary articles around the one under test.
_BODY = "".join(
    f"<p>Art. {n}. Dispositivo em vigor sem qualquer alteração posterior.</p>"
    for n in range(1, 16)
)
_CONSTITUTION_PAGE = f"<html><body>{_BODY}{_CONSTITUTION}{_BODY}</body></html>"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_revoked_rule_is_gone_and_the_current_one_stays():
    text = _flat(html_to_text(_TENANCY))

    assert "duas vezes nos doze meses" not in text
    assert "nos 24 (vinte e quatro) meses" in text


def test_the_interest_cap_the_constitution_no_longer_has_is_not_read_as_law():
    """The case that exposed this: a firm asking whether there is a
    constitutional limit on interest must not be answered from EC 40/2003's
    predecessor."""
    text = _flat(html_to_text(_CONSTITUTION))

    assert "doze por cento ao ano" not in text
    assert "O sistema financeiro nacional" in text


@pytest.mark.parametrize("tag", ["strike", "s", "del"])
def test_every_struck_through_tag_counts(tag):
    text = _flat(html_to_text(f"<p>vigente <{tag}>revogado</{tag}> também vigente</p>"))

    assert text == "vigente também vigente"


def test_a_struck_heading_goes_with_everything_inside_it():
    """The Constitution strikes headings by inline style, and a span can hold
    spans of its own: the end is found by counting, not by the first close."""
    markup = (
        '<p>antes <span style="text-transform: uppercase; text-decoration: line-through">'
        "TÍTULO IV <span>DOS SERVIDORES</span> PÚBLICOS MILITARES</span> depois</p>"
    )

    assert _flat(html_to_text(markup)) == "antes depois"


def test_a_whole_article_struck_by_style_goes_too():
    """How the tenancy law marks its original art. 4."""
    markup = (
        '<p><span style="color:black; text-decoration:line-through">'
        '<a name="art4"></a>Art. 4<u><sup>o</sup></u> Durante o prazo estipulado'
        "</span></p><p>Art. 4º Texto em vigor.</p>"
    )

    text = _flat(html_to_text(markup))

    assert "Durante o prazo estipulado" not in text
    assert "Texto em vigor" in text


@pytest.mark.parametrize("markup, kept", [
    ("<p><span>span</span></p>", "span"),
    ("<p>art. 4<sup>o</sup></p>", "art. 4 o"),
    ("<p><small>small</small> <strong>strong</strong></p>", "small strong"),
    ("<section><p>section</p></section>", "section"),
    ("<details><p>details</p></details>", "details"),
    # The case a loose match gets wrong: taken for <s>, the <span> would open a
    # strike that the real </s> then closes, and the text in force goes with it.
    ("<p><span>fica</span> <s>sai</s> também fica</p>", "fica também fica"),
])
def test_tags_that_only_begin_like_a_strike_are_left_alone(markup, kept):
    """<s> must not take <span>, <sup>, <small>, <strong> or <section> with it,
    and <del> must not take <details>: a guard names the thing, never a prefix
    that resembles it."""
    assert _flat(html_to_text(markup)) == kept


def test_malformed_markup_keeps_the_text_rather_than_losing_it():
    """A struck span that never closes cannot say where the revoked part ends.
    Dropping everything after it would cost the law in force; keeping the text
    costs, at worst, one revoked sentence."""
    text = _flat(html_to_text('<p>antes <span style="text-decoration: line-through">resto'))

    assert text == "antes resto"


def test_the_archive_reads_saved_pages_the_same_way():
    """Ingestion goes through the same converter, which is how the revoked
    passage reached the firm's corpus in the first place."""
    text = _flat(ingestion.extract_text_bytes(_CONSTITUTION_PAGE.encode("utf-8"), "cf.html"))

    assert "O sistema financeiro nacional" in text

    assert "doze por cento ao ano" not in text


# ── putting the corrected text back on the same document ─────────────────────


class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def stored(monkeypatch):
    """A document 7 whose stored original is the Planalto page above."""
    state = SimpleNamespace(
        full_text="",
        replaced=None, logged=None, invalidated=0, embedded=0,
        original=_CONSTITUTION_PAGE.encode("utf-8"), fail_on_write=False,
    )
    # What the old converter left in the archive: the same page with the strike
    # markup ignored, so the revoked paragraph read as ordinary text.
    state.full_text = ingestion.extract_text_bytes(
        _CONSTITUTION_PAGE.replace("<strike>", "").replace("</strike>", "").encode("utf-8"),
        "cf.html",
    )
    assert "doze por cento ao ano" in state.full_text, "the fixture must start out wrong"

    async def get_source(_conn, doc_id, scope=None):
        if doc_id != 7:
            return None
        return SimpleNamespace(
            source_wrapped_key=None, source_storage_key="k7",
            source_sha256="a" * 64, source_filename="cf.html",
        )

    async def get_document(_conn, doc_id, scope=None):
        return SimpleNamespace(id=doc_id, full_text=state.full_text) if doc_id == 7 else None

    async def embed(chunks):
        state.embedded += len(chunks)
        return [[0.0] for _ in chunks]

    async def replace(_conn, doc_id, **kwargs):
        if state.fail_on_write:
            raise RuntimeError("disk full")
        state.replaced = (doc_id, kwargs["full_text"], len(kwargs["chunks"]))
        return True

    async def log(_conn, **kwargs):
        state.logged = kwargs

    async def invalidate():
        state.invalidated += 1

    monkeypatch.setattr(ingestion, "get_document_source", get_source)
    monkeypatch.setattr(ingestion, "get_document", get_document)
    async def _read(key, wrapped_key=None, **kwargs):
        return state.original

    monkeypatch.setattr(ingestion.document_storage, "read", _read)
    monkeypatch.setattr(ingestion, "embed_texts", embed)
    monkeypatch.setattr(ingestion, "replace_document_text", replace)
    monkeypatch.setattr(ingestion, "log_access", log)
    monkeypatch.setattr(ingestion.cache, "invalidate_answers", invalidate)
    return state


def test_the_corrected_text_lands_on_the_same_document(stored):
    """Ingesting the file again would not do it: the corrected text hashes
    differently, so it would arrive as a second document beside the wrong one."""
    conn = _Conn()

    result = asyncio.run(ingestion.reprocess_document(conn, 7, staff_id=1))

    doc_id, text, _ = stored.replaced
    assert doc_id == 7, "a new id would orphan everything the audit log points at"
    assert "doze por cento ao ano" not in text
    assert result.changed and result.chars_after < result.chars_before
    assert conn.commits == 1


def test_the_correction_is_audited_and_stale_answers_are_dropped(stored):
    """An answer cached from the old text may quote exactly what was removed."""
    asyncio.run(ingestion.reprocess_document(_Conn(), 7, staff_id=1))

    assert stored.logged["action"] == "reprocess"
    assert stored.logged["document_ids"] == [7]
    assert stored.invalidated == 1


def test_an_unchanged_extraction_writes_and_spends_nothing(stored):
    stored.full_text = ingestion.extract_text_bytes(stored.original, "cf.html")
    conn = _Conn()

    result = asyncio.run(ingestion.reprocess_document(conn, 7))

    assert not result.changed
    assert stored.replaced is None
    assert stored.embedded == 0, "no embeddings paid for a text that did not change"
    assert conn.commits == 0


def test_a_failed_write_leaves_the_document_as_it_was(stored):
    stored.fail_on_write = True
    conn = _Conn()

    with pytest.raises(RuntimeError):
        asyncio.run(ingestion.reprocess_document(conn, 7))

    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert stored.invalidated == 0


def test_a_document_without_a_stored_original_says_so(stored):
    with pytest.raises(ValueError, match="no stored original"):
        asyncio.run(ingestion.reprocess_document(_Conn(), 99))
