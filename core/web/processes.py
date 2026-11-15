"""The Processos page: the cases a person follows, and their deadlines.

A case is the person's own (individual) or one of their teams'; the reading
rule is `processes.process_clause`, so an id outside it reads as not found.
Built like the integrations page: GET shows, every change is a POST from this
site with the form token, the person is always the session's, decided by
action name (`process.track`) on the staff row read again for this request.

A deadline given as a date is saved at once. One given as a count (start,
days, úteis or corridos, dobro) is never saved by the first POST: the page
answers with the count laid out, as `/prazo` does, and only "Salvar prazo"
saves the date it shows.
"""
from __future__ import annotations

import html
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from core.config import settings
from core.integrations import datajud
from core.models import StaffMember
from core.repositories import firm_holidays, processes, publications
from core.repositories.connection import get_pool
from core.repositories.teams import Scope, scope_for, teams_of
from core.schedule import local_now
from core.services import authorization, deadline_request
from core.services.deadlines import calcular_prazo, calendario_do_escritorio
from core.web import actions
from core.web.pages import (
    from_another_site,
    page,
    read_form,
    refused_from_another_site,
    remember_theme,
)
from core.web.staff_web import (
    COOKIE,
    app_lock_meta,
    csrf_for,
    csrf_matches,
    current_staff,
    nav,
    signed_out,
)

HOME = "/web/processos/"
router = APIRouter(prefix=HOME.rstrip("/"), tags=["web"],
                   dependencies=[Depends(remember_theme)])

_MAX_BODY = 4096
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TEAM = re.compile(r"^team:(\d{1,9})$")

NOTES = {
    "criado": "Processo adicionado.",
    "duplicado": "Este processo já está em Processos, neste mesmo lugar.",
    "cnj": "Número CNJ inválido. Confira os dígitos.",
    "equipe": "Você só pode colocar um processo numa equipe de que faz parte.",
    "nao_encontrado": "Processo ou prazo não encontrado.",
    "prazo": "Prazo salvo. Os avisos chegam 10 e 5 dias antes e no dia.",
    "concluido": "Prazo concluído. Os avisos dele param.",
    "arquivado": "Processo arquivado.",
    "movido": "Processo movido.",
    "titulo": "Dê um nome ao prazo (até 120 caracteres).",
    "data": "Data inválida. Use dd/mm/aaaa.",
    "refused": "Recarregue a página e tente de novo.",
}

STYLE = """
.new{margin:1.5rem 0}
.new>summary,.card details>summary{cursor:pointer;padding:.3rem 0}
.new form,.card details form{display:grid;gap:.7rem;padding:.6rem 0;max-width:32rem}
.cards{display:grid;gap:1.2rem;margin-top:1.5rem}
.card{padding:1.2rem 1.3rem;border:1px solid var(--line);border-radius:var(--radius-lg);
 background:var(--panel);display:grid;gap:.7rem;min-width:0}
.card h2{margin:0;font-size:1.05rem;overflow-wrap:anywhere}
.card p{margin:0;overflow-wrap:anywhere}
.card .meta{color:var(--mid);font-size:.9rem}
.card form{margin:0}
.card button{width:auto;margin:0}
.card .row{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center}
.deadlines{list-style:none;margin:0;padding:0;display:grid;gap:.5rem}
.deadlines li{display:flex;align-items:center;justify-content:space-between;gap:.8rem;
 flex-wrap:wrap}
.deadlines button{min-height:2.4rem;padding:.4rem .9rem}
.left{font-size:.85rem;padding:.15rem .55rem;border-radius:999px;border:1px solid var(--line)}
.left.bad{color:var(--bad,#c0392b);border-color:currentColor}
.left.warn{color:var(--warn,#b7791f);border-color:currentColor}
label{display:grid;gap:.3rem;font-size:.85rem}
label.check{display:flex;align-items:center;gap:.5rem}
input,select{margin:0}
.count{white-space:pre-wrap;font-size:.9rem}
.hint{font-size:.85rem;color:var(--muted)}
"""


def _switched_on() -> None:
    if not settings.web_enabled:
        raise HTTPException(status_code=404)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _allowed(staff: StaffMember, action: authorization.Action) -> bool:
    return authorization.decide(staff, action).allowed


