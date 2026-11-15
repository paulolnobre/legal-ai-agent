"""Internal (staff) bot handler: staff gate + command routing (async).

Staff can write or send a voice note; audio is transcribed behind the gate,
so a stranger's audio never reaches Whisper.

Anyone not on the allowlist (`staff`) gets NO response from the corpus — only a
message stating that access is restricted and showing their chat_id (so the
administrator can authorize them). Denial replies are themselves rate-limited
so strangers cannot use the bot as a reply amplifier.

User-facing strings are intentionally in Portuguese — the language the
assistant speaks to the firm's staff.
"""
import asyncio
import contextlib
import dataclasses
import difflib
import logging
import re
import secrets
import time
from contextvars import ContextVar
from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from core.config import (
    LEGAL_TELEGRAM_TOKEN,
    pages_base_url,
    settings,
)
from core.integrations.llm_utils import plain_text
from core.integrations.telegram import (
    download_file,
    download_voice,
)
from core.integrations import pje_mni
from core.metrics import observe_feedback
from core.repositories import (
    answer_feedback,
    callback_actions,
    conversation,
    firm_holidays,
    panel,
    panel_uploads,
    pending_actions,
    pje_credentials,
    web_access,
)
from core.services import deadline_request, media, panel_intake, second_factor
from core.services.legal_assistant.graph import budget_reached
from core.services.deadlines import calcular_prazo, calendario_do_escritorio
from core.services import pje as pje_service
from core.repositories.connection import get_pool
from core.repositories.microsoft_accounts import (
    TokenSealError,
    forget_account,
    get_account,
    save_account,
)
from core.services import authorization, budget, channels, folders, push
from core.schedule import local_now
from core.repositories.documents import (
    private_documents_of,
    take_over_document,
    MAX_DOC_TYPE_CHARS,
    MAX_MATTER_CHARS,
    MAX_TITLE_CHARS,
    count_documents,
    get_document_source,
    get_document_summary,
    folder_of,
    get_official_source,
    list_matters,
    log_access,
    list_doc_types,
    rename_document,
    update_document_fields,
    search_documents,
)
from core.repositories.teams import (
    Scope,
    add_member,
    create_team,
    find_team,
    list_teams,
    remove_member,
    scope_for,
    teams_of,
)
from core.repositories.publications import (
    OAB_UFS,
    list_own_watches,
    unwatch_oab,
    unwatch_process,
    watch_oab,
    watch_process,
)
from core.repositories.staff import (
    ERASED_NAME,
    MANAGEMENT_ROLES,
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_LAWYER,
    ROLE_OWNER,
    ROLE_ORDER,
    StaffDeactivationError,
    cancel_access_request,
    create_or_refresh_access_request,
    deactivate_staff,
    decide_access_request,
    erase_person,
    get_access_request,
    get_staff_by_chat_id,
    get_staff_by_id,
    has_rank,
    list_pending_access_requests,
    list_staff,
    normalize_role,
    role_rank,
    set_staff_role,
)
from core.integrations import (
    cache,
    datajud,
    djen,
    inbox_bucket,
    microsoft,
    process_links,
    web_fetch,
)
from core.security import RateLimiter, parse_int_id
from core.services.audio import is_silence_artifact, transcribe
from core.services import ingestion
from core.services.ingestion import DOCUMENT_EXTENSIONS, ingest_bytes, ingest_document
from core.services.document_removal import remove_document
from core.services.inbox_import import import_new_documents
from core.services import microsoft_files
from core.services.law_refresh import refresh_document
from core.services import document_storage
from core.services.exporting import FORMATS, build_async, normalize_format
from core.services.legal_assistant.reply import AgentReply
from core.services.legal_assistant import conversation_router
from core.services.legal_assistant.actions import CATALOGUE as _ACTIONS, ground
from core.services.legal_assistant import (
    answer_question,
    sources as sources_module,
)
from core.services.legal_assistant.intents import (
    DocumentFilters,
    classify_local_intent,
    detect_document_request,
    detect_export_request,
    format_in,
    parse_document_filters,
    wants_sources,
)

logger = logging.getLogger(__name__)


_limiter = RateLimiter(settings.rate_limit_max, settings.rate_limit_window)
_denied_limiter = RateLimiter(1, 3600)  # one denial reply per stranger per hour

# A manager may simply type "/autorizar": the code is then asked for and read from
# the next message, so nobody has to retype the decision as a single line.
_PENDING_DECISION_TTL = 300.0  # seconds a pending question stays answerable

# The confirmations that destroy something by a typed word, bound to the
# window that asked for them; any other question follows the one conversation.
_CHANNEL_BOUND_CONFIRMATIONS = frozenset({
    "confirm_document_deletion", "confirm_deactivation", "confirm_erase",
})
_CHANNEL_NAMES = {"telegram": "Telegram", "panel": "painel"}


_PendingAction = Literal[
    "promote",
    "demote",
    "deactivate",
    "confirm_deactivation",
    "erase",
    "confirm_erase",
    "delete_document",
    "confirm_document_deletion",
    "original",
    "details",
    "rename",
    "rename_title",
    "edit",
    "edit_fields",
    "process",
    "research",
    "draft",
    "doc_filter",
]



# What /status says about each awaited answer: the flow and the next move, never
# the name of the person or document involved.
_PENDING_STEPS: dict[_PendingAction, tuple[str, str]] = {
    "promote": ("promoção de usuário", "envie o ID do usuário que deve subir de papel"),
    "demote": ("rebaixamento de usuário", "envie o ID do usuário que deve descer de papel"),
    "deactivate": ("exclusão de acesso", "envie o ID do usuário que perderá o acesso"),
    "confirm_deactivation": ("exclusão de acesso", "responda confirmar para concluir"),
    "erase": ("remoção de dados pessoais", "envie o ID da pessoa"),
    "confirm_erase": (
        "remoção de dados pessoais", "responda APAGAR para concluir",
    ),
    "delete_document": ("exclusão de documento", "envie o ID do documento a apagar"),
    "confirm_document_deletion": (
        "exclusão de documento", "responda confirmar para apagar definitivamente",
    ),
    "original": ("envio de arquivo original", "envie o ID do documento"),
    "details": ("detalhes de documento", "envie o ID do documento"),
    "rename": ("renomear documento", "envie o ID do documento"),
    "edit": ("edição de documento", "envie o ID do documento"),
    "edit_fields": (
        "edição de documento", "envie os campos, por exemplo: tipo: lei",
    ),
    "rename_title": ("renomear documento", "envie o novo nome do documento"),
    "process": ("consulta de processo", "envie o número CNJ completo"),
    "research": ("pesquisa externa", "envie sua pergunta"),
    "draft": ("redação de documento", "descreva o que você quer redigir"),
    "doc_filter": ("busca no acervo", "envie o filtro (cliente, tipo ou período)"),
}

# The last answer sent to each chat, so "quais as fontes?" and "manda em pdf"
# work as follow-ups instead of starting a new request from scratch.
_DELIVERY_TTL = 3600.0
_last_deliveries: dict = {}  # chat_id -> (title, reply, expires_at)
_EXPORT_OFFER_CHARS = 1200  # above this, an answer is worth a file

# Whisper spells unfamiliar words by ear. Seeding it with the vocabulary of the
# practice keeps "agravo de instrumento" from arriving as "agravo de estrumento".
_VOICE_HINT = (
    "Transcrição jurídica brasileira. Termos frequentes: petição inicial, contestação, "
    "réplica, agravo de instrumento, embargos de declaração, recurso especial, "
    "tutela de urgência, liminar, jurisprudência, súmula, STF, STJ, TST, CLT, CPC, "
    "Código Civil, cláusula, aditivo contratual, rescisão, distrato, usucapião, "
    "honorários sucumbenciais, danos morais, escritório Demo & Associados."
)


def _caption_field(caption: str, name: str) -> str | None:
    """Read an explicit metadata field from a Telegram document caption."""
    match = re.search(rf"(?:^|[;\n])\s*{name}\s*:\s*([^;\n]+)", caption, re.IGNORECASE)
    return match.group(1).strip() if match else None


_HELP = (
    "👨‍⚖️ Assistente jurídico interno da Demo & Associados\n\n"
    "Escreva ou mande um áudio: eu transcrevo e encaminho para o que a "
    "mensagem pedir:\n"
    "• pergunta sobre um caso nosso → busco no acervo e cito a fonte\n"
    "• legislação, jurisprudência, prazo → pesquiso em fontes públicas\n"
    "• texto colado no chat → reviso e aponto riscos\n"
    "• 'redija uma cláusula de...' → escrevo a minuta\n\n"
    "Comandos:\n"
    "/comandos (ou /ajuda): lista os comandos\n"
    "/docs [filtros]: lista o acervo; aceita filtros como\n"
    "  /docs contratos da Acme de 2024\n"
    "  /docs petições dos últimos 6 meses\n"
    "/casos: lista os casos/clientes do acervo\n"
    "/detalhes <id>: tipo, cliente/matéria e data de um documento\n"
    "/compartilhar <id> <nome da equipe|escritorio|privado>: move um documento seu de pasta\n"
    "/grupos: mostra as equipes e quem está em cada uma\n"
    "/microsoft: conecta a sua conta Microsoft, para o agente ler os seus arquivos\n"
    "/pje: cadastra o seu acesso ao PJe, por um link seguro fora do chat\n"
    "/assumir <id>: traz para a sua pasta um documento de outra pessoa (owner)\n"
    "/web: link de acesso às páginas (arquivos, editor)\n"
    "/web encerrar: remove as chaves de Face ID e encerra todas as sessões\n"
    "/mfa: situação do autenticador, ou /mfa refazer\n"
    "/intimacoes: lista o que está pendente de leitura no PJe, sem abrir\n"
    "/autos <número>: mostra o processo no PJe e os seus documentos\n"
    "/onedrive [busca]: lista os seus arquivos; /onedrive trazer <n> ingere um deles\n"
    "/processo <número CNJ>: situação, movimentações e onde abrir o processo\n"
    "/prazo <data> <dias> <úteis|corridos>: calcula o vencimento e mostra a conta\n"
    "  /prazo 06/10/2026 15 úteis  (acrescente dobro para prazo em dobro)\n"
    "/feriados: feriados e suspensões locais que entram no /prazo\n"
    "/oab <número> <UF>: aviso aqui toda intimação nova no DJEN para essa OAB\n"
    "/acompanhar <número CNJ>: aviso aqui toda publicação nova desse processo\n"
    "/desacompanhar <CNJ ou OAB>: para de avisar\n"
    "/fontes: lista as fontes da última resposta, com o que há em cada uma\n"
    "/exportar <docx|pdf|xlsx>: envia a última resposta como arquivo\n"
    "/id: mostra seu identificador\n"
    "/status: mostra o que estou aguardando de você agora\n"
    "/cancelar: encerra o pedido em andamento, sem apagar nada\n\n"
    "⚠️ As respostas auxiliam o trabalho; o parecer final é do advogado responsável."
)

_LAWYER_HELP = (
    "\n\nTrabalho sobre o acervo (advogados):\n"
    "/arquivo <id>: envia o arquivo original salvo\n"
    "/ingerir <url oficial>: baixa a peça, ingere no acervo e te envia o arquivo\n"
    "/renomear <id> <novo nome>: dá um nome legível a um documento\n"
    "/editar <id>: corrige a classificação de um documento\n"
    "  /editar 12 tipo: lei; caso: Acme\n"
    "/atualizar <id> [url oficial]: liga uma lei à página oficial; confiro toda "
    "semana e atualizo o acervo quando ela mudar\n"
    "/importar: lê o balde de documentos do escritório e ingere o que for novo\n"
    "  (anexar um arquivo na conversa também ingere no acervo)\n"
)

_MANAGEMENT_HELP = (
    "\n\nGestão de acesso:\n"
    "/grupos criar <nome>: cria uma equipe\n"
    "/grupos adicionar <nome> <id>: põe alguém na equipe\n"
    "/grupos remover <nome> <id>: tira alguém da equipe\n"
    "/pendentes: solicitações aguardando decisão\n"
    "/autorizar [código]: libera uma pessoa como estagiário\n"
    "/recusar [código]: recusa uma solicitação\n"
    "  (sem o código, eu pergunto qual é a solicitação)\n"
    "/usuarios (ou /equipe): lista usuários e papéis\n"
    "/feriado <data> <nome>: cadastra um feriado ou suspensão local\n"
    "  /feriado remover <data> tira do calendário\n"
)

_OWNER_HELP = (
    "/promover <id>: sobe um degrau, estagiário → advogado → admin\n"
    "/rebaixar <id>: desce um degrau\n"
    "/excluir_usuario <id>: revoga o acesso após confirmação\n"
    "/esquecer <id>: apaga os dados pessoais de vez (LGPD), após confirmação\n"
    "  (revogar tira o acesso; esquecer tira o nome, o e-mail e o Telegram,\n"
    "   e não tem como desfazer)\n"
    "/excluir_documento <id>: apaga um documento do acervo, após confirmação\n"
    "  (o arquivo original também é apagado; a auditoria é preservada)\n"
)

# What replaces /resumo and /revisar. It names both supported routes, because a
# refusal that does not say what to do instead reads as a broken feature.
_CORPUS_IS_A_SOURCE = (
    "📚 O acervo é fonte de consulta, não de processamento.\n\n"
    "Posso encontrar o documento, citar os trechos que respondem à sua pergunta "
    "e enviar o arquivo original com /arquivo <id>. O que não faço é ler um "
    "documento inteiro do acervo para resumir ou revisar: um único pedido desses "
    "pode custar mais do que um mês de uso normal.\n\n"
    "Duas formas de conseguir o que você quer:\n"
    "• Faça a pergunta direto (\"o que o contrato da Acme diz sobre rescisão?\") "
    "e eu respondo com os trechos que importam;\n"
    "• Cole o texto aqui no chat, ou envie o arquivo, e eu analiso o que você "
    "mandou."
)

_DOCUMENT_HELP = (
    "Não precisa usar comando. Toque no clipe/anexo do Telegram e envie o arquivo "
    "diretamente nesta conversa. Aceito PDF, DOCX, TXT, Markdown e imagens "
    f"(PNG, JPG ou TIFF), com até {settings.max_doc_mb} MB.\n\n"
    "Para obter melhores resultados, prefira arquivos com texto selecionável, "
    "como DOCX, TXT, Markdown ou PDF pesquisável. Imagens e PDFs escaneados também "
    "são aceitos, mas dependem de OCR e podem ter menor precisão.\n\n"
    "Se quiser organizar o acervo, use na legenda: caso: Nome do caso; tipo: contrato. "
    "Quando eu confirmar o processamento, você já pode fazer perguntas sobre o documento."
)


def _buttons_of(reply_markup: dict | None) -> list[list[channels.Button]] | None:
    """A Telegram inline keyboard as the channel's buttons."""
    rows = (reply_markup or {}).get("inline_keyboard") or []
    buttons = [[channels.Button(str(b.get("text", "")), str(b.get("callback_data", "")))
                for b in row] for row in rows]
    return [row for row in buttons if row] or None


async def _send(chat_id, text, reply_markup: dict | None = None, *, preview: bool = True):
    """Send long text without Markdown (avoids errors with legal content).

    The person being served is answered through the channel their message
    came by; anyone else (a manager told of a request) by Telegram. Where the
    channel limits a message's length the text goes in parts, and a keyboard
    rides on the final part, so buttons sit under the end of what they refer
    to rather than in the middle of a split message.
    """
    text = plain_text(str(text or ""))
    if not text:
        return None
    channel = channels.for_chat(chat_id)
    talking = _conversation.get()
    if talking is not None and talking[2] == str(chat_id) and not channel.records_itself:
        await _record(talking[0], talking[1], chat_id, "bot", text)
    limit = channel.max_text
    parts = [text] if not limit else [text[i:i + limit] for i in range(0, len(text), limit)]
    buttons = _buttons_of(reply_markup)
    reference = None
    for index, part in enumerate(parts):
        reference = await channel.send_text(
            chat_id, part, buttons if index == len(parts) - 1 else None, preview=preview,
        )
    return reference


_CATALOG_PAGE = 30  # documents per reply; the whole archive does not fit a chat


def _render_document_details(document, official=None, folder: str | None = None) -> str:
    """The metadata already on file for one document, nothing generated."""
    lines = [
        f"📄 [{document.id}] {document.title}",
        f"Tipo: {document.doc_type or 'não informado'}",
        f"Cliente/matéria: {document.matter or 'não informado'}",
    ]
    if document.created_at:
        lines.append(f"Ingerido em: {document.created_at:%d/%m/%Y}")
    if folder is not None:
        lines.append(f"Pasta: {folder}")
    if official is not None and official.source_url:
        lines.append(f"Página oficial: {official.source_url}")
        if official.checked_at:
            lines.append(f"Conferida em: {local_now(official.checked_at):%d/%m/%Y}")
    lines.append(
        f"\nPara corrigir a classificação: /editar {document.id} tipo: lei\n"
        "Para receber o arquivo original: /arquivo seguido do ID."
    )
    return "\n".join(lines)


async def _describe_editable(conn, document) -> str:
    """What can be changed, what it is now, and the words to change it with.

    The types already in use are offered because an archive collects "lei",
    "Lei", "LEI" and "legislacao" as four different kinds of thing unless
    somebody is shown what is already there.
    """
    lines = [
        f"✏️ Editando o documento [{document.id}]",
        f"Nome atual: {document.title}",
        f"Tipo atual: {document.doc_type or 'não informado'}",
        f"Cliente/matéria atual: {document.matter or 'não informado'}",
    ]
    try:
        types = await list_doc_types(conn, scope=viewer())
    except Exception:  # pragma: no cover - a listing failure must not block the edit
        logger.exception("Could not list the document types in use")
        types = []
    if types:
        lines.append(
            "\nTipos já usados no acervo: "
            + ", ".join(f"{name} ({count})" for name, count in types)
        )
    lines.append(
        "\nResponda com o que quer mudar, por exemplo:\n"
        "  tipo: lei\n"
        "  tipo: lei; caso: legislação federal\n"
        "  nome: Código Civil (Lei 10.406/2002); tipo: lei\n\n"
        "O que você não citar fica como está. A data de ingestão não muda: ela "
        "registra quando o acervo recebeu o documento.\n"
        "Ou envie /cancelar."
    )
    return "\n".join(lines)


async def _send_document_catalog(
    conn, chat_id, query: str = "", *, explicit: bool = True
) -> None:
    """Render the corpus, narrowed by whatever filters the message carried.

    Listing everything stops being useful the moment the archive is real, so a
    filtered request is answered with the slice asked for, and an unfiltered one
    says how to narrow it instead of dumping hundreds of lines.

    `explicit` is False when nobody typed /docs and the catalogue was inferred
    from a sentence. The parser is stricter there, because a sentence that
    mentions documents is not a filter somebody wrote.
    """
    filters = parse_document_filters(query, explicit=explicit) if query else DocumentFilters()
    docs = await search_documents(
        conn,
        scope=viewer(),
        matter=filters.matter,
        doc_type=filters.doc_type,
        since=filters.since,
        until=filters.until,
        limit=_CATALOG_PAGE + 1,
    )

    if not docs:
        total = await count_documents(conn, scope=viewer())
        if not total:
            await _send(chat_id, "📂 Acervo vazio: nenhum documento foi ingerido ainda.")
            return
        await _send(
            chat_id,
            f"Nenhum documento encontrado com esse filtro ({filters.describe()}).\n"
            f"O acervo tem {total} documento(s). Tente outro recorte, "
            "ou /docs sem filtro para ver os mais recentes.",
        )
        return

    truncated = len(docs) > _CATALOG_PAGE
    shown = docs[:_CATALOG_PAGE]
    lines = [f"[{document.id}] {document.title}{_catalog_metadata(document)}"
             for document in shown]

    header = "📂 Documentos"
    if not filters.is_empty():
        header += f" ({filters.describe()})"
    message = f"{header}:\n" + "\n".join(lines)

    if truncated:
        total = await count_documents(conn, scope=viewer())
        message += (
            f"\n\nMostrando os {_CATALOG_PAGE} mais recentes de {total} no acervo. "
            "Refine com, por exemplo:\n"
            "  /docs contratos da Acme\n"
            "  /docs petições dos últimos 6 meses\n"
            "  /docs acórdãos de março de 2025"
        )
    await _send(chat_id, message)


def _catalog_metadata(document) -> str:
    """The parenthesis after a title, or nothing when there is nothing to say.

    It used to print `(- | -)` for a document with no type and no matter, which
    is two dashes of noise on every line of a list people read to find things.
    An absent field is now absent.
    """
    parts = [part for part in (document.doc_type, document.matter) if part]
    if document.created_at:
        parts.append(f"{document.created_at:%m/%Y}")
    return f" ({' | '.join(parts)})" if parts else ""
def _imported_line(entry) -> str:
    """One imported file, with what its folder said about it.

    Showing the classification is what makes the folder convention visible.
    Somebody who sees "sem classificação" beside a file learns, without being
    told, that a file dropped at the root of the bucket arrives unclassified.
    """
    name, matter, doc_type = entry
    parts = [part for part in (doc_type, matter) if part]
    suffix = f" ({', '.join(parts)})" if parts else " (sem classificação)"
    return f"  • {name}{suffix}"


def _render_import_report(report) -> str:
    """What the import did, including what it refused and why.

    A run that says only "3 documents imported" hides the two it skipped, and
    the skipped ones are the ones somebody needs to act on: a wrong format is a
    file the firm believes it delivered and did not.
    """
    if not report.listed:
        return "📥 O balde de documentos está vazio."
    if not report.touched:
        # "Already known" is not "already in the archive", and saying so sent
        # somebody looking in /docs for a document that had been refused. The
        # ledger holds what was ingested, what turned out to be a duplicate and
        # what failed to parse; only the first of those is in the archive.
        return (
            f"📥 Nada novo no balde. Os {report.already_known} arquivo(s) que "
            "estão lá já foram avaliados antes.\n\n"
            "Isso não quer dizer que todos entraram no acervo: use /docs para "
            "ver o que está lá de fato."
        )

    lines = ["📥 Importação do balde:"]
    if report.ingested:
        lines.append(f"\n✅ {len(report.ingested)} documento(s) ingerido(s):")
        lines += [_imported_line(entry) for entry in report.ingested]
    if report.duplicates:
        lines.append(
            f"\n♻️ {len(report.duplicates)} já estavam no acervo com outro nome:"
        )
        lines += [f"  • {name}" for name in report.duplicates]
    if report.skipped:
        lines.append(f"\n⏭️ {len(report.skipped)} ignorado(s):")
        lines += [f"  • {name}: {reason}" for name, reason in report.skipped]
    if report.failed:
        lines.append(f"\n⚠️ {len(report.failed)} com erro:")
        lines += [f"  • {name}: {reason}" for name, reason in report.failed]
    if report.already_known:
        lines.append(
            f"\n{report.already_known} arquivo(s) já lidos antes foram pulados."
        )
    return "\n".join(lines)


async def _send_matter_index(conn, chat_id) -> None:
    """The matters in use, so someone can filter without guessing a name."""
    matters = await list_matters(conn, scope=viewer())
    if not matters:
        await _send(
            chat_id,
            "Nenhum caso/cliente foi registrado ainda. Use 'caso: Nome do caso' na "
            "legenda ao enviar um documento para organizá-lo.",
        )
        return
    lines = [f"• {name} ({total})" for name, total in matters]
    await _send(
        chat_id,
        "🗂️ Casos e clientes no acervo:\n" + "\n".join(lines)
        + "\n\nFiltre com /docs contratos da <nome>.",
    )


async def _send_staff_directory(conn, chat_id) -> None:
    users = await list_staff(conn)
    # Highest rank first: the roster is read to find who can decide something.
    ordered = sorted(users, key=lambda user: (-role_rank(user.role), user.name.casefold()))
    lines = [
        f"[{user.id}] {user.name} ({ROLE_LABELS[normalize_role(user.role)]})"
        for user in ordered
    ]
    await _send(chat_id, "👥 Equipe autorizada:\n" + "\n".join(lines))


