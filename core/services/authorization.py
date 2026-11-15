"""The single place that decides whether somebody may do something.

Authorization attaches to the **action**, never to the spelling of the request.
A rank check bound to a command string was once walked around by asking for the
same thing in Portuguese prose: the natural-language route was a second door to
the same room, and only one of them was locked. Every entry point — a typed
command, a dictated sentence, an answered question, and later a button or any
other caller — resolves what is being asked and brings that action here.

Deciding is pure. It reads a staff member and an action and returns a verdict;
it sends nothing, logs nothing and touches no connection. That is what lets a
second transport reuse it: the caller decides how a refusal is delivered, while
the rule about who may do what stays in one place.

This is the outer gate, not the only one. Actions that change a person's access
are authorized again inside the repository, under the transaction that performs
them, so neither layer is the only thing standing between a caller and a write.
"""
from dataclasses import dataclass
from typing import Literal, get_args

from core.repositories.staff import (
    ROLE_ADMIN,
    ROLE_INTERN,
    ROLE_LAWYER,
    ROLE_OWNER,
    has_rank,
    normalize_role,
)

Action = Literal[
    # Acting on the archive. Reading and researching the corpus is open to every
    # authorized rank; producing from it, or taking an original out of it, is
    # work the firm answers for.
    "document.read_original",
    "document.revise",
    "document.ingest",
    # Editing changes what everyone sees in the catalogue: the name, the type
    # and the matter. It answers to the rank that may put a document there in
    # the first place. The ingestion date is not on that list and cannot be
    # edited by anyone: it records something that already happened.
    "document.edit",
    # Linking a document to its official page and replacing its text with the
    # page's. It rewrites what everyone reads as the law, so it sits with the
    # rank that may put a law in the corpus in the first place.
    "document.refresh",
    # Removing from the archive. Owner-only, and in its own sentence because it
    # is the one document action that cannot be undone: the row, its chunks and
    # the original file all go, and the only copy left is in a backup.
    "document.delete",
    # Taking a document out of somebody else's private folder. The owner's
    # alone, because the case file belongs to the firm and a resignation must
    # not take a client's papers with it. It is deliberately not a wider view:
    # the owner reaches one named document and the reach is logged and told,
    # rather than browsing what the folders exist to keep separate.
    "document.claim",
    # Connecting one's own Microsoft account. Open to every rank: it is their
    # account, the sign-in happens at Microsoft, and what the agent gets back
    # can do only what that person can already do.
    "microsoft.connect",
    # Enrolling one's own PJe access. Open to every rank, like connecting a
    # Microsoft account: it is their credential, and what it buys is reading
    # what that person can already read. Nothing here can act in a process.
    "pje.connect",
    # Reading a process and listing what is pending, with one's own
    # credential. Open to every rank because the court decides what each
    # person may see; this cannot widen that, only spend it. Reading the
    # teor of a pending intimation is not an action here at all, because
    # doing so is ciência and starts the deadline.
    "pje.read",
    # Opening the web pages. Every rank: the page shows only what that
    # person can already see in the bot, through the same scope.
    "web.access",
    "web.end_all",
    # Starting someone else's second factor over, for a lost phone.
    "web.release",
    # Moving one's own document between the personal folder, a team's and the
    # firm's shelf. Open to every rank: the narrow default means everybody
    # starts with a private folder, and sharing out of it is how the firm's
    # knowledge gets to circulate. Whose document it is, is decided in the
    # repository, not by rank.
    "document.share",
    # Saying whether an answer helped. Open to every rank, and on purpose the
    # one action with no gate worth arguing about: the whole value is in how
    # many people bother, and a rank check would only cost signal.
    "answer.rate",
    # Creating a team and changing who is in it: an administrative act, like
    # deciding who gets in at all.
    "team.manage",
    # Browsing, and the free-text specialists behind the menu. Every one of
    # these is already open to any authorized rank on the text path — /docs,
    # /exportar and plain-text research carry no rank check today — so a menu
    # button reaching the same code must not become a narrower door than the
    # one already open.
    "document.browse",
    "document.view_details",
    "document.upload_help",
    "research.external",
    # Following an OAB or a process in the DJEN. Open to every rank, like
    # /processo: the publications are public, and an intern following the
    # cases they work on is the point.
    "publications.watch",
    # Counting a deadline and reading the firm's calendar. Every rank: it reads
    # nothing private, and an intern checking a date is exactly who should.
    "deadline.calculate",
    # Changing the firm's calendar. Every deadline the firm counts reads it, so
    # a wrong day here moves due dates for everyone: an administrative act.
    "calendar.manage",
    # Following a case and its deadlines in "Processos". Every rank: an intern
    # keeps the calendar too, and what they see is their own or their team's.
    "process.track",
    "drafting.compose",
    "delivery.export",
    # Opening Grafana: the metrics, the logs and the dashboards over the
    # archive's catalogue. Owner-only, because the logs and the panels over
    # the database describe everybody's work, and nothing in Grafana is
    # scoped by folder the way the pages are.
    "observability.view",
    # Managing who gets in.
    "access.review",
    # Reading one request back: who asked, from which Telegram account, when.
    # It decides nothing, so it sits with the reviewing rank rather than above
    # it — anyone who may decide a request may see what they are deciding.
    "access.view_request",
    "staff.read_directory",
    # Changing what someone is. Owner-only, and never granted by a chat command.
    "staff.change_rank",
    "staff.deactivate",
    "staff.erase",
]