def _back(note: str = "") -> Response:
    return RedirectResponse(f"{HOME}?aviso={note}" if note else HOME, status_code=303)


def _refused() -> Response:
    return page("Pedido recusado", "<h1 class=bad>Pedido recusado</h1>"
                f"<p>{NOTES['refused']}</p>", status=403)


async def _enter(conn, request: Request) -> tuple[StaffMember, str] | Response:
    signed_in = await current_staff(conn, request)
    if signed_in is None:
        return signed_out(request)
    staff, session = signed_in
    if not _allowed(staff, "process.track"):
        return _refused()
    return staff, session


async def _posted(request: Request) -> dict[str, str] | Response:
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_MAX_BODY)
    if not csrf_matches(request.cookies.get(COOKIE) or "", form.get("csrf", "")):
        return _refused()
    return form


def _form(path: str, csrf: str, inner: str, label: str, *, css: str = "quiet") -> str:
    return (f"<form method=post action='{HOME}{path}'>"
            f"<input type=hidden name=csrf value='{_e(csrf)}'>{inner}"
            f"<button type=submit class={css}>{_e(label)}</button></form>")


def _where_options(teams: list[tuple[int, str]], current: int | None = None) -> str:
    options = [("individual", "Individual (só você)", current is None)]
    options += [(f"team:{tid}", f"Equipe {name}", current == tid) for tid, name in teams]
    return "".join(f"<option value='{_e(value)}'{' selected' if chosen else ''}>"
                   f"{_e(label)}</option>" for value, label, chosen in options)


def _where(raw: str, scope: Scope) -> int | None | str:
    """The team id, None for individual, or "equipe" when it is not theirs."""
    if raw == "individual":
        return None
    match = _TEAM.fullmatch(raw or "")
    if match and int(match[1]) in scope.team_ids:
        return int(match[1])
    return "equipe"


def _date(raw: str, today: date) -> date | None:
    raw = (raw or "").strip()
    iso = _ISO_DATE.fullmatch(raw)
    word = f"{iso[3]}/{iso[2]}/{iso[1]}" if iso else raw
    return deadline_request.parse_date(word, today)


def _days_left(due: date, today: date) -> tuple[str, str]:
    days = (due - today).days
    css = "bad" if days <= 5 else "warn" if days <= 10 else ""
    if days < 0:
        text = f"venceu há {-days} dia{'s' if days != -1 else ''}"
    elif days == 0:
        text = "vence hoje"
    elif days == 1:
        text = "vence amanhã"
    else:
        text = f"em {days} dias"
    return css, text


def _card(process: processes.TrackedProcess, staff: StaffMember, csrf: str,
          teams: list[tuple[int, str]], today: date) -> str:
    where = f"Equipe {process.team_name}" if process.team_id else "Individual"
    meta = " · ".join(_e(part) for part in (process.client, where) if part)
    open_items, done_items = [], []
    for d in process.deadlines:
        if d.done_at is None:
            css, left = _days_left(d.due_date, today)
            open_items.append(
                f"<li><span><strong>{_e(d.title)}</strong> · {d.due_date:%d/%m/%Y} "
                f"<span class='left {css}'>{_e(left)}</span></span>"
                + _form(f"prazo/{d.id}/concluir", csrf, "", "Concluído") + "</li>")
        else:
            done_items.append(f"<li><span>{_e(d.title)} · {d.due_date:%d/%m/%Y}</span></li>")
    deadlines = (f"<ul class=deadlines>{''.join(open_items)}</ul>" if open_items
                 else "<p class=meta>Nenhum prazo em aberto.</p>")
    done = (f"<details><summary>Concluídos ({len(done_items)})</summary>"
            f"<ul class=deadlines>{''.join(done_items)}</ul></details>" if done_items else "")
    new_deadline = (
        "<details><summary>Novo prazo</summary>"
        + _form(f"{process.id}/prazo", csrf,
                "<label>Título<input name=titulo required maxlength=120></label>"
                "<label>Vence em<input name=vence type=date></label>"
                "<p class=hint>Ou conte os dias a partir da intimação:</p>"
                "<label>Início (data da intimação)<input name=inicio type=date></label>"
                "<label>Dias<input name=dias type=number min=1 max=365 inputmode=numeric></label>"
                "<label>Contagem<select name=regime><option value=uteis>úteis</option>"
                "<option value=corridos>corridos</option></select></label>"
                "<label class=check><input type=checkbox name=dobro value=1>Em dobro</label>",
                "Salvar", css="primary")
        + "</details>")
    move = ""
    if process.created_by == staff.id and teams:
        move = ("<details><summary>Onde</summary>"
                + _form(f"{process.id}/onde", csrf,
                        f"<label>Onde<select name=onde>"
                        f"{_where_options(teams, process.team_id)}</select></label>", "Mover")
                + "</details>")
    note = f"<p>{_e(process.note)}</p>" if process.note else ""
    return (f"<section class=card><h2>{_e(datajud.format_process_number(process.process_number))}"
            f"</h2><p class=meta>{meta}</p>{note}{deadlines}{done}{new_deadline}{move}"
            f"<div class=row>{_form(f'{process.id}/arquivar', csrf, '', 'Arquivar')}</div>"
            "</section>")