def _help_for(staff) -> str:
    """The commands this person can actually run — nothing they cannot."""
    text = _HELP
    if has_rank(staff.role, ROLE_LAWYER):
        text += _LAWYER_HELP
    if has_rank(staff.role, ROLE_ADMIN):
        text += _MANAGEMENT_HELP
    if has_rank(staff.role, ROLE_OWNER):
        text += _OWNER_HELP
    return text


def _commands_for(staff) -> list[str]:
    """The commands this person may actually run, as they appear in the help."""
    return re.findall(r"^(/[a-z_]+)", _help_for(staff), re.MULTILINE)


# A help line is "/name <args>: what it does". The argument part may hold
# parentheses and other commands, as /comandos does, but never a colon, so the
# first colon after the name is reliably the one separating the description.
_MENU_LINE = re.compile(r"^/(?P<name>[a-z_]+)(?P<args>[^:\n]*): (?P<description>[^\n]+)$",
                        re.MULTILINE)


def command_menu() -> list[dict]:
    """The command list Telegram shows when somebody types "/".

    Built from the help text rather than from a second list kept beside it.
    The help is already the one place that says which commands exist, because
    `_commands_for` reads it to decide whether a command is real, and a menu
    that drifted from it would offer the firm something the bot would then
    refuse.

    Every role's commands are listed, not just the reader's: Telegram can scope
    a menu per chat, but that means re-registering it whenever somebody is
    promoted, and the menu is a convenience, never the authorization boundary.
    What stops an intern running /promover is `core/services/authorization.py`,
    exactly as before, whether or not the command is visible.
    """
    everyone = _HELP + _LAWYER_HELP + _MANAGEMENT_HELP + _OWNER_HELP
    menu: list[dict] = []
    seen: set[str] = set()
    for match in _MENU_LINE.finditer(everyone):
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        # Telegram caps a description at 256 characters and shows it on one
        # line, so the first clause is the part worth keeping.
        description = match.group("description").split(";")[0].strip()
        menu.append({"command": name, "description": description[:256]})
    return menu


def _unknown_command_reply(command_name: str, staff) -> str:
    """What to say about a command that does not exist.

    A typo gets its correction; anything else gets the real list. Either way the
    answer is built from the help text itself, so an invented command name can
    never be confirmed by an improvised reply.
    """
    known = _commands_for(staff)
    # 0.75 is where real typos separate from false friends: "/usuario" scores
    # 0.94 against "/usuarios" and "/procesos" 0.89 against "/processo", while
    # "/listar" only reaches 0.67 against "/revisar", a suggestion that would
    # send someone to rewrite a document when they asked for a listing.
    close = difflib.get_close_matches(command_name, known, n=1, cutoff=0.75)
    if close:
        return f"Não conheço {command_name}. Você quis dizer {close[0]}?"
    return (
        f"Não conheço o comando {command_name}. Estes são os que existem:\n\n"
        + _help_for(staff)
    )


def _requester_identity(message: dict, chat_id) -> tuple[str, str | None]:
    sender = message.get("from") or {}
    name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    ).strip()
    username = sender.get("username")
    return name or username or f"Solicitante {chat_id}", username


async def _notify_managers(conn, request) -> None:
    """Offer each manager the decision as buttons, keeping the commands too.

    Every manager gets their own pair of tokens: a button is bound to the
    person who may press it, so one manager's message cannot be forwarded to
    somebody else and still work.
    """
    managers = await list_staff(conn, roles=MANAGEMENT_ROLES)
    username = f" (@{request.username})" if request.username else ""
    message = (
        "🔔 Nova solicitação de acesso\n"
        f"Pessoa: {request.name}{username}\n"
        f"Código: {request.request_code}\n\n"
        f"Decida no botão abaixo, ou use /autorizar {request.request_code} "
        f"ou /recusar {request.request_code}."
    )
    for manager in managers:
        if not manager.chat_id:
            continue
        keyboard = None
        try:
            tokens = [
                await callback_actions.issue(
                    conn,
                    chat_id=manager.chat_id,
                    staff_id=manager.id,
                    channel=channels.for_chat(manager.chat_id).name,
                    issued_role=normalize_role(manager.role),
                    action=(
                        "access.view_request" if operation == "details"
                        else "access.review"
                    ),
                    operation=operation,
                    resource=request.request_code,
                    ttl_seconds=_CALLBACK_TTL,
                )
                for operation in ("approve", "refuse", "details")
            ]
            keyboard = _decision_keyboard(*tokens)
        except Exception:
            # The commands in the text still work, so a failure to mint the
            # buttons must not cost the manager the notification itself.
            logger.exception("Could not issue decision buttons; sending text only")
            tokens = []
        message_id = await _send(manager.chat_id, message, reply_markup=keyboard)
        if tokens and message_id:
            try:
                await callback_actions.attach_reference(
                    conn, tokens, reference=str(message_id),
                )
            except Exception:
                # Only costs the later rewrite of this copy; the button itself
                # still works and still decides once.
                logger.exception("Could not record which message holds the buttons")
        if message_id:
            # After the message it points to, never instead of it.
            await push.notify(manager.id, "access")


async def _retire_request_buttons(conn, request, footer: str) -> None:
    """Take the decision back off every manager's screen.

    A request nobody can decide any more must stop offering the decision, the
    same way a decided one does. The managers' copies are rewritten from a line
    rebuilt here rather than from the original text, which only the chat that
    holds it can read.
    """
    try:
        retired = await callback_actions.cancel_siblings(
            conn, resource=request.request_code,
        )
    except Exception:
        logger.exception("Could not retire the buttons of a withdrawn request")
        return
    for chat_id, channel_name, reference in retired:
        target = _channel_for_copy(chat_id, channel_name)
        if target is not None:
            await _rewrite_decided(
                chat_id, reference,
                f"🔔 Solicitação {request.request_code} de {request.name}",
                footer, channel=target,
            )


async def _withdraw_access_request(conn, chat_id) -> bool:
    """Let somebody take back the request they made. True when there was one."""
    request = await cancel_access_request(conn, chat_id)
    if request is None:
        await _send(
            chat_id,
            "Você não tem nenhuma solicitação de acesso em aberto.",
        )
        return False
    logger.info("Access request withdrawn by the requester")
    await _retire_request_buttons(conn, request, "Retirada pela própria pessoa.")
    await _send(
        chat_id,
        f"✅ Sua solicitação {request.request_code} foi retirada e não será mais "
        "analisada.\n\nSe mudar de ideia, é só mandar qualquer mensagem: uma nova "
        "solicitação é aberta na hora.",
    )
    return True


async def _handle_pending_access(conn, message: dict, chat_id) -> None:
    # Withdrawal is read before anything else, because every other message from
    # somebody outside the firm opens or refreshes a request — including, if it
    # were read later, the very message asking to withdraw one.
    if (message.get("text") or "").strip().casefold() in ("/cancelar", "/cancel"):
        await _withdraw_access_request(conn, chat_id)
        return

    name, username = _requester_identity(message, chat_id)
    request, should_notify = await create_or_refresh_access_request(
        conn, chat_id=chat_id, name=name, username=username,
    )
    allowed, _ = _denied_limiter.check(chat_id)
    if should_notify or allowed:
        await _send(
            chat_id,
            "🔐 Seu acesso está aguardando autorização.\n"
            f"Código de solicitação: {request.request_code}\n\n"
            "Envie esse código a um admin ou ao owner. Quando o acesso for liberado, "
            "você poderá usar o assistente normalmente.\n\n"
            "Se preferir, envie /cancelar para retirar a solicitação.",
        )
    if should_notify:
        await _notify_managers(conn, request)


# What the person now being served may see in the archive. Set once per
# message and per button press, right after the staff row is known, and read
# by every catalogue query. It defaults to the firm's shelf alone, so a path
# that forgets to set it shows less than it should rather than more: the same
# rule as role normalisation, which falls to the lowest rank rather than up.
_viewer_scope: ContextVar[Scope] = ContextVar("viewer_scope", default=Scope())


def viewer() -> Scope:
    """The folders the person being served may read."""
    return _viewer_scope.get()


@contextlib.asynccontextmanager
async def _serving(conn, staff):
    """Bind this person's folders, and their conversation, for one message."""
    token = _viewer_scope.set(await scope_for(conn, getattr(staff, "id", None)))
    chat = getattr(staff, "chat_id", None)
    talking = _conversation.set(
        (conn, getattr(staff, "id", None), str(chat)) if chat is not None else None
    )
    try:
        yield
    finally:
        _conversation.reset(talking)
        _viewer_scope.reset(token)


# Who is being served, so `_send` can record what it tells them: every reply
# passes through it, the deterministic ones included. A message to another
# chat (an admin's notification) is not this conversation.
_conversation: ContextVar[tuple | None] = ContextVar("conversation", default=None)


# Sign-in flows never enter the conversation: /web and /pje answer with a
# one-time link, and in "/mfa refazer <código>" the person types a recovery
# code. Their messages are recorded as the bare command, and nothing the bot
# says while serving them is recorded at all.
_WORD_ONLY_INTENTS = frozenset({"greeting", "thanks", "help", "document_upload"})
_UNRECORDED = frozenset({"/web", "/acesso", "/mfa", "/autenticador", "/pje"})
# The second barrier, a login or PJe enrolment link wherever it appears, is
# `conversation.redact_access_links`, shared with the panel's recording.


async def _record_person(conn, staff, chat_id, text: str) -> None:
    """The person's turn; a sign-in flow's as its bare command, and nothing
    the bot answers it is recorded (the binding is restored by _serving).
    The panel wrote the person's turn with their request, so not again."""
    if channels.for_chat(chat_id).records_itself:
        return
    words = text.split(maxsplit=1)
    first = words[0].casefold() if words else ""
    if first in _UNRECORDED:
        await _record(conn, staff.id, chat_id, "person", first)
        _conversation.set(None)
        return
    await _record(conn, staff.id, chat_id, "person", text)


async def _record(conn, staff_id: int, chat_id, speaker, text: str) -> None:
    """A turn in the conversation table. Never costs the reply: a failure is
    logged by its kind only, since its message could carry the text."""
    text = conversation.redact_access_links(text)
    try:
        await conversation.record_turn(
            conn, staff_id=staff_id, chat_id=chat_id, speaker=speaker, text=text,
        )
    except Exception as exc:
        logger.warning("Conversation turn not recorded: %s", type(exc).__name__)


async def _may(chat_id, staff, action: str) -> bool:
    """Ask the authorization service, and deliver its refusal over Telegram.

    The rule about who may do what is not here — this only carries the verdict
    back to the person. A second transport reuses ``authorization.decide`` and
    answers in whatever way suits it.
    """
    decision = authorization.decide(staff, action)
    if not decision.allowed:
        await _send(chat_id, decision.reason)
    return decision.allowed


def _normalize_request_code(raw) -> str:
    """Accept 'NS-12AB34CD', 'ns 12ab34cd' or just '12ab34cd' as the same code.

    Managers dictate or retype these codes, and a voice note arrives without the
    hyphen, so the shape is rebuilt instead of rejected.
    """
    stripped = "".join(ch for ch in str(raw).upper() if ch.isalnum())
    if stripped.startswith("NS"):
        stripped = stripped[2:]
    if re.fullmatch(r"[A-F0-9]{8}", stripped):
        return f"NS-{stripped}"
    return str(raw).strip().upper()


def _parse_selection_id(raw) -> int | None:
    """Accept the bare number or the ``[number]`` shape shown in option lists."""
    return parse_int_id(str(raw).strip().removeprefix("[").removesuffix("]"))


async def _remember_pending_decision(conn, chat_id, approved: bool) -> None:
    await pending_actions.remember(
        conn,
        chat_id,
        kind="decision",
        approved=approved,
        ttl_seconds=_PENDING_DECISION_TTL,
    )


async def _take_pending_decision(conn, chat_id) -> bool | None:
    """Take the awaited decision, or None when it never existed or expired."""
    pending = await pending_actions.consume(conn, chat_id)
    if pending is None or pending.kind != "decision":
        return None
    return pending.approved


async def _remember_pending_identifier(
    conn,
    chat_id,
    action: _PendingAction,
    *,
    target_id: int | None = None,
) -> None:
    await pending_actions.remember(
        conn,
        chat_id,
        kind="identifier",
        action=action,
        target_id=target_id,
        ttl_seconds=_PENDING_DECISION_TTL,
        channel=channels.for_chat(chat_id).name,
    )


async def _ask_for_staff_id(conn, chat_id, action: _PendingAction) -> None:
    users = [
        user for user in await list_staff(conn)
        if normalize_role(user.role) != ROLE_OWNER
    ]
    if not users:
        await _send(chat_id, "Não há usuários ativos disponíveis para essa ação.")
        return
    verbs = {
        "promote": "promover",
        "demote": "rebaixar",
        "deactivate": "excluir",
    }
    await _remember_pending_identifier(conn, chat_id, action)
    lines = [
        f"[{user.id}] {user.name} ({ROLE_LABELS[normalize_role(user.role)]})"
        for user in users
    ]
    await _send(
        chat_id,
        f"Qual é o ID do usuário que você quer {verbs[action]}?\n\n"
        + "\n".join(lines)
        + "\n\nResponda somente com o ID ou envie /cancelar.",
    )


async def _ask_for_research_question(conn, chat_id) -> None:
    await _remember_pending_identifier(conn, chat_id, "research")
    await _send(
        chat_id,
        "Qual é a sua pergunta? Pesquiso em fontes públicas oficiais e trago as fontes.",
    )


async def _ask_for_draft_instructions(conn, chat_id) -> None:
    await _remember_pending_identifier(conn, chat_id, "draft")
    await _send(
        chat_id,
        "Descreva o que você quer redigir. Por exemplo, "
        '"cláusula de rescisão para contrato de prestação de serviços".',
    )


async def _ask_for_document_filter(conn, chat_id) -> None:
    await _remember_pending_identifier(conn, chat_id, "doc_filter")
    await _send(
        chat_id,
        "O que você procura? Cliente, tipo de documento ou período. Por exemplo, "
        '"contratos da Acme de 2024".',
    )


async def _ask_for_document_id(conn, chat_id, action: _PendingAction) -> None:
    if not await count_documents(conn, scope=viewer()):
        await _send(chat_id, "📂 Acervo vazio: nenhum documento foi ingerido ainda.")
        return
    verbs = {
        "original": "enviar",
        "details": "ver os detalhes de",
        "rename": "renomear",
        "edit": "editar",
        "delete_document": "excluir do acervo",
    }
    await _remember_pending_identifier(conn, chat_id, action)
    await _send_document_catalog(conn, chat_id)
    await _send(
        chat_id,
        f"Qual é o ID do documento que você quer {verbs[action]}? "
        "Responda com o ID ou envie /cancelar.",
    )


async def _resume_pending_identifier(
    conn,
    chat_id,
    text: str,
    command: str,
    pending,
) -> tuple[bool, str]:
    """Turn the awaited identifier into the corresponding deterministic command."""
    if pending is None or pending.kind != "identifier":
        return False, text
    if pending.expired:
        await pending_actions.forget(conn, chat_id)
        if not command.startswith("/"):
            await _send(
                chat_id,
                "Esse pedido expirou. Envie o comando novamente para recomeçar.",
            )
            return True, text
        return False, text
    if command in ("/cancelar", "/cancel"):
        await pending_actions.forget(conn, chat_id)
        await _send(chat_id, "Ok, essa ação foi cancelada.")
        return True, text
    if command.startswith("/"):
        await pending_actions.forget(conn, chat_id)
        return False, text

    if pending.action == "edit_fields":
        # The answer is a set of fields, not an identifier, so it is carried
        # through as written and parsed by the command it reaches.
        await pending_actions.forget(conn, chat_id)
        return False, f"/editar {pending.target_id} {text.strip()}"

    if pending.action == "rename_title":
        # The answer is the name itself, so it is carried through as text rather
        # than parsed as an identifier. The document it belongs to is the one
        # the step remembered.
        await pending_actions.forget(conn, chat_id)
        return False, f"/renomear {pending.target_id} {text.strip()}"

    if (pending.action in _CHANNEL_BOUND_CONFIRMATIONS
            and pending.channel != channels.for_chat(chat_id).name):
        # A typed word that destroys something counts only in the window that
        # asked for it (revision 11): the step stays for that window, and
        # /cancelar from either still ends it.
        where = _CHANNEL_NAMES.get(pending.channel, "outro canal")
        await _send(chat_id, f"Essa confirmação foi pedida no {where}: confirme lá "
                             "ou envie /cancelar.")
        return True, text

    if pending.action == "confirm_document_deletion":
        if command.strip() != "confirmar":
            await _send(
                chat_id,
                "Para concluir a exclusão do documento, responda exatamente "
                "confirmar ou envie /cancelar.",
            )
            return True, text
        await pending_actions.forget(conn, chat_id)
        return False, f"/excluir_documento {pending.target_id} confirmar"

    if pending.action == "confirm_erase":
        # Not "confirmar". Every other confirmation in this bot uses that word,
        # and this is the only operation that cannot be undone, so it must not
        # be reachable by the reflex that answers all the others. Read from the
        # text as typed: `command` is casefolded, and "APAGAR" in capitals is
        # the whole point of the word.
        if text.strip() != "APAGAR":
            await _send(
                chat_id,
                "Para concluir a remoção dos dados, responda exatamente APAGAR "
                "em maiúsculas, ou envie /cancelar.",
            )
            return True, text
        await pending_actions.forget(conn, chat_id)
        return False, f"/esquecer {pending.target_id} APAGAR"

    if pending.action == "confirm_deactivation":
        if command.strip() != "confirmar":
            await _send(
                chat_id,
                "Para concluir a exclusão, responda exatamente confirmar ou envie /cancelar.",
            )
            return True, text
        await pending_actions.forget(conn, chat_id)
        return False, f"/excluir_usuario {pending.target_id} confirmar"

    if pending.action in ("research", "draft"):
        # The button only opened the door; the person's own words are the
        # request. Handing the raw text back, unrewritten, is what makes it
        # walk the same specialist a typed question reaches — there is no
        # /pesquisar or /redigir command to translate into.
        await pending_actions.forget(conn, chat_id)
        return False, text

    if pending.action == "doc_filter":
        await pending_actions.forget(conn, chat_id)
        return False, f"/docs {text.strip()}"

    first, _, remainder = text.strip().partition(" ")
    if pending.action == "process":
        numbers = datajud.extract_process_numbers(text, limit=1)
        if not numbers:
            await _send(
                chat_id,
                "Não reconheci um número CNJ. Envie o número completo ou /cancelar.",
            )
            return True, text
        await pending_actions.forget(conn, chat_id)
        return False, f"/processo {numbers[0]}"

    identifier = _parse_selection_id(first)
    if identifier is None:
        await _send(chat_id, "Não reconheci esse ID. Envie somente o número ou /cancelar.")
        return True, text

    if pending.action in {"promote", "demote", "deactivate"}:
        target = await get_staff_by_id(conn, identifier)
        if not target or normalize_role(target.role) == ROLE_OWNER:
            await _send(
                chat_id,
                "Não encontrei um usuário elegível com esse ID. Tente outro ou envie /cancelar.",
            )
            return True, text
    else:
        document = await get_document_summary(conn, identifier, viewer())
        if not document:
            await _send(
                chat_id,
                "Não encontrei um documento com esse ID. Tente outro ou envie /cancelar.",
            )
            return True, text

    await pending_actions.forget(conn, chat_id)
    commands = {
        "promote": "/promover",
        "demote": "/rebaixar",
        "deactivate": "/excluir_usuario",
        "erase": "/esquecer",
        "original": "/arquivo",
        "details": "/detalhes",
        "rename": "/renomear",
        "edit": "/editar",
        "delete_document": "/excluir_documento",
    }
    return False, f"{commands[pending.action]} {identifier}"


async def _status_for(conn, chat_id) -> str:
    """What /status reports: active flow, step and next move, with no case content.

    Read-only on purpose. The flow it describes must survive being asked about,
    so nothing here consumes or expires a pending step.
    """
    pending = await pending_actions.peek(conn, chat_id)
    if pending is not None and not pending.expired:
        remaining = max(1, round(pending.seconds_left / 60))
        if pending.kind == "identifier" and pending.action in _PENDING_STEPS:
            flow, next_step = _PENDING_STEPS[pending.action]
            return (
                f"⏳ Fluxo ativo: {flow}.\n"
                f"Etapa: aguardando sua resposta (expira em ~{remaining} min).\n"
                f"Próxima ação: {next_step}, ou envie /cancelar."
            )
        if pending.kind == "decision":
            flow = "autorização de acesso" if pending.approved else "recusa de acesso"
            return (
                f"⏳ Fluxo ativo: {flow}.\n"
                f"Etapa: aguardando o código da solicitação (expira em ~{remaining} min).\n"
                "Próxima ação: envie o código, ou envie /cancelar."
            )
        if pending.kind == "action" and pending.action in _ACTIONS:
            return (
                f"⏳ Fluxo ativo: {_ACTIONS[pending.action].command}, pedido pela conversa.\n"
                f"Etapa: aguardando sua resposta (expira em ~{remaining} min).\n"
                "Próxima ação: responda a pergunta, ou envie /cancelar."
            )
        if pending.kind == "export":
            return (
                "⏳ Fluxo ativo: exportação da última resposta.\n"
                "Etapa: aguardando o formato.\n"
                "Próxima ação: responda docx, pdf ou xlsx, ou envie /cancelar."
            )

    title, _ = _last_delivery(chat_id)
    if title:
        return (
            "Nenhum fluxo aguardando resposta.\n"
            "Há uma resposta recente disponível: use /fontes para as fontes "
            "ou /exportar para receber o arquivo."
        )
    return "Nenhum fluxo em andamento. Faça um pedido ou envie /comandos para ver os comandos."


def _br_int(value: int) -> str:
    """3422 as 3.422, the way the firm writes it."""
    return f"{value:,}".replace(",", ".")


def _br_money(value: float) -> str:
    """1234.5 as 1.234,50: thousands with a dot, cents with a comma."""
    whole, cents = f"{value:,.2f}".split(".")
    return f"{whole.replace(',', '.')},{cents}"


async def _budget_status() -> str:
    """What the pilot has spent on the model, for the people who answer for it.

    Only owner and admin get this, and it carries nothing but counts: no
    question, client or document, the same rule the budget alerts follow.
    """
    if not settings.budget_enabled:
        return "💰 Orçamento do piloto: desligado nesta instalação."
    report = await budget.used()
    if not report:
        return "💰 Orçamento do piloto: o medidor não respondeu agora."
    lines = ["💰 Orçamento do piloto (visível só para owner e admin):"]
    for window, label in (("day", "Hoje"), ("month", "Este mês")):
        row = report[window]
        ceiling = budget.cost_ceiling(row["input"], row["output"])
        lines.append(
            f"{label}: {_br_int(row['input'])} de {_br_int(row['input_limit'])} tokens "
            f"de entrada ({row['input'] / row['input_limit']:.0%}), "
            f"{_br_int(row['output'])} de {_br_int(row['output_limit'])} de saída "
            f"({row['output'] / row['output_limit']:.0%}). "
            f"Custo: no máximo US$ {_br_money(ceiling)}."
        )
    return "\n".join(lines)


async def _ask_for_request_code(conn, chat_id, approved: bool) -> None:
    """Bare /autorizar or /recusar: ask which request, and wait for the answer."""
    pending = await list_pending_access_requests(conn)
    if not pending:
        await _send(chat_id, "Não há solicitações de acesso pendentes no momento.")
        return
    await _remember_pending_decision(conn, chat_id, approved)
    lines = [
        f"{request.request_code}: {request.name}"
        + (f" (@{request.username})" if request.username else "")
        for request in pending
    ]
    await _send(
        chat_id,
        f"Qual é o código da solicitação que você quer {'autorizar' if approved else 'recusar'}?\n\n"
        + "\n".join(lines)
        + f"\n\nResponda com o código (ex.: {pending[0].request_code}) ou envie /cancelar.",
    )


