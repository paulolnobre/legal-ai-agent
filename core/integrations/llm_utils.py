import json
import re

_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()<>#+\-.!])")


def extract_json_from_text(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def sanitize_response(response: str) -> str:
    if response.strip().startswith("{"):
        inner = extract_json_from_text(response)
        if inner.get("client_response"):
            return str(inner["client_response"])
    return response


def plain_text(response: str) -> str:
    """Make model output readable in Telegram's plain-text mode.

    The bot intentionally sends plain text to avoid Telegram Markdown parsing
    errors. Some models still emit escaped Markdown, which otherwise reaches
    lawyers as literal ``\\**`` characters.
    """
    text = response.replace("\\n", "\n")
    text = _MARKDOWN_ESCAPE.sub(r"\1", text)
    text = _MARKDOWN_LINK.sub(r"\1: \2", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = text.replace("**", "").replace("__", "")
    text = text.replace("```", "")
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    return text.strip()
