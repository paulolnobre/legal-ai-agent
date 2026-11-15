"""A table added after the first deploy must also be created at runtime.

`infra/init.sql` is mounted into `/docker-entrypoint-initdb.d/`, which
PostgreSQL runs only when it initializes an empty data directory. It therefore
reaches a fresh install and never a database that has simply been running for a
while. Anything added later needs a `CREATE TABLE IF NOT EXISTS` on the startup
path as well, or the first query against it fails in production while every
test passes locally against a database built from init.sql.

This is a real incident, not a hypothetical: `pending_actions` shipped in
init.sql alone, and the deploy that would have carried it was the one that
broke.
"""
import inspect
import re
from pathlib import Path

from core.repositories import (
    answer_feedback,
    budget_history,
    callback_actions,
    conversation,
    documents,
    firm_holidays,
    inbox,
    microsoft_accounts,
    panel,
    pending_actions,
    pje_credentials,
    processes,
    publications,
    staff,
    teams,
    web_access,
)

# The tables that existed at the first install. Every database has them, so
# they never needed a runtime path. Nothing new belongs in this set — a table
# added today is added to a database that is already running.
_FIRST_INSTALL_TABLES = frozenset({
    "staff",
    "documents",
    "document_chunks",
    "document_access_log",
})

_BOOTSTRAPS = (
    staff.ensure_access_schema,
    documents.ensure_document_storage_schema,
    pending_actions.ensure_pending_actions_schema,
    callback_actions.ensure_callback_actions_schema,
    inbox.ensure_inbox_schema,
    publications.ensure_publication_schema,
    teams.ensure_team_schema,
    microsoft_accounts.ensure_microsoft_schema,
    budget_history.ensure_budget_history_schema,
    pje_credentials.ensure_pje_schema,
    answer_feedback.ensure_feedback_schema,
    web_access.ensure_web_access_schema,
    firm_holidays.ensure_firm_holidays_schema,
    conversation.ensure_conversation_schema,
    panel.ensure_panel_schema,
    processes.ensure_process_schema,
)


def _tables_in(sql: str) -> set[str]:
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))


def test_every_table_added_after_the_first_install_is_created_at_startup():
    declared = _tables_in(Path("infra/init.sql").read_text(encoding="utf-8"))
    created_at_startup = _tables_in(
        "\n".join(inspect.getsource(fn) for fn in _BOOTSTRAPS)
    )

    missing = declared - _FIRST_INSTALL_TABLES - created_at_startup
    assert not missing, (
        f"{sorted(missing)} exist only in init.sql, so a database that is "
        "already running will never get them. Add a CREATE TABLE IF NOT EXISTS "
        "to a startup bootstrap in core/repositories/."
    )


def test_the_first_install_set_is_not_quietly_widened():
    """Adding a new table to the exemption defeats the check above."""
    assert _FIRST_INSTALL_TABLES == {
        "staff",
        "documents",
        "document_chunks",
        "document_access_log",
    }


def test_startup_bootstraps_are_reachable_from_the_application():
    """A bootstrap nobody calls protects nothing."""
    startup = Path("core/web/app.py").read_text(encoding="utf-8")
    for bootstrap in _BOOTSTRAPS:
        assert f"await {bootstrap.__name__}(conn)" in startup, (
            f"{bootstrap.__name__} is never called during startup"
        )


def _value_checks(sql: str) -> dict[str, set[str]]:
    """Every `CHECK (column IN ('a', 'b'))` in a piece of SQL, by column."""
    found: dict[str, set[str]] = {}
    for column, values in re.findall(r"CHECK \((\w+) IN \(([^)]*)\)\)", sql):
        found.setdefault(column, set()).update(re.findall(r"'([^']*)'", values))
    return found


def _migrated_checks(sql: str) -> dict[str, set[str]]:
    """Only a CHECK that an ADD CONSTRAINT installs, which is the migrating one.

    A CHECK inside `CREATE TABLE IF NOT EXISTS` governs a table that does not
    exist yet. On a database running since before a column gained a value, that
    statement does nothing, so reading it as proof the value is allowed is how
    the first version of this guard passed with the migration deleted.

    The constraint name and the CHECK are matched together, in one pattern, for
    the second reason this was wrong: scanning from one ADD CONSTRAINT to the
    next swept up the CREATE TABLE that sits between them, and found the new
    value there instead.
    """
    found: dict[str, set[str]] = {}
    for column, values in re.findall(
        r"ADD CONSTRAINT \w+\s+CHECK \((\w+) IN \(([^)]*)\)\)", sql
    ):
        found.setdefault(column, set()).update(re.findall(r"'([^']*)'", values))
    return found


def test_a_widened_check_constraint_also_reaches_a_database_that_exists():
    """The table guard above does not catch this, and it is the same bug.

    A CHECK written months ago still governs a running database: init.sql never
    runs again, and CREATE TABLE IF NOT EXISTS does nothing to a table that is
    already there. So adding a value to an enumeration in init.sql without an
    ALTER on the startup path produces a constraint violation in production
    while every local test, running against a database built from init.sql,
    passes. `staff_management_log.action` gained 'erase' exactly this way.
    """
    declared = _value_checks(Path("infra/init.sql").read_text(encoding="utf-8"))
    bootstrap_sql = "\n".join(inspect.getsource(fn) for fn in _BOOTSTRAPS)
    created = _value_checks(bootstrap_sql)
    migrated = _migrated_checks(bootstrap_sql)

    for column, values in declared.items():
        if column not in created:
            # No bootstrap governs this column, so the table only ever arrives
            # fresh and the table guard above already covers it.
            continue
        missing = values - migrated.get(column, set())
        assert not missing, (
            f"init.sql allows {sorted(missing)} for {column}, but no ADD "
            "CONSTRAINT on the startup path installs it, so a database that is "
            "already running keeps the older, narrower CHECK and refuses the "
            "new value. Add ALTER TABLE ... DROP CONSTRAINT IF EXISTS followed "
            "by ADD CONSTRAINT beside the CREATE TABLE."
        )
