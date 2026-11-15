"""Deterministic routing for basic conversational and operational messages.

These intents must keep working even when an LLM provider is unavailable. The
rules are deliberately narrow: legal questions still go through the agent
graph, while greetings and questions about using the bot receive immediate,
predictable answers.
"""
from __future__ import annotations

import re
import unicodedata
from core.services.ingestion import DOCUMENT_EXTENSIONS
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

LocalIntent = Literal[
    "greeting",
    "thanks",
    "help",
    "document_upload",
    "document_catalog",
    "team_directory",
    "open_documents",
    "start_research",
    "start_draft",
]


# The verbs someone uses when asking to be shown something they know exists.
# Shared by the team roster and the command list, which are the same request in
# two subjects: "me passa a equipe", "me passa os comandos".
_LISTING_ACTIONS = {
    "conhecer", "consultar", "diz", "dizer", "envia", "enviar", "fala",
    "falar", "informa", "informar", "lista", "listar", "manda", "mandar",
    "mostra", "mostrar", "passa", "passar", "passe", "quais", "quem", "saber",
    "traz", "trazer", "ve", "veja", "vejo", "ver",
}

# Phrases that mark a question about how the assistant works, as opposed to a
# request for it to do something. "o documento tem que ser ingerido para
# aparecer na lista de documentos?" names documents and names a list, and asks
# neither for the archive nor for an upload: it asks what the rule is. Those
# answers come from the agent, which can explain, and not from a listing, which
# can only enumerate.
#
# The set stays narrow and holds no bare "posso" or "consigo", because "quais
# documentos eu posso ver?" really is a request for the list. Every phrase here
# asks about a condition or a mechanism, which no listing ever satisfies.
_RULE_QUESTION_PHRASES = (
    "tem que", "tem de", "necessario", "obrigatorio",
    "como funciona", "como faco", "da para", "tem como", "serve para",
)

# "precisar" is the one that cuts both ways: "preciso ingerir este arquivo" is
# somebody asking to do it, and "preciso ingerir para ele entrar no acervo?" is
# somebody asking whether they have to. Only the question mark separates them,
# so here alone it is required.
_RULE_QUESTION_WORDS = {"precisa", "precisam", "preciso", "precisava"}

# ── Doors: a sentence that only names what somebody wants to do ────────────────
# "quero fazer uma pesquisa" names an action and carries nothing to act on, so it
# opens the flow the menu button opens, which asks for the rest. "pesquisa sobre
# prescrição intercorrente" carries its content and is answered directly. The
# difference is decided by what is left once the action's own words, the objects
# it may take and the filler are removed: nothing left is a door, anything left
# is a question. Narrow on purpose: routing too early is how a lawyer's question
# once came back as a failed search (#46).
_DOOR_FILLER = frozenset({
    "quero", "queria", "quer", "preciso", "precisaria", "gostaria", "vou",
    "vamos", "fazer", "faca", "faz", "abrir", "abre", "abra", "iniciar",
    "comecar", "nova", "novo", "uma", "um", "outra", "outro", "algo", "alguma",
    "algum", "coisa", "me", "eu", "de", "do", "da", "dos", "das", "o", "a",
    "os", "as", "com", "no", "na", "nos", "nas", "por", "favor", "pf", "pfv",
    "agora", "aqui", "entao", "tambem", "so", "pode", "podemos", "consegue",
    "voce", "vc", "para", "pra", "ir",
})

# (intent, verbs that must be present, objects the verb may take, whether an
# object is required). Drafting is checked first so that "redigir um documento"
# opens drafting rather than the documents menu.
_DOORS: tuple[tuple[str, frozenset[str], frozenset[str], bool], ...] = (
    (
        "start_draft",
        frozenset({
            "redigir", "redija", "redacao", "minutar", "rascunho", "escrever",
            "escreva",
        }),
        frozenset({
            "documento", "peca", "texto", "minuta", "clausula", "contrato",
            "peticao", "parecer", "notificacao", "procuracao", "recurso",
        }),
        False,
    ),
    (
        "start_research",
        frozenset({"pesquisa", "pesquisar", "pesquise", "pesquisas"}),
        frozenset({"externa", "externas"}),
        False,
    ),
    (
        "open_documents",
        frozenset({
            "ver", "abrir", "abre", "abra", "acessar", "mexer", "gerenciar",
            "menu", "mostra", "mostrar", "mostre",
        }),
        frozenset({
            "documento", "documentos", "acervo", "arquivo", "arquivos", "internos",
        }),
        True,
    ),
)