async def _decide_access(conn, chat_id, staff, code: str, approved: bool) -> bool:
    request = await decide_access_request(conn, code, approved=approved, decided_by=staff.id)
    if not request:
        await _send(chat_id, "Não encontrei uma solicitação pendente com esse código.")
        return False
    if approved:
        await _send(chat_id, f"✅ Acesso de {request.name} liberado, como estagiário(a).")
        await _welcome(conn, request)
        return True
    await _send(chat_id, f"Solicitação de {request.name} recusada.")
    await _send(
        request.chat_id,
        "Sua solicitação de acesso não foi aprovada. Fale com o administrador do escritório "
        "se acredita que isso foi um engano.",
    )
    return True


async def _send_original_document(conn, chat_id, document_id: int, staff_id: int) -> None:
    source = await get_document_source(conn, document_id, viewer())
    if not source:
        await _send(
            chat_id,
            "O arquivo original não está disponível para este documento. "
            "Ele pode ter sido ingerido antes da retenção de originais; envie-o novamente "
            "para arquivá-lo.",
        )
        return
    channel = channels.for_chat(chat_id)
    if channel.name != "telegram":
        # The panel links the original instead of carrying it: nothing is
        # read or decrypted here, and the download page checks the rank and
        # the scope again when used, writing decrypt and download then.
        await channel.send_file(
            chat_id, source.source_filename, b"",
            f"Arquivo original do documento [{document_id}]",
            kind="original", document_id=document_id,
        )
        return
    data = await document_storage.read(
        source.source_storage_key, source.source_wrapped_key,
        raw_sha256=source.source_sha256,
    )
    if not data:
        logger.error("Original source missing for document_id=%s", document_id)
        await _send(chat_id, "Não localizei o arquivo original. Avise o administrador para verificar o backup.")
        return
    # Recorded before delivery, and separately from it, because these are two
    # different facts and an audit needs both. This one says the original was
    # reconstituted in clear on this host for this person, which is true even
    # if Telegram then fails to deliver it. `download_original` below says it
    # reached them. With the archive in a bucket the first is the one that
    # answers "who saw this document", since the bucket only ever held
    # ciphertext.
    await log_access(
        conn,
        action="decrypt_original",
        staff_id=staff_id,
        chat_id=chat_id,
        document_ids=[document_id],
    )
    await channels.for_chat(chat_id).typing(chat_id)
    sent = await channels.for_chat(chat_id).send_file(
        chat_id, source.source_filename, data,
        f"Arquivo original do documento [{document_id}]",
        kind="original", document_id=document_id,
    )
    if not sent:
        await _send(chat_id, "Não consegui enviar o arquivo original. Tente novamente em instantes.")
        return
    await log_access(
        conn,
        action="download_original",
        staff_id=staff_id,
        chat_id=chat_id,
        document_ids=[document_id],
    )


# ── Following a case: what DataJud knows, and where to open the real thing ───

async def _djen_section(number: str) -> tuple[str, list]:
    """What the DJEN published about the process, for /processo.

    DataJud says what moved; this says what was published, which is where a
    deadline comes from. A failure here costs this section, never the answer.
    """
    if not settings.djen_enabled:
        return "", []
    try:
        communications = await djen.communications(number)
    except djen.DjenRefused as exc:
        logger.warning("DJEN lookup failed: %s", exc)
        return (
            "\n\n📰 O DJEN recusou a consulta feita pelo servidor do assistente, então "
            "as publicações não vieram. Elas podem ser lidas pelo número do processo "
            f"em {djen.PUBLIC_SITE}",
            [],
        )
    except (djen.DjenError, ValueError) as exc:
        # Both messages are fixed text; neither carries the process number.
        logger.warning("DJEN lookup failed: %s", exc)
        return (
            "\n\n📰 O DJEN não respondeu agora. As publicações podem ser lidas pelo "
            f"número do processo em {djen.PUBLIC_SITE}",
            [],
        )
    return (
        "\n\n" + djen.format_for_staff(number, communications),
        [sources_module.from_djen(c) for c in communications],
    )


async def _report_process(conn, chat_id, staff, number: str) -> None:
    """Public metadata and movements, the publications, and where to open it."""
    await channels.for_chat(chat_id).typing(chat_id)
    publications, publication_sources = await _djen_section(number)
    try:
        lookup = await datajud.lookup_process(number)
    except (datajud.DataJudError, ValueError) as exc:
        logger.info("DataJud lookup failed: %s", exc)
        await _send(
            chat_id,
            "Não consegui consultar o DataJud agora. "
            + process_links.describe(number)
            + publications,
        )
        return

    await log_access(
        conn, action="process_lookup", staff_id=staff.id, chat_id=chat_id,
        query=lookup.process_number,
    )
    link = process_links.consultation_link(lookup.process_number)
    reply = AgentReply(
        text=datajud.format_for_staff(lookup) + publications,
        task="process_lookup",
        sources=publication_sources + [
            sources_module.from_datajud(link.formatted_number, lookup.endpoint),
            sources_module.Source(
                kind="web",
                title=link.system,
                reference=link.url,
                detail="Consulta processual oficial do tribunal: é onde estão as "
                       "peças do processo, que o DataJud não fornece.",
            ),
        ],
    )
    await _deliver(chat_id, reply, f"Processo {link.formatted_number}",
                   conn=conn, staff=staff)


_FORCE_WORDS = {"forçar", "forcar", "force"}


async def _refresh_from_official(conn, chat_id, staff, arguments: list[str]) -> None:
    """Link a law to its official page, or compare it with the page now.

    `/atualizar 7 <url>` links and checks, `/atualizar 7` checks the page
    already linked, and `forçar` applies a change the size guard held back.
    """
    document_id = parse_int_id(arguments[0]) if arguments else None
    if document_id is None:
        await _send(
            chat_id,
            "Uso: /atualizar <id> [url oficial]\n"
            "Ligo a lei à página oficial dela (Planalto, por exemplo) e confiro toda "
            "semana. Quando o texto mudar, atualizo o documento no acervo.\n"
            "Ex.: /atualizar 7 https://www.planalto.gov.br/ccivil_03/constituicao/"
            "constituicao.htm",
        )
        return
    rest = arguments[1:]
    force = any(word.casefold() in _FORCE_WORDS for word in rest)
    url = next((word for word in rest if word.lower().startswith("https://")), None)
    if rest and url is None and not force:
        await _send(chat_id, "Envie uma URL https de fonte oficial (Planalto, tribunal, LexML).")
        return

    await channels.for_chat(chat_id).typing(chat_id)
    try:
        result = await refresh_document(
            conn, document_id, url=url, force=force, staff_id=staff.id,
        )
    except LookupError:
        document = await get_document_summary(conn, document_id, viewer())
        if document is None:
            await _send(chat_id, f"Não encontrei o documento {document_id} no acervo.")
        else:
            await _send(
                chat_id,
                f"O documento [{document_id}] ainda não está ligado a uma página "
                f"oficial. Envie /atualizar {document_id} <url oficial>.",
            )
        return
    except (web_fetch.FetchError, ValueError) as exc:
        logger.info("Refresh of document %s refused: %s", document_id, exc)
        await _send(
            chat_id,
            f"Não consegui ler a página oficial: {exc}. Nada foi alterado. Aceito "
            "apenas URLs https de domínios oficiais autorizados.",
        )
        return
    except RuntimeError:
        logger.exception("Refresh of document %s failed", document_id)
        await _send(
            chat_id,
            "Não consegui reindexar o documento agora. Nada foi alterado; tente de "
            "novo em alguns minutos.",
        )
        return

    label = f"[{document_id}] {result.title}"
    if result.outcome == "unchanged":
        await _send(
            chat_id,
            f"✅ {label} está igual à página oficial. Confiro de novo em "
            f"{settings.law_refresh_days} dias.",
        )
    elif result.outcome == "updated":
        await _send(
            chat_id,
            f"✅ {label} atualizado a partir da página oficial: "
            f"{result.chunks_after} trechos. Confiro de novo em "
            f"{settings.law_refresh_days} dias.",
        )
    else:
        direction = "maior" if result.change > 0 else "menor"
        forced = f"/atualizar {document_id}" + (f" {url}" if url else "") + " forçar"
        await _send(
            chat_id,
            f"⚠️ O texto da página oficial é {abs(result.change) * 100:.0f}% {direction} "
            f"que o de {label}. Isso costuma ser URL errada ou página incompleta, não "
            f"mudança da lei, então não alterei nada"
            + ("" if url is None else " nem guardei essa URL")
            + f".\nSe a página estiver certa, envie: {forced}",
        )


# ── Folders: mine, my team's, the firm's ─────────────────────────────────────

def _folder_label(visibility: str, team_name: str | None) -> str:
    if visibility == "firm":
        return "escritório"
    if visibility == "team":
        return f"equipe {team_name}" if team_name else "equipe"
    return "pessoal"


async def _claim_document(conn, chat_id, staff, arguments: list[str]) -> None:
    """Take one document out of whoever's folder it is in, into the owner's.

    The folders exist so a client's papers stay between the lawyer and the
    client, and this is the one door through that wall. It is the owner's
    alone, and it is open for a reason the firm answers for: the case file
    belongs to the firm, and a resignation, an illness or a death must not
    take a client's papers out of reach.

    What keeps it from becoming a way to read colleagues' folders is not the
    rank, which only says who may. It is that the reach is narrow, one named
    id at a time with no listing to browse from, that every use is written to
    the audit log, and that **the person it was taken from is told**. An owner
    can reach, and cannot do it quietly.
    """
    document_id = parse_int_id(arguments[0]) if arguments else None
    if document_id is None:
        await _send(
            chat_id,
            "Uso: /assumir <id>\n"
            "Traz para a sua pasta pessoal um documento que está na pasta de "
            "outra pessoa, por exemplo quando alguém sai do escritório. "
            "Quem enviou é avisado, e fica registrado.",
        )
        return

    taken = await take_over_document(conn, document_id, new_owner_id=staff.id)
    if taken is None:
        await _send(chat_id, f"Não existe documento {document_id}.")
        return
    title, previous = taken

    await log_access(
        conn,
        action="claim_document",
        staff_id=staff.id,
        chat_id=chat_id,
        query=f"de staff {previous}" if previous else "sem dono anterior",
        document_ids=[document_id],
    )

    if previous == staff.id:
        await _send(chat_id, f"O documento [{document_id}] já era seu.")
        return

    await _send(
        chat_id,
        f"Assumi o documento [{document_id}] “{title}”. Ele está na sua pasta "
        f"pessoal agora.\n\n"
        f"Para levar ao escritório: /compartilhar {document_id} escritorio\n"
        f"Quem o enviou foi avisado, e a operação ficou registrada.",
    )

    # Telling them is the control, so a failure to tell is worth a log line
    # rather than being swallowed: an unannounced takeover is the thing this
    # design promises does not happen.
    if previous:
        holder = await get_staff_by_id(conn, previous)
        if holder is not None and getattr(holder, "chat_id", None):
            try:
                await _send(
                    holder.chat_id,
                    f"{staff.name} assumiu o documento [{document_id}] "
                    f"“{title}”, que estava na sua pasta pessoal. "
                    f"Isso é uma ação do responsável pelo escritório e fica "
                    f"registrada no histórico de acessos.",
                )
            except Exception:
                logger.exception(
                    "Claimed document %s but could not tell staff %s",
                    document_id, previous,
                )
        else:
            logger.info(
                "Claimed document %s from staff %s, who has no active chat to tell",
                document_id, previous,
            )


async def _share_document(conn, chat_id, staff, arguments: list[str]) -> None:
    """Move one document between the personal folder, a team's and the firm's.

    Who may move it is decided by who put it there, not by rank: a document
    somebody uploaded is theirs to share, and a document nobody uploaded (the
    shared inbox) is the firm's, so moving it answers to the rank that may
    edit the catalogue. The rule itself is `folders.decide_move`, shared with
    the panel; this only reads the command and words the answer.
    """
    document_id = parse_int_id(arguments[0]) if arguments else None
    destination = " ".join(arguments[1:]).strip()
    if document_id is None or not destination:
        await _send(
            chat_id,
            "Uso: /compartilhar <id> <nome da equipe|escritorio|privado>\n"
            "Ex.: /compartilhar 12 escritorio, ou /compartilhar 12 Trabalhista.\n"
            "Documento que você envia é pessoal até você compartilhar.",
        )
        return

    place = await folders.resolve_destination(conn, destination)
    if place is None:
        # Whether the document is theirs to move is answered first, as it
        # always was: a document they cannot see is "not found", whatever
        # they typed after it.
        source = await folders.check_source(conn, staff, viewer(), document_id)
        if not source.allowed:
            await _send(chat_id, source.reason)
            return
        teams = await list_teams(conn)
        known = ", ".join(name for _id, name, _count in teams) or "nenhuma ainda"
        await _send(
            chat_id,
            f"Não conheço a equipe “{destination}”. Equipes: {known}.\n"
            "Ou use: /compartilhar <id> escritorio",
        )
        return

    result = await folders.move(conn, staff, viewer(), document_id, place, chat_id=chat_id)
    if not result.moved:
        await _send(chat_id, result.reason)
        return
    await _send(
        chat_id,
        f"✅ [{document_id}] {result.title} agora está na pasta: {result.label}."
        + ("\nTodo mundo do escritório passa a encontrá-lo nas buscas."
           if place.kind == "firm" else ""),
    )


async def _handle_deadline(conn, chat_id, arguments: list[str]) -> None:
    """/prazo: when a deadline falls, with the count laid out to be checked.

    The calculator gets the firm's own non-working days on top of the national
    ones. Every firm holiday from the start date on is passed, rather than a
    window guessed here, so this cannot drift from how far the calculator
    itself looks ahead.
    """
    request = deadline_request.parse(arguments, local_now().date())
    if isinstance(request, deadline_request.Incomplete):
        await _send(chat_id, request.message)
        return
    local = await firm_holidays.holidays_between(conn, request.start, date.max)
    prazo = calcular_prazo(
        request.start,
        deadline_request.counted_days(request),
        regime=request.regime,
        calendario=calendario_do_escritorio((day.day, day.reason) for day in local),
    )
    on_file = await firm_holidays.holidays_between(
        conn, date(request.start.year, 1, 1), date(prazo.vencimento.year, 12, 31),
    )
    await _send(chat_id, deadline_request.describe(
        request, prazo, local_days_on_file=bool(on_file),
    ))


async def _handle_holidays(conn, chat_id, staff, command_name: str,
                           arguments: list[str]) -> None:
    """/feriados lists the firm's calendar; /feriado changes it (admin)."""
    today = local_now().date()
    if command_name == "/feriados" and not arguments:
        days = await firm_holidays.holidays_between(conn, today, today + timedelta(days=365))
        if not days:
            await _send(
                chat_id,
                "Nenhum feriado local cadastrado para os próximos 12 meses. O /prazo "
                "já conta sozinho os feriados nacionais e os dias que a Justiça "
                "costuma fechar (Carnaval, Sexta-feira Santa e Corpus Christi). "
                "Feriados do Pará, de Belém, do TJPA e suspensões por portaria "
                "precisam ser cadastrados por um admin com /feriado <data> <nome>.",
            )
            return
        listed = "\n".join(f"{day.day:%d/%m/%Y}: {day.reason}" for day in days)
        await _send(
            chat_id,
            f"📅 Feriados e suspensões do escritório (próximos 12 meses):\n\n{listed}\n\n"
            "Os nacionais, o Carnaval, a Sexta-feira Santa e o Corpus Christi não "
            "aparecem aqui: o /prazo já os conta sozinho.",
        )
        return

    if not await _may(chat_id, staff, "calendar.manage"):
        return
    removing = bool(arguments) and arguments[0].casefold() in ("remover", "apagar", "tirar")
    rest = arguments[1:] if removing else arguments
    day = deadline_request.parse_date(rest[0], today) if rest else None
    if day is None:
        await _send(
            chat_id,
            "Uso:\n/feriado 15/08/2026 Adesão do Pará\n/feriado remover 15/08/2026",
        )
        return
    if removing:
        reason = await firm_holidays.remove_holiday(conn, day)
        await _send(
            chat_id,
            f"Removido do calendário do escritório: {day:%d/%m/%Y} ({reason})."
            if reason is not None else f"{day:%d/%m/%Y} não estava cadastrado.",
        )
        return
    name = " ".join(rest[1:]).strip()
    if not name:
        await _send(chat_id, f"Qual é o nome do dia? Ex.: /feriado {day:%d/%m/%Y} Adesão do Pará")
        return
    previous = await firm_holidays.add_holiday(conn, day, name[:200], added_by=staff.id)
    if previous is None:
        await _send(chat_id, f"Cadastrado: {day:%d/%m/%Y}, {name}. Não conta como dia útil no /prazo.")
    else:
        await _send(chat_id, f"Atualizado: {day:%d/%m/%Y} era \"{previous}\", agora \"{name}\".")


async def _handle_teams(conn, chat_id, staff, arguments: list[str]) -> None:
    """List the teams, or change them when the rank allows it."""
    action = (arguments[0].casefold() if arguments else "")
    rest = arguments[1:]

    if action in ("criar", "nova", "create"):
        if not await _may(chat_id, staff, "team.manage"):
            return
        name = " ".join(rest).strip()
        if not name:
            await _send(chat_id, "Uso: /grupos criar <nome>")
            return
        team_id = await create_team(conn, name, created_by=staff.id)
        if team_id is None:
            await _send(chat_id, f"Já existe uma equipe chamada {name}.")
            return
        await add_member(conn, team_id, staff.id)
        await _send(
            chat_id,
            f"✅ Equipe {name} criada, e você já está nela.\n"
            f"Para incluir alguém: /grupos adicionar {name} <id do usuário>\n"
            "Os ids aparecem em /usuarios.",
        )
        return

    if action in ("adicionar", "incluir", "add", "remover", "tirar", "remove"):
        if not await _may(chat_id, staff, "team.manage"):
            return
        staff_id = parse_int_id(rest[-1]) if rest else None
        name = " ".join(rest[:-1]).strip()
        if staff_id is None or not name:
            await _send(chat_id, f"Uso: /grupos {action} <nome da equipe> <id do usuário>")
            return
        team = await find_team(conn, name)
        if team is None:
            await _send(chat_id, f"Não conheço a equipe “{name}”.")
            return
        team_id, team_name = team
        if action in ("adicionar", "incluir", "add"):
            added = await add_member(conn, team_id, staff_id)
            await _send(chat_id, f"✅ Usuário {staff_id} na equipe {team_name}."
                        if added else f"O usuário {staff_id} já estava na equipe {team_name}.")
        else:
            removed = await remove_member(conn, team_id, staff_id)
            await _send(chat_id, f"✅ Usuário {staff_id} saiu da equipe {team_name}."
                        if removed else f"O usuário {staff_id} não estava na equipe {team_name}.")
        return

    teams = await list_teams(conn)
    mine = {team_id for team_id, _ in await teams_of(conn, staff.id)}
    if not teams:
        await _send(
            chat_id,
            "Ainda não há equipes. Cada pessoa tem a própria pasta, e o que é do "
            "escritório todo mundo vê.\n"
            "Um admin cria uma equipe com: /grupos criar <nome>",
        )
        return
    lines = ["👥 Equipes:"]
    for team_id, name, members in teams:
        mark = " (você)" if team_id in mine else ""
        lines.append(f"• {name}: {members} pessoa(s){mark}")
    lines.append("\nPara compartilhar um documento com a sua equipe: "
                 "/compartilhar <id> <nome da equipe>")
    await _send(chat_id, "\n".join(lines))


# ── Connecting a Microsoft account ───────────────────────────────────────────

# One sign-in at a time per person. A second /microsoft while the first code is
# still good would print a code that the first task is not waiting on, and the
# person would sign in against a flow nobody is polling.
_MS_SIGNING_IN: set[int] = set()
# Kept so shutdown can cancel them rather than leaving the loop running into a
# closed pool. A cancelled sign-in costs the person one retry.
_MS_TASKS: set[asyncio.Task] = set()


async def _await_microsoft_sign_in(flow, chat_id, staff_id: int) -> None:
    """The background sign-in, cut loose from the update that started it.

    The task inherits the worker's context when created, including the
    conversation binding and its connection, which is long closed by the time
    the person finishes signing in. It answers by Telegram, on a connection of
    its own, and records nothing through the old one.
    """
    _conversation.set(None)
    with channels.serving(channels.TELEGRAM, chat_id):
        await _finish_microsoft_sign_in(flow, chat_id, staff_id)


