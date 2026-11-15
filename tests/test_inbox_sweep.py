"""Reading the bucket on a schedule without becoming noise.

`/importar` was enough while somebody uploaded a file and immediately asked
about it. Drive synchronising into the bucket changed that: documents arrive
continuously, from people who are not in the chat and have no reason to know a
command exists, and a document the firm believes it delivered sits unread
because nobody typed anything.

A scheduled job that talks too much is worse than no scheduled job, because
people learn to ignore the channel and then miss the run that mattered. Most of
what follows is about staying quiet.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from core.models import StaffMember
from core.services import inbox_sweep
from core.services.inbox_import import ImportReport

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
ADMIN = StaffMember(id=2, chat_id="456", name="Admin", role="admin")


@pytest.fixture
def sweep(monkeypatch):
    """The bucket, the database and Telegram, all recorded rather than real."""
    state = SimpleNamespace(report=ImportReport(), sent=[], imports=0, staff_ids=[])

    async def import_new_documents(_conn, *, staff_id):
        state.imports += 1
        state.staff_ids.append(staff_id)
        return state.report

    async def list_staff(_conn, roles=None):
        return [OWNER, ADMIN]

    async def send_message(chat_id, text, **_kwargs):
        state.sent.append((chat_id, text))

    class _Pool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    return "connection"

                async def __aexit__(self, *_exc):
                    return False
            return _Ctx()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(inbox_sweep, "import_new_documents", import_new_documents)
    monkeypatch.setattr(inbox_sweep, "list_staff", list_staff)
    monkeypatch.setattr(inbox_sweep, "get_pool", get_pool)
    monkeypatch.setattr(inbox_sweep.telegram, "send_message", send_message)
    return state


def _ingested(*entries):
    return ImportReport(ingested=list(entries))


# ── staying quiet ───────────────────────────────────────────────────────────

def test_a_sweep_that_found_nothing_says_nothing(sweep):
    """A message every half hour saying "nothing new" trains people to ignore
    the channel, and the one run that mattered arrives into that habit."""
    sweep.report = ImportReport(listed=12, already_known=12)

    asyncio.run(inbox_sweep.sweep_once())

    assert sweep.sent == []


def test_a_skip_is_never_announced(sweep):
    """A skip is decided from the listing and repeats every single sweep for as
    long as the file sits there: a holiday photo would announce itself forever.

    Skips stay in /importar, where a person asked and is present to act.
    """
    sweep.report = ImportReport(
        listed=3, skipped=[("foto-das-ferias.heic", "formato .heic não é aceito")]
    )

    asyncio.run(inbox_sweep.sweep_once())

    assert sweep.sent == []


def test_a_failure_is_announced_because_it_needs_a_person(sweep):
    """Unlike a skip, a parse failure means a document the firm believes it
    delivered is not in the archive and never will be without help."""
    sweep.report = ImportReport(failed=[("contrato.pdf", "não consegui extrair o texto")])

    asyncio.run(inbox_sweep.sweep_once())

    assert len(sweep.sent) == 2
    assert "contrato.pdf" in sweep.sent[0][1]


# ── what it says when it does speak ─────────────────────────────────────────

def test_every_manager_is_told_what_arrived_and_how_it_was_filed(sweep):
    sweep.report = _ingested(
        ("Clientes/Acme/Contratos/aditivo.pdf", "Acme", "contrato"),
        ("solto.pdf", None, None),
    )

    asyncio.run(inbox_sweep.sweep_once())

    assert [chat for chat, _ in sweep.sent] == ["123", "456"]
    message = sweep.sent[0][1]
    assert "aditivo.pdf" in message
    assert "contrato" in message and "Acme" in message
    assert "sem classificação" in message, "an unfiled document has to look unfiled"
    assert "/editar" in message, "say how to correct it, next to the thing to correct"


def test_one_unreachable_chat_does_not_silence_the_others(sweep, monkeypatch):
    """The documents are in the archive either way; the notice is not the work."""
    async def send_message(chat_id, text, **_kwargs):
        if chat_id == "123":
            raise RuntimeError("chat not found")
        sweep.sent.append((chat_id, text))

    monkeypatch.setattr(inbox_sweep.telegram, "send_message", send_message)
    sweep.report = _ingested(("x.pdf", None, None))

    asyncio.run(inbox_sweep.sweep_once())

    assert [chat for chat, _ in sweep.sent] == ["456"]


# ── who the scheduled run says it is ────────────────────────────────────────

def test_a_scheduled_import_claims_no_uploader(sweep):
    """Filling uploaded_by with the owner's id is a small lie in an audit trail
    that exists to be believed. Nobody uploaded it; a folder synchronised."""
    sweep.report = _ingested(("x.pdf", None, None))

    asyncio.run(inbox_sweep.sweep_once())

    assert sweep.staff_ids == [None]


# ── not running twice at once ───────────────────────────────────────────────

def test_a_sweep_never_overlaps_another(sweep, monkeypatch):
    """Two passes list the same files and download them twice before either
    writes to the ledger. Ingestion deduplicates, so the cost is bandwidth and
    embeddings rather than a wrong archive, which is still worth not paying."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_import(_conn, *, staff_id):
        sweep.imports += 1
        started.set()
        await release.wait()
        return ImportReport()

    monkeypatch.setattr(inbox_sweep, "import_new_documents", slow_import)

    async def scenario():
        first = asyncio.create_task(inbox_sweep.sweep_once())
        await started.wait()
        # Must return at once without importing, rather than queueing behind
        # the running pass: a tick that waits piles ticks up behind it.
        await asyncio.wait_for(inbox_sweep.sweep_once(), timeout=1)
        release.set()
        await first

    asyncio.run(scenario())

    assert sweep.imports == 1, "the second tick ran the import anyway"


