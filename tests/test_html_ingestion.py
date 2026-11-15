"""Saved web pages, which is how Brazilian legislation is actually published.

Planalto and the Senate serve statutes as HTML, so a lawyer who wants the Civil
Code in the archive saves a page. Two things make that harder than it sounds,
and both were met on the first real attempt.

The pages are old. Planalto still announces itself as `Microsoft FrontPage 6.0`
and serves a Windows codepage, so decoding as UTF-8 either raises or turns every
accented word in the Civil Code into mojibake.

And the sites fight back. The firm's first upload to the document bucket was a
9KB file named `Código Civil 2 ed.htm` containing 385 characters of text, zero
occurrences of "Art.", and the sentence "Verificando sua conexão". The browser
had been shown a bot-protection page and had saved that. Ingested, it would
have sat in the archive as the Civil Code and been cited as one.
"""
import pytest

from core.services.ingestion import DOCUMENT_EXTENSIONS, extract_text_bytes

# Reduced from the real file the firm uploaded, keeping what identifies it.
_BLOCK_PAGE = (
    b"<!DOCTYPE html><meta name='viewport' content='width=device-width'>"
    b"<title>Verifica&ccedil;&atilde;o de seguran&ccedil;a</title>"
    b"<style>body { background: #f1f1f1; }</style>"
    b"<body><h1>Verificando sua conex&atilde;o</h1>"
    b"<p>Estamos confirmando que este acesso &eacute; leg&iacute;timo.</p></body>"
)


def _statute(encoding: str = "utf-8", articles: int = 30) -> bytes:
    body = "".join(
        f"<p>Art. {n}º Toda pessoa é capaz de direitos e deveres na ordem civil.</p>"
        for n in range(1, articles + 1)
    )
    charset = f"<meta charset='{encoding}'>"
    return f"<html><head>{charset}</head><body>{body}</body></html>".encode(encoding)


@pytest.mark.parametrize("extension", [".htm", ".html"])
def test_a_saved_page_is_a_format_the_archive_accepts(extension):
    assert extension in DOCUMENT_EXTENSIONS


def test_a_statute_saved_from_a_browser_becomes_text():
    text = extract_text_bytes(_statute(), "codigo-civil.htm")

    assert "Art. 1º" in text
    assert "<p>" not in text, "markup is not text"


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "latin-1"])
def test_accents_survive_the_encodings_these_sites_actually_use(encoding):
    """Planalto serves a Windows codepage. Decoding it as UTF-8 either raises
    or silently mangles every accented word in the law."""
    text = extract_text_bytes(_statute(encoding), "lei.htm")

    assert "é capaz" in text
    assert "�" not in text, "a replacement character means the decode was wrong"


def test_an_undeclared_encoding_still_decodes():
    markup = "<html><body>" + "<p>Ação de execução fiscal.</p>" * 40 + "</body></html>"
    text = extract_text_bytes(markup.encode("cp1252"), "sem-charset.htm")

    assert "Ação de execução" in text


def test_the_bot_protection_page_is_refused_rather_than_archived():
    """This is the real failure, not a hypothetical: it arrived wearing the
    filename of the Civil Code."""
    with pytest.raises(ValueError) as refused:
        extract_text_bytes(_BLOCK_PAGE, "Código Civil 2 ed.htm")

    message = str(refused.value)
    assert "quase nenhum texto" in message
    # The message has to name the cause, or the person retries the same save.
    assert "verificação de segurança" in message


def test_the_refusal_is_about_the_text_not_the_file_size():
    """A page can be large and still say nothing: the block page was 9KB of
    stylesheet around one sentence."""
    padded = _BLOCK_PAGE + b"<style>" + b"a { color: red; } " * 500 + b"</style>"

    with pytest.raises(ValueError):
        extract_text_bytes(padded, "grande-mas-vazio.htm")


def test_the_threshold_is_a_trade_and_this_is_what_it_costs():
    """A page under 500 characters is refused, whatever it contains.

    That is the deliberate cost of catching the block page, which carried 385.
    A statute of any archivable length clears it easily: eight articles do.
    A genuinely tiny note refused here belongs in the archive as text anyway,
    where no such guard applies, because there is no site to be blocked by.
    """
    assert extract_text_bytes(_statute(articles=10), "dez-artigos.htm")

    with pytest.raises(ValueError):
        extract_text_bytes(_statute(articles=1), "um-artigo.htm")


def test_an_unsupported_format_still_says_so():
    with pytest.raises(ValueError, match="Unsupported format"):
        extract_text_bytes(b"...", "planilha.xlsx")
