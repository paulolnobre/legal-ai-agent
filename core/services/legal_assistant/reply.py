"""What an agent hands back: the text to send, plus what it was built on.

Kept apart from the graph so the document commands (`/resumo`, `/revisar`) can
return the same shape without importing the graph that imports them.
"""
from __future__ import annotations

from pydantic import BaseModel

from .sources import Source


class AgentReply(BaseModel):
    """An answer and its provenance.

    The sources travel beside the text, not inside it: the handler offers them
    and sends the full list when asked, instead of appending a bibliography to
    every message.
    """

    text: str
    sources: list[Source] = []
    limitations: list[str] = []
    task: str = ""
    cached: bool = False

    def __str__(self) -> str:  # usable wherever the plain text is expected
        return self.text