def _door_in(words: set[str]) -> LocalIntent | None:
    """The flow a sentence opens, or None when the sentence carries content.

    A sentence that names a stored document by number is never a door, and it
    needs no check of its own: the number is content, so it is always left
    over. An explicit id check sat here until a mutation check showed that
    removing it changed nothing, and a guard that protects nothing is removed.
    """
    for intent, verbs, objects, needs_object in _DOORS:
        if not words & verbs:
            continue
        if needs_object and not words & objects:
            continue
        if words - verbs - objects - _DOOR_FILLER:
            continue
        return intent  # type: ignore[return-value]
    return None


# Words that mark a legal question rather than a request about the assistant:
# "o comando normativo do artigo" must not be read as "list your commands".
_LEGAL_CONTEXT = {
    "art", "artigo", "artigos", "clausula", "codigo", "constitucional",
    "decisao", "jurisprudencia", "lei", "legal", "normativo", "normativos",
    "processo", "sentenca", "sumula",
}


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def classify_local_intent(text: str) -> LocalIntent | None:
    """Classify only low-risk messages that do not require legal reasoning."""
    normalized = _normalize(text)
    if not normalized:
        return None

    words = set(normalized.split())
    document_terms = {
        "acervo", "anexo", "anexos", "arquivo", "arquivos", "documento",
        "documentos", "imagem", "imagens", "memoria", "pdf",
    }
    upload_terms = {
        "adicionar", "anexar", "armazenar", "carregar", "digerir", "enviar",
        "envio", "guardar", "importar", "ingerir", "ingestao", "mandar",
        "mando", "memorizar", "processar", "salvar", "upload",
    }
    catalog_terms = {
        "armazenado", "armazenados", "cadastrado", "cadastrados", "disponivel",
        "disponiveis", "ingerido", "ingeridos", "lista", "listado", "listados",
        "listar", "salvo", "salvos",
    }

    asks_about_a_rule = any(
        phrase in normalized for phrase in _RULE_QUESTION_PHRASES
    ) or ("?" in text and bool(words & _RULE_QUESTION_WORDS))

    if words & document_terms and words & upload_terms and not asks_about_a_rule:
        return "document_upload"

    catalog_phrases = (
        "o que tem no acervo",
        "documentos que voce tem",
        "quais documentos voce tem",
        "quais documentos vc tem",
    )
    if (
        words & document_terms
        and (words & catalog_terms or any(phrase in normalized for phrase in catalog_phrases))
        and not asks_about_a_rule
    ):
        return "document_catalog"

    # Before the team and the help checks, so "menu de documentos" opens the
    # documents menu instead of the main one. After the upload and catalogue
    # checks, which are more specific and already answer their sentences.
    door = _door_in(words)
    if door and not asks_about_a_rule:
        return door

    team_terms = {
        "colaboradores", "equipe", "funcionarios", "membros", "staff", "time",
        "usuarios",
        # Role names, so "quem é o owner aqui?" reads the real roster instead of
        # letting the model name people. "administrador" is deliberately absent:
        # "quais as obrigações do administrador?" is company law, not this bot.
        "admin", "admins", "owner",
    }
    team_phrases = (
        "quem esta autorizado",
        "quem tem acesso",
        "quem trabalha aqui",
    )
    if any(phrase in normalized for phrase in team_phrases) or (
        words & team_terms and words & _LISTING_ACTIONS
    ):
        return "team_directory"

    help_phrases = (
        "como usar",
        "o que voce faz",
        "como voce pode ajudar",
        "como pode me ajudar",
        "quais comandos",
        "que comandos",
        "lista de comandos",
        "suas capacidades",
    )
    # "me passe a lista de comandos" is the same request as "quais comandos" and
    # must not reach the model, which answers it by inventing command names.
    # "o comando normativo do art. 5º" is not: a legal question keeps its route.
    asks_for_commands = (
        words & {"comando", "comandos"}
        and words & _LISTING_ACTIONS
        and not words & _LEGAL_CONTEXT
    )
    # "menu" is what the lawyers call the thing they want to see, and it has no
    # legal meaning to protect, so it needs no guard. "opções" does ("quais as
    # opções de recurso?"), so it counts only as the whole message.
    asks_for_the_menu = "menu" in words
    if (
        normalized in {"ajuda", "help", "menu", "comandos", "opcoes"}
        or asks_for_commands
        or asks_for_the_menu
        or any(phrase in normalized for phrase in help_phrases)
    ):
        return "help"

    greeting_phrases = {
        "bom dia", "boa tarde", "boa noite", "e ai", "ola", "ola tudo bem",
        "oi", "oi tudo bem", "opa", "tudo bem",
    }
    if normalized in greeting_phrases:
        return "greeting"

    thanks_phrases = {
        "obrigada", "obrigado", "muito obrigada", "muito obrigado", "valeu",
    }
    if normalized in thanks_phrases:
        return "thanks"

    return None