async def _finish_microsoft_sign_in(flow, chat_id, staff_id: int) -> None:
    """Wait for the person to sign in at Microsoft, then store the connection.

    Runs as its own task: the sign-in takes minutes and the update worker
    serves one message at a time, so waiting here would stop the assistant
    answering anybody until this person finished typing a code.
    """
    try:
        tokens = await microsoft.wait_for_sign_in(flow)
        account = await microsoft.whoami(tokens.access_token)
        pool = await get_pool()
        async with pool.connection() as conn:
            await save_account(
                conn, staff_id, account_name=account, refresh_token=tokens.refresh_token,
            )
        await _send(
            chat_id,
            f"✅ Conta Microsoft conectada: {account}.\n"
            "Só leitura dos seus arquivos, e só para você. Para desconectar aqui: "
            "/microsoft desconectar. Para revogar do lado da Microsoft, use a página "
            "de segurança da sua conta.",
        )
    except microsoft.SignInDeclined:
        await _send(
            chat_id,
            "O código expirou ou o acesso foi recusado. Nada foi conectado. "
            "Mande /microsoft de novo quando quiser tentar.",
        )
    except TokenSealError as exc:
        logger.error("Microsoft token could not be sealed: %s", exc)
        await _send(
            chat_id,
            "Você entrou na Microsoft, mas eu não consegui guardar a conexão com "
            "segurança. Avise o administrador; nada foi salvo.",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Microsoft sign-in failed for staff %s", staff_id)
        await _send(chat_id, "Não consegui concluir a conexão com a Microsoft. Tente de novo.")
    finally:
        _MS_SIGNING_IN.discard(staff_id)


async def _handle_web(conn, chat_id, staff) -> None:
    """Send a one-time link to the web pages.

    Holding a live link is the first factor: the bot hands it only to the
    Telegram account bound to this staff row. The link is sent with previews
    off, so Telegram's servers do not visit it before the person does.
    """
    if not settings.web_enabled:
        await _send(chat_id, "As páginas web ainda não estão ligadas neste servidor.")
        return
    base_url = pages_base_url()
    if not base_url:
        logger.error("A web login was asked for, but neither WEB_BASE_URL nor "
                     "PUBLIC_BASE_URL is set")
        await _send(chat_id, "Falta configurar o endereço público do servidor. "
                             "Avise o administrador.")
        return
    try:
        token = await web_access.open_login_link(
            conn, staff.id, ttl_seconds=settings.web_login_link_ttl,
        )
    except Exception:
        logger.exception("Could not open a web login link")
        await _send(chat_id, "Não consegui gerar o link agora. Tente em instantes.")
        return
    minutes = max(1, int(settings.web_login_link_ttl // 60))
    await _send(
        chat_id,
        f"Seu link de acesso, válido por {minutes} minutos e uma vez só:\n\n"
        f"{base_url}/web/login/{token}\n\n"
        "Não encaminhe: quem abrir primeiro usa. Se for o primeiro acesso, "
        "a página vai te guiar para configurar o autenticador.",
        preview=False,
    )


async def _handle_mfa(conn, chat_id, staff, arguments: list[str]) -> None:
    """Say whether the second factor is set up, or start it over.

    Setting it up happens on the web page, never here: the Telegram account
    is the first factor, and a secret sent through it would hand both factors
    to whoever controlled the account.

    For the same reason this account alone cannot start it over. If it could,
    whoever held the Telegram would drop the authenticator, set up their own,
    and the second factor would have protected nothing. So starting over asks
    for a code the person still has, and someone who has lost everything goes
    through the owner, whose account is a different one.
    """
    word = (arguments[0].casefold() if arguments else "")
    if word == "liberar":
        await _release_second_factor(conn, chat_id, staff, arguments[1:])
        return

    enrolment = await web_access.enrolment_of(conn, staff.id)
    if word in ("refazer", "resetar", "remover", "perdi"):
        if enrolment is not None and enrolment.confirmed:
            code = "".join(arguments[1:])
            if not code:
                await _send(
                    chat_id,
                    "Para refazer, mande um código atual do autenticador ou um "
                    "código de recuperação: /mfa refazer <código>\n\n"
                    "Perdeu os dois? Peça ao responsável pelo escritório.",
                )
                return
            verdict = await second_factor.check(conn, staff.id, code)
            if verdict is second_factor.Check.LOCKED:
                await _send(chat_id, "Muitos códigos errados seguidos. Tente de novo "
                                     "em alguns minutos.")
                return
            if verdict is not second_factor.Check.ACCEPTED:
                await _send(chat_id, "Esse código não foi aceito.")
                return
        await web_access.forget_enrolment(conn, staff.id)
        await log_access(conn, action="reset_second_factor", staff_id=staff.id,
                         chat_id=chat_id, query="pelo próprio titular", document_ids=[])
        await _send(
            chat_id,
            "Pronto: o autenticador antigo, os códigos de recuperação e todas "
            "as sessões abertas foram desfeitos.\n\n"
            "Para configurar de novo, peça um link com /web.",
        )
        return

    if enrolment is None or not enrolment.confirmed:
        await _send(
            chat_id,
            "Você ainda não configurou o autenticador. Peça um link com /web: "
            "no primeiro acesso a página te guia.",
        )
        return
    await _send(
        chat_id,
        f"🔐 Autenticador configurado em "
        f"{local_now(enrolment.enrolled_at):%d/%m/%Y}.\n"
        f"Códigos de recuperação restantes: {enrolment.recovery_left}.\n\n"
        "Trocou de celular? /mfa refazer <código>",
    )


async def _release_second_factor(conn, chat_id, staff, arguments: list[str]) -> None:
    """The owner starts someone else's second factor over.

    For the person who lost both the phone and the codes. The owner is asked
    to speak to them first, by some channel other than this bot, because a
    request arriving from their Telegram is exactly what an attacker holding
    it would send. The person is told, and the reset is written down.
    """
    if not await _may(chat_id, staff, "web.release"):
        return
    target_id = parse_int_id(arguments[0]) if arguments else None
    if target_id is None:
        await _send(
            chat_id,
            "Uso: /mfa liberar <id>\n"
            "Desfaz o autenticador de outra pessoa, para ela configurar de novo. "
            "Antes, confirme com ela por telefone ou pessoalmente: um pedido "
            "vindo do Telegram dela é o que alguém com o celular dela mandaria.",
        )
        return
    target = await get_staff_by_id(conn, target_id)
    if target is None:
        await _send(chat_id, f"Não existe pessoa ativa com id {target_id}.")
        return
    await web_access.forget_enrolment(conn, target.id)
    await log_access(conn, action="reset_second_factor", staff_id=staff.id,
                     chat_id=chat_id, query=f"de staff {target.id}", document_ids=[])
    await _send(chat_id, f"Autenticador de {target.name} desfeito, com as sessões "
                         f"abertas. Ela configura de novo com /web.")
    if target.chat_id and target.id != staff.id:
        try:
            await _send(
                target.chat_id,
                f"{staff.name} desfez o seu autenticador das páginas. Para "
                "configurar de novo, peça um link com /web. Se você não pediu "
                "isso, avise o escritório agora.",
            )
        except Exception:
            logger.exception("Released staff %s but could not tell them", target.id)


async def _handle_pje(conn, chat_id, staff, arguments: list[str]) -> None:
    """Enrol, show or remove this person's PJe access.

    The credential itself is never taken here. The command hands out a link to
    a page this host serves, because a password typed in the chat is on
    Telegram's servers, in our queue and in the history on the phone, and we
    control none of those. If somebody types a password here anyway, the reply
    says to change it: a secret that reached the chat has to be treated as
    having left.
    """
    word = (arguments[0].casefold() if arguments else "")

    if word in pje_service.REMOVE_WORDS:
        if await pje_credentials.forget_credential(conn, staff.id):
            await _send(
                chat_id,
                "Acesso ao PJe removido daqui. Se quiser, troque a senha no "
                "próprio PJe: é o único jeito de invalidar o que já foi usado.",
            )
        else:
            await _send(chat_id, "Você não tem acesso ao PJe cadastrado.")
        return

    if not settings.pje_enabled:
        await _send(
            chat_id,
            "O acesso ao PJe ainda não está configurado neste servidor. "
            "Avise o administrador.",
        )
        return

    base_url = pages_base_url()
    if not base_url:
        logger.error("PJe enrolment asked for, but neither WEB_BASE_URL nor "
                     "PUBLIC_BASE_URL is set")
        await _send(
            chat_id,
            "Não consigo gerar o link de cadastro porque falta configurar o "
            "endereço público do servidor. Avise o administrador.",
        )
        return

    credential = await pje_credentials.get_credential(conn, staff.id)
    if credential and word not in pje_service.ENROL_WORDS:
        used = (f"Último uso: {local_now(credential.last_used_at):%d/%m/%Y}"
                if credential.last_used_at else "Ainda não usei esse acesso.")
        await _send(
            chat_id,
            f"⚖️ Acesso ao {credential.court} cadastrado (CPF "
            f"{credential.cpf[:3]}.***.***-{credential.cpf[-2:]}).\n"
            f"Cadastrado em: {local_now(credential.enrolled_at):%d/%m/%Y}\n"
            f"{used}\n\n"
            "Para trocar: /pje cadastrar\nPara remover: /pje remover",
        )
        return

    if channels.for_chat(chat_id).name != channels.TELEGRAM.name:
        # The panel records every turn it answers, so no token goes in one:
        # the link leads to a page that opens the enrolment behind the
        # signed-in session, on a POST (`core/web/pje_panel.py`).
        await _send(
            chat_id,
            f"Para cadastrar o seu acesso ao {settings.pje_court}, abra a página "
            f"de cadastro no painel:\n\n{base_url}{pje_service.PANEL_PAGE}\n\n"
            "Digite a senha só na página, nunca aqui no chat.",
            preview=False,
        )
        return

    try:
        token = await pje_service.open_enrolment(conn, staff.id)
    except Exception:
        logger.exception("Could not open a PJe enrolment")
        await _send(chat_id, "Não consegui gerar o link agora. Tente em instantes.")
        return

    minutes = pje_service.enrolment_minutes()
    await _send(
        chat_id,
        f"Abra este link para cadastrar o seu acesso ao {settings.pje_court}:\n\n"
        f"{base_url}/pje/{token}\n\n"
        f"Ele vale uma vez só e expira em {minutes} minutos. "
        "Digite a senha na página, nunca aqui no chat: o que passa por aqui "
        "fica no histórico do Telegram, que não é nosso para apagar.",
        preview=False,
    )


async def _handle_microsoft(conn, chat_id, staff, arguments: list[str]) -> None:
    """Connect, show, or drop this person's own Microsoft account."""
    word = (arguments[0].casefold() if arguments else "")

    if word in ("desconectar", "desconecta", "sair", "remover"):
        if await forget_account(conn, staff.id):
            await _send(
                chat_id,
                "Pronto, desconectei a sua conta Microsoft daqui.\n"
                "Isso apaga o acesso guardado deste lado. Para cancelar a autorização "
                "na própria Microsoft, use a página de segurança da sua conta.",
            )
        else:
            await _send(chat_id, "Você não tem nenhuma conta Microsoft conectada.")
        return

    account = await get_account(conn, staff.id)
    if account and not word:
        used = (f"Último uso: {local_now(account.last_used_at):%d/%m/%Y}"
                if account.last_used_at else "Ainda não usei essa conexão.")
        await _send(
            chat_id,
            f"🔗 Conta Microsoft conectada: {account.account_name}\n"
            f"Conectada em: {local_now(account.connected_at):%d/%m/%Y}\n"
            f"{used}\n\n"
            "Para desconectar: /microsoft desconectar",
        )
        return

    if not (settings.ms_enabled and microsoft.configured()):
        await _send(
            chat_id,
            "A conexão com a Microsoft ainda não está configurada neste servidor. "
            "Avise o administrador.",
        )
        return

    if staff.id in _MS_SIGNING_IN:
        await _send(
            chat_id,
            "Já mandei um código para você agora há pouco e ainda estou esperando. "
            "Use aquele código, ou espere alguns minutos para pedir outro.",
        )
        return

    try:
        flow = await microsoft.start_device_flow()
    except microsoft.MicrosoftError as exc:
        logger.warning("Could not start a Microsoft sign-in: %s", exc)
        await _send(chat_id, "Não consegui iniciar a conexão com a Microsoft agora. Tente de novo.")
        return

    _MS_SIGNING_IN.add(staff.id)
    task = asyncio.create_task(
        _await_microsoft_sign_in(flow, chat_id, staff.id), name=f"ms-signin-{staff.id}",
    )
    _MS_TASKS.add(task)
    task.add_done_callback(_MS_TASKS.discard)

    await _send(
        chat_id,
        f"🔗 Para conectar a sua conta Microsoft:\n\n"
        f"1. Abra {flow.verification_uri} (no celular ou no computador)\n"
        f"2. Digite o código: {flow.user_code}\n"
        f"3. Entre com a sua conta e autorize\n\n"
        f"O código vale {flow.expires_in // 60} minutos. Eu aviso aqui quando terminar.\n"
        "A sua senha não passa por mim: você digita só no site da Microsoft. "
        "Eu recebo permissão de leitura dos seus arquivos, que você pode revogar quando quiser.",
    )


# The last listing shown to each chat, so "trazer 3" means something. In
# process memory, like the last delivered answer: a restart costs one /onedrive.
# Keyed by str(chat_id), like every per-chat memory here: Telegram's chat id
# arrives as a number and the panel's as text, and they are the same person.
_LAST_DRIVE_LISTING: dict = {}


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, size // 1024)} KB"


# The documents last listed for a process, per chat, so "trazer 2" means the
# second line the person is looking at. Same shape as the drive listing, and
# in process for the same reason: it is a shortcut for the message on screen,
# not a record, and a deploy losing it costs one repeated command.
_LAST_PROCESS_LISTING: dict = {}


async def _handle_intimacoes(conn, chat_id, staff) -> None:
    """Say what is waiting in the PJe, and refuse to open it.

    The refusal is the feature. Consulting the teor of a pending intimation is
    ciência under Lei 11.419/2006 art. 5º and starts the deadline, so an agent
    that helpfully summarised them would start deadlines nobody chose to
    start. The message says that, because a lawyer who does not know why the
    agent stops short will ask it to go further.
    """
    await channels.for_chat(chat_id).typing(chat_id)
    try:
        notices = await pje_service.pending_notices(conn, staff.id)
    except pje_service.NotEnrolled:
        await _send(
            chat_id,
            "Você ainda não cadastrou o seu acesso ao PJe, ou ele parou de "
            "funcionar. Mande /pje para cadastrar.",
        )
        return
    except pje_mni.MNIError as exc:
        logger.info("Pending notices lookup failed: %s", exc)
        await _send(chat_id, f"Não consegui consultar o PJe agora: {exc}")
        return

    lines = [
        f"{n + 1}. processo {notice.process_number or 'não informado'}"
        + (f", {notice.kind}" if notice.kind else "")
        + (f", disponibilizada em {notice.made_available}"
           if notice.made_available else "")
        for n, notice in enumerate(notices)
    ]
    message = pje_mni.deadline_warning(notices)
    await _send(chat_id, message + ("\n\n" + "\n".join(lines) if lines else ""))


async def _handle_autos(conn, chat_id, staff, arguments: list[str]) -> None:
    """Show a process from the PJe, and bring one of its documents in."""
    if arguments and arguments[0].casefold() in ("trazer", "importar"):
        await _import_from_process(conn, chat_id, staff, arguments[1:])
        return

    number = " ".join(arguments).strip()
    if not number:
        await _send(chat_id, "Diga o número do processo: /autos 0000000-00.0000.0.00.0000")
        return

    await channels.for_chat(chat_id).typing(chat_id)
    try:
        found = await pje_service.process(conn, staff.id, number)
    except ValueError:
        await _send(chat_id, "Esse número não tem os 20 dígitos de um número CNJ.")
        return
    except pje_service.NotEnrolled:
        await _send(
            chat_id,
            "Você ainda não cadastrou o seu acesso ao PJe, ou o tribunal recusou "
            "as credenciais. Mande /pje para cadastrar.",
        )
        return
    except pje_mni.MNIError as exc:
        logger.info("Process lookup on the PJe failed: %s", exc)
        await _send(chat_id, f"Não consegui consultar o processo agora: {exc}")
        return

    head = [f"⚖️ Processo {number}"]
    if found.court:
        head.append(f"Tribunal: {found.court}")
    if found.matter:
        head.append(f"Classe: {found.matter}")
    for movement in found.movements[:5]:
        head.append(f"• {movement.happened_at} {movement.description}".rstrip())

    _LAST_PROCESS_LISTING[str(chat_id)] = (found.number, found.documents)
    if found.documents:
        head.append("\nDocumentos:")
        head += [
            f"{n + 1}. {document.described()}"
            for n, document in enumerate(found.documents[:20])
        ]
        head.append("\nPara trazer um para a sua pasta pessoal: /autos trazer <n>")
    await _send(chat_id, "\n".join(head))


async def _import_from_process(conn, chat_id, staff, arguments: list[str]) -> None:
    """Bring the nth document of the last listed process into the archive."""
    number, documents = _LAST_PROCESS_LISTING.get(str(chat_id), ("", []))
    if not documents:
        await _send(chat_id, "Peça o processo primeiro: /autos <número>.")
        return
    try:
        position = int((arguments[0] if arguments else "").strip())
    except ValueError:
        await _send(chat_id, "Diga o número da linha: /autos trazer 2")
        return
    if not 1 <= position <= len(documents):
        await _send(chat_id, f"Escolha um número entre 1 e {len(documents)}.")
        return

    await channels.for_chat(chat_id).typing(chat_id)
    try:
        result = await pje_service.import_document(
            conn, staff.id, number=number, document=documents[position - 1],
        )
    except pje_service.NotEnrolled:
        await _send(chat_id, "O tribunal recusou as credenciais. Mande /pje de novo.")
        return
    except pje_mni.MNIError as exc:
        logger.info("Importing a process document failed: %s", exc)
        await _send(chat_id, f"Não consegui trazer esse documento: {exc}")
        return

    await _send(
        chat_id,
        f"Trouxe para a sua pasta pessoal como [{result.document_id}], em "
        f"{result.chunk_count} trechos. Para compartilhar com a equipe ou o "
        f"escritório: /compartilhar {result.document_id} equipe",
    )


async def _handle_onedrive(conn, chat_id, staff, arguments: list[str]) -> None:
    """List this person's own Microsoft files, and bring one into the archive."""
    word = (arguments[0].casefold() if arguments else "")

    if word in ("trazer", "ingerir", "importar"):
        await _import_from_drive(conn, chat_id, staff, arguments[1:])
        return

    query = " ".join(arguments).strip()
    await channels.for_chat(chat_id).typing(chat_id)
    try:
        items = await microsoft_files.list_files(conn, staff.id, query=query)
    except microsoft_files.NotConnected:
        await _send(
            chat_id,
            "Você ainda não conectou uma conta Microsoft, ou a conexão expirou. "
            "Mande /microsoft para conectar.",
        )
        return
    except microsoft.MicrosoftError as exc:
        logger.info("OneDrive listing failed: %s", exc)
        await _send(chat_id, "Não consegui ler os seus arquivos agora. Tente de novo.")
        return

    if not items:
        await _send(
            chat_id,
            f"Não encontrei arquivos para “{query}”." if query
            else "Não encontrei arquivos recentes nessa conta.",
        )
        return

    _LAST_DRIVE_LISTING[str(chat_id)] = items
    lines = [f"📁 Seus arquivos{f' para “{query}”' if query else ' (recentes)'}:"]
    for index, item in enumerate(items, start=1):
        when = f", {item.modified}" if item.modified else ""
        lines.append(f"{index}. {item.name} ({_human_size(item.size)}{when})")
    lines.append("\nPara trazer um para o acervo: /onedrive trazer <número>")
    lines.append("Ele entra na sua pasta pessoal, e só você vê até compartilhar.")
    await _send(chat_id, "\n".join(lines))


async def _import_from_drive(conn, chat_id, staff, arguments: list[str]) -> None:
    listing = _LAST_DRIVE_LISTING.get(str(chat_id)) or []
    choice = parse_int_id(arguments[0]) if arguments else None
    if choice is None or not 1 <= choice <= len(listing):
        await _send(
            chat_id,
            "Use o número que aparece na lista: /onedrive trazer 2\n"
            "Se a lista já saiu da tela, mande /onedrive de novo." if listing
            else "Mande /onedrive primeiro, para eu listar os seus arquivos.",
        )
        return

    item = listing[choice - 1]
    await channels.for_chat(chat_id).typing(chat_id)
    try:
        result = await microsoft_files.import_file(conn, staff.id, item)
    except microsoft_files.NotConnected:
        await _send(chat_id, "A conexão com a Microsoft expirou. Mande /microsoft para reconectar.")
        return
    except (ValueError, RuntimeError, microsoft.MicrosoftError) as exc:
        logger.info("OneDrive import refused: %s", exc)
        await _send(chat_id, f"Não consegui trazer “{item.name}”: {exc}")
        return

    if result.chunk_count:
        await _send(
            chat_id,
            f"✅ “{item.name}” está no acervo como documento [{result.document_id}], "
            f"em {result.chunk_count} trechos, na sua pasta pessoal.\n"
            f"Para compartilhar: /compartilhar {result.document_id} escritorio",
        )
    else:
        await _send(
            chat_id,
            f"Esse arquivo já estava no acervo, como documento [{result.document_id}].",
        )


async def cancel_microsoft_sign_ins() -> None:
    """Stop the waiting tasks on shutdown. A cancelled sign-in costs a retry."""
    for task in list(_MS_TASKS):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _MS_TASKS.clear()
    _MS_SIGNING_IN.clear()


# ── Following the DJEN: an OAB, or a process ─────────────────────────────────


def _parse_oab(arguments: list[str]) -> tuple[str, str] | None:
    """Read "12345 PA", "12345/PA", "PA 12345" or "12.345-PA" as number and UF."""
    joined = " ".join(arguments).upper()
    ufs = [token for token in re.findall(r"[A-Z]{2}", joined) if token in OAB_UFS]
    digits = "".join(ch for ch in joined if ch.isdigit())
    if len(ufs) != 1 or not 1 <= len(digits) <= 7:
        return None
    return digits.lstrip("0") or "0", ufs[0]


async def _watch_list(conn, staff) -> str:
    oabs, processes = await list_own_watches(conn, staff.id)
    if not oabs and not processes:
        return (
            "Você ainda não acompanha nada no DJEN.\n\n"
            "/oab <número> <UF>: aviso toda intimação nova para a sua OAB, em "
            "qualquer tribunal\n"
            "/acompanhar <número CNJ>: aviso toda publicação nova de um processo"
        )
    lines = ["📰 Você acompanha no DJEN:"]
    lines += [f"• OAB {oab}" for oab in oabs]
    lines += [f"• Processo {datajud.format_process_number(p)}" for p in processes]
    lines.append(
        f"\nConfiro a cada {settings.publication_watch_minutes // 60} horas, no horário "
        "do escritório. Para parar: /desacompanhar seguido do CNJ ou da OAB."
    )
    return "\n".join(lines)


async def _watch_oab(conn, chat_id, staff, arguments: list[str]) -> None:
    if not arguments:
        await _send(chat_id, await _watch_list(conn, staff))
        return
    parsed = _parse_oab(arguments)
    if parsed is None:
        await _send(chat_id, "Envie o número e a UF da OAB. Ex.: /oab 12345 PA")
        return
    number, uf = parsed
    added = await watch_oab(conn, staff.id, number, uf)
    if not added:
        await _send(chat_id, f"Você já acompanha a OAB {number}/{uf}.")
        return

    # A count of the last week, so a mistyped number shows itself now rather
    # than as a silence that looks like a quiet week.
    today = local_now().date()
    try:
        recent = await djen.communications_for_lawyer(
            number, uf, since=today - timedelta(days=7), until=today,
        )
        seen = (
            f"Nos últimos 7 dias houve {len(recent)} intimação(ões) para essa OAB."
            if recent else
            "Não encontrei intimações para essa OAB nos últimos 7 dias. Se isso "
            "não parece certo, confira o número."
        )
    except djen.DjenError:
        seen = "Não consegui consultar o DJEN agora; o acompanhamento já está valendo."
    await _send(
        chat_id,
        f"✅ Pronto: aviso aqui toda intimação nova no DJEN para a OAB {number}/{uf}, "
        f"a partir de hoje, em qualquer tribunal.\n{seen}",
    )


async def _watch_process(conn, chat_id, staff, arguments: list[str]) -> None:
    if not arguments:
        await _send(chat_id, await _watch_list(conn, staff))
        return
    try:
        digits = datajud.normalize_process_number("".join(arguments))
    except ValueError:
        await _send(chat_id, "Envie o número CNJ completo, com 20 dígitos.\n"
                             "Ex.: /acompanhar 0801234-56.2024.8.14.0301")
        return
    number = datajud.format_process_number(digits)
    if not datajud.has_valid_check_digits(digits):
        await _send(
            chat_id,
            f"O número {number} não confere: os dígitos verificadores não batem com o "
            "resto do número. Confira se não houve erro de digitação.",
        )
        return
    if not await watch_process(conn, staff.id, digits):
        await _send(chat_id, f"Você já acompanha o processo {number}.")
        return
    await _send(
        chat_id,
        f"✅ Pronto: aviso aqui toda publicação nova no DJEN do processo {number}, "
        "a partir de hoje.",
    )


async def _unwatch(conn, chat_id, staff, arguments: list[str]) -> None:
    joined = "".join(arguments)
    if sum(ch.isdigit() for ch in joined) == 20:
        digits = datajud.normalize_process_number(joined)
        number = datajud.format_process_number(digits)
        if await unwatch_process(conn, staff.id, digits):
            await _send(chat_id, f"Pronto, não aviso mais sobre o processo {number}.")
        else:
            await _send(chat_id, f"Você não acompanha o processo {number}.")
        return
    parsed = _parse_oab(arguments)
    if parsed is None:
        await _send(chat_id, await _watch_list(conn, staff))
        return
    number, uf = parsed
    if await unwatch_oab(conn, staff.id, number, uf):
        await _send(chat_id, f"Pronto, não aviso mais sobre a OAB {number}/{uf}.")
    else:
        await _send(chat_id, f"Você não acompanha a OAB {number}/{uf}.")


async def _ingest_from_url(conn, chat_id, staff, argument: str) -> None:
    """Ingest an official page or PDF whole, and hand the piece back as a file.

    This is the loop the firm actually works in: DataJud says where the case is,
    the lawyer opens the court's system or the Planalto, and the piece that
    matters comes back here to become part of the corpus.

    It used to index what `fetch_page` returns, which is cut to the size that
    fits beside a question: a law ingested by URL held its first 20,000
    characters and nothing else, and the file sent back was that same fragment.
    Now the whole document is read, the whole of it is indexed, and what goes
    back is the piece itself: the court's PDF as it stands, or a PDF built from
    the page's text, which is what a lawyer can actually read on a phone.
    """
    index_only = bool(re.search(r"\b(so|somente|apenas)[\s-]*(indexar|ingerir)\b",
                                argument, re.IGNORECASE))
    url = (argument.split()[0] if argument else "").strip()
    if not url.lower().startswith("https://"):
        await _send(chat_id, "Envie uma URL https de fonte oficial (tribunal, Planalto, LexML).")
        return

    await channels.for_chat(chat_id).typing(chat_id)
    try:
        fetched = await web_fetch.fetch_document(url)
    except web_fetch.FetchError as exc:
        logger.info("Ingestion by URL refused: %s", exc)
        await _send(
            chat_id,
            f"Não consegui baixar essa página: {exc}. "
            "Aceito apenas URLs https de domínios oficiais autorizados. Se a peça "
            "estiver atrás de login, baixe pelo sistema do tribunal e me envie o arquivo.",
        )
        return

    filename = web_fetch.filename_for(fetched.url, fetched.content_type)
    stem = filename.rsplit(".", 1)[0]
    try:
        result = await ingest_document(
            conn,
            fetched.data,
            filename=filename,
            doc_type=_caption_field(argument, "tipo"),
            matter=_caption_field(argument, "(?:caso|processo|matéria)"),
            uploaded_by=staff.id,
            source_url=fetched.url,
            # A law or an acórdão from an official page is what the firm knows,
            # not material one client handed one lawyer.
            visibility="firm",
        )
    except (ValueError, RuntimeError) as exc:
        logger.warning("Ingestion from URL failed: %s", exc)
        await _send(chat_id, f"Não consegui ingerir o conteúdo dessa página: {exc}")
        return

    if result.chunk_count:
        status = (
            f"✅ Ingerido como documento [{result.document_id}]: {_br_int(len(result.text))} "
            f"caracteres em {result.chunk_count} trechos.\nFonte: {fetched.url}\n"
            f"Confiro essa página toda semana e atualizo o acervo quando a lei mudar."
        )
    else:
        status = (
            f"Esse conteúdo já estava no acervo, como documento "
            f"[{result.document_id}]. Fonte registrada: {fetched.url}"
        )
    await _send(chat_id, status)

    if index_only:
        return

    # The court's own PDF travels as it is. Anything else becomes a PDF of the
    # text that was indexed, so the file and the corpus say the same thing.
    if "pdf" in fetched.content_type:
        data, name = fetched.data, f"{stem}.pdf"
    else:
        try:
            data, name = await build_async("pdf", stem, result.text)
        except Exception:
            logger.exception("Could not render the ingested page as a PDF")
            data, name = fetched.data, filename

    sent = await channels.for_chat(chat_id).send_file(
        chat_id, name, data, f"Peça completa: {fetched.url}",
        kind="export", document_id=None,
    )
    if not sent:
        await _send(
            chat_id,
            f"Ingeri o conteúdo, mas não consegui enviar o arquivo. Use /arquivo "
            f"{result.document_id} para recebê-lo.",
        )


# ── Delivering an answer: its sources, and the file the lawyer may want ──────

_FEEDBACK_TTL = 7 * 24 * 3600.0  # a week: somebody may come back to an answer

# What each rateable answer was about, so a 👎 is actionable rather than a
# number. In process, and therefore lost on a deploy: the verdict still gets
# stored, only the question text is missing from that row. That is the honest
# trade for not writing a row for every answer nobody rates, and it is worth
# revisiting if the pilot shows the blanks matter.
_rated_questions: dict[str, tuple[str, str, float]] = {}
_RATED_TTL = 24 * 3600.0


def _remember_rated_question(answer_ref: str, title: str, reply) -> None:
    now = time.monotonic()
    for ref, (_, _, expires_at) in list(_rated_questions.items()):
        if expires_at <= now:
            del _rated_questions[ref]
    _rated_questions[answer_ref] = (
        title or "", getattr(reply, "task", "") or "", now + _RATED_TTL,
    )


def _rated_question(answer_ref: str) -> tuple[str, str]:
    question, task, expires_at = _rated_questions.get(answer_ref, ("", "", 0.0))
    if expires_at <= time.monotonic():
        _rated_questions.pop(answer_ref, None)
        return "", ""
    return question, task


async def _feedback_keyboard(conn, chat_id, staff, title: str, reply):
    """The 👍/👎 under an answer, or None when the buttons cannot be minted.

    Both buttons share one `answer_ref`, which is what lets a second press
    correct the first instead of adding a contradicting row. Unlike the
    decision buttons, the siblings are not retired: a decision happens once,
    an opinion may be revised, and the corrected verdict is the better datum.

    A failure to mint costs the signal, never the answer. Losing a rating is a
    gap in a metric; losing the answer is the lawyer's work.
    """
    answer_ref = secrets.token_urlsafe(9)
    try:
        tokens = [
            await callback_actions.issue(
                conn, chat_id=chat_id, staff_id=staff.id, issued_role=staff.role,
                action="answer.rate", operation=verdict, resource=answer_ref,
                ttl_seconds=_FEEDBACK_TTL, channel=channels.for_chat(chat_id).name,
            )
            for verdict in answer_feedback.VERDICTS
        ]
    except Exception:
        logger.exception("Could not issue the answer feedback buttons")
        return None
    async with panel.publishing_current(conn):
        _remember_rated_question(answer_ref, title, reply)
    return {"inline_keyboard": [[
        {"text": "👍 Ajudou", "callback_data": tokens[0]},
        {"text": "👎 Não ajudou", "callback_data": tokens[1]},
    ]]}


async def _deliver(chat_id, reply, title: str, *, conn, staff) -> None:
    """Send an answer, offer what backs it, and remember it for the follow-ups.

    Sources are offered rather than dumped: a bibliography under every reply
    buries the answer, but the lawyer must always be able to get it in one word.

    The 👍/👎 pair rides along because this is the one place every agent answer
    leaves through, so no specialist can ship without the question being asked.
    `conn` and `staff` are required rather than defaulted for the same reason:
    a call site that forgot them would keep working and quietly stop asking,
    and a rating nobody notices missing is worse than a crash.
    """
    text = str(reply)
    offer = sources_module.offer(getattr(reply, "sources", []) or [])
    if len(text) >= _EXPORT_OFFER_CHARS:
        offer += (
            "\n\nSe preferir em arquivo, posso gerar em DOCX, PDF ou XLSX: "
            "é só pedir."
        )
    async with panel.publishing_current(conn):
        _remember_delivery(chat_id, title, reply)
    keyboard = await _feedback_keyboard(conn, chat_id, staff, title, reply)
    await _send(chat_id, text + offer, reply_markup=keyboard)


def _remember_delivery(chat_id, title: str, reply) -> None:
    now = time.monotonic()
    for key, (_, _, expires_at) in list(_last_deliveries.items()):
        if expires_at <= now:
            del _last_deliveries[key]
    _last_deliveries[str(chat_id)] = (title, reply, now + _DELIVERY_TTL)


def _last_delivery(chat_id):
    """The last answer sent to this chat, or None once it has aged out."""
    title, reply, expires_at = _last_deliveries.get(str(chat_id), (None, None, 0.0))
    if reply is None or expires_at <= time.monotonic():
        _last_deliveries.pop(str(chat_id), None)
        return None, None
    return title, reply


async def _send_sources(chat_id, reply) -> None:
    await _send(chat_id, sources_module.listing(getattr(reply, "sources", []) or []))


async def _export_last_answer(chat_id, fmt: str) -> None:
    """Render the last answer as a file and send it through Telegram."""
    title, reply = _last_delivery(chat_id)
    if reply is None:
        await _send(chat_id, "Não tenho uma resposta recente para exportar. Faça o pedido e eu gero o arquivo em seguida.")
        return
    listing = sources_module.listing(getattr(reply, "sources", []) or [])
    await channels.for_chat(chat_id).typing(chat_id)
    try:
        data, filename = await build_async(fmt, title or "Resposta", str(reply), listing)
    except Exception:
        logger.exception("Export to %s failed", fmt)
        await _send(chat_id, "Não consegui gerar o arquivo agora. Tente novamente.")
        return
    sent = await channels.for_chat(chat_id).send_file(
        chat_id, filename, data, f"{title or 'Resposta'} ({fmt.upper()})",
        kind="export", document_id=None,
    )
    if not sent:
        await _send(chat_id, "Não consegui enviar o arquivo. Tente novamente em instantes.")


async def _ask_export_format(conn, chat_id) -> None:
    _, reply = _last_delivery(chat_id)
    if reply is None:
        await _send(chat_id, "Não tenho uma resposta recente para exportar. Faça o pedido e eu gero o arquivo em seguida.")
        return
    await pending_actions.remember(
        conn, chat_id, kind="export", ttl_seconds=_PENDING_DECISION_TTL,
    )
    await _send(
        chat_id,
        "Em qual formato você quer o arquivo?\n"
        "• DOCX: para continuar editando no Word\n"
        "• PDF: versão fechada, para o cliente ou os autos\n"
        "• XLSX: planilha, para comparativos e tabelas de prazos\n\n"
        "Responda com docx, pdf ou xlsx.",
    )


async def _handle_answer_followup(conn, chat_id, text: str, command: str) -> bool:
    """Answer "quais as fontes?" / "manda em pdf" about the previous reply.

    Returns True when the message was a follow-up and is already answered.
    """
    # A format asked for right after "em qual formato?"
    pending = await pending_actions.peek(conn, chat_id)
    if pending is not None and pending.kind == "export" and not pending.expired:
        chosen = format_in(text) or normalize_format(text)
        if chosen:
            await pending_actions.forget(conn, chat_id)
            await _export_last_answer(chat_id, chosen)
            return True
        if command.startswith("/") or detect_export_request(text) is None:
            await pending_actions.forget(conn, chat_id)  # the lawyer moved on

    _, reply = _last_delivery(chat_id)
    if reply is None or command.startswith("/"):
        return False
    if detect_document_request(text):
        return False  # points at a stored document: a new request, not a follow-up

    export = detect_export_request(text)
    if export == "ask":
        await _ask_export_format(conn, chat_id)
        return True
    if export in FORMATS:
        await _export_last_answer(chat_id, export)
        return True
    if wants_sources(text) and (getattr(reply, "sources", None) or []):
        await _send_sources(chat_id, reply)
        return True
    return False


async def _serve_document_request(conn, chat_id, staff, request, text: str) -> None:
    """Answer "resuma o doc [2]" from the stored file instead of the chat text.

    The corpus is the whole point of the assistant: a message that clearly points
    at a stored document must never reach the agent that only sees the message.
    """
    # Asking in words is the same act as typing the command, so it answers to
    # the same rank: pulling an original out of the archive is a lawyer's job
    # however the request is phrased. An analysis request is not gated here at
    # all, because it no longer reaches anything to gate — it is answered with
    # the policy, which everybody is entitled to be told.
    if request.action == "original":
        if not await _may(chat_id, staff, "document.read_original"):
            return

    # Resolved against the database, not against a page of the newest entries:
    # every document falls off that page once the archive is real, and pointing
    # at one by number must not stop working because it got old.
    document = None
    if request.document_id is not None:
        document = await get_document_summary(conn, request.document_id, viewer())
    if document is None and request.title_hint:
        matches = await search_documents(conn, title=request.title_hint, limit=2, scope=viewer())
        if len(matches) == 1:
            document = matches[0]
    if document is None:
        if not await count_documents(conn, scope=viewer()):
            await _send(chat_id, "📂 Acervo vazio: nenhum documento foi ingerido ainda.")
            return
        await _send(chat_id, "Não identifiquei de qual documento você fala. Escolha pelo número:")
        await _send_document_catalog(conn, chat_id)
        return

    document_id = document.id
    if request.action == "original":
        await _send_original_document(conn, chat_id, document_id, staff.id)
        return

    # "resuma o doc [2]" is still recognised deterministically, and still costs
    # nothing to recognise. What changed is the answer: the whole-document
    # operations are gone, so the request is named back and redirected instead
    # of being quietly handed to the agent, which would answer something
    # narrower without ever saying it had.
    await _send(chat_id, _CORPUS_IS_A_SOURCE)


async def _voice_to_text(chat_id, voice) -> str:
    """Voice note -> transcript. Returns "" after telling the sender why not."""
    if voice.get("duration", 0) > settings.max_voice_seconds:
        await _send(chat_id, "O áudio é muito longo. Pode mandar um trecho menor ou escrever?")
        return ""
    file_id = voice.get("file_id")
    if not file_id:
        return ""
    await channels.for_chat(chat_id).typing(chat_id)
    audio = await download_voice(file_id, token=LEGAL_TELEGRAM_TOKEN)
    if not audio:
        await _send(chat_id, "Não consegui baixar o áudio. Pode tentar de novo ou escrever?")
        return ""
    text = (await transcribe(audio, mime=voice.get("mime_type"), prompt=_VOICE_HINT,
                             seconds=float(voice.get("duration") or 0)) or "").strip()
    if is_silence_artifact(text):
        await _send(chat_id, _SILENT_VOICE)
        return ""
    return text


_SILENT_VOICE = ("Não entendi: o áudio chegou sem fala audível. "
                 "Pode gravar de novo mais perto do microfone ou escrever a mensagem?")


async def _queue_document(
    conn,
    chat_id,
    document,
    staff,
    caption: str | None = None,
    update_id: int | str | None = None,
) -> None:
    """Download and ingest an authorized Telegram document in the VPS worker."""
    filename = document.get("file_name") or "documento"
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in DOCUMENT_EXTENSIONS:
        await _send(chat_id, ingestion.DOCUMENT_FORMAT_REFUSED)
        return

    max_bytes = settings.max_doc_mb * 1024 * 1024
    declared_size = int(document.get("file_size") or 0)
    if declared_size > max_bytes:
        await _send(chat_id, ingestion.document_too_large())
        return

    file_id = document.get("file_id")
    if not file_id:
        await _send(chat_id, ingestion.DOCUMENT_UNIDENTIFIED)
        return

    await channels.for_chat(chat_id).typing(chat_id)
    data = await download_file(file_id, token=LEGAL_TELEGRAM_TOKEN, max_bytes=max_bytes)
    if not data:
        await _send(chat_id, "Não consegui baixar o documento. Tente enviá-lo novamente.")
        return

    await _send(chat_id, ingestion.document_received(filename))
    try:
        document_id, chunk_count = await ingest_bytes(
            conn,
            data,
            filename=filename,
            title=filename,
            doc_type=_caption_field(caption or "", "tipo"),
            matter=_caption_field(caption or "", "(?:caso|processo|matéria)"),
            uploaded_by=staff.id,
        )
    except ValueError:
        logger.warning("Permanent document ingestion error", exc_info=True)
        await _send(chat_id, ingestion.document_failed(filename))
        return

    await _send(chat_id, ingestion.document_done(filename) if chunk_count
                else ingestion.document_known(filename))


# How long a decision button stays live. Longer than a pending question, since
# a manager may only open Telegram hours after the request arrives.
_CALLBACK_TTL = 72 * 3600.0

# What the presser is told, by why the press failed. None of these name the
# person or the matter: a toast is visible over the chat, and a token that is
# not yours must not describe what it would have done.
# The state of a request, in the words the notification already uses.
_REQUEST_STATUS_LABELS = {
    "pending": "pendente",
    "approved": "autorizada",
    "rejected": "recusada",
    "cancelled": "retirada pela própria pessoa",
}

_PRESS_REFUSALS = {
    "already_used": "Essa solicitação já foi decidida.",
    "cancelled": "Essa solicitação já foi decidida em outra mensagem.",
    "expired": "Esse botão expirou. Use /pendentes para ver o que falta decidir.",
    "unknown": "Esse botão não é mais válido.",
    "forbidden": "Esse botão não é mais válido.",
    "other_channel": "Esse botão é do outro canal.",
}
# What a press answers, now and on any later press of the same button, when
# its action failed after the press was taken: the button cannot run again,
# and saying it was already decided would be untrue.
_PRESS_FAILED = "Não consegui concluir. Tente pelo comando."
# How long a press cut off by the worker's time limit may still spend saying so.
_FAILED_PRESS_GRACE = 5


def _decision_keyboard(
    approve_token: str, refuse_token: str, details_token: str,
) -> dict:
    """Three buttons carrying nothing but their tokens.

    The decision sits on the first row and the reading on the second, so the
    irreversible pair is never the neighbour of the harmless one under a thumb.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Autorizar", "callback_data": approve_token},
                {"text": "🚫 Recusar", "callback_data": refuse_token},
            ],
            [{"text": "ℹ️ Ver detalhes", "callback_data": details_token}],
        ]
    }


# A confirmation lives a shorter life than a decision: nobody is expected to
# confirm a deletion hours after asking for it, and a shorter window means a
# stale button fails closed sooner rather than sitting live in old chat history.
_CONFIRMATION_TTL = 3600.0


# The buttons that confirm an irreversible deletion. They share the default
# refusals with access decisions, which were written for approvals: an expired
# one would have sent the owner to /pendentes.
_CONFIRMATION_PRESSES = frozenset({
    ("staff.deactivate", "confirm"),
    ("document.delete", "confirm"),
})


def _refusal_for(claim) -> str:
    """Why a press did nothing, in the terms of the button that was pressed.

    One sentence used to answer every expired button, and it was written for an
    access decision, so an owner whose confirmation had expired was told to
    check /pendentes: a decision nobody had asked them to make.

    Only two kinds of button are left, both confirming something irreversible.
    The navigation menu that used to mint most of these is gone, replaced by the
    command list Telegram itself offers.
    """
    if (claim.action, claim.operation) in _CONFIRMATION_PRESSES:
        if claim.outcome == "expired":
            return "Esse botão de confirmação expirou. Envie o comando de novo para confirmar."
        if claim.outcome in ("already_used", "cancelled"):
            return "Essa exclusão já foi concluída."
    return _PRESS_REFUSALS.get(claim.outcome, "Botão inválido.")


async def _send_commands(chat_id, staff) -> None:
    """Every command this person may run, as plain text.

    It used to arrive with a keyboard of navigation buttons beside it. Those
    are gone: Telegram offers the same list itself the moment somebody types a
    slash, filtered as they keep typing, and it does that without leaving a
    screenful of buttons behind in the conversation. The registration lives in
    clients/demo_firm/set_webhook.py and is built from this very text, so the
    two can never disagree about which commands exist.
    """
    await _send(chat_id, _help_for(staff))


async def _welcome(conn, request) -> None:
    """The first thing somebody sees once they are let in: what they can do.

    It used to say "Envie /ajuda", which asks a person who has just arrived to
    learn a command before being shown anything. This is the one moment the
    list is pushed rather than asked for, because it answers the question the
    newcomer is about to ask. Afterwards it is theirs to ask for, with
    /comandos.
    """
    greeting = "✅ Seu acesso foi liberado. Boas-vindas ao Assistente Legal Demo & Associados.\n\n"
    newcomer = await get_staff_by_chat_id(conn, request.chat_id)
    if newcomer is None:
        # decide_access_request commits the row before returning, so this is
        # not expected. The welcome still goes out, pointing at the command,
        # rather than not at all.
        logger.error("Approved request %s has no staff record yet", request.request_code)
        await _send(request.chat_id, greeting + "Envie /comandos para ver o que posso fazer.")
        return
    await _send(request.chat_id, greeting + _help_for(newcomer))


def _render_request_details(request) -> str:
    """Everything known about one access request, and nothing beyond it.

    The notification carries a name and a code, which is enough to decide when
    the person is expected and not enough when they are not. This is what a
    manager needs to recognise a stranger: the account that asked, and when.
    """
    username = f" (@{request.username})" if request.username else ""
    lines = [
        f"ℹ️ Solicitação {request.request_code}",
        f"Pessoa: {request.name}{username}",
        f"Telegram: {request.chat_id}",
    ]
    if request.requested_at:
        lines.append(f"Recebida em: {local_now(request.requested_at):%d/%m/%Y às %H:%M}")
    status = _REQUEST_STATUS_LABELS.get(request.status, request.status)
    lines.append(f"Situação: {status}")
    if request.status == "pending":
        lines.append(
            f"\nDecida no botão da notificação, ou use /autorizar "
            f"{request.request_code} ou /recusar {request.request_code}."
        )
    return "\n".join(lines)


def _staff_resource(target_id: int) -> str:
    """What a deactivation button is about, in the shape the token store keys on.

    Namespaced because `resource` is shared with access decisions, which carry a
    request code: two kinds of subject in one column must not be able to collide
    and retire each other.
    """
    return f"staff:{target_id}"


def _document_resource(document_id: int) -> str:
    """What a document deletion button is about, namespaced like a staff one."""
    return f"document:{document_id}"


async def _document_deletion_keyboard(conn, chat_id, actor, document_id: int) -> dict | None:
    """One button that finishes what the deletion preview describes.

    /excluir_documento was the one irreversible command still confirmed only by
    typing, a gap recorded when stage 3 closed. The typed word keeps working;
    this is the shortcut beside it, minted for the owner who asked and checked
    against that rank again when it is pressed.
    """
    try:
        token = await callback_actions.issue(
            conn,
            chat_id=chat_id,
            staff_id=actor.id,
            issued_role=normalize_role(actor.role),
            action="document.delete",
            operation="confirm",
            resource=_document_resource(document_id),
            ttl_seconds=_CONFIRMATION_TTL,
            channel=channels.for_chat(chat_id).name,
        )
    except Exception:
        # Typing confirmar still works, so a failure to mint must not cost the
        # owner the preview itself.
        logger.exception("Could not issue the document deletion button")
        return None
    return {
        "inline_keyboard": [[
            {"text": "🗑️ Confirmar exclusão", "callback_data": token},
        ]]
    }


async def _retire_document_deletion_buttons(
    conn, document_id: int, *, keep_token: str | None = None,
) -> None:
    """Take back every deletion button still offered for this document."""
    try:
        retired = await callback_actions.cancel_siblings(
            conn, resource=_document_resource(document_id), keep_token=keep_token,
        )
    except Exception:
        logger.exception("Could not retire the document deletion buttons")
        return
    for other_chat, channel_name, reference in retired:
        target = _channel_for_copy(other_chat, channel_name)
        if target is not None:
            await _rewrite_decided(
                other_chat, reference, f"Exclusão do documento [{document_id}]",
                "Já concluída.", channel=target,
            )


async def _apply_document_removal(conn, chat_id, actor, document_id: int) -> str:
    """Delete one document and say exactly what happened, for command and button.

    Both paths end here, so what is said about the original file, kept, removed
    or left on disk, is said once and identically whichever way the owner asked.
    """
    outcome = await remove_document(
        conn, document_id, staff_id=actor.id, chat_id=chat_id,
    )
    if outcome.status == "not_found":
        message = "Não encontrei um documento com esse ID."
        await _send(chat_id, message)
        return message
    reply = [f"✅ Documento [{document_id}] {outcome.title} foi apagado do acervo."]
    if outcome.original_kept_for_another_document:
        reply.append(
            "O arquivo original foi mantido porque outro documento do acervo "
            "usa o mesmo arquivo."
        )
    elif outcome.orphaned_storage_key:
        # Saying this plainly matters: the row is gone, so nothing in the
        # assistant can reach the file any more, and only this message says
        # where it still is.
        reply.append(
            "⚠️ Não consegui apagar o arquivo original do disco. Ele ficou em "
            f"{outcome.orphaned_storage_key} e precisa ser removido à mão."
        )
    elif outcome.original_removed:
        reply.append("O arquivo original também foi apagado.")
    reply.append("O histórico de auditoria foi preservado.")
    message = " ".join(reply)
    await _send(chat_id, message)
    return message


async def _deactivation_keyboard(conn, chat_id, actor, target_id: int) -> dict | None:
    """One button that finishes what the preview started.

    A separate button rather than a second use of the decision keyboard: this
    one is not a choice between two outcomes, it is the confirmation of the one
    already described in the message above it.
    """
    try:
        token = await callback_actions.issue(
            conn,
            chat_id=chat_id,
            staff_id=actor.id,
            issued_role=normalize_role(actor.role),
            action="staff.deactivate",
            operation="confirm",
            resource=_staff_resource(target_id),
            ttl_seconds=_CONFIRMATION_TTL,
            channel=channels.for_chat(chat_id).name,
        )
    except Exception:
        # Typing confirmar still works, so a failure to mint must not cost the
        # owner the preview itself.
        logger.exception("Could not issue the deactivation confirmation button")
        return None
    return {
        "inline_keyboard": [[
            {"text": "🗑️ Confirmar exclusão", "callback_data": token},
        ]]
    }


async def _retire_deactivation_buttons(
    conn, target_id: int, *, keep_token: str | None = None,
) -> None:
    """Take back every confirmation still offered for this account.

    The account is already gone, so a button that still offers to remove it is
    a lie the owner would only discover by pressing it. The message is rewritten
    to say so rather than left looking live.
    """
    try:
        retired = await callback_actions.cancel_siblings(
            conn, resource=_staff_resource(target_id), keep_token=keep_token,
        )
    except Exception:
        logger.exception("Could not retire the deactivation buttons")
        return
    for other_chat, channel_name, reference in retired:
        target = _channel_for_copy(other_chat, channel_name)
        if target is not None:
            await _rewrite_decided(
                other_chat, reference, f"Exclusão do usuário [{target_id}]",
                "Já concluída.", channel=target,
            )


async def _apply_deactivation(conn, chat_id, actor, target_id: int) -> str:
    """Revoke one account, and say what happened, for command and button alike.

    Both paths end here, so the rank check inside the service, the audit entry
    and the notice to the person who lost access happen once and identically
    whichever way the owner asked. The order of the two messages is deliberate:
    the owner learns it worked before the other person does.
    """
    try:
        result = await deactivate_staff(conn, actor_id=actor.id, target_id=target_id)
    except StaffDeactivationError as exc:
        message = (
            "🔒 Este comando é exclusivo do owner." if exc.code == "actor_not_owner"
            else "Contas owner não podem ser excluídas por este comando."
        )
        await _send(chat_id, message)
        return message
    if result.status == "not_found":
        message = "Não encontrei um usuário com esse ID."
    elif result.status == "already_inactive":
        message = "Esse usuário já está sem acesso."
    elif result.staff is None:
        # Defensive: a successful result always carries the target.
        logger.error("Staff deactivation returned no target for id=%s", target_id)
        message = "Não consegui excluir esse usuário agora."
    else:
        message = (
            f"✅ O acesso de {result.staff.name} foi excluído. "
            "O histórico de auditoria foi preservado."
        )
    await _send(chat_id, message)
    if result.status == "deactivated" and result.staff:
        # What this person had in their personal folder is now reachable by
        # nobody: visibility is decided by `uploaded_by`, and there is no
        # longer anybody with that id asking. The documents are not deleted,
        # they are dark, and the firm still owes a duty to the clients whose
        # papers those are. Said now, with the ids, because the alternative is
        # finding out the day a client asks for their own file.
        try:
            orphaned = await private_documents_of(conn, result.staff.id)
        except Exception:
            logger.exception("Could not list the private documents of staff %s",
                             result.staff.id)
            orphaned = []
        if orphaned:
            listed = "\n".join(f"  [{doc_id}] {title}" for doc_id, title in orphaned[:15])
            more = (f"\n  e mais {len(orphaned) - 15}" if len(orphaned) > 15 else "")
            await _send(
                chat_id,
                f"⚠️ {len(orphaned)} documento(s) na pasta pessoal de "
                f"{result.staff.name} ficaram sem ninguém que os alcance:\n"
                f"{listed}{more}\n\n"
                f"Eles não foram apagados. Para trazer um para a sua pasta: "
                f"/assumir <id>",
            )
        # Revoking access has to reach the browser too. The session would
        # already fail on its next request, since the staff row is re-read
        # each time, but a closed session is a fact rather than an inference.
        try:
            await web_access.revoke_devices(conn, result.staff.id)
        except Exception:
            logger.exception("Could not close the web sessions of staff %s",
                             result.staff.id)
        if result.staff.chat_id:
            await _send(
                result.staff.chat_id,
                "Seu acesso ao Assistente Legal Demo & Associados foi revogado pelo owner.",
            )
    return message


def _erasure_warning(target, command_name: str) -> str:
    """What is about to be destroyed, and what is deliberately not.

    A confirmation that only says "are you sure" makes the owner guess. This one
    lists both halves, because the surprise people get from an erasure is never
    what went, it is what stayed: the documents this person uploaded belong to
    the firm, and the audit trail of who read what is a record the firm has to
    keep. Neither of those is theirs to take away.
    """
    label = ROLE_LABELS[normalize_role(target.role)]
    return (
        f"⚠️ Apagar os dados pessoais de {target.name} ({label}).\n\n"
        "Isso é definitivo e não tem como desfazer.\n\n"
        "Some para sempre:\n"
        "  • o nome, o e-mail e o identificador do Telegram\n"
        "  • as conversas em andamento e os botões pendentes\n"
        "  • o identificador nos registros de acesso\n\n"
        "Permanece:\n"
        "  • os documentos que essa pessoa enviou, que são do escritório\n"
        "  • o histórico de quais documentos foram lidos e quando, sem "
        "identificar quem\n\n"
        "Responda exatamente APAGAR em maiúsculas, envie /cancelar, ou use a "
        f"forma completa:\n{command_name} {target.id} APAGAR"
    )


async def _apply_erasure(conn, chat_id, actor, target_id: int) -> str:
    """Erase one person, and report what was actually touched.

    The counts come back from the repository rather than being described in
    advance, because "os dados foram apagados" is a claim and the numbers are
    the evidence. The person is not notified: there is no longer anywhere to
    send it, which is the point.
    """
    try:
        result = await erase_person(conn, actor_id=actor.id, target_id=target_id)
    except StaffDeactivationError as exc:
        message = (
            "🔒 Este comando é exclusivo do owner." if exc.code == "actor_not_owner"
            else "Contas owner não podem ser apagadas por este comando."
        )
        await _send(chat_id, message)
        return message
    if result.status == "not_found":
        message = "Não encontrei uma pessoa com esse ID."
    elif result.status == "already_erased":
        message = "Os dados dessa pessoa já haviam sido apagados."
    elif result.staff is None:
        # Defensive: a successful result always carries the target.
        logger.error("Person erasure returned no target for id=%s", target_id)
        message = "Não consegui apagar os dados dessa pessoa agora."
    else:
        message = (
            f"✅ Dados pessoais apagados. O registro {target_id} agora aparece "
            f"como “{ERASED_NAME}”.\n\n"
            f"Registros de acesso anonimizados: {result.access_log_entries}\n"
            f"Conversas em andamento removidas: {result.steps_removed}\n"
            f"Botões pendentes removidos: {result.buttons_removed}\n"
            f"Solicitações de acesso anonimizadas: {result.requests_anonymised}\n"
            f"Acompanhamentos no DJEN removidos: {result.watches_removed}\n\n"
            "Os documentos enviados por essa pessoa continuam no acervo."
        )
    await _send(chat_id, message)
    return message


def _channel_for_copy(chat_id, channel_name: str) -> channels.Channel | None:
    """The channel that can rewrite one copy of a retired button, or None.

    Telegram copies go by Telegram, except the served person's own Telegram
    copy while another channel serves them: rewriting it would carry the
    decision's words to Telegram from a conversation held elsewhere (its
    buttons are already dead and say so if pressed). A copy in any other
    channel is reachable only while that channel serves its own chat.
    """
    served = channels.served_chat()
    current = channels.current()
    if channel_name == current.name and served is not None and str(chat_id) == served:
        return current
    if channel_name == channels.TELEGRAM.name:
        if current is not channels.TELEGRAM and served is not None and str(chat_id) == served:
            return None
        return channels.TELEGRAM
    return None


async def _rewrite_decided(chat_id, reference, original: str, footer: str,
                           channel: channels.Channel | None = None) -> None:
    """Replace a decision message with its outcome and drop the keyboard.

    Passing no keyboard is what removes the buttons, so a settled request stops
    offering a decision in every chat it was sent to. `channel` is the copy's
    own; by default, the channel of the chat (the press's own copy).
    """
    try:
        await (channel or channels.for_chat(chat_id)).edit_text(
            chat_id, str(reference), f"{original}\n\n{footer}",
        )
    except Exception:
        # The button is already retired in the database, so a failed rewrite
        # leaves a stale-looking button that refuses when pressed — worth a log,
        # not worth failing the decision that already happened.
        logger.exception("Could not rewrite a decided request message")


async def _process_callback_query(conn, callback: dict) -> None:
    """Handle one button press: identify, claim once, re-authorize, then act.

    The press is answered on every path, including every refusal — Telegram
    leaves the button spinning until it is, and a spinner that never stops
    reads as a broken assistant.
    """
    query_id = callback.get("id")
    token = (callback.get("data") or "").strip()
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    presser_id = (callback.get("from") or {}).get("id")

    async def answered(text: str | None = None) -> None:
        await channels.for_chat(chat_id).ack_button(query_id, text)

    # One line, before any decision, naming every condition the checks below
    # branch on. A press that is refused for a structural reason used to leave
    # no trace at all: the webhook logged a 200, the handler logged nothing,
    # and the only visible symptom was a button that span and gave up. This
    # says which branch was taken without carrying any case content — the token
    # is a capability, so only its length is recorded, never its value.
    logger.info(
        "Button press received: chat=%s type=%s presser_owns_chat=%s "
        "has_query_id=%s data_len=%d",
        chat_id,
        chat.get("type"),
        str(presser_id) == str(chat_id),
        bool(query_id),
        len(token),
    )

    if chat_id is None or not token:
        logger.info(
            "Button press ignored: it carried no chat (%s) or no data (%s)",
            chat_id is None,
            not token,
        )
        await answered(_PRESS_REFUSALS["unknown"])
        return
    # The corpus is granted to a person, never to a room — same rule the text
    # path applies, for the same reason.
    if chat.get("type") not in (None, "private") or str(presser_id) != str(chat_id):
        logger.warning("Ignored a button press whose presser does not own the chat")
        await answered(_PRESS_REFUSALS["unknown"])
        return

    staff = await get_staff_by_chat_id(conn, chat_id)
    if not staff:
        logger.info("Button press ignored: chat %s is not authorized staff", chat_id)
        await answered(_PRESS_REFUSALS["unknown"])
        return

    # Served like a message: the person's folders and their conversation are
    # bound, so what the press says is recorded and routed the same way.
    async with _serving(conn, staff):
        await _run_callback(conn, message, chat_id, message_id, staff, token, answered)


_FEEDBACK_THANKS = {
    "util": "Obrigado. Anotado que ajudou.",
    "nao_util": (
        "Obrigado, anotado. Se puder dizer em uma linha o que faltou ou o que "
        "saiu errado, me responda aqui: é isso que faz a próxima resposta "
        "melhorar."
    ),
}


async def _record_answer_feedback(conn, chat_id, staff, answer_ref: str,
                                  verdict: str, answered) -> None:
    """Store one 👍 or 👎, and say so.

    Unknown verdicts are refused by the repository rather than stored, but the
    press is still answered: a button that does nothing and says nothing reads
    as the bot having crashed.
    """
    question, task = _rated_question(answer_ref)
    try:
        changed = await answer_feedback.record(
            conn, answer_ref=answer_ref, staff_id=staff.id, chat_id=chat_id,
            verdict=verdict, question=question, task=task,
        )
    except ValueError:
        logger.error("A feedback button carried an unknown verdict: %r", verdict)
        await answered(_PRESS_REFUSALS["unknown"])
        return
    except Exception:
        logger.exception("Could not store the answer feedback")
        await answered("Não consegui registrar agora. Tente de novo em instantes.")
        return

    observe_feedback(verdict, changed)
    logger.info("Answer feedback recorded: verdict=%s changed=%s", verdict, changed)
    if changed:
        await answered("Anotado, mudei o registro para o que você marcou agora.")
        return
    await answered(_FEEDBACK_THANKS[verdict])


async def _run_callback(conn, message, chat_id, message_id, staff, token, answered) -> None:
    """The press itself, with this person's folders already bound."""
    claim = await callback_actions.claim(conn, token, chat_id=chat_id, staff_id=staff.id,
                                         channel=channels.current().name)
    if not claim.may_run:
        logger.info("Button press refused: outcome=%s", claim.outcome)
        await answered(claim.result or _refusal_for(claim))
        return

    action = claim.action
    if not action:
        # A granted row carrying no action is a corrupted record, and the safe
        # reading of a corrupted record is to refuse it.
        logger.error("Button press granted a row carrying no action")
        await answered(_PRESS_REFUSALS["unknown"])
        return

    # The row stores the action, not the verdict, so the rank is re-read now:
    # a button issued before a demotion stops working at the demotion.
    decision = authorization.decide(staff, action)
    if not decision.allowed:
        logger.info("Button press denied by authorization: action=%s", action)
        await answered("Você não tem permissão para essa ação.")
        return

    logger.info(
        "Button press granted: action=%s operation=%s", action, claim.operation,
    )
    try:
        await _act_on_press(conn, message, chat_id, message_id, staff, token, answered,
                            claim, action)
    except BaseException:
        # The press is spent and its action did not finish, whether it raised
        # or the worker's time limit cancelled it. Whatever presses it again
        # is told so, instead of "already decided", which would be untrue;
        # the update's retry then answers from this result.
        await _remember_failed_press(conn, token)
        raise


async def _remember_failed_press(conn, token) -> None:
    """Record that a spent press did not finish, within a bounded wait.

    A failed statement leaves the transaction aborted, so the action's own
    writes are rolled back first; that also discards anything half-done.
    A result the action already committed (its decision went through and
    only the reply after it failed) is kept.
    """
    async def record():
        await conn.rollback()
        await callback_actions.record_result(conn, token, _PRESS_FAILED,
                                             unless_recorded=True)

    try:
        await asyncio.wait_for(record(), timeout=_FAILED_PRESS_GRACE)
    except Exception:
        logger.exception("Could not record that a pressed action failed")


async def _act_on_press(conn, message, chat_id, message_id, staff, token, answered,
                        claim, action) -> None:
    """What a granted, authorized press does."""

    if action == "answer.rate" and claim.resource:
        await _record_answer_feedback(
            conn, chat_id, staff, claim.resource, claim.operation or "", answered,
        )
        return

    if action == "access.review" and claim.resource:
        approved = claim.operation == "approve"
        completed = await _decide_access(
            conn, chat_id, staff, claim.resource, approved,
        )
        outcome = (
            ("Autorizado." if approved else "Recusada.") if completed
            else "Essa solicitação já não estava pendente."
        )
        await callback_actions.record_result(conn, token, outcome)
        # Every manager received the same request; one decision retires all of
        # those copies, so it cannot be decided again from another chat — and
        # each of those messages is rewritten, so nobody is left looking at a
        # live-looking button for something already settled.
        retired = await callback_actions.cancel_siblings(
            conn, resource=claim.resource, keep_token=token,
        )
        original = (message.get("text") or "Solicitação de acesso").strip()
        # A message holds both buttons, and the presser's own copy is among the
        # rows just retired, so each message is rewritten once — and the
        # presser is not told who decided, since they are the one who did.
        # A reference names a message only on its own channel, so a copy is
        # known by all three.
        rewritten = set()
        if message_id is not None:
            await _rewrite_decided(chat_id, message_id, original, outcome)
            rewritten.add((str(chat_id), channels.for_chat(chat_id).name, str(message_id)))
        for other_chat, channel_name, reference in retired:
            copy = (str(other_chat), channel_name, str(reference))
            if copy in rewritten:
                continue
            rewritten.add(copy)
            target = _channel_for_copy(other_chat, channel_name)
            if target is not None:
                await _rewrite_decided(
                    other_chat, reference, original,
                    f"{outcome} Decidido por {staff.name}.", channel=target,
                )
        await answered(outcome)
        return

    if action == "access.view_request" and claim.resource:
        # A read, so nothing is retired and no message is rewritten: the
        # decision buttons on this notification stay exactly as they were. The
        # claim still consumes the token, because every row in this table is
        # single-use by design; a second press is answered from the recorded
        # result instead of silently doing nothing.
        request = await get_access_request(conn, claim.resource)
        if request is None:
            outcome = "Essa solicitação não existe mais."
            await callback_actions.record_result(conn, token, outcome)
            await answered(outcome)
            return
        await callback_actions.record_result(
            conn, token, "Os detalhes já foram enviados nesta conversa.",
        )
        await answered()
        await _send(chat_id, _render_request_details(request))
        return

    if action == "staff.deactivate":
        if claim.operation != "confirm" or not claim.resource:
            # Nothing mints this any more: it was the main menu's entry, and the
            # menu is gone. It stays as the safe landing for a press that is not
            # a confirmation, including a button still sitting in somebody's
            # chat history from before the menu was removed. Opening the flow
            # asks who, names them, and waits; skipping to the deletion would be
            # a shorter path to an irreversible act.
            await answered()
            await _ask_for_staff_id(conn, chat_id, "deactivate")
            return
        target_id = parse_int_id(claim.resource.rpartition(":")[2])
        if target_id is None:
            logger.error("Deactivation button carried no usable target: %s", claim.resource)
            await answered(_PRESS_REFUSALS["unknown"])
            return
        await answered()
        # The same step was also being awaited as the word confirmar. It has
        # just been answered by a press, so the question is closed either way.
        await pending_actions.forget(conn, chat_id)
        outcome = await _apply_deactivation(conn, chat_id, staff, target_id)
        await callback_actions.record_result(conn, token, outcome)
        if message_id is not None:
            original = (message.get("text") or f"Exclusão do usuário [{target_id}]").strip()
            await _rewrite_decided(chat_id, message_id, original, outcome)
        await _retire_deactivation_buttons(conn, target_id, keep_token=token)
        return

    if action == "document.delete":
        document_id = parse_int_id((claim.resource or "").rpartition(":")[2])
        if claim.operation != "confirm" or document_id is None:
            logger.error("Document deletion button carried no usable target: %s", claim.resource)
            await answered(_PRESS_REFUSALS["unknown"])
            return
        await answered()
        # The same step was also waiting for the word confirmar. A press has
        # answered it, so the question is closed either way.
        await pending_actions.forget(conn, chat_id)
        outcome = await _apply_document_removal(conn, chat_id, staff, document_id)
        await callback_actions.record_result(conn, token, outcome)
        if message_id is not None:
            original = (message.get("text") or f"Exclusão do documento [{document_id}]").strip()
            await _rewrite_decided(chat_id, message_id, original, outcome)
        await _retire_document_deletion_buttons(conn, document_id, keep_token=token)
        return

    # Every remaining action is navigation or a prompt: nothing here decides
    # anything, retires a sibling or rewrites a message, so each branch below
    # only has to run the same step the equivalent command or menu text would.
    operation = claim.operation
    if action == "document.browse":
        await answered()
        if operation == "search":
            await _ask_for_document_filter(conn, chat_id)
        else:
            await _send_document_catalog(conn, chat_id)
        return

    if action == "document.view_details":
        await answered()
        await _ask_for_document_id(conn, chat_id, "details")
        return

    if action == "document.edit":
        await answered()
        await _ask_for_document_id(conn, chat_id, "edit")
        return

    if action == "document.upload_help":
        await answered()
        await _send(chat_id, _DOCUMENT_HELP)
        return

    if action == "document.read_original":
        await answered()
        await _ask_for_document_id(conn, chat_id, "original")
        return

    if action == "research.external":
        await answered()
        await _ask_for_research_question(conn, chat_id)
        return

    if action == "drafting.compose":
        await answered()
        await _ask_for_draft_instructions(conn, chat_id)
        return

    if action == "delivery.export":
        await answered()
        await _ask_export_format(conn, chat_id)
        return

    logger.warning("Button press for an action with no handler: %s", action)
    await answered("Essa ação ainda não está disponível.")


async def process_legal_message(conn, update):
    # Which kind of update actually arrived. Telegram forwards only the kinds
    # registered in the webhook's allowed_updates, and a kind left out is
    # dropped on Telegram's servers with no trace here — indistinguishable, from
    # inside, from an update that arrived and was mishandled. One line settles
    # which of the two happened. Keys only: the values carry the message.
    logger.info("Update received: kinds=%s", sorted(k for k in update if k != "update_id"))

    if update.get("channel") == "panel":
        await _process_panel_update(conn, update)
        return

    callback = update.get("callback_query")
    if callback:
        pressed_in = ((callback.get("message") or {}).get("chat") or {}).get("id")
        with channels.serving(channels.TELEGRAM, pressed_in):
            await _process_callback_query(conn, callback)
        return

    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    # The corpus is the whole firm's confidential material, so access is granted
    # to a person, never to a room: in a group every member would inherit the
    # authorization of whoever added the bot, and the audit log would record the
    # group instead of the lawyer.
    if chat.get("type") not in (None, "private"):
        logger.warning("Ignored a message from a non-private chat")
        return
    sender_id = (msg.get("from") or {}).get("id")
    if sender_id is not None and str(sender_id) != str(chat_id):
        logger.warning("Ignored a message whose sender does not own the chat")
        return

    text = (msg.get("text") or "").strip()
    voice = msg.get("voice") or msg.get("audio")
    document = msg.get("document")
    caption = (msg.get("caption") or "").strip()

    # ── Access gate ──────────────────────────────────────────────────────────
    staff = await get_staff_by_chat_id(conn, chat_id)
    if not staff:
        logger.info("Access request received")
        await _handle_pending_access(conn, msg, chat_id)
        return

    with channels.serving(channels.TELEGRAM, chat_id):
        async with _serving(conn, staff):
            await _serve_message(conn, update, msg, chat_id, staff, text, voice, document,
                                 caption)


_PANEL_NO_ACCESS = ("Seu acesso ao assistente não está ativo. "
                    "Fale com o administrador do escritório.")

# A message with nothing but its attachment asks this (an audio file's own
# question is its transcript's summary; a voice note's is its transcript).
_PHOTO_QUESTION = "o que é isto?"
_AUDIO_QUESTION = "Resuma a transcrição."

# What the person reads after the reading when they asked to archive it.
_ARCHIVED = "Guardado no arquivo, na sua pasta pessoal: {title} (documento {document_id})."
_ARCHIVE_REFUSED = "Arquivar pelo chat fica com advogados e administradores."
_ARCHIVE_FAILED = ("Não consegui guardar o anexo no arquivo agora. "
                   "Para guardar, anexe de novo e marque a opção.")


@dataclasses.dataclass(frozen=True)
class UpdateLimit:
    """The worker's time limit on the update being served (set by
    `legal_agent`), and the answer's usual time, so the media stage can move
    the limit: its own deadline first, then the answer's from where it ended."""

    timeout: asyncio.Timeout
    answer_seconds: float


update_limit: ContextVar[UpdateLimit | None] = ContextVar("update_limit", default=None)


def _allow_update(seconds: float) -> None:
    """The update may run `seconds` more, then the answer's usual time."""
    limit = update_limit.get()
    if limit is not None:
        limit.timeout.reschedule(
            asyncio.get_running_loop().time() + seconds + limit.answer_seconds)


async def _process_panel_update(conn, update) -> None:
    """A message or a press from the web panel, answered in the panel.

    The request row is the authority, not the payload. It is started once,
    in its own committed statement, so a queue retry or a redelivery after a
    crash finds it started and does nothing (revision 6). Its person is the
    one it names, re-read now: deactivated since they wrote, they are told
    so and nothing else runs. Any failure, the worker's time limit included,
    ends it failed, and it is never run again.
    """
    try:
        request_id = UUID(str(update.get("panel_request")))
    except ValueError:
        logger.warning("A panel update carried no request")
        return
    if update.get("update_id") != f"panel:{request_id}":
        logger.warning("A panel update named a request other than its own")
        return
    if not await panel.start(conn, request_id):
        logger.info("Panel request not startable (already started or ended); skipped")
        return
    try:
        await _serve_panel_request(conn, update, request_id)
        await panel.finish(conn, request_id, "done")
    except panel.RequestGone:
        # Erased or deactivated while it ran: nothing was written, and
        # nothing is retried.
        logger.info("Panel request stopped: its person was erased or deactivated")
        await _end_panel_request_failed(conn, request_id)
        # No answer was written, so there is nothing for a notice to point
        # at. A deactivated person's request still exists, so the writer
        # check below would not stop it.
        return
    except BaseException:
        await _end_panel_request_failed(conn, request_id)
        raise
    # The reply and its ending are committed: now the phone may say so, to
    # whoever wrote it. Only a message gets a new answer; a press (a rating,
    # a confirmation) ends `done` too, on the turn the person is looking at.
    # `notify` never raises, so this cannot undo either.
    writer = await panel.message_writer(conn, request_id)
    if writer is not None:
        await push.notify(writer, "answer")


_attachment_received: ContextVar[bool] = ContextVar("panel_attachment_received", default=False)


async def _serve_panel_request(conn, update, request_id: UUID) -> None:
    owner = await panel.owner(conn, request_id)
    if owner is None:
        return
    staff_id, chat_id = owner
    channel = channels.PanelChannel(conn, staff_id=staff_id, chat_id=chat_id,
                                    request_id=request_id)
    with channels.serving(channel, chat_id), panel.serving_request(request_id):
        staff = await get_staff_by_chat_id(conn, chat_id)
        if staff is None or staff.id != staff_id:
            logger.info("Panel request refused: its person is no longer active staff")
            await channel.refuse(_PANEL_NO_ACCESS)
            return
        # Rebuilt around the row's chat, whatever the payload carried.
        private = {"id": chat_id, "type": "private"}
        callback = update.get("callback_query")
        if callback:
            message = {**(callback.get("message") or {}), "chat": private}
            await _process_callback_query(conn, {**callback, "from": {"id": chat_id},
                                                 "message": message})
            return
        text = ((update.get("message") or {}).get("text") or "").strip()
        # Counted once, here, whatever the message carries: refused before
        # its attachment is decrypted or anything is paid for, and not
        # counted again by the answer. Every refusal is said, unlike on
        # Telegram: the page is waiting on this request, and a silent end
        # would leave the person looking at a message (and an attachment)
        # with no reply.
        allowed, _first = await _rate_limit(chat_id)
        if not allowed:
            logger.warning("Rate limit hit for legal assistant")
            await _refuse_panel_message(conn, request_id, chat_id, staff_id, _RATE_LIMITED)
            return
        # What the attachment is, its seal still closed.
        info = await panel_uploads.peek_upload(conn, request_id)
        if info is None and await panel.attachment_sent(conn, request_id):
            # Sent with an attachment no longer there (purged, past its day):
            # never answered as the text alone, whatever was typed.
            await _refuse_panel_message(conn, request_id, chat_id, staff_id,
                                        panel_intake.NO_UPLOAD)
            return
        if info is not None and info.kind == "document":
            # As on Telegram: archived, with what was typed read as its
            # caption, and nothing answered. The rank decides before the
            # seal is opened.
            await _ingest_panel_document(conn, request_id, chat_id, info, text)
            return
        upload = None
        if info is not None:
            upload = await panel_uploads.take_upload(conn, request_id)
            if upload is None:
                # Gone since the peek, or a seal that does not open.
                await _refuse_panel_message(conn, request_id, chat_id, staff_id,
                                            panel_intake.NO_UPLOAD)
                return
            question = await _read_attachment(conn, request_id, chat_id, upload, text)
            if question is None:
                return
            text = question
        msg = {"chat": private, "from": {"id": chat_id}, "text": text}
        async with _serving(conn, staff):
            token = _attachment_received.set(upload is not None and upload.kind in ("photo", "audio"))
            try:
                await _serve_message(conn, update, msg, chat_id, staff, text, None, None, "",
                                     counted=True)
            finally:
                _attachment_received.reset(token)


async def _refuse_panel_message(conn, request_id: UUID, chat_id: str, staff_id: int,
                                message: str | None) -> None:
    """A message refused before its attachment was read: what the person
    reads (if anything) and the deletion of whatever upload the request
    holds, in one transaction behind the publication guard."""
    async with panel.publishing(conn, request_id) as ok:
        if not ok:
            raise panel.RequestGone
        if message:
            async with conn.cursor() as cur:
                await conversation.insert_turn(
                    cur, staff_id=staff_id, chat_id=chat_id, speaker="bot", text=message,
                    channel="panel", request_id=request_id,
                )
        await panel_uploads.delete_request_upload(conn, request_id, commit=False)


def _media_seconds(kind: str) -> float:
    return {
        "photo": settings.panel_media_photo_seconds,
        "voice": settings.panel_media_voice_seconds,
        "audio": settings.panel_media_audio_seconds,
        "document": settings.panel_media_document_seconds,
    }[kind]


async def _ingest_panel_document(conn, request_id: UUID, chat_id: str,
                                 info: panel_uploads.UploadInfo, caption: str) -> None:
    """A document sent in the panel's chat, archived as Telegram archives
    one (`_queue_document`): the same rank, the same copy, the same
    deduplication by text, the caption read for `tipo` and `caso`.

    In the media stage's deadline. Extraction, embeddings and the stored
    original run before the publication guard, so an erasure never waits on
    them; under it, the rank is re-read on the locked staff row and decides,
    the document is written, what the person reads is written, and the
    upload is deleted, all in one transaction. An original the database
    did not come to hold is taken back.

    The rank is re-read and decides before the upload is unsealed: a
    person who may not archive has nothing of theirs decrypted.
    """
    filename = info.filename or "documento"
    seconds = _media_seconds("document")
    await panel.begin_media(conn, request_id, seconds)
    _allow_update(seconds)
    deadline = asyncio.get_running_loop().time() + seconds

    person = await get_staff_by_id(conn, info.staff_id)
    await conn.commit()
    if person is None:
        # No longer active: the guard would stop every write anyway.
        raise panel.RequestGone
    decision = authorization.decide(person, "document.ingest")
    if not decision.allowed:
        # Nothing is decrypted or paid for a rank that may not archive.
        await _publish_document(conn, request_id, chat_id, info, decision.reason)
        return
    upload = await panel_uploads.take_upload(conn, request_id)
    if upload is None:
        # Gone since the peek, or a seal that does not open.
        await _publish_document(conn, request_id, chat_id, info, panel_intake.NO_UPLOAD)
        return
    await _send(chat_id, ingestion.document_received(filename))

    prepared: ingestion.PreparedDocument | None = None
    written: tuple[ingestion.Ingested, bool] | None = None
    # From the moment an original is stored until a document owns it, any
    # way out (a failed commit, a refusal, a cancellation) takes it back.
    try:
        failure: str | None = None
        try:
            async with asyncio.timeout_at(deadline):
                prepared = await ingestion.prepare_document(conn, upload.data,
                                                            filename=filename)
            # Not left open while the guard is waited on.
            await conn.commit()
        except ValueError:
            logger.warning("Permanent document ingestion error (panel)", exc_info=True)
            failure = ingestion.document_failed(filename)
        except panel.RequestGone:
            raise
        except Exception:
            # The deadline, the embeddings, the store or the commit: a reading
            # that failed.
            logger.exception("Document ingestion failed (panel)")
            failure = media.UNREADABLE
        if failure is not None:
            await conn.rollback()
            await _publish_document(conn, request_id, chat_id, upload, failure)
            return

        assert prepared is not None
        written = await _publish_document(
            conn, request_id, chat_id, upload, None, prepared=prepared, caption=caption)
    finally:
        if prepared is not None and (written is None or not written[1]):
            await _discard_document(prepared)
    if written is not None and written[0].chunk_count:
        # Cached answers were true for the archive a moment ago.
        await cache.invalidate_answers()


async def _discard_document(prepared: ingestion.PreparedDocument) -> None:
    try:
        await ingestion.discard_document(prepared)
    except Exception:
        logger.exception("Could not remove an original stored for a document not written")


async def _publish_document(conn, request_id: UUID, chat_id: str,
                            upload: panel_uploads.UploadInfo, message: str | None, *,
                            prepared: ingestion.PreparedDocument | None = None,
                            caption: str = "") -> tuple[ingestion.Ingested, bool] | None:
    """What the person reads, the document when there is one, and the
    upload's deletion, in one transaction behind the publication guard.
    With `prepared`, the rank re-read under the guard's lock decides whether
    it is written: refused, its refusal is what the person reads. Returns
    what was written and whether the stored original is now a document's,
    or None when nothing was written."""
    written = None
    async with panel.publishing(conn, request_id) as ok:
        if not ok:
            raise panel.RequestGone
        if prepared is not None:
            filename = upload.filename or "documento"
            person = await get_staff_by_id(conn, upload.staff_id)
            if person is None:
                raise panel.RequestGone  # the guard saw them active; not reached
            decision = authorization.decide(person, "document.ingest")
            if not decision.allowed:
                message = decision.reason
            else:
                result, kept = await ingestion.write_document(
                    conn, prepared, title=filename, uploaded_by=person.id,
                    doc_type=_caption_field(caption, "tipo"),
                    matter=_caption_field(caption, "(?:caso|processo|matéria)"))
                written = (result, kept)
                message = (ingestion.document_done(filename) if result.chunk_count
                           else ingestion.document_known(filename))
        async with conn.cursor() as cur:
            await conversation.insert_turn(
                cur, staff_id=upload.staff_id, chat_id=chat_id, speaker="bot",
                text=conversation.redact_access_links(message or ""), channel="panel",
                request_id=request_id,
            )
        await panel_uploads.delete_upload(conn, upload.id, commit=False)
    return written


async def _read_attachment(conn, request_id: UUID, chat_id: str,
                           upload: panel_uploads.Upload, text: str) -> str | None:
    """The media stage: read the attachment within its own deadline, then
    publish the reading and delete the upload together, and return the
    question the answer is to take. None when it could not be read: the
    person is told why, the upload is deleted, and nothing else runs.

    The request records the stage's deadline (`panel.begin_media`), so it is
    not given up on while it reads, and the worker's limit on the update
    moves the same way: the stage's deadline, then the answer's usual 300 s
    from wherever the stage ended.
    """
    seconds = _media_seconds(upload.kind)
    await panel.begin_media(conn, request_id, seconds)
    _allow_update(seconds)
    # One deadline for the whole stage, the archive's preparation included,
    # so the answer keeps its usual time whatever the archive costs.
    deadline = asyncio.get_running_loop().time() + seconds
    message = media.UNREADABLE
    duration = None
    try:
        async with asyncio.timeout_at(deadline):
            if upload.kind == "photo":
                question = text or _PHOTO_QUESTION
                reading = await media.read_photo(upload.data, question)
                published = f"Leitura da foto:\n{reading}"
                archived = reading
            else:
                voice = upload.kind == "voice"
                transcript = await media.transcribe_audio(upload.data, voice=voice,
                                                          prompt=_VOICE_HINT)
                duration = transcript.seconds
                spoken = archived = (transcript.text or "").strip()
                if is_silence_artifact(spoken):
                    # A voice note answers as Telegram does; an audio file
                    # with nothing read is a reading that failed.
                    raise media.MediaError(_SILENT_VOICE if voice else media.UNREADABLE)
                if voice:
                    question = " ".join(part for part in (text, spoken) if part)
                    published = f'🎤 Entendi: "{spoken}"'
                else:
                    question = text or _AUDIO_QUESTION
                    published = f"Transcrição ({transcript.minutes} min):\n{spoken}"
    except budget.BudgetExceeded as exc:
        logger.warning("Attachment refused: the %s pilot budget is spent", exc.window)
        message = budget_reached(exc)
    except media.MediaError as exc:
        logger.info("Attachment not read (%s)", upload.kind)
        message = exc.message
    except TimeoutError:
        logger.warning("Attachment not read within its %s s (%s)", seconds, upload.kind)
    except panel.RequestGone:
        raise
    except Exception:
        logger.exception("Attachment reading failed (%s)", upload.kind)
    else:
        prepared, note = (await _prepare_archive(conn, upload, archived, deadline)
                          if upload.archive else (None, None))
        document_id = None
        try:
            document_id = await _publish_reading(
                conn, request_id, chat_id, upload, published, seconds=duration,
                archive=prepared, archive_note=note)
        finally:
            if prepared is not None and document_id is None:
                await _discard_archive(prepared)
        if document_id is not None:
            # The person's own cached answers predate this document.
            await cache.invalidate_answers()
        _allow_update(0)
        return question
    await _publish_reading(conn, request_id, chat_id, upload, message, reading=False)
    return None


async def _prepare_archive(conn, upload: panel_uploads.Upload, text: str, deadline: float
                           ) -> tuple[ingestion.PreparedMedia | None, str | None]:
    """The archive's network work (embeddings, the stored original), before
    the publication guard takes its locks, so an erasure never waits on it,
    and within the media stage's `deadline`: past it, the archive is said
    to have failed and the reading and the answer still go out. Returns
    what to write under the guard, or why nothing will be.
    """
    try:
        async with asyncio.timeout_at(deadline):
            return await _prepare_archive_now(conn, upload, text)
    except TimeoutError:
        logger.warning("Archiving an attachment ran past its stage's deadline (%s)",
                       upload.kind)
        # A query cut off mid-way leaves its transaction to be rolled back.
        await conn.rollback()
        return None, _ARCHIVE_FAILED


async def _prepare_archive_now(conn, upload: panel_uploads.Upload, text: str
                               ) -> tuple[ingestion.PreparedMedia | None, str | None]:
    """The rank is read here only so nothing is paid for a person who may
    not archive; the check that decides is the one under the guard."""
    person = await get_staff_by_id(conn, upload.staff_id)
    # Not left open while the network is waited on.
    await conn.commit()
    if person is None:
        return None, None  # no longer active: the guard stops the request
    if not authorization.decide(person, "document.ingest").allowed:
        return None, _ARCHIVE_REFUSED
    try:
        prepared = await ingestion.prepare_media(
            data=upload.data, media_type=upload.media_type,
            text=conversation.redact_access_links(text), staff=person)
    except Exception:
        logger.exception("Archiving an attachment failed (%s)", upload.kind)
        return None, _ARCHIVE_FAILED
    return prepared, None


async def _discard_archive(prepared: ingestion.PreparedMedia) -> None:
    try:
        await ingestion.discard_media(prepared)
    except Exception:
        logger.exception("Could not remove an original stored for an archive not written")


async def _publish_reading(conn, request_id: UUID, chat_id: str,
                           upload: panel_uploads.Upload, text: str, *, reading: bool = True,
                           seconds: float | None = None,
                           archive: ingestion.PreparedMedia | None = None,
                           archive_note: str | None = None) -> int | None:
    """The reading (or why there is none) and the upload's deletion, in one
    transaction behind the publication guard: an erasure that ran first
    leaves nothing to write, one that comes after finds both.

    With `archive` (prepared before the guard), the archived document is
    written in the same transaction, when the person's rank, re-read under
    the guard's lock on their row, still allows it; its id is returned.
    `archive_note` says why an archive asked for was not prepared."""
    document_id = None
    async with panel.publishing(conn, request_id) as ok:
        if not ok:
            raise panel.RequestGone
        async with conn.cursor() as cur:
            await conversation.insert_turn(
                cur, staff_id=upload.staff_id, chat_id=chat_id, speaker="bot",
                text=conversation.redact_access_links(text), channel="panel",
                extras={"reading": upload.kind} if reading else None, request_id=request_id,
            )
        if reading and seconds is not None:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE conversation_turns SET extras = jsonb_set(extras, "
                    "'{attachment,seconds}', to_jsonb(%s::double precision)) "
                    "WHERE request_id = %s AND staff_id = %s AND speaker = 'person'",
                    (seconds, request_id, upload.staff_id),
                )
        if reading and archive is not None:
            # The staff row is locked by the guard: the rank read now is the
            # one in force, not the one the upload or the preparation saw.
            person = await get_staff_by_id(conn, upload.staff_id)
            if person is not None and authorization.decide(person, "document.ingest").allowed:
                label = "Foto" if upload.kind == "photo" else "Áudio"
                title = f"{label} de {local_now().strftime('%d/%m/%Y %H:%M')}"
                document_id = await ingestion.write_media(conn, archive, title=title,
                                                          staff=person)
                archive_note = _ARCHIVED.format(title=title, document_id=document_id)
            else:
                archive_note = _ARCHIVE_REFUSED
        if reading and archive_note:
            async with conn.cursor() as cur:
                await conversation.insert_turn(
                    cur, staff_id=upload.staff_id, chat_id=chat_id, speaker="bot",
                    text=archive_note, channel="panel", request_id=request_id,
                )
        await panel_uploads.delete_upload(conn, upload.id, commit=False)
    return document_id


async def _end_panel_request_failed(conn, request_id: UUID) -> None:
    """Mark the request failed on a fresh connection (the update's may be
    aborted or mid-query), shielded from the cancellation that may have
    brought us here, within a bounded wait. The update's own transaction is
    rolled back first: it may hold the request row's lock, which the fresh
    connection would otherwise wait on until it gave up."""
    async def end():
        try:
            await conn.rollback()
        except Exception:
            logger.warning("Could not roll back a failed panel request's connection")
        pool = await get_pool()
        async with pool.connection() as fresh:
            await panel.finish(fresh, request_id, "failed")

    try:
        await asyncio.wait_for(asyncio.shield(end()), timeout=_FAILED_PRESS_GRACE)
    except Exception:
        logger.exception("Could not mark a failed panel request")


async def _answer_question(conn, text, *, staff_id, chat_id, staff_name, routed=None):
    """`answer_question`, carrying the attachment in the panel: the one just
    read, or on a follow-up the latest within two hours. A stored turn alone
    would not reach the answer (recent turns go only to the router, cut
    short), so the text goes in its own field. Telegram has none."""
    attachment = (await conversation.latest_attachment_text(conn, staff_id)
                  if channels.for_chat(chat_id).name == "panel" else None)
    kwargs = {"attachment_text": attachment} if attachment else {}
    return await answer_question(conn, text, staff_id=staff_id, chat_id=chat_id,
                                 staff_name=staff_name, routed=routed, **kwargs)


async def _converse(conn, update, msg, chat_id, staff, text, pending) -> bool:
    """A sentence, read with the last two hours of the conversation.

    True when it was answered here; False sends it on today's path. An action
    becomes the command a person would type and runs through it, so the
    command's authorization and reply are the answer. The deadline regime and
    "dobro" count only when the person said them (`ground`).
    """
    history = await conversation.recent_turns(conn, staff.id)
    decision = await conversation_router.route(text, history, pending, local_now().date())
    if decision is None:
        return False
    if isinstance(decision, conversation_router.Clarify):
        await _send(chat_id, decision.question)
        return True
    if isinstance(decision, conversation_router.CommandOnly):
        await _send(chat_id, f"Isso eu faço pelo comando {decision.command}. "
                             f"Envie {decision.command} para começar.")
        return True
    if isinstance(decision, conversation_router.TaskRoute):
        await channels.for_chat(chat_id).typing(chat_id)
        reply = await _answer_question(
            conn, text, staff_id=staff.id, chat_id=chat_id, staff_name=staff.name,
            routed=decision,
        )
        await _deliver(chat_id, reply, text[:60], conn=conn, staff=staff)
        return True

    entry = _ACTIONS[decision.name]
    args = decision.args
    if pending is not None and pending.action == decision.name and pending.args:
        # The router answered what was missing; what was known stays.
        try:
            args = entry.arguments(**{**pending.args,
                                      **decision.args.model_dump(exclude_unset=True)})
        except Exception:
            args = decision.args
    person_texts = [turn.text for turn in history if turn.speaker == "person"] + [text]
    args = ground(decision.name, args, person_texts)
    question = entry.missing(args)
    if question:
        await pending_actions.remember(
            conn, chat_id, kind="action", action=decision.name,
            args=args.model_dump(mode="json"), ttl_seconds=_PENDING_DECISION_TTL,
        )
        await _send(chat_id, question)
        return True
    await pending_actions.forget(conn, chat_id)
    await _serve_message(conn, update, msg, chat_id, staff, entry.to_command(args),
                         None, None, "", from_conversation=True)
    return True


_RATE_LIMITED = "⚠️ Muitas solicitações em sequência. Aguarde um instante."


async def _rate_limit(chat_id) -> tuple[bool, bool]:
    """Count one request from this chat: (allowed, notify), notify only on
    the first refusal of a window. The shared counter in Redis holds across
    restarts and workers; the in-process one still guards the case where
    Redis is down."""
    allowed, notify = _limiter.check(str(chat_id))
    if allowed:
        allowed, notify = await cache.rate_limit_hit(
            chat_id, settings.rate_limit_max, settings.rate_limit_window,
        )
    return allowed, notify


async def _serve_message(conn, update, msg, chat_id, staff, text, voice, document,
                         caption, *, from_conversation: bool = False,
                         counted: bool = False) -> None:
    """Everything a message does, with this person's folders already bound.

    `from_conversation` is a command the agent built from a sentence already
    recorded and counted: it skips the rate limit, the length check and the
    person's turn, and is otherwise a typed command in every respect.
    `counted` is a panel message the worker already counted, before its
    attachment was read: it skips the rate limit only.
    """

    if not text and not voice and not document:
        return

    # Checked before the download and the Whisper call: transcription is the
    # most expensive thing this handler can be made to do.
    allowed, notify = ((True, False) if from_conversation or counted
                       else await _rate_limit(chat_id))
    if not allowed:
        if notify:
            await _send(chat_id, _RATE_LIMITED)
        logger.warning("Rate limit hit for legal assistant")
        return
    if document:
        if not await _may(chat_id, staff, "document.ingest"):
            return
        await _queue_document(
            conn,
            chat_id,
            document,
            staff,
            caption=caption,
            update_id=update.get("update_id"),
        )
        return

    dictated = False
    if not text:
        text = await _voice_to_text(chat_id, voice)
        if not text:
            return
        dictated = True
        # Recorded before the echo below, so the conversation reads in order.
        await _record_person(conn, staff, chat_id, text)
        # Whisper mishears names and jargon; showing the transcript lets staff
        # catch it before acting on an answer built from the wrong question.
        await _send(chat_id, f'🎤 Entendi: "{text}"')
    elif not from_conversation:
        await _record_person(conn, staff, chat_id, text)

    if not from_conversation and len(text) > settings.max_question_chars:
        await _send(chat_id, "A mensagem é muito longa. Pode resumir a pergunta?")
        return

    logger.info(
        "Legal request received: staff_id=%s voice=%s chars=%s",
        staff.id,
        dictated,
        len(text),
    )

    # A photo or an audio file just read: its question is about it, never a
    # command, so it goes straight to the answer with the attachment.
    if _attachment_received.get():
        reply = await _answer_question(conn, text, staff_id=staff.id,
                                       chat_id=chat_id, staff_name=staff.name)
        await _deliver(chat_id, reply, text[:60], conn=conn, staff=staff)
        return

    command = text.casefold()
    command_name = command.split(maxsplit=1)[0]

    # The panel refuses these before storing anything; refused here too, so no
    # route (a resumed step, a built command) runs a sign-in from the panel.
    if (command_name in panel_intake.REFUSED_COMMANDS
            and channels.for_chat(chat_id).name != "telegram"):
        await _send(chat_id, f"Isso é feito pelo Telegram: envie {command_name} ao bot.")
        return

    # /status answers at any step and is deliberately read-only: it reports the
    # pending flow instead of counting as "the person moved on" and dropping it.
    if command_name == "/status":
        text = await _status_for(conn, chat_id)
        if has_rank(staff.role, ROLE_ADMIN):
            text += "\n\n" + await _budget_status()
        await _send(chat_id, text)
        return

    # Read the open step once; the blocks below decide what it means.
    pending = await pending_actions.peek(conn, chat_id)

    # A code asked for by a bare /autorizar or /recusar arrives as the next message.
    if pending is not None and pending.kind == "decision":
        if command in ("/cancelar", "/cancel"):
            await pending_actions.forget(conn, chat_id)
            await _send(chat_id, "Ok, nenhuma decisão foi tomada.")
            return
        if not command.startswith("/"):
            approved = await _take_pending_decision(conn, chat_id)
            if approved is None:
                await _send(
                    chat_id,
                    "O pedido de código expirou. Envie /autorizar ou /recusar novamente.",
                )
                return
            if await _may(chat_id, staff, "access.review"):
                completed = await _decide_access(
                    conn, chat_id, staff, _normalize_request_code(text), approved,
                )
                if not completed:
                    await _remember_pending_decision(conn, chat_id, approved)
            return
        # Any other command means the manager moved on.
        await pending_actions.forget(conn, chat_id)
        pending = None

    # A step the conversation left open is dropped by any typed command but
    # /cancelar (which says so) and /status (which reports it).
    if (pending is not None and pending.kind == "action" and command.startswith("/")
            and command not in ("/cancelar", "/cancel")):
        await pending_actions.forget(conn, chat_id)
        pending = None

    handled, text = await _resume_pending_identifier(conn, chat_id, text, command, pending)
    if handled:
        return
    command = text.casefold()
    command_name = command.split(maxsplit=1)[0]

    if command in ("/cancelar", "/cancel"):
        if await pending_actions.consume(conn, chat_id):
            await _send(chat_id, "Ok, essa ação foi cancelada.")
        else:
            await _send(chat_id, "Não há nenhuma ação aguardando informação agora.")
        return

    # "quais as fontes?" / "manda em pdf" refer to the answer just delivered.
    if await _handle_answer_followup(conn, chat_id, text, command):
        return

    # /start is here on purpose: it is what Telegram sends the first time
    # anybody opens the bot, and the first use is the one moment this list is
    # pushed rather than asked for. /ajuda and /help stay because they are the
    # words somebody reaches for, and being told "comando desconhecido" for
    # asking for help is a poor way to answer it.
    if command in ("/start", "/comandos", "/ajuda", "/help"):
        await _send_commands(chat_id, staff)
        return

    if command == "/id":
        await _send(chat_id, f"Seu identificador: {chat_id}")
        return

    if command_name in ("/docs", "/documentos", "/buscar", "/busca"):
        parts = text.split(maxsplit=1)
        await _send_document_catalog(conn, chat_id, parts[1] if len(parts) > 1 else "")
        return

    if command in ("/casos", "/clientes"):
        await _send_matter_index(conn, chat_id)
        return

    if command_name in ("/autorizar", "/aurorizar", "/recusar"):
        if not await _may(chat_id, staff, "access.review"):
            return
        approved = command_name != "/recusar"
        parts = text.split(maxsplit=1)
        code = _normalize_request_code(parts[1]) if len(parts) > 1 else ""
        if not code:
            await _ask_for_request_code(conn, chat_id, approved)
            return
        await _decide_access(conn, chat_id, staff, code, approved)
        return

    if command == "/pendentes":
        if not await _may(chat_id, staff, "access.review"):
            return
        pending = await list_pending_access_requests(conn)
        if not pending:
            await _send(chat_id, "Não há solicitações de acesso pendentes.")
            return
        lines = [
            f"{request.request_code}: {request.name}"
            + (f" (@{request.username})" if request.username else "")
            for request in pending
        ]
        await _send(chat_id, "🔔 Solicitações pendentes:\n" + "\n".join(lines))
        return

    if command in ("/usuarios", "/equipe", "/time", "/staff"):
        if not await _may(chat_id, staff, "staff.read_directory"):
            return
        await _send_staff_directory(conn, chat_id)
        return

    if command_name in ("/editar", "/editar_documento", "/classificar"):
        if not await _may(chat_id, staff, "document.edit"):
            return
        parts = text.split(maxsplit=1)
        document_id = parse_int_id(parts[1].split()[0]) if len(parts) > 1 else None
        if document_id is None:
            await _ask_for_document_id(conn, chat_id, "edit")
            return
        document = await get_document_summary(conn, document_id, viewer())
        if not document:
            await _send(chat_id, "Não encontrei um documento com esse ID.")
            return
        # The same words that classify a document when it is attached classify
        # it later: "tipo:" and "caso:" are already how a caption says this, so
        # there is no second vocabulary to learn.
        argument = parts[1] if len(parts) > 1 else ""
        _, _, after_id = argument.partition(" ")
        fields = {
            "title": _caption_field(after_id, "nome"),
            "doc_type": _caption_field(after_id, "tipo"),
            "matter": _caption_field(after_id, "(?:caso|processo|matéria|materia)"),
        }
        if not any(fields.values()):
            await _remember_pending_identifier(
                conn, chat_id, "edit_fields", target_id=document_id,
            )
            await _send(chat_id, await _describe_editable(conn, document))
            return
        for field, limit in (
            ("title", MAX_TITLE_CHARS),
            ("doc_type", MAX_DOC_TYPE_CHARS),
            ("matter", MAX_MATTER_CHARS),
        ):
            value = fields[field]
            if value and len(value) > limit:
                await _send(
                    chat_id,
                    f"Esse valor tem {len(value)} caracteres e o limite é {limit}.",
                )
                return
        changed = await update_document_fields(
            conn,
            document_id,
            title=fields["title"],
            doc_type=fields["doc_type"],
            matter=fields["matter"],
            scope=viewer(),
        )
        if not changed:
            await _send(chat_id, "Não encontrei um documento com esse ID.")
            return
        await log_access(
            conn,
            action="edit_document",
            staff_id=staff.id,
            chat_id=chat_id,
            query="; ".join(f"{k}={v}" for k, v in fields.items() if v)[:500],
            document_ids=[document_id],
        )
        updated = await get_document_summary(conn, document_id, viewer())
        await _send(
            chat_id,
            f"✅ Documento [{document_id}] atualizado.\n\n"
            + _render_document_details(updated or document),
        )
        return

    if command_name in ("/renomear", "/renomear_documento"):
        if not await _may(chat_id, staff, "document.edit"):
            return
        parts = text.split(maxsplit=2)
        document_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if document_id is None:
            await _ask_for_document_id(conn, chat_id, "rename")
            return
        document = await get_document_summary(conn, document_id, viewer())
        if not document:
            await _send(chat_id, "Não encontrei um documento com esse ID.")
            return
        # A name can contain spaces, so everything after the id is the name.
        new_title = " ".join((parts[2] if len(parts) > 2 else "").split())
        if not new_title:
            await _remember_pending_identifier(
                conn, chat_id, "rename_title", target_id=document_id,
            )
            await _send(
                chat_id,
                f"Como devo chamar o documento [{document_id}]?\n"
                f"Nome atual: {document.title}\n\n"
                "Envie o novo nome ou /cancelar.",
            )
            return
        if len(new_title) > MAX_TITLE_CHARS:
            # The column would truncate silently, and a silent truncation is a
            # lie about what was saved.
            await _send(
                chat_id,
                f"Esse nome tem {len(new_title)} caracteres e o limite é "
                f"{MAX_TITLE_CHARS}. Pode encurtar?",
            )
            return
        previous = document.title
        if not await rename_document(conn, document_id, new_title, scope=viewer()):
            await _send(chat_id, "Não encontrei um documento com esse ID.")
            return
        await log_access(
            conn,
            action="rename_document",
            staff_id=staff.id,
            chat_id=chat_id,
            query=new_title,
            document_ids=[document_id],
        )
        await _send(
            chat_id,
            f"✅ Documento [{document_id}] renomeado.\n"
            f"Antes: {previous}\n"
            f"Agora: {new_title}",
        )
        return

    if command_name in ("/importar", "/importar_documentos"):
        # Same rank as attaching a file, because it is the same act: putting
        # documents into the archive. The bucket is a delivery route, not a
        # second door with a different lock.
        if not await _may(chat_id, staff, "document.ingest"):
            return
        await channels.for_chat(chat_id).typing(chat_id)
        try:
            report = await import_new_documents(conn, staff_id=staff.id)
        except inbox_bucket.InboxError as exc:
            await _send(chat_id, f"Não consegui ler o balde de documentos: {exc}.")
            return
        await _send(chat_id, _render_import_report(report))
        return

    if command_name in ("/excluir_documento", "/apagar_documento"):
        if not await _may(chat_id, staff, "document.delete"):
            return
        parts = text.split()
        document_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if document_id is None:
            await _ask_for_document_id(conn, chat_id, "delete_document")
            return
        confirmed = len(parts) == 3 and parts[2].casefold() == "confirmar"
        if not confirmed:
            document = await get_document_summary(conn, document_id, viewer())
            if not document:
                await _send(chat_id, "Não encontrei um documento com esse ID.")
                return
            await _remember_pending_identifier(
                conn, chat_id, "confirm_document_deletion", target_id=document.id,
            )
            # Naming the document back is the whole point of the step: an id is
            # easy to mistype and this is the one command with nothing to undo
            # it. The only copy left afterwards is in a backup.
            keyboard = await _document_deletion_keyboard(conn, chat_id, staff, document.id)
            message_id = await _send(
                chat_id,
                f"⚠️ Isso apagará definitivamente o documento [{document.id}] "
                f"{document.title}, seus trechos indexados e o arquivo original. "
                "O histórico de auditoria é preservado. Depois disso, só uma "
                "restauração de backup traz o documento de volta.\n\n"
                "Toque no botão, responda exatamente confirmar, envie /cancelar "
                f"ou use a forma completa:\n{command_name} {document.id} confirmar",
                reply_markup=keyboard,
            )
            if keyboard and message_id:
                try:
                    await callback_actions.attach_reference(
                        conn,
                        [keyboard["inline_keyboard"][0][0]["callback_data"]],
                        reference=str(message_id),
                    )
                except Exception:
                    # Only costs the later rewrite of this message; the button
                    # itself still works and still deletes once.
                    logger.exception("Could not record which message holds the button")
            return
        await _apply_document_removal(conn, chat_id, staff, document_id)
        # A preview elsewhere, or an older one here, may still offer to delete
        # a document that is already gone.
        await _retire_document_deletion_buttons(conn, document_id)
        return

    if command_name in ("/excluir_usuario", "/excluir", "/revogar"):
        if not await _may(chat_id, staff, "staff.deactivate"):
            return
        parts = text.split()
        target_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if target_id is None:
            await _ask_for_staff_id(conn, chat_id, "deactivate")
            return
        confirmed = len(parts) == 3 and parts[2].casefold() == "confirmar"
        if not confirmed:
            target = await get_staff_by_id(conn, target_id)
            if not target:
                await _send(chat_id, "Não encontrei um usuário ativo com esse ID.")
                return
            if target.id == staff.id or normalize_role(target.role) == ROLE_OWNER:
                await _send(
                    chat_id,
                    "Contas owner não podem ser excluídas por este comando.",
                )
                return
            label = ROLE_LABELS[normalize_role(target.role)]
            await _remember_pending_identifier(
                conn,
                chat_id,
                "confirm_deactivation",
                target_id=target.id,
            )
            keyboard = await _deactivation_keyboard(conn, chat_id, staff, target.id)
            message_id = await _send(
                chat_id,
                f"⚠️ Isso revogará imediatamente o acesso de {target.name} ({label}) "
                "e preservará o histórico de auditoria. Toque no botão, responda "
                "exatamente confirmar, envie /cancelar ou use a forma completa:\n"
                f"{command_name} {target.id} confirmar",
                reply_markup=keyboard,
            )
            if keyboard and message_id:
                try:
                    await callback_actions.attach_reference(
                        conn,
                        [keyboard["inline_keyboard"][0][0]["callback_data"]],
                        reference=str(message_id),
                    )
                except Exception:
                    # Only costs the later rewrite of this message; the button
                    # itself still works and still revokes once.
                    logger.exception("Could not record which message holds the button")
            return
        await _apply_deactivation(conn, chat_id, staff, target_id)
        # The preview in another chat, or an older one here, is now stale: the
        # account is already gone, so the button that offered to remove it is
        # retired and its message rewritten.
        await _retire_deactivation_buttons(conn, target_id)
        return

    if command_name in ("/esquecer", "/esquecer_pessoa"):
        if not await _may(chat_id, staff, "staff.erase"):
            return
        parts = text.split()
        target_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if target_id is None:
            await _ask_for_staff_id(conn, chat_id, "erase")
            return
        confirmed = len(parts) == 3 and parts[2] == "APAGAR"
        if not confirmed:
            target = await get_staff_by_id(conn, target_id)
            if not target:
                await _send(chat_id, "Não encontrei uma pessoa com esse ID.")
                return
            if target.id == staff.id or normalize_role(target.role) == ROLE_OWNER:
                await _send(
                    chat_id,
                    "Contas owner não podem ser apagadas por este comando.",
                )
                return
            await _remember_pending_identifier(
                conn, chat_id, "confirm_erase", target_id=target.id,
            )
            # No button. Every other confirmation here offers one, and this is
            # the only operation with nothing behind it to undo: the friction of
            # typing the word is the safeguard.
            await _send(chat_id, _erasure_warning(target, command_name))
            return
        await _apply_erasure(conn, chat_id, staff, target_id)
        return

    if command_name in ("/promover", "/rebaixar"):
        if not await _may(chat_id, staff, "staff.change_rank"):
            return
        parts = text.split()
        target_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if target_id is None:
            action = "promote" if command_name == "/promover" else "demote"
            await _ask_for_staff_id(conn, chat_id, action)
            return
        target = await get_staff_by_id(conn, target_id)
        if not target:
            await _send(chat_id, "Não encontrei um usuário ativo com esse ID.")
            return
        if normalize_role(target.role) == ROLE_OWNER:
            await _send(chat_id, "O papel de owner não pode ser alterado por este comando.")
            return
        # One step at a time on the scale, so a promotion is always a deliberate
        # decision and nobody jumps from estagiário to admin by accident. Owner
        # is not reachable: the firm has one, and it is not granted by command.
        current = role_rank(target.role)
        step = 1 if command_name == "/promover" else -1
        new_index = min(max(current + step, 0), ROLE_ORDER.index(ROLE_ADMIN))
        new_role = ROLE_ORDER[new_index]
        if new_role == normalize_role(target.role):
            limit = "no topo que este comando alcança" if step > 0 else "no papel mais básico"
            await _send(
                chat_id,
                f"{target.name} já está {limit} ({ROLE_LABELS[new_role]}).",
            )
            return
        updated = await set_staff_role(conn, target.id, new_role)
        if not updated:
            await _send(chat_id, "Não encontrei um usuário ativo com esse ID.")
            return
        label = ROLE_LABELS[normalize_role(updated.role)]
        await _send(chat_id, f"✅ {updated.name} agora é {label}.")
        if updated.chat_id:
            await _send(
                updated.chat_id,
                f"Seu papel no Assistente Legal Demo & Associados foi alterado para {label}.",
            )
        return

    if command_name in ("/assumir",):
        if not await _may(chat_id, staff, "document.claim"):
            return
        await _claim_document(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/compartilhar", "/compartilha", "/mover"):
        if not await _may(chat_id, staff, "document.share"):
            return
        await _share_document(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/onedrive", "/meusarquivos"):
        if not await _may(chat_id, staff, "microsoft.connect"):
            return
        await _handle_onedrive(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/microsoft", "/ms"):
        if not await _may(chat_id, staff, "microsoft.connect"):
            return
        await _handle_microsoft(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/web", "/acesso"):
        if command.split()[1:] == ["encerrar"]:
            if not await _may(chat_id, staff, "web.end_all"):
                return
            await web_access.revoke_devices(conn, staff.id)
            await _send(
                chat_id,
                "Pronto: todas as chaves de Face ID foram removidas e todas as sessões "
                "do painel foram encerradas. Para voltar, peça um link com /web.",
            )
            return
        if not await _may(chat_id, staff, "web.access"):
            return
        await _handle_web(conn, chat_id, staff)
        return

    if command_name in ("/mfa", "/autenticador"):
        if not await _may(chat_id, staff, "web.access"):
            return
        await _handle_mfa(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/pje",):
        if not await _may(chat_id, staff, "pje.connect"):
            return
        await _handle_pje(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/intimacoes", "/intimacao"):
        if not await _may(chat_id, staff, "pje.read"):
            return
        await _handle_intimacoes(conn, chat_id, staff)
        return

    if command_name in ("/autos",):
        if not await _may(chat_id, staff, "pje.read"):
            return
        await _handle_autos(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/grupos", "/equipes", "/grupo"):
        await _handle_teams(conn, chat_id, staff, text.split()[1:])
        return

    if command_name in ("/prazo", "/prazos", "/vencimento"):
        if not await _may(chat_id, staff, "deadline.calculate"):
            return
        await _handle_deadline(conn, chat_id, text.split()[1:])
        return

    if command_name in ("/feriados", "/feriado"):
        if not await _may(chat_id, staff, "deadline.calculate"):
            return
        await _handle_holidays(conn, chat_id, staff, command_name, text.split()[1:])
        return

    if command_name in ("/oab", "/acompanhar", "/desacompanhar"):
        if not await _may(chat_id, staff, "publications.watch"):
            return
        arguments = text.split()[1:]
        if command_name == "/oab":
            await _watch_oab(conn, chat_id, staff, arguments)
        elif command_name == "/acompanhar":
            await _watch_process(conn, chat_id, staff, arguments)
        else:
            await _unwatch(conn, chat_id, staff, arguments)
        return

    if command_name in ("/processo", "/processos"):
        numbers = datajud.extract_process_numbers(text, limit=1)
        if not numbers:
            await _remember_pending_identifier(conn, chat_id, "process")
            await _send(
                chat_id,
                "Qual é o número CNJ do processo? Envie o número completo ou /cancelar.\n"
                "Ex.: 1000000-00.2024.8.26.0100",
            )
            return
        await _report_process(conn, chat_id, staff, numbers[0])
        return

    if command_name in ("/ingerir", "/ingere", "/ingest"):
        if not await _may(chat_id, staff, "document.ingest"):
            return
        parts = text.split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        if not argument:
            await _send(
                chat_id,
                "Uso: /ingerir <url oficial> [; caso: Nome; tipo: acórdão]\n"
                "Baixo a peça da página oficial, ingiro no acervo e te envio o arquivo "
                "original. Se quiser apenas indexar, use /ingerir <url> so-indexar.",
            )
            return
        await _ingest_from_url(conn, chat_id, staff, argument)
        return

    if command_name in ("/atualizar", "/atualiza"):
        if not await _may(chat_id, staff, "document.refresh"):
            return
        await _refresh_from_official(conn, chat_id, staff, text.split()[1:])
        return

    if command in ("/fontes", "/fonte"):
        _, reply = _last_delivery(chat_id)
        if reply is None:
            await _send(chat_id, "Ainda não há uma resposta recente para detalhar as fontes.")
            return
        await _send_sources(chat_id, reply)
        return

    if command_name in ("/exportar", "/exporta", "/export"):
        parts = text.split(maxsplit=1)
        chosen = normalize_format(parts[1]) if len(parts) > 1 else None
        if chosen:
            await _export_last_answer(chat_id, chosen)
        else:
            await _ask_export_format(conn, chat_id)
        return

    if command.startswith("/arquivo"):
        if not await _may(chat_id, staff, "document.read_original"):
            return
        parts = text.split()
        doc_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if doc_id is None:
            await _ask_for_document_id(conn, chat_id, "original")
            return
        await _send_original_document(conn, chat_id, doc_id, staff.id)
        return

    if command.startswith("/detalhes") or command.startswith("/detalhe"):
        parts = text.split()
        doc_id = parse_int_id(parts[1]) if len(parts) > 1 else None
        if doc_id is None:
            await _ask_for_document_id(conn, chat_id, "details")
            return
        document = await get_document_summary(conn, doc_id, viewer())
        if not document:
            await _send(chat_id, f"Não encontrei o documento {doc_id} no acervo.")
            return
        official = await get_official_source(conn, doc_id)
        folder = await folder_of(conn, doc_id)
        label = None
        if folder is not None:
            team_name = None
            if folder[1] is not None:
                team_name = next(
                    (name for team_id, name, _ in await list_teams(conn) if team_id == folder[1]),
                    None,
                )
            label = _folder_label(folder[0], team_name)
        await _send(chat_id, _render_document_details(document, official, label))
        return

    if command.startswith("/resumo"):
        # Answered rather than dropped into "unknown command". Both were real
        # commands for months and are in people's habits, so the reply has to
        # say what changed and where the capability went.
        await _send(chat_id, _CORPUS_IS_A_SOURCE)
        return

    if command.startswith("/revisar"):
        await _send(chat_id, _CORPUS_IS_A_SOURCE)
        return

    # Nothing above claimed this slash command, so it does not exist. Answering
    # it with the model is how "/equipe" got invented and then confidently
    # explained: the assistant must never improvise about its own interface.
    if command.startswith("/"):
        await _send(chat_id, _unknown_command_reply(command_name, staff))
        return

    # The router is asked at most once per message. When it gives no usable
    # answer, the message takes today's path, never a second router call.
    conversing = settings.conversational_actions
    if (conversing and pending is not None and pending.kind == "action"
            and not pending.expired):
        if await _converse(conn, update, msg, chat_id, staff, text, pending):
            return
        conversing = False

    local_intent = classify_local_intent(text)
    # With the conversation on, only the replies that run nothing stay
    # word-matched. Anything that reads goes to the router first, which hears
    # a commands-only request ("autoriza o João e lista os documentos") before
    # a read can run; the word-matched reads below remain the fallback.
    if conversing and local_intent not in _WORD_ONLY_INTENTS:
        if await _converse(conn, update, msg, chat_id, staff, text, None):
            return
        conversing = False
    if local_intent == "greeting":
        first_name = (staff.name or "").strip().split(maxsplit=1)[0]
        greeting = f"Olá, {first_name}!" if first_name else "Olá!"
        await _send(
            chat_id,
            f"{greeting} Como posso ajudar? Você pode escrever, mandar um áudio "
            "ou anexar um documento.",
        )
        return
    if local_intent == "thanks":
        await _send(chat_id, "Por nada! Quando precisar, é só chamar.")
        return
    if local_intent == "help":
        # Pointing rather than repeating. The list is pushed once, at the first
        # contact, and after that it is a command away: twenty lines arriving
        # because somebody wrote "me ajuda" mid-conversation is the noise this
        # change exists to remove.
        await _send(
            chat_id,
            "Envie /comandos para ver tudo o que posso fazer, ou digite / para "
            "o Telegram mostrar a lista.",
        )
        return
    if local_intent == "document_upload":
        await _send(chat_id, _DOCUMENT_HELP)
        return
    if local_intent == "document_catalog":
        await _send_document_catalog(conn, chat_id, text, explicit=False)
        return
    if local_intent == "team_directory":
        if not await _may(chat_id, staff, "staff.read_directory"):
            return
        await _send_staff_directory(conn, chat_id)
        return

    # A sentence that names what somebody wants to do, and nothing else, opens
    # the flow the menu button opens, through the button's own authorization
    # action. A sentence that carries the content is answered further down:
    # "quero fazer uma pesquisa" is a door, "pesquisa sobre prescrição
    # intercorrente" is the question itself. The classifier decides which.
    if local_intent == "open_documents":
        if not await _may(chat_id, staff, "document.browse"):
            return
        await _send(
            chat_id,
            "Envie /docs para listar o acervo, ou /docs seguido de um recorte, "
            "como /docs contratos da Acme de 2024.",
        )
        return
    if local_intent == "start_research":
        if not await _may(chat_id, staff, "research.external"):
            return
        await _ask_for_research_question(conn, chat_id)
        return
    if local_intent == "start_draft":
        if not await _may(chat_id, staff, "drafting.compose"):
            return
        await _ask_for_draft_instructions(conn, chat_id)
        return

    # "resuma o doc [2]", "revise contrato.pdf" — an explicit pointer to a stored
    # document is served from the corpus, never from the text of the message.
    document_request = detect_document_request(text)
    if document_request:
        await _serve_document_request(conn, chat_id, staff, document_request, text)
        return

    # Free text -> the orchestrator picks the specialist (corpus, web, review, drafting)
    await channels.for_chat(chat_id).typing(chat_id)
    reply = await _answer_question(
        conn, text, staff_id=staff.id, chat_id=chat_id, staff_name=staff.name,
    )
    await _deliver(chat_id, reply, text[:60], conn=conn, staff=staff)