# ── the schedule ────────────────────────────────────────────────────────────

def _expect_the_worker_to_decline():
    """Run the worker under a deadline instead of waiting on it.

    A worker that is supposed to decline returns at once. One whose guard is
    broken enters the loop and waits out the whole sweep interval on a stop
    event nothing will set, so without the deadline the test for a broken guard
    hangs for half an hour rather than failing. Measured: that is exactly what
    happened while mutation-checking this file.
    """
    async def bounded():
        await asyncio.wait_for(
            inbox_sweep.run_inbox_sweep_worker(asyncio.Event()), timeout=1
        )

    asyncio.run(bounded())


def test_the_worker_does_nothing_when_the_sweep_is_off(monkeypatch, sweep):
    monkeypatch.setattr(settings, "inbox_enabled", True)
    monkeypatch.setattr(settings, "inbox_sweep_enabled", False)

    _expect_the_worker_to_decline()

    assert sweep.imports == 0


def test_the_worker_does_nothing_when_the_bucket_itself_is_off(monkeypatch, sweep):
    """Turning the sweep on must not turn the bucket on by implication."""
    monkeypatch.setattr(settings, "inbox_enabled", False)
    monkeypatch.setattr(settings, "inbox_sweep_enabled", True)

    _expect_the_worker_to_decline()

    assert sweep.imports == 0


@pytest.mark.parametrize("hour, expected", [(3, 0), (10, 1)])
def test_it_sweeps_in_office_hours_and_not_at_three_in_the_morning(
    monkeypatch, sweep, hour, expected,
):
    """Nothing in a law firm needs a contract indexed at three in the morning,
    and a sweep at that hour spends money to produce a notice nobody wants.

    Anybody working late still has /importar, which is unchanged.
    """
    monkeypatch.setattr(settings, "inbox_enabled", True)
    monkeypatch.setattr(settings, "inbox_sweep_enabled", True)
    monkeypatch.setattr(settings, "inbox_sweep_minutes", 5)
    monkeypatch.setattr(
        inbox_sweep, "is_office_hours",
        lambda: 8 <= datetime(2026, 9, 10, hour, tzinfo=SAO_PAULO).hour < 19,
    )

    stop = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(inbox_sweep.run_inbox_sweep_worker(stop))
        await asyncio.sleep(0)          # let the first pass run
        await asyncio.sleep(0)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert sweep.imports == expected
