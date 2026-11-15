"""The page a lawyer types their PJe credential into, once.

This exists because the chat is the wrong place for a password. Anything typed
in Telegram is on Telegram's servers, in our Redis queue and in the history on
the lawyer's phone, and we control none of those and can wipe none of them.
The agent therefore sends a link, the browser posts straight to this host over
HTTPS, and the chat only ever carries the link.

The link is single use and expires in minutes, and what the database holds is
the SHA-256 of its token rather than the token, so a leaked database does not
hand over a live enrolment link.

**A link that is not usable gets one message, whatever the reason.** Expired,
already used, never existed and belongs to somebody else all read the same,
because telling them apart tells a stranger which tokens are real.

The page is deliberately plain: no external stylesheet, no font, no script, no
analytics. Anything fetched from elsewhere is another party watching a
password being typed.
"""
from __future__ import annotations

import html
import logging
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from core.config import settings
from core.web.pages import page, read_form, remember_theme
from core.repositories.connection import get_pool
from core.repositories.pje_credentials import (
    peek_enrolment,
    save_credential,
    spend_enrolment,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pje", tags=["pje"], dependencies=[Depends(remember_theme)])

_CPF = re.compile(r"^\d{11}$")

# A CPF, a password and the form overhead. Generous for what the form holds
# and small enough that a public endpoint cannot be made to read megabytes:
# Starlette buffers the body in memory before anything here sees it.
_MAX_BODY = 4096





def _unusable() -> HTMLResponse:
    """One message for every reason a link does not work."""
    return page(
        "Link inválido",
        "<h1 class=bad>Este link não vale mais</h1>"
        "<p>Ele pode ter expirado, já ter sido usado, ou não ser válido.</p>"
        "<p>Peça um novo no Telegram com <strong>/pje cadastrar</strong>.</p>",
        status=404,
    )


def _form(token: str) -> HTMLResponse:
    court = html.escape(settings.pje_court)
    return page(
        "Cadastrar acesso ao PJe",
        f"<h1>Cadastrar seu acesso ao {court}</h1>"
        f"<p class=sub>Este link vale uma vez só e expira em poucos minutos.</p>"
        f"<form method=post action='/pje/{html.escape(token)}'>"
        f"<label for=cpf>CPF (somente números)</label>"
        f"<input id=cpf name=cpf inputmode=numeric autocomplete=username required>"
        f"<label for=senha>Senha do PJe</label>"
        f"<input id=senha name=senha type=password autocomplete=current-password required>"
        f"<button type=submit>Salvar</button></form>"
        f"<p class=note>A senha é gravada criptografada e usada apenas para "
        f"<strong>consultar</strong> processos e listar intimações pendentes em "
        f"seu nome. O agente nunca abre o teor de uma intimação pendente, porque "
        f"isso daria ciência e começaria a contar o prazo. Para remover, use "
        f"<strong>/pje remover</strong> no Telegram.</p>",
    )


@router.get("/{token}", response_class=HTMLResponse, include_in_schema=False)
async def show_form(token: str) -> HTMLResponse:
    """Render the form. Looking does not spend the link; posting does."""
    if not settings.pje_enabled:
        return _unusable()
    pool = await get_pool()
    async with pool.connection() as conn:
        staff_id = await peek_enrolment(conn, token)
    if staff_id is None:
        logger.info("A PJe enrolment link was opened and is not usable")
        return _unusable()
    return _form(token)




@router.post("/{token}", response_class=HTMLResponse, include_in_schema=False)
async def submit(request: Request, token: str) -> HTMLResponse:
    """Take the credential, once.

    The link is spent before the credential is stored, so a submission that
    fails validation still burns it. That is the safe direction: a link that
    survives a failed attempt is a link that can be tried repeatedly.
    """
    if not settings.pje_enabled:
        return _unusable()
    form = await read_form(request, max_bytes=_MAX_BODY)
    cpf, senha = form.get("cpf", ""), form.get("senha", "")
    digits = re.sub(r"\D", "", cpf or "")
    pool = await get_pool()
    async with pool.connection() as conn:
        staff_id = await spend_enrolment(conn, token)
        if staff_id is None:
            logger.info("A PJe enrolment was posted with an unusable link")
            return _unusable()
        if not _CPF.match(digits) or not (senha or "").strip():
            logger.info("A PJe enrolment was refused: the form was incomplete")
            return page(
                "Dados incompletos",
                "<h1 class=bad>Não consegui salvar</h1>"
                "<p>O CPF precisa ter 11 dígitos e a senha não pode ficar vazia.</p>"
                "<p>Peça um link novo no Telegram com <strong>/pje cadastrar</strong>.</p>",
                status=400,
            )
        await save_credential(
            conn, staff_id, cpf=digits, password=senha, court=settings.pje_court,
        )
    # Nothing about the credential goes to the log, here or anywhere else.
    logger.info("A PJe credential was enrolled for staff %s", staff_id)
    return page(
        "Acesso cadastrado",
        f"<h1>Pronto</h1><p>Seu acesso ao {html.escape(settings.pje_court)} "
        f"foi cadastrado. Pode fechar esta página e voltar ao Telegram.</p>"
        f"<p class=note>Se quiser remover depois, use <strong>/pje remover</strong>.</p>",
    )


__all__ = ["router"]
