"""The last node: refuse an answer that cites what it did not read.

This runs after whichever agent produced the answer, once, for all of them.
Putting the check inside each agent would be the same decision written four
times, and the fourth copy is the one that drifts and stops checking.

**It only refuses when there was something to check against.** An answer built
on no evidence, a greeting or a draft written from the model's own knowledge,
has nothing to compare against, and refusing every article it mentions would
make those flows unusable while proving nothing. Evidence present means the
answer claimed to rest on material, and then every citation in it must be in
that material.

**A cached answer is not re-checked.** It was checked when it was produced,
and the evidence is not carried in the cache: re-running the comparison
against an empty set would refuse everything that was already cleared.

The refusal is the whole answer, not a footnote, because the firm asked for
that and the reasoning holds: a citation is copied out of an answer into a
petition, and a warning beside it is read by the person who least needs it.
The message names what could not be confirmed, so the lawyer can ask again or
check the source by hand, which is rule 33.
"""
from __future__ import annotations

import logging

from core.metrics import observe_citation_check

from . import citations
from .state import LegalState

logger = logging.getLogger(__name__)

_REFUSAL = (
    "Não vou enviar esta resposta. Ela citava {what}, e isso não aparece "
    "em nenhuma das fontes que consultei, então não posso garantir que exista.\n\n"
    "Isso é uma checagem automática das citações, não uma opinião do modelo "
    "sobre si mesmo. Você pode reformular a pergunta, ou me pedir para "
    "pesquisar essa referência especificamente para conferir na fonte."
)

_MALFORMED = (
    "Não vou enviar esta resposta. Ela citava {what}, e esse número não passa "
    "no dígito verificador do CNJ (Resolução 65/2008), ou seja, está errado "
    "mesmo que a fonte o traga assim.\n\n"
    "Confira o número e me pergunte de novo."
)


def _listed(found: list[citations.Citation]) -> str:
    """"a Súmula 331 do TST e o art. 20-A da Lei 8.245", in the firm's words."""
    described = [citation.described() for citation in found]
    if len(described) == 1:
        return described[0]
    return ", ".join(described[:-1]) + " e " + described[-1]


def verify(state: LegalState) -> dict:
    """Hold back an answer whose citations are not in what it read."""
    if not state.answer or state.cached:
        return {}

    malformed = citations.malformed_process_numbers(state.answer)
    if malformed:
        observe_citation_check("malformed")
        logger.warning("Refused an answer citing %s malformed process number(s)",
                       len(malformed))
        return {
            "answer": _MALFORMED.format(what=_listed(malformed)),
            "sources": [],
            "cacheable": False,
        }

    if not state.evidence:
        observe_citation_check("not_applicable")
        return {}

    missing = citations.unsupported(state.answer, state.evidence)
    if not missing:
        observe_citation_check("supported")
        return {}

    observe_citation_check("unsupported")
    logger.warning("Refused an answer with %s unsupported citation(s)", len(missing))
    return {
        "answer": _REFUSAL.format(what=_listed(missing)),
        # The sources are dropped with the answer: offering the bibliography of
        # a refused answer invites reading it as though it had been sent.
        "sources": [],
        "cacheable": False,
    }


def evidence_from(content: list[dict]) -> list[str]:
    """The text of the document blocks an agent put in front of the model.

    Read from the blocks themselves rather than rebuilt from the sources list,
    because the sources list is trimmed for display and the model saw the whole
    thing. The trailing question block is not evidence and carries no type
    `document`, so it is skipped by the same rule.
    """
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "document":
            continue
        if title := block.get("title"):
            texts.append(str(title))
        source = block.get("source") or {}
        for part in source.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
    return texts


__all__ = ["evidence_from", "verify"]