# ── Operations on a stored document ──────────────────────────────────────────
# "faça um resumo do doc [2]" must reach the stored file, not the agent that
# reviews text pasted into the chat — that one only sees the message itself and
# ends up asking for a document the firm already stored.

# "analysis" covers what used to be summary and revision. Both are refused now,
# and they are still detected on purpose: recognising the ask costs nothing and
# is what lets the answer explain the rule instead of guessing at a narrower
# question the person did not ask.
DocumentAction = Literal["analysis", "original"]

# The same allowlist ingestion enforces, without the dots the regex supplies.
_DOCUMENT_EXTENSIONS = tuple(
    sorted(extension.lstrip(".") for extension in DOCUMENT_EXTENSIONS)
)

# "[2]", "doc 2", "documento nº 2", "id 2", "#2", "arquivo 2" — how staff refer
# to a catalog entry in prose. A bare number is never enough on its own.
_ID_PATTERNS = (
    r"\[\s*(\d{1,6})\s*\]",
    r"\b(?:doc|documento|arquivo|contrato|id|n|no|num|numero)\s*(?:de\s*)?#?\s*(\d{1,6})\b",
    r"#\s*(\d{1,6})\b",
)

_SUMMARY_TERMS = {
    "resumo", "resuma", "resumir", "resumindo", "sintese", "sintetize",
    "sumario", "sumarize",
}
_REVISION_TERMS = {
    "revisao", "revisar", "revise", "revisa", "reveja", "corrigir", "corrija",
    "correcao", "analisar", "analise", "analisa", "avaliar", "avalie",
    "auditar", "audite", "riscos",
}
_ORIGINAL_TERMS = {
    "original", "originais", "baixar", "download", "anexo",
}
_SEND_TERMS = {"envia", "enviar", "manda", "mandar", "me", "quero", "preciso"}


@dataclass(frozen=True)
class DocumentRequest:
    """A message that asks for something about a document already in the corpus."""

    action: DocumentAction
    document_id: int | None = None
    title_hint: str | None = None


def _document_id_in(text: str) -> int | None:
    normalized = _normalize_keep_brackets(text)
    for pattern in _ID_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            return int(match.group(1))
    return None


