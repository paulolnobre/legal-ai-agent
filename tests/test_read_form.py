"""Posted forms are read up to their cap and no further.

The cap is what keeps a public POST from being made to hold megabytes in
memory, so it is enforced while the body arrives, not after it all has.
"""
import asyncio

from starlette.requests import Request

from core.web.pages import read_form


def _request(chunks: list[bytes], read: list[int], headers=None) -> Request:
    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            read.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or []}
    return Request(scope, receive)


def test_a_form_within_the_cap_is_read():
    read: list[int] = []
    form = asyncio.run(read_form(_request([b"csrf=a&texto=", b"ol%C3%A1"], read),
                                 max_bytes=100))

    assert form == {"csrf": "a", "texto": "olá"}


def test_an_oversized_body_stops_being_read_at_the_cap():
    read: list[int] = []
    chunks = [b"x" * 1024 for _ in range(2048)]  # 2 MiB

    form = asyncio.run(read_form(_request(chunks, read), max_bytes=4096))

    assert form == {}
    assert sum(read) <= 4096 + 1024, f"read {sum(read)} bytes past a 4 KiB cap"


def test_a_declared_length_over_the_cap_is_refused_before_reading():
    read: list[int] = []
    request = _request([b"x" * 10], read, headers=[(b"content-length", b"999999")])

    assert asyncio.run(read_form(request, max_bytes=4096)) == {}
    assert read == []