_REQUIRED_RANK: dict[Action, str] = {
    "document.read_original": ROLE_LAWYER,
    "document.revise": ROLE_LAWYER,
    "document.ingest": ROLE_LAWYER,
    "document.edit": ROLE_LAWYER,
    "document.refresh": ROLE_LAWYER,
    "document.share": ROLE_INTERN,
    "answer.rate": ROLE_INTERN,
    "microsoft.connect": ROLE_INTERN,
    "pje.connect": ROLE_INTERN,
    "pje.read": ROLE_INTERN,
    "web.access": ROLE_INTERN,
    "web.end_all": ROLE_INTERN,
    "web.release": ROLE_OWNER,
    "team.manage": ROLE_ADMIN,
    "document.delete": ROLE_OWNER,
    "document.claim": ROLE_OWNER,
    "document.browse": ROLE_INTERN,
    "document.view_details": ROLE_INTERN,
    "document.upload_help": ROLE_INTERN,
    "research.external": ROLE_INTERN,
    "publications.watch": ROLE_INTERN,
    "deadline.calculate": ROLE_INTERN,
    "calendar.manage": ROLE_ADMIN,
    "process.track": ROLE_INTERN,
    "drafting.compose": ROLE_INTERN,
    "delivery.export": ROLE_INTERN,
    "observability.view": ROLE_OWNER,
    "access.review": ROLE_ADMIN,
    "access.view_request": ROLE_ADMIN,
    "staff.read_directory": ROLE_ADMIN,
    "staff.change_rank": ROLE_OWNER,
    "staff.deactivate": ROLE_OWNER,
    "staff.erase": ROLE_OWNER,
}

_REFUSAL_BY_RANK = {
    # Reached only when staff is None: an action open to every rank still
    # needs *some* rank on file, so an unauthenticated caller is refused here
    # rather than the lookup itself raising.
    ROLE_INTERN: "🔒 É preciso ter o acesso liberado para usar o assistente.",
    ROLE_LAWYER: (
        "🔒 Este recurso é para advogados. Como estagiário(a), você pode consultar o "
        "acervo, fazer perguntas, pesquisar e calcular prazos. Fale com um admin se "
        "precisar de mais."
    ),
    ROLE_ADMIN: "🔒 Este comando é exclusivo de admins e do owner.",
    ROLE_OWNER: "🔒 Este comando é exclusivo do owner.",
}

# An action nobody thought to add to the table must not become an open door.
_UNKNOWN_ACTION_REFUSAL = "🔒 Esta ação não está disponível."


@dataclass(frozen=True)
class Decision:
    """Whether the action may proceed, and what to say when it may not."""

    allowed: bool
    action: str
    required_rank: str | None = None
    reason: str = ""


def decide(staff, action: str) -> Decision:
    """Rule on one action for one person. Pure: no I/O, no messaging.

    An unrecognized action is refused. Adding a name to ``Action`` without
    giving it a rank is a mistake, and the safe reading of a mistake is "no" —
    the same reasoning that makes an unreadable role fall to the lowest rank
    instead of the highest.
    """
    minimum = _REQUIRED_RANK.get(action)  # type: ignore[arg-type]
    if minimum is None:
        return Decision(
            allowed=False,
            action=action,
            reason=_UNKNOWN_ACTION_REFUSAL,
        )
    if staff is None:
        return Decision(
            allowed=False,
            action=action,
            required_rank=minimum,
            reason=_REFUSAL_BY_RANK[minimum],
        )
    if has_rank(normalize_role(staff.role), minimum):
        return Decision(allowed=True, action=action, required_rank=minimum)
    return Decision(
        allowed=False,
        action=action,
        required_rank=minimum,
        reason=_REFUSAL_BY_RANK[minimum],
    )


def known_actions() -> tuple[str, ...]:
    """Every action the service rules on, for tests and for callers listing them."""
    return get_args(Action)


def required_rank(action: str) -> str | None:
    """The minimum rank an action needs, or None when it has no rule.

    None means the service will refuse the action, so a caller enumerating
    actions can tell a deliberate rule from a gap.
    """
    return _REQUIRED_RANK.get(action)  # type: ignore[arg-type]