def _shell(staff: StaffMember, session: str, title: str, body: str, *,
           status: int = 200) -> HTMLResponse:
    return page(title, body, wide=True, style=STYLE, status=status,
                head=app_lock_meta(staff), navigation=nav(staff, session))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def processes_page(request: Request, aviso: str = "") -> Response:
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        scope = await scope_for(conn, staff.id)
        listed = await processes.list_open(conn, scope)
        teams = await teams_of(conn, staff.id)
    csrf = csrf_for(session)
    today = local_now().date()
    note = f"<p class=note role=status>{_e(NOTES[aviso])}</p>" if aviso in NOTES else ""
    djen = ("<label class=check><input type=checkbox name=djen value=1 checked>"
            "Acompanhar publicações no DJEN</label>"
            if settings.djen_enabled and _allowed(staff, "publications.watch") else "")
    new = ("<details class=new><summary>Novo processo</summary>"
           + _form("novo", csrf,
                   "<label>Número CNJ<input name=cnj required inputmode=numeric "
                   "placeholder='0000000-00.0000.0.00.0000' maxlength=25></label>"
                   "<label>Cliente<input name=cliente maxlength=160></label>"
                   "<label>Observação<input name=observacao maxlength=500></label>"
                   f"<label>Onde<select name=onde>{_where_options(teams)}</select></label>"
                   + djen, "Adicionar", css="primary")
           + "</details>")
    cards = "".join(_card(p, staff, csrf, teams, today) for p in listed)
    empty = "" if listed else "<p class=meta>Nenhum processo ainda.</p>"
    body = (f"<h1>Processos</h1><p class=sub>Os processos que você acompanha e os prazos "
            f"deles. Avisos 10 e 5 dias antes e no dia do vencimento.</p>{note}{new}{empty}"
            f"<div class=cards>{cards}</div>")
    return _shell(staff, session, "Processos", body)


@router.post("/novo", include_in_schema=False)
async def new_process(request: Request) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        cited = actions._cnj((form.get("cnj") or "").strip())
        if cited is None or not datajud.has_valid_check_digits(cited):
            return _back("cnj")
        number = datajud.normalize_process_number(cited)
        scope = await scope_for(conn, staff.id)
        team_id = _where(form.get("onde", ""), scope)
        if team_id == "equipe":
            return _back("equipe")
        assert not isinstance(team_id, str)
        made = await processes.create(
            conn, scope, process_number=number,
            client=(form.get("cliente") or "").strip()[:160] or None,
            note=(form.get("observacao") or "").strip()[:500] or None, team_id=team_id)
        if made is None:
            return _back("equipe")
        if made == processes.DUPLICATE:
            return _back("duplicado")
        if (form.get("djen") == "1" and settings.djen_enabled
                and _allowed(staff, "publications.watch")):
            await publications.watch_process(conn, staff.id, number)
    return _back("criado")