def _normalize_keep_brackets(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9\[\]#]+", " ", without_accents).strip()


def _filename_in(text: str) -> str | None:
    match = re.search(
        rf"[\w.\-]+\.(?:{'|'.join(_DOCUMENT_EXTENSIONS)})\b", text, re.IGNORECASE
    )
    return match.group(0) if match else None


def detect_document_request(text: str) -> DocumentRequest | None:
    """Recognize "resuma o doc [2]" / "revise modelo_contrato.pdf" and similar.

    Requires an unambiguous pointer to a stored document — an id or a filename —
    so an ordinary question about a contract still goes through the agent graph,
    where it is answered from retrieved passages rather than from a whole file.
    """
    document_id = _document_id_in(text)
    filename = _filename_in(text)
    if document_id is None and not filename:
        return None

    words = set(_normalize(text).split())
    if words & (_SUMMARY_TERMS | _REVISION_TERMS):
        action: DocumentAction = "analysis"
    elif words & _ORIGINAL_TERMS and words & _SEND_TERMS:
        action = "original"
    else:
        return None

    return DocumentRequest(action=action, document_id=document_id, title_hint=filename)


# ── Follow-ups to an answer already delivered ────────────────────────────────
# Every answer ends with an offer ("quer que eu liste as fontes?"), so the next
# message is often a bare "sim". These rules read that answer without spending
# a model call on it.

_SOURCE_TERMS = {"fonte", "fontes", "referencia", "referencias", "citacoes", "links"}
_AFFIRMATIVE = {
    "sim", "s", "quero", "pode", "por favor", "claro", "manda", "mande",
    "sim por favor", "quero sim", "pode sim", "manda sim", "isso",
}
_FILE_TERMS = {
    "arquivo", "documento", "exportar", "exporta", "exporte", "baixar",
    "download", "anexo", "planilha", "word", "excel", "pdf", "docx", "xlsx",
}
_EXPORT_ACTIONS = {
    "exportar", "exporta", "exporte", "gerar", "gera", "gere", "manda", "mandar",
    "envia", "enviar", "quero", "preciso", "transforma", "transformar", "salva",
    "salvar", "converte", "converter", "baixar", "download",
}
_FORMAT_WORDS = {
    "docx": "docx", "doc": "docx", "word": "docx",
    "pdf": "pdf",
    "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx", "planilha": "xlsx",
}


def wants_sources(text: str) -> bool:
    """"sim", "quais as fontes?", "lista as fontes" — right after an offer."""
    normalized = _normalize(text)
    if not normalized:
        return False
    if normalized in _AFFIRMATIVE:
        return True
    return bool(set(normalized.split()) & _SOURCE_TERMS)


def format_in(text: str) -> str | None:
    """The export format named in a message, if any."""
    for word in _normalize(text).split():
        if word in _FORMAT_WORDS:
            return _FORMAT_WORDS[word]
    return None


_FILENAME = re.compile(r"\S+\.(?:pdf|docx?|xlsx?|txt|md|png|jpe?g|tiff?)\b", re.IGNORECASE)


def detect_export_request(text: str) -> str | None:
    """Return the format asked for, "ask" when the format is still missing.

    "me manda em pdf" -> "pdf"; "quero isso em arquivo" -> "ask"; anything that
    is not about turning the last answer into a file -> None.

    Filenames are removed first: "revise o contrato.pdf" names a document to
    work on, not the format the lawyer wants back.
    """
    without_filenames = _FILENAME.sub(" ", text)
    normalized = _normalize(without_filenames)
    if not normalized:
        return None
    words = set(normalized.split())
    # A sentence that names a different action is not asking for a file. "quero"
    # is an export verb here and "documento" a file word, so right after an
    # answer "quero ver um documento" used to come back as "which format do you
    # want the answer in?". Whether a sentence only names an action is decided
    # once, by _door_in, and this defers to it rather than keeping a second list.
    if _door_in(words) is not None:
        return None
    named_format = format_in(without_filenames)
    asked_as = bool(named_format) and bool(re.search(
        r"\b(?:em|para|no|na|formato|como)\s+\w*\b", normalized
    ))
    if named_format and (words & _EXPORT_ACTIONS or asked_as):
        return named_format
    if words & _EXPORT_ACTIONS and words & _FILE_TERMS:
        return named_format or "ask"
    return None


# ── Filtering the catalogue ──────────────────────────────────────────────────
# Listing every document works until the archive is real. What a lawyer actually
# asks for is a slice of it — "contratos da Acme de 2024", "petições dos últimos
# seis meses" — so the filters are read from the message deterministically,
# before any model call.

_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}

