"""Agent 3: restricted official-source research with DataJud and a web search.

DataJud is authoritative for public process metadata and movements. It is not a
general web or full-text case-law search. The web search (Serper or Exa, chosen
by WEB_SEARCH_PROVIDER) provides current results, but only HTTPS URLs inside the
configured official-domain allowlist reach the language model, and the answer
names the provider that actually found them.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit
import asyncio
import logging

from core.config import settings
from core.integrations import datajud, dje, djen, exa, serper, web_fetch, websearch
from core.integrations.llm import complete
from core.repositories.documents import log_access

from . import acordao
from . import attachments, prompts
from . import sources as sources_module
from . import verification
from .rendering import document_block
from .state import LegalState

logger = logging.getLogger(__name__)

SEARCH_ERROR = "Não consegui pesquisar fontes externas agora. Tente novamente."
LIMITATION_NOTE = (
    "\n\nObservação: a camada atual ainda não consultou fontes públicas em tempo "
    "real. Confirme jurisprudência, legislação e links antes de usar a resposta."
)
DATAJUD_NOTE = (
    "\n\nFonte externa consultada em tempo real: API Pública do DataJud/CNJ. "
    "Ela fornece metadados e movimentações públicas, mas não substitui a leitura "
    "do inteiro teor da decisão nem uma pesquisa jurisprudencial completa."
)
SERPER_NOTE = (
    "\n\nPesquisa web realizada em tempo real via Serper, limitada a domínios "
    "oficiais autorizados. Os trechos retornados pelo buscador devem ser "
    "confirmados na página oficial antes da utilização em peça ou parecer."
)
EXA_NOTE = (
    "\n\nPesquisa web realizada em tempo real via Exa, limitada a domínios "
    "oficiais autorizados. Os trechos foram extraídos das próprias páginas, mas "
    "não correspondem à leitura integral e devem ser confirmados na página "
    "oficial antes da utilização em peça ou parecer."
)
DJE_NOTE = (
    "\n\n📰 Publicações localizadas no Diário da Justiça Eletrônico do tribunal, por "
    "busca restrita ao diário usando apenas o número do processo. São trechos do "
    "índice de busca: confira a edição antes de contar um prazo."
)
DJEN_NOTE = (
    "\n\n📰 Publicações do DJEN/CNJ consultadas pelo número do processo. "
    + djen.DATE_RULE
)
FETCH_NOTE = (
    "\n\nAs páginas oficiais mais relevantes foram abertas e lidas em tempo real; "
    "o texto usado é o da própria página, não apenas o resumo do buscador."
)
NO_EXTERNAL_EVIDENCE = "Nenhuma fonte pública em tempo real sustentou a resposta"
GENERATION_FAILURE = "A resposta não pôde ser gerada a partir das evidências recuperadas"


async def _datajud_context(text: str) -> tuple[list[dict], list, list[str], list[str]]:
    """DataJud's answer for each process number, and the numbers it confirmed.

    The confirmed numbers are the only ones the diary search may use: a number
    read from the question and not found is never sent anywhere else.
    """
    numbers = datajud.extract_process_numbers(text)
    if not numbers:
        return [], [], [], []

    results = await asyncio.gather(
        *(datajud.lookup_process(number) for number in numbers),
        return_exceptions=True,
    )
    blocks: list[dict] = []
    sources: list[sources_module.Source] = []
    errors: list[str] = []
    identified: list[str] = []
    for number, result in zip(numbers, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.warning("DataJud lookup failed for %s: %s", number, result)
            errors.append(f"Processo {number}: {result}")
            continue
        if isinstance(result, BaseException):
            raise result
        blocks.append(document_block(
            f"DataJud/CNJ, process {number}",
            datajud.format_lookup(result),
            "public source queried in real time",
        ))
        sources.append(sources_module.from_datajud(number, result.endpoint))
        # Confirmed means found. A lookup that succeeds with no record is a
        # number DataJud does not know, possibly mistyped, and it is exactly the
        # number that must not be searched for anywhere else.
        if result.records:
            identified.append(number)
    return blocks, sources, errors, identified


async def _djen_context(text: str) -> tuple[list[dict], list, list[str]]:
    """What the DJEN published about each process named in the question.

    Asked for every number, beside DataJud rather than after it: the CNJ is the
    authority for the case, so this is not the diary search's exception and
    needs no confirmation first.
    """
    if not settings.djen_enabled:
        return [], [], []
    numbers = datajud.extract_process_numbers(text)
    if not numbers:
        return [], [], []

    results = await asyncio.gather(
        *(djen.communications(number) for number in numbers),
        return_exceptions=True,
    )
    blocks: list[dict] = []
    found: list[sources_module.Source] = []
    problems: list[str] = []
    for number, result in zip(numbers, results, strict=True):
        formatted = datajud.format_process_number(number)
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            # The reason is logged only when it is the DJEN's own error, whose
            # message never carries the number; anything else by its type.
            reason = str(result) if isinstance(result, djen.DjenError) else type(result).__name__
            logger.warning("DJEN lookup failed: %s", reason)
            why = (
                " porque o DJEN recusou a consulta feita pelo servidor"
                if isinstance(result, djen.DjenRefused) else ""
            )
            problems.append(
                f"DJEN: a consulta das publicações do processo {formatted} falhou{why}; "
                f"as publicações podem ser lidas pelo número em {djen.PUBLIC_SITE}"
            )
            continue
        if isinstance(result, BaseException):
            raise result
        if not result:
            problems.append(f"DJEN: nenhuma publicação encontrada para o processo {formatted}")
            continue
        for communication in result:
            when = (
                f"{communication.made_available:%d/%m/%Y}"
                if communication.made_available else "data não informada"
            )
            blocks.append(document_block(
                f"DJEN/CNJ: {communication.kind} de {when}, processo {communication.process_number}",
                djen.format_for_model(communication),
                "official publication read in real time",
            ))
            found.append(sources_module.from_djen(communication))
    return blocks, found, problems


async def _dje_context(numbers: list[str]) -> tuple[list[dict], list, list[str], bool]:
    """Each confirmed process's publications, from its court's diary.

    Returns the blocks, the sources, the limitations, and whether a search was
    made at all, which the audit entry records: this is the one path on which a
    process number leaves for a search engine.
    """
    if not settings.dje_search_enabled or not numbers:
        return [], [], [], False

    searches = await asyncio.gather(
        *(dje.search_publications(number) for number in numbers),
        return_exceptions=True,
    )
    blocks: list[dict] = []
    found: list[sources_module.Source] = []
    problems: list[str] = []
    attempted = False
    for search in searches:
        if isinstance(search, asyncio.CancelledError):
            raise search
        if isinstance(search, Exception):
            # The type only: the message could carry the process number.
            logger.warning("Diary search failed: %s", type(search).__name__)
            problems.append("DJe: a busca no diário falhou")
            continue
        if isinstance(search, BaseException):
            raise search
        if search is None:
            continue  # this court has no diary configured
        attempted = True
        logger.info(
            "Diary search site=%s provider=%s results=%d",
            search.site, search.provider, len(search.results),
        )
        if not search.results:
            problems.append(
                f"DJe ({search.site}): nenhuma publicação encontrada para o processo "
                f"{search.number} ({'; '.join(search.problems)}). Isso não prova que o "
                "processo não teve publicações: pode ser só uma edição não indexada."
            )
            continue
        describe = exa.format_result if search.provider == "exa" else serper.format_result
        for result in search.results:
            shown = dje.mentions(search.number, f"{result.title} {result.snippet}")
            evidence = (
                "O trecho abaixo mostra o número do processo."
                if shown else
                "O trecho abaixo NÃO mostra o número do processo: não afirme que "
                "esta edição trata dele."
            )
            blocks.append(document_block(
                f"DJe {search.site}: {result.title}",
                f"Busca restrita ao Diário da Justiça Eletrônico pelo número "
                f"{search.number}, via {search.provider}.\n{evidence}\n\n"
                f"{describe(result)}",
                "diary edition found by a site-restricted search",
            ))
            found.append(sources_module.from_web_result(result))
    return blocks, found, problems, attempted


# Words that only frame a process lookup. What is left after removing them and
# the ordinary connectives is the actual subject of the question — if nothing
# remains, the person asked "how is my case going", which DataJud answers alone.
_LOOKUP_WORDS = {
    "andamento", "andamentos", "processo", "processos", "situacao", "situação",
    "status", "movimentacao", "movimentação", "movimentacoes", "movimentações",
    "consulta", "consultar", "consulte", "ver", "verificar", "acompanhar",
    "numero", "número", "autos", "como", "esta", "está", "sobre", "favor",
}
_CONNECTIVES = {
    "a", "as", "ao", "aos", "da", "das", "de", "do", "dos", "e", "em", "na",
    "nas", "no", "nos", "o", "os", "para", "por", "que", "qual", "um", "uma",
}


def _has_a_topic(text: str) -> bool:
    """Whether anything worth searching the web for survives the redaction."""
    words = [w.strip(".,;:!?()").casefold() for w in text.split()]
    remaining = [w for w in words if w and w not in _LOOKUP_WORDS and w not in _CONNECTIVES]
    return len(remaining) >= 2


@dataclass(frozen=True)
class _WebProvider:
    """How the answer names the web search that found its candidate pages.

    Each provider's evidence is described as what it is: Serper returns a
    search-index snippet, Exa an excerpt taken from the page's own text. Neither
    is a full reading, which only the page reader gives. The Serper wording is
    the one this module has always used, kept word for word.
    """

    key: str
    name: str
    evidence: str
    limitation: str
    prompt: str
    note: str


_WEB_PROVIDERS = {
    "serper": _WebProvider(
        key="serper",
        name="Serper",
        evidence="Serper result filtered by domain",
        limitation="Pesquisa web oficial",
        prompt=(
            "\n\nOs blocos identificados como fontes oficiais foram retornados "
            "pela Serper e passaram pela lista local de domínios permitidos. "
            "Eles contêm apenas título e trecho do índice de busca: não afirme "
            "ter lido o inteiro teor da página e cite a URL oficial."
        ),
        note=SERPER_NOTE,
    ),
    "exa": _WebProvider(
        key="exa",
        name="Exa",
        evidence="Exa result filtered by domain, excerpt taken from the page",
        limitation="Pesquisa web oficial via Exa",
        prompt=(
            "\n\nOs blocos identificados como fontes oficiais foram retornados "
            "pelo Exa e passaram pela lista local de domínios permitidos. Eles "
            "contêm título e um trecho extraído do texto da própria página, "
            "selecionado para a pesquisa, e não a página inteira: não afirme ter "
            "lido o inteiro teor da página, não extrapole o trecho e cite a URL "
            "oficial."
        ),
        note=EXA_NOTE,
    ),
}


def _web_provider() -> _WebProvider:
    """The provider this research searches with, resolved once.

    Everything that names the provider reads this one value, and the search is
    told to use it, so a label can never describe a supplier that did not run.
    """
    return _WEB_PROVIDERS[websearch.active_provider()]


async def _web_search_context(
    query: str, provider: _WebProvider,
) -> tuple[list[dict], list, list[str]]:
    try:
        results = await websearch.search(query, provider.key)
    except Exception as exc:
        logger.warning("Restricted %s search failed: %s", provider.name, exc)
        return [], [], [f"{provider.limitation}: {exc}"]

    if not results:
        return [], [], [
            f"{provider.limitation}: nenhum resultado aceito nos domínios autorizados"
        ]

    blocks = [
        document_block(
            f"Official source: {result.title}",
            websearch.format_result(result, provider.key),
            provider.evidence,
        )
        for result in results
    ]
    sources = [sources_module.from_web_result(result) for result in results]
    return blocks, sources, []


# What a decision looks like in a URL or a title, learned from what the official
# indexes actually return: STJ serves inteiro teor from SCON, TJs serve an
# espelho do acórdão, STF serves a pronouncement.
_DECISION_MARKERS = (
    "getinteiroteordoacordao", "relatorioespelhoacordao", "verpronunciamento",
    "/jurisprudencia", "inteiroteor", "inteiro_teor", "acordao", "acórdão",
    "resp ", "aresp", "agint", "ementa", "recurso especial",
)


def _looks_like_a_decision(source) -> bool:
    haystack = f"{source.reference} {source.title}".casefold()
    return any(marker in haystack for marker in _DECISION_MARKERS)


def _reading_order(results: list) -> list:
    """Decisions first, because only the first few are actually opened.

    Reading is capped, so the cap decides what the answer can cite as read
    rather than as a search snippet. Left in search order, an institutional
    page that merely ranks well consumes a slot a decision needed.
    """
    decisions = [r for r in results if _looks_like_a_decision(r)]
    others = [r for r in results if not _looks_like_a_decision(r)]
    return decisions + others


def _page_block(page) -> tuple[str, str, str]:
    """One fetched page as a document block, with its header fields read out.

    When the page is a decision, the process number, the relator and the
    judgment date are extracted deterministically and put in front of the text
    rather than left for the model to find. Those three are exactly the fields a
    model completes from context when they are not in front of it, and a relator
    invented from a plausible pattern reads like a citation.

    The header says the fields were read and that nothing else may be claimed,
    because a block that only offers them still leaves inventing the missing one
    available.
    """
    body = f"URL: {page.url}\n\n{page.text}"
    if page.truncated:
        body += "\n\n[Texto truncado no limite configurado.]"

    found = acordao.extract(page.text, url=page.url)
    if found.is_empty():
        return (
            f"Official page read in full: {page.title}",
            body,
            "official page fetched and read in real time",
        )
    return (
        f"Decision read in full: {page.title}",
        "Dados de identificação e ementa lidos do próprio acórdão. Cite apenas os "
        "campos que estão abaixo: um campo ausente aqui não foi encontrado no "
        "documento e não deve ser afirmado. A ementa abaixo é o único texto desta "
        "decisão que pode ir entre aspas, e deve ser copiada sem alterar palavras.\n"
        f"{found.describe()}\n\n{body}",
        "decision fetched and read in real time, identifying fields verified",
    )


def _page_detail(page) -> str:
    """The footnote a lawyer reads under the answer.

    A decision's footnote leads with the fields that make it citable. Without
    them the reader gets an opening line of ementa and has to open the PDF to
    find out which case it was.
    """
    found = acordao.extract(page.text, url=page.url)
    opening = f"Início do conteúdo: {' '.join(page.text.split())[:200]}"
    if found.is_empty():
        return (
            f"Página oficial aberta e lida em tempo real ({page.content_type}). "
            f"{opening}"
        )
    return (
        "Decisão aberta e lida em tempo real. "
        + " | ".join(found.citation().splitlines())
        + f". {opening}"
    )


# Hosts that answer automated reading with a bot challenge, measured on
# 2026-09-11: the STJ's SCON returned HTTP 403 "Verificação automática em
# andamento" for every inteiro teor tried. Fetching them spends the few reading
# slots a question has on pages that cannot open, while a TJ's acórdão that
# would open waits behind them. They stay as search results; they are only not
# fetched, and the answer is told why.
_UNREADABLE_HOSTS = {"scon.stj.jus.br": "STJ"}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _block_chars(block: dict) -> int:
    """The size a document block adds to the request, in characters."""
    try:
        return len(block["source"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError):
        return len(str(block))


# Priority order for what stays when the assembled evidence would exceed
# external_research_max_context_chars, highest priority first. Grounded in
# what this module already tells the model about each source's own reliability
# (see the system_prompt additions below): DataJud and DJEN are read from an
# official CNJ channel with no "may not be extrapolated" caveat attached; a
# fetched page is a page actually read in real time, but from the open web and
# possibly large; DJe and the general web search are themselves described to
# the model as a search-index snippet, not the thing itself. Within one group
# the existing order is kept, so a fetched page is dropped worst-first (the
# one _reading_order already ranked last), never by cutting into the middle of
# one, because a truncated ementa reads like a shorter ementa, not a missing
# one.
_EVIDENCE_PRIORITY = ("acervo", "DataJud", "DJEN", "páginas lidas", "DJe", "busca web")


def _bounded_evidence(
    groups: dict[str, list[dict]], max_chars: int,
) -> tuple[list[dict], list[str]]:
    """Trim whole blocks, lowest-priority group's tail first, to fit the budget.

    Returns the blocks that survived, in `_EVIDENCE_PRIORITY` order, and one
    limitation per group something was dropped from. Groups are looked up by
    name and any name not in `_EVIDENCE_PRIORITY` is a programming error, not a
    runtime one: every caller passes a fixed, known set of keys.
    """
    kept = {label: list(groups.get(label, ())) for label in _EVIDENCE_PRIORITY}
    total = sum(_block_chars(b) for blocks in kept.values() for b in blocks)
    dropped: dict[str, int] = {}
    for label in reversed(_EVIDENCE_PRIORITY):
        while total > max_chars and kept[label]:
            total -= _block_chars(kept[label].pop())
            dropped[label] = dropped.get(label, 0) + 1
        if total <= max_chars:
            break
    flattened = [block for label in _EVIDENCE_PRIORITY for block in kept[label]]
    limitations = [
        f"Pesquisa externa: {count} bloco(s) de evidência de {label} "
        "omitido(s) por limite de tamanho do contexto"
        for label, count in dropped.items()
    ]
    return flattened, limitations


async def _read_official_pages(results) -> tuple[list[dict], list, list[str]]:
    """Open the top official pages and pass their real text to the model."""
    if not settings.fetch_enabled or not results:
        return [], [], []

    ordered = _reading_order(list(results))
    blocked = sorted({
        _UNREADABLE_HOSTS[_host(r.reference)]
        for r in ordered if _host(r.reference) in _UNREADABLE_HOSTS
    })
    readable = [r for r in ordered if _host(r.reference) not in _UNREADABLE_HOSTS]
    notes = [
        f"O {court} bloqueia a leitura automatizada do inteiro teor (verificação "
        f"anti-robô): para as decisões do {court}, número, relator, data e ementa só "
        "podem vir do trecho do buscador, e o que não estiver no trecho não deve ser "
        "afirmado"
        for court in blocked
    ]
    if not readable:
        return [], [], notes
    ordered = readable
    pages = await web_fetch.fetch_pages([result.reference for result in ordered])
    if not pages:
        return [], [], notes + ["Leitura das páginas oficiais: nenhuma pôde ser aberta"]

    blocks = [document_block(*_page_block(page)) for page in pages]
    read = {page.url for page in pages}
    sources = [
        sources_module.Source(
            kind="web",
            title=page.title,
            reference=page.url,
            detail=_page_detail(page),
        )
        for page in pages
    ]
    missed = [
        f"Não consegui abrir: {result.reference}"
        for result in ordered if result.reference not in read
    ]
    return blocks, sources, notes + missed


async def research(state: LegalState) -> dict:
    # The note travels as the group's own last element, not appended to
    # `content` separately, so it is dropped together with the corpus
    # documents it refers to in the one scenario ("acervo" itself has to give
    # ground) where that would otherwise leave "os documentos acima" pointing
    # at nothing.
    corpus_blocks = [
        document_block(h.title, h.content, h.doc_type) for h in state.hits
    ]
    if corpus_blocks:
        corpus_blocks.append({
            "type": "text",
            "text": (
                "Os documentos acima são do acervo do escritório. Considere-os "
                "junto com o que encontrar em fontes públicas, e deixe claro o "
                "que vem de cada origem."
            ),
        })

    asked = f"{state.question}\n{state.query}"
    web_query = state.query or state.question

    # A process number goes to DataJud, which is authoritative for it, and not
    # to the search engine. If redacting it leaves nothing to search for, the
    # question was purely a process lookup and the web adds nothing — so no
    # request is made at all, and no client case leaves for a third party.
    if datajud.extract_process_numbers(asked):
        web_query = datajud.redact_process_numbers(web_query, " ").strip()
        if not _has_a_topic(web_query):
            web_query = ""

    provider = _web_provider()
    if web_query:
        datajud_result, djen_result, web_result = await asyncio.gather(
            _datajud_context(asked),
            _djen_context(asked),
            _web_search_context(web_query, provider),
        )
    else:
        datajud_result, djen_result = await asyncio.gather(
            _datajud_context(asked),
            _djen_context(asked),
        )
        web_result = ([], [], [])
        logger.info("Skipping web search: the request was a process lookup")

    datajud_blocks, datajud_sources, datajud_errors, identified = datajud_result
    djen_blocks, djen_sources, djen_errors = djen_result
    web_blocks, web_sources, web_errors = web_result
    # After DataJud, never beside it: only a number it confirmed is searched in
    # the court's diary.
    dje_blocks, dje_sources, dje_errors, dje_attempted = await _dje_context(identified)

    # The snippet says a page exists; reading it is what lets the assistant say
    # what the decision or the article actually contains.
    fetch_blocks, fetch_sources, fetch_errors = await _read_official_pages(web_sources)

    # A page that was read replaces its own snippet, the way it already replaces
    # it in the source list below. The same URL arriving twice puts two
    # instructions about it in front of the model: one saying this is only an
    # index snippet and the inteiro teor was not read, the other saying the full
    # text is right there and its ementa may be quoted. Blocks and sources are
    # built one per result and in order, so strict=True makes a future drift fail
    # here rather than quietly pair a block with another result's URL.
    read_urls = {source.reference for source in fetch_sources}
    deduped_web_blocks = [
        block
        for block, source in zip(web_blocks, web_sources, strict=True)
        if source.reference not in read_urls
    ]

    content, size_limitations = _bounded_evidence(
        {
            "acervo": corpus_blocks,
            "DataJud": datajud_blocks,
            "DJEN": djen_blocks,
            "páginas lidas": fetch_blocks,
            "DJe": dje_blocks,
            "busca web": deduped_web_blocks,
        },
        settings.external_research_max_context_chars,
    )
    if size_limitations:
        logger.warning(
            "External research context exceeded %d chars, trimmed: %s",
            settings.external_research_max_context_chars,
            "; ".join(size_limitations),
        )
    # Known gap: the system_prompt additions and notes below still gate on
    # *_sources, not on whether a group's blocks survived trimming. A source
    # trimmed away entirely leaves its instruction in the prompt with nothing
    # left to apply it to: prompt noise, not a fabrication risk, since the
    # instruction is conditional ("when you see a DJEN block, treat it as..."),
    # but real enough to fix before this budget is ever tightened further.

    limitations = (
        datajud_errors + djen_errors + dje_errors + web_errors + fetch_errors
        + size_limitations
    )
    if not datajud_sources and not web_sources and not dje_sources and not djen_sources:
        limitations.append(NO_EXTERNAL_EVIDENCE)
    if limitations:
        content.append({
            "type": "text",
            "text": (
                "A pesquisa externa teve as seguintes limitações:\n- "
                + "\n- ".join(limitations)
                + "\nNão invente dados para compensar essas falhas."
            ),
        })
    content.append({"type": "text", "text": state.question})

    try:
        system_prompt = prompts.external_research(provider.key)
        if datajud_sources:
            system_prompt += (
                "\n\nOs blocos identificados como DataJud/CNJ foram obtidos em consulta "
                "real à fonte oficial. Use-os apenas para os fatos que efetivamente "
                "contêm e identifique o DataJud/CNJ como fonte."
            )
        if djen_sources:
            system_prompt += (
                "\n\nOs blocos identificados como DJEN/CNJ são publicações oficiais "
                "lidas em tempo real no Diário de Justiça Eletrônico Nacional, com o "
                "texto como foi publicado. Transcreva só o que está no texto, cite a "
                "data e o tipo da comunicação, e deixe claro que a data informada é a "
                "de disponibilização: a publicação considera-se feita no primeiro dia "
                "útil seguinte e o prazo começa no dia útil seguinte a ela (Lei "
                "11.419/2006, art. 4º, §§ 3º e 4º). Não conte prazo sem essa ressalva."
            )
        if dje_sources:
            system_prompt += (
                "\n\nOs blocos identificados como DJe vieram de uma busca restrita ao "
                "Diário da Justiça Eletrônico do tribunal que o DataJud identificou, "
                "usando só o número do processo. São trechos do índice de busca de uma "
                "edição do diário, não a edição inteira: afirme apenas o que o trecho "
                "mostra e cite a URL. Se o bloco disser que o trecho não mostra o número "
                "do processo, não atribua aquela edição ao processo. Não conte prazo a "
                "partir desses trechos sem dizer que a data deve ser conferida na edição."
            )
        if web_sources:
            system_prompt += provider.prompt
        if fetch_sources:
            system_prompt += (
                "\n\nOs blocos identificados como página oficial lida em tempo real "
                "contêm o texto integral (ou o início) da própria página. Use-os "
                "preferencialmente aos trechos do buscador, cite a URL e diga o que "
                "há na página."
            )
        if not datajud_sources and not web_sources and not dje_sources and not djen_sources:
            system_prompt += "\n\nNão afirme que pesquisou a web em tempo real."
        content = attachments.content_for(state, content)
        text = await complete(
            system_prompt,
            content,
            task="complex", model=settings.research_model, max_tokens=8000,
            **attachments.reservation_for(state),
        )
    except Exception:
        logger.exception("External research failed")
        return {
            "answer": SEARCH_ERROR,
            "limitations": limitations + [GENERATION_FAILURE],
            "cacheable": False,
        }

    action = (
        "official_web_and_datajud_research"
        if datajud_sources and web_sources
        else "datajud_research"
        if datajud_sources
        else "official_web_research"
        if web_sources
        else "external_research"
    )
    if dje_attempted:
        # The audit says that a process number left for a search engine.
        action += "_dje"
    await log_access(
        state.conn,
        action=action,
        staff_id=state.staff_id,
        chat_id=state.chat_id,
        query=state.query,
        document_ids=sorted({h.document_id for h in state.hits}) or None,
    )
    logger.info(
        "External research answered (datajud_sources=%d, %s_sources=%d)",
        len(datajud_sources),
        provider.key,
        len(web_sources),
    )

    if not text:
        return {
            "answer": SEARCH_ERROR,
            "limitations": limitations + [GENERATION_FAILURE],
            "cacheable": False,
        }
    notes: list[str] = []
    if datajud_sources:
        notes.append(DATAJUD_NOTE)
    if djen_sources:
        notes.append(DJEN_NOTE)
    if dje_sources:
        notes.append(DJE_NOTE)
    if web_sources:
        notes.append(provider.note)
    if not notes:
        notes.append(LIMITATION_NOTE)
    if fetch_sources:
        notes.append(FETCH_NOTE)
    collected = (
        [sources_module.from_corpus_hit(hit) for hit in state.hits]
        + datajud_sources
        + djen_sources
        + dje_sources
        + fetch_sources
        + [source for source in web_sources if source.reference not in read_urls]
    )
    return {
        "answer": text + "".join(notes),
        "sources": collected,
        "evidence": verification.evidence_from(content),
        "limitations": limitations,
        "cacheable": bool(datajud_sources or web_sources or dje_sources or djen_sources),
    }