@router.post("/{process_id}/prazo", include_in_schema=False)
async def new_deadline(request: Request, process_id: int) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        title = (form.get("titulo") or "").strip()
        if not 1 <= len(title) <= 120:
            return _back("titulo")
        scope = await scope_for(conn, staff.id)
        today = local_now().date()
        if (form.get("vence") or "").strip():
            due = _date(form["vence"], today)
            if due is None:
                return _back("data")
            made = await processes.add_deadline(conn, scope, process_id, title=title,
                                                due_date=due, staff_id=staff.id)
            return _back("prazo" if made else "nao_encontrado")
        if not any(p.id == process_id for p in await processes.list_open(conn, scope)):
            return _back("nao_encontrado")
        start = _date(form.get("inicio", ""), today)
        if start is None:
            return _back("data")
        words = [f"{start:%d/%m/%Y}", (form.get("dias") or "").strip(),
                 "corridos" if form.get("regime") == "corridos" else "úteis"]
        if form.get("dobro") == "1":
            words.append("dobro")
        request_ = deadline_request.parse(words, today)
        csrf = csrf_for(session)
        if isinstance(request_, deadline_request.Incomplete):
            body = (f"<h1>Prazo não calculado</h1><p>{_e(request_.message)}</p>"
                    f"<p><a href='{HOME}'>Voltar</a></p>")
            return _shell(staff, session, "Prazo", body, status=422)
        local = await firm_holidays.holidays_between(conn, request_.start, date.max)
        prazo = calcular_prazo(
            request_.start, deadline_request.counted_days(request_), regime=request_.regime,
            calendario=calendario_do_escritorio((d.day, d.reason) for d in local))
        on_file = await firm_holidays.holidays_between(
            conn, date(request_.start.year, 1, 1), date(prazo.vencimento.year, 12, 31))
    if _date(f"{prazo.vencimento:%d/%m/%Y}", today) is None:
        # Saving reads the date back through the same window: never offer a
        # button whose press would be refused.
        body = (f"<h1>Prazo não calculado</h1><p>{_e(NOTES['data'])} O vencimento "
                f"{prazo.vencimento:%d/%m/%Y} fica fora do período aceito.</p>"
                f"<p><a href='{HOME}'>Voltar</a></p>")
        return _shell(staff, session, "Prazo", body, status=422)
    count = deadline_request.describe(request_, prazo, local_days_on_file=bool(on_file))
    body = (f"<h1>Conferir o prazo</h1><p><strong>{_e(title)}</strong></p>"
            f"<p class=count>{_e(count)}</p>"
            + _form(f"{process_id}/prazo", csrf,
                    f"<input type=hidden name=titulo value='{_e(title)}'>"
                    f"<input type=hidden name=vence value='{prazo.vencimento:%d/%m/%Y}'>"
                    "<input type=hidden name=confirmar value=1>", "Salvar prazo", css="primary")
            + f"<p><a href='{HOME}'>Cancelar</a></p>")
    return _shell(staff, session, "Conferir o prazo", body)


@router.post("/prazo/{deadline_id}/concluir", include_in_schema=False)
async def finish_deadline(request: Request, deadline_id: int) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        done = await processes.mark_done(conn, await scope_for(conn, staff.id), deadline_id,
                                         staff.id)
    return _back("concluido" if done else "nao_encontrado")


@router.post("/{process_id}/arquivar", include_in_schema=False)
async def archive_process(request: Request, process_id: int) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        scope = await scope_for(conn, staff.id)
        if form.get("confirmar") != "1":
            if not any(p.id == process_id for p in await processes.list_open(conn, scope)):
                return _back("nao_encontrado")
            body = ("<h1>Arquivar este processo?</h1><p>Ele sai da lista e os avisos dos "
                    "prazos dele param.</p>"
                    + _form(f"{process_id}/arquivar", csrf_for(session),
                            "<input type=hidden name=confirmar value=1>", "Arquivar")
                    + f"<p><a href='{HOME}'>Cancelar</a></p>")
            return _shell(staff, session, "Arquivar processo", body)
        archived = await processes.archive(conn, scope, process_id)
    return _back("arquivado" if archived else "nao_encontrado")


@router.post("/{process_id}/onde", include_in_schema=False)
async def move_process(request: Request, process_id: int) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        scope = await scope_for(conn, staff.id)
        team_id = _where(form.get("onde", ""), scope)
        if team_id == "equipe":
            return _back("equipe")
        assert not isinstance(team_id, str)
        moved = await processes.set_visibility(conn, scope, process_id, team_id=team_id)
    if moved == processes.DUPLICATE:
        return _back("duplicado")
    return _back("movido" if moved is True else "nao_encontrado")


__all__ = ["HOME", "router"]
