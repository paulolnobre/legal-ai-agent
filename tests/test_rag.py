"""
test_rag.py
Pure-logic tests for the legal assistant — no DB, no API calls.
Run: python3 core/test_rag.py
"""

from core.integrations.llm_utils import plain_text, sanitize_response
from core.repositories.documents import _vec
from core.services.ingestion import chunk_text


def test_vec_literal():
    assert _vec([1, 2.5, 0]) == "[1.0,2.5,0.0]"
    assert _vec([]) == "[]"
    print("  OK _vec serializes embeddings as a vector literal")


def test_chunk_small_text():
    chunks = chunk_text("Single short paragraph.", size=1200)
    assert chunks == ["Single short paragraph."], chunks
    print("  OK short text becomes a single chunk")


def test_chunk_splits_long_text():
    paras = [f"Clause {i}: " + ("text " * 30) for i in range(20)]
    text = "\n".join(paras)
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) > 1, "long text should produce multiple chunks"
    assert all(c.strip() for c in chunks), "no chunk should be empty"
    # essential content must be preserved (first and last clause present)
    joined = " ".join(chunks)
    assert "Clause 0" in joined and "Clause 19" in joined
    print(f"  OK long text split into {len(chunks)} chunks with overlap")


def test_chunk_empty():
    assert chunk_text("", size=400) == []
    assert chunk_text("   \n  \n", size=400) == []
    print("  OK empty text yields an empty list")


def test_sanitize_extracts_json():
    assert sanitize_response('{"client_response": "olá"}') == "olá"
    print("  OK sanitize extracts client_response from JSON")


def test_sanitize_passthrough():
    assert sanitize_response("plain text without json") == "plain text without json"
    print("  OK sanitize returns plain text unchanged")


def test_plain_text_removes_escaped_markdown_without_losing_links():
    response = (
        "1\\. \\*\\*Tema central\\*\\*\\n"
        "Consulte [STJ](https://scon.stj.jus.br/SCON/)."
    )
    assert plain_text(response) == (
        "1. Tema central\nConsulte STJ: https://scon.stj.jus.br/SCON/."
    )
    print("  OK escaped Markdown is rendered as readable plain text")


if __name__ == "__main__":
    print("\n-- RAG logic tests --")
    tests = [
        test_vec_literal,
        test_chunk_small_text,
        test_chunk_splits_long_text,
        test_chunk_empty,
        test_sanitize_extracts_json,
        test_sanitize_passthrough,
        test_plain_text_removes_escaped_markdown_without_losing_links,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{'OK all passed' if not failed else f'{failed} failure(s)'}\n")
