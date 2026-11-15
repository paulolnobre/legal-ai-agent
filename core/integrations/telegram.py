"""Async Telegram Bot API client (long polling + messaging).

Hardening:
- every request carries a timeout (read timeout > long-poll window);
- send_message retries without parse_mode when Telegram rejects broken
  Markdown (LLM output or user-controlled text can contain stray markers);
- voice downloads are size-capped.
"""
import logging

import httpx

from core.config import LEGAL_TELEGRAM_TOKEN, LEGAL_TELEGRAM_URL, settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # read timeout must exceed the getUpdates long-poll window (10s)
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=35.0))
    return _client


async def send_message(
    chat_id,
    text,
    base_url=None,
    parse_mode: str | None = "Markdown",
    reply_markup: dict | None = None,
    preview: bool = True,
) -> int | None:
    """Send a message and return its id, or None when it could not be sent.

    `preview=False` stops Telegram fetching the links in the text to build a
    preview. For a one-time link that matters: without it Telegram's own
    servers visit a URL that was meant for one person, before that person has.

    The id is what lets a message be rewritten later — how a decided request
    stops offering its buttons in every chat that was asked to decide it. An
    int is truthy, so callers that only checked success still read correctly.
    """
    base = base_url or LEGAL_TELEGRAM_URL
    payload = {"chat_id": chat_id, "text": text}
    no_preview = {"is_disabled": True}
    if not preview:
        payload["link_preview_options"] = no_preview
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = await _http().post(f"{base}/sendMessage", json=payload)
        if resp.status_code != 200 and parse_mode:
            # Broken Markdown from LLM/user content — retry as plain text. The
            # keyboard has to survive that retry, or the buttons silently
            # disappear exactly when the text was hardest to format.
            retry = {"chat_id": chat_id, "text": text}
            if reply_markup:
                retry["reply_markup"] = reply_markup
            # The same for the preview, which is the flag most easily lost
            # here: the retry is rebuilt from scratch.
            if not preview:
                retry["link_preview_options"] = no_preview
            resp = await _http().post(f"{base}/sendMessage", json=retry)
        if resp.status_code != 200:
            logger.error("Telegram sendMessage returned status %s", resp.status_code)
            return None
        try:
            return (resp.json().get("result") or {}).get("message_id")
        except ValueError:
            logger.error("Telegram sendMessage returned a body that is not JSON")
            return None
    except httpx.HTTPError as e:
        logger.error("sendMessage transport error: %s", e)
        return None


async def edit_message_text(
    chat_id,
    message_id: int,
    text: str,
    base_url=None,
    reply_markup: dict | None = None,
) -> bool:
    """Rewrite a message already sent, and replace or drop its keyboard.

    Passing no ``reply_markup`` removes the buttons, which is how a decided
    request stops offering a second decision.
    """
    base = base_url or LEGAL_TELEGRAM_URL
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    try:
        resp = await _http().post(f"{base}/editMessageText", json=payload)
        if resp.status_code != 200:
            logger.error("Telegram editMessageText returned status %s", resp.status_code)
            return False
        return True
    except httpx.HTTPError as e:
        logger.error("editMessageText transport error: %s", e)
        return False


async def answer_callback_query(
    callback_query_id: str,
    text: str | None = None,
    base_url=None,
    show_alert: bool = False,
) -> bool:
    """Clear the spinner on a pressed button, optionally with a short notice.

    Telegram leaves the button spinning until this is answered, so it is sent
    on every path — including refusals — and never carries case content: it is
    a toast, visible over the chat.
    """
    base = base_url or LEGAL_TELEGRAM_URL
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
        payload["show_alert"] = show_alert
    try:
        resp = await _http().post(f"{base}/answerCallbackQuery", json=payload)
        if resp.status_code != 200:
            logger.error("Telegram answerCallbackQuery returned status %s", resp.status_code)
            return False
        return True
    except httpx.HTTPError as e:
        logger.error("answerCallbackQuery transport error: %s", e)
        return False


async def send_document(
    chat_id,
    data: bytes,
    filename: str,
    *,
    caption: str | None = None,
    base_url: str | None = None,
) -> bool:
    """Send an authorized original back to the staff member who requested it."""
    base = base_url or LEGAL_TELEGRAM_URL
    form = {"chat_id": str(chat_id)}
    if caption:
        form["caption"] = caption
    try:
        response = await _http().post(
            f"{base}/sendDocument",
            data=form,
            files={"document": (filename, data)},
        )
        if response.status_code != 200:
            logger.error("Telegram sendDocument returned status %s", response.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        logger.error("sendDocument transport error: %s", exc)
        return False


async def send_typing(chat_id, base_url=None):
    base = base_url or LEGAL_TELEGRAM_URL
    try:
        await _http().post(
            f"{base}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
        )
    except httpx.HTTPError as e:
        logger.debug("sendChatAction failed: %s", e)


async def download_voice(file_id: str, token: str | None = None) -> bytes | None:
    """Fetch a voice/audio file. `token` selects the bot — the file endpoint
    carries the token in the path, so a base_url alone would not be enough."""
    return await download_file(file_id, token=token, max_bytes=settings.max_voice_bytes)


async def download_file(
    file_id: str,
    token: str | None = None,
    max_bytes: int | None = None,
) -> bytes | None:
    """Download a Telegram file with an optional hard size limit."""
    bot = token or LEGAL_TELEGRAM_TOKEN
    try:
        resp = await _http().get(
            f"https://api.telegram.org/bot{bot}/getFile", params={"file_id": file_id}
        )
        file_path = resp.json().get("result", {}).get("file_path")
        if not file_path:
            return None
        download = await _http().get(
            f"https://api.telegram.org/file/bot{bot}/{file_path}"
        )
        if download.status_code != 200:
            return None
        if max_bytes is not None and len(download.content) > max_bytes:
            logger.warning("Telegram file too large (%d bytes), rejected", len(download.content))
            return None
        return download.content
    except httpx.HTTPError as e:
        logger.error("Failed to download a Telegram file: %s", e)
        return None


async def close() -> None:
    """Release the shared Telegram HTTP client on replica shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