# Each written form maps to the term stored in doc_type, because a lookup is by
# whole word: "contratos" and "contrato" both have to be spelled out here.
_DOC_TYPES = {
    "contratos": "contrato", "contrato": "contrato",
    "peticoes": "petição", "peticao": "petição",
    "acordaos": "acórdão", "acordao": "acórdão",
    "pareceres": "parecer", "parecer": "parecer",
    "procuracoes": "procuração", "procuracao": "procuração",
    "sentencas": "sentença", "sentenca": "sentença",
    "recursos": "recurso", "recurso": "recurso",
    "aditivos": "aditivo", "aditivo": "aditivo",
    "distratos": "distrato", "distrato": "distrato",
    "notificacoes": "notificação", "notificacao": "notificação",
}

def normalize_doc_type(value: str) -> str | None:
    """Map a word a person wrote to one of the catalogue's document types.

    Public because the bucket import needs the same map: a folder called
    "Contratos" and a message saying "contratos" mean the same type, and two
    copies of this table would drift into two vocabularies for one archive.
    """
    return _DOC_TYPES.get(_normalize(value).strip())


_PERIOD_UNITS = {
    "dia": 1, "dias": 1,
    "semana": 7, "semanas": 7,
    "mes": 30, "meses": 30,
    "ano": 365, "anos": 365,
}

# Words that introduce a client or matter, split by how much they promise.
#
# A noun names the thing that follows it: "do CLIENTE Acme" can only be
# introducing a client. A bare preposition promises nothing, and in a sentence
# it usually introduces a subordinate clause instead: "ingerido PARA aparecer na
# lista" is not a client called "aparecer na lista". Both are trusted when
# somebody typed /docs, because typing the command is the claim that what
# follows is a filter. Only the nouns are trusted in free prose.
_MATTER_NOUNS = {
    "cliente", "clientes", "empresa", "caso", "processo", "matéria", "materia",
}
_MATTER_PREPOSITIONS = {"da", "das", "de", "do", "dos", "para", "sobre"}
_MATTER_LEAD_WORDS = _MATTER_NOUNS | _MATTER_PREPOSITIONS
# What follows a lead word but names no client.
_NOT_A_MATTER = {"acervo", "documentos", "tudo", "todos", "todas", "arquivo", "arquivos"}


@dataclass(frozen=True)
class DocumentFilters:
    """What to narrow the catalogue by. Every field is optional."""

    matter: str | None = None
    doc_type: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    def is_empty(self) -> bool:
        return not any((self.matter, self.doc_type, self.since, self.until))

    def describe(self) -> str:
        """The filters in words, so the answer says what it searched for."""
        parts = []
        if self.doc_type:
            parts.append(f"tipo {self.doc_type}")
        if self.matter:
            parts.append(f"caso/cliente contendo “{self.matter}”")
        if self.since and self.until:
            parts.append(f"entre {self.since:%d/%m/%Y} e {self.until:%d/%m/%Y}")
        elif self.since:
            parts.append(f"a partir de {self.since:%d/%m/%Y}")
        elif self.until:
            parts.append(f"até {self.until:%d/%m/%Y}")
        return ", ".join(parts)


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=UTC)
    return start, end


def _fold_words(text: str) -> list[tuple[str, str]]:
    """Pair every word with its accent-folded form, so patterns match either."""
    return [(word, _normalize(word)) for word in text.split()]


def parse_document_filters(
    text: str,
    *,
    now: datetime | None = None,
    explicit: bool = True,
) -> DocumentFilters:
    """Read "contratos da Acme de 2024" as a set of catalogue filters.

    Matching happens on the accent-folded text, because nobody types "acórdão"
    the same way twice, but the matter is returned with its original spelling —
    it goes back to the lawyer in the answer.

    `explicit` says where the text came from, and it is the difference between a
    filter and a sentence. Someone who typed `/docs <text>` has declared that the
    text is a filter, so every lead word counts. Text that merely got classified
    as a catalogue request is still a sentence, and a bare preposition inside a
    sentence introduces a clause far more often than a client: a lawyer asking
    "o documento tem que ser ingerido para aparecer na lista?" was told that no
    document matched "caso/cliente contendo 'aparecer na lista'". In prose, only
    a noun that names the thing is trusted to introduce one.
    """
    now = now or datetime.now(UTC)
    pairs = _fold_words(text)
    folded = " ".join(fold for _, fold in pairs)
    if not folded:
        return DocumentFilters()

    doc_type = next((_DOC_TYPES[fold] for _, fold in pairs if fold in _DOC_TYPES), None)

    since = until = None
    consumed: set[int] = set()  # word positions already explained by a filter

    for index, (_, fold) in enumerate(pairs):
        if fold in _DOC_TYPES:
            consumed.add(index)

    # "últimos 6 meses", "última semana", "nos últimos 2 anos"
    for index, (_, fold) in enumerate(pairs):
        if not fold.startswith("ultim"):
            continue
        amount, unit_index = 1, index + 1
        if unit_index < len(pairs) and pairs[unit_index][1].isdigit():
            amount = int(pairs[unit_index][1])
            unit_index += 1
        if unit_index < len(pairs) and pairs[unit_index][1] in _PERIOD_UNITS:
            since = now - timedelta(days=amount * _PERIOD_UNITS[pairs[unit_index][1]])
            # Also swallow the preposition that introduced it ("dos" últimos),
            # so it does not end up glued to the end of the matter.
            consumed.update(range(max(0, index - 1), unit_index + 1))
            break

    # "de março de 2024" / "em março"
    if since is None:
        for index, (_, fold) in enumerate(pairs):
            if fold not in _MONTHS:
                continue
            year, last = now.year, index
            for offset in (1, 2):  # "março 2024" and "março de 2024"
                ahead = index + offset
                if ahead < len(pairs) and re.fullmatch(r"20\d{2}", pairs[ahead][1]):
                    year, last = int(pairs[ahead][1]), ahead
                    break
            since, until = _month_bounds(year, _MONTHS[fold])
            consumed.update(range(max(0, index - 1), last + 1))
            break

    # A bare year: "de 2024", "em 2023".
    if since is None:
        for index, (_, fold) in enumerate(pairs):
            if re.fullmatch(r"20\d{2}", fold):
                since = datetime(int(fold), 1, 1, tzinfo=UTC)
                until = datetime(int(fold) + 1, 1, 1, tzinfo=UTC)
                consumed.update(range(max(0, index - 1), index + 1))
                break

    # Whatever is left after the type and the dates, introduced by a lead word,
    # is the client or matter. Which words count as a lead depends on whether
    # the person typed the command: see `explicit` above.
    matter = None
    remaining = [(index, word, fold) for index, (word, fold) in enumerate(pairs)
                 if index not in consumed]
    lead_words = _MATTER_LEAD_WORDS if explicit else _MATTER_NOUNS
    for position, (_, _, fold) in enumerate(remaining):
        if fold not in lead_words:
            continue
        tail = remaining[position + 1:]
        # A name never spans a question or an exclamation: whatever follows one
        # is a second sentence, not more of the client. The full stop is not on
        # this list on purpose, because "Acme S.A." is a real name.
        for stop, (_, word, _) in enumerate(tail):
            if "?" in word or "!" in word:
                tail = tail[:stop + 1]
                break
        # Drop a second lead word: "do cliente Acme" names Acme, not "cliente".
        while tail and tail[0][2] in _MATTER_LEAD_WORDS:
            tail = tail[1:]
        # A preposition left dangling at the end belongs to a filter that was
        # already parsed out, not to the client's name.
        while tail and tail[-1][2] in _MATTER_LEAD_WORDS:
            tail = tail[:-1]
        candidate = " ".join(word for _, word, _ in tail).strip(" .,;:!?")
        if len(candidate) >= 2 and _normalize(candidate) not in _NOT_A_MATTER:
            matter = candidate
        break

    return DocumentFilters(matter=matter, doc_type=doc_type, since=since, until=until)
