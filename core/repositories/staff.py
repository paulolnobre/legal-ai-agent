"""Authorized staff and human-controlled Telegram access requests.

The internal assistant has access to the ENTIRE document corpus, so access is
restricted: only chat_ids registered and active here may run queries.
"""
import secrets
from dataclasses import dataclass
from typing import Literal

from core.models import AccessRequest, StaffMember
from core.repositories import web_access

# Four ranks, ordered by how much of the firm's material each may reach. The
# order matters: a check is "at least this rank", so adding a rank in the middle
# never silently widens what the ranks above it could already do.
ROLE_INTERN = "estagiario"
ROLE_LAWYER = "advogado"
ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"

ROLE_ORDER = (ROLE_INTERN, ROLE_LAWYER, ROLE_ADMIN, ROLE_OWNER)
VALID_ROLES = ROLE_ORDER
MANAGEMENT_ROLES = (ROLE_ADMIN, ROLE_OWNER)

# "user" predates this scale and meant a working lawyer, so those rows migrate
# to ROLE_LAWYER. Anything else unrecognized falls to the LOWEST rank: a role
# corrupted in the database must lose privilege, never gain it.
ROLE_USER = ROLE_LAWYER  # legacy alias, kept so old imports keep working
_LEGACY_ROLES = {"user": ROLE_LAWYER, "staff": ROLE_LAWYER}

ROLE_LABELS = {
    ROLE_INTERN: "estagiário",
    ROLE_LAWYER: "advogado",
    ROLE_ADMIN: "admin",
    ROLE_OWNER: "owner",
}


class StaffDeactivationError(ValueError):
    """A safe policy refusal raised by the staff repository."""

    def __init__(self, code: Literal["actor_not_owner", "self", "target_owner"]):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StaffDeactivation:
    status: Literal["deactivated", "already_inactive", "not_found"]
    staff: StaffMember | None = None


# What a person's row says after their data is erased. It is a marker, not a
# name: it has to be obvious in a listing that nobody is called this.
ERASED_NAME = "Pessoa removida"


@dataclass(frozen=True)
class PersonErasure:
    status: Literal["erased", "already_erased", "not_found"]
    staff: StaffMember | None = None
    # What the erasure touched, so the answer can say it rather than promise it.
    access_log_entries: int = 0
    steps_removed: int = 0
    buttons_removed: int = 0
    requests_anonymised: int = 0
    watches_removed: int = 0


def normalize_role(value) -> str:
    """Map anything stored in the column to one of the four current ranks."""
    role = str(value or "").strip().casefold()
    if role in VALID_ROLES:
        return role
    return _LEGACY_ROLES.get(role, ROLE_INTERN)


def role_rank(value) -> int:
    """Position on the scale, so permissions read as "at least this rank"."""
    return ROLE_ORDER.index(normalize_role(value))


def has_rank(value, minimum: str) -> bool:
    return role_rank(value) >= role_rank(minimum)


def new_request_code() -> str:
    """Create a short code that staff can safely dictate or type in Telegram."""
    return f"NS-{secrets.token_hex(4).upper()}"


async def get_staff_by_chat_id(conn, chat_id) -> StaffMember | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, chat_id, email, name, role, active
            FROM staff
            WHERE chat_id = %s AND active = TRUE
            """,
            (str(chat_id),),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return StaffMember(
        id=row[0], chat_id=row[1], email=row[2], name=row[3], role=row[4], active=row[5],
    )


async def has_unrestricted_access(conn, chat_id) -> bool:
    """True when this chat may be served outside office hours.

    Asked by the webhook before it decides which queue an update goes to, so it
    is a single indexed lookup that returns a boolean and never the person's
    details — the webhook has no business holding those.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT role FROM staff WHERE chat_id = %s AND active = TRUE",
            (str(chat_id),),
        )
        row = await cur.fetchone()
    return bool(row) and has_rank(row[0], ROLE_OWNER)


async def get_staff_by_id(conn, staff_id: int) -> StaffMember | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, chat_id, email, name, role, active
            FROM staff
            WHERE id = %s AND active = TRUE
            """,
            (staff_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return StaffMember(
        id=row[0], chat_id=row[1], email=row[2], name=row[3], role=row[4], active=row[5],
    )


async def add_staff(conn, name, chat_id=None, email=None, role=ROLE_LAWYER):
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid staff role: {role}")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO staff (name, chat_id, email, role)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE
              SET name = EXCLUDED.name, role = EXCLUDED.role, active = TRUE
            RETURNING id
            """,
            (name, str(chat_id) if chat_id else None, email, role),
        )
        row = await cur.fetchone()
        await conn.commit()
        return row[0]


async def list_staff(conn, roles: tuple[str, ...] | None = None) -> list[StaffMember]:
    params: tuple = ()
    where = "WHERE active = TRUE"
    if roles:
        where += " AND role = ANY(%s)"
        params = (list(roles),)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT id, chat_id, email, name, role, active
            FROM staff
            {where}
            ORDER BY CASE role
                WHEN 'owner' THEN 0
                WHEN 'admin' THEN 1
                ELSE 2
            END, name, id
            """,
            params,
        )
        rows = await cur.fetchall()
    return [
        StaffMember(
            id=row[0], chat_id=row[1], email=row[2], name=row[3],
            role=row[4], active=row[5],
        )
        for row in rows
    ]


async def create_or_refresh_access_request(
    conn,
    *,
    chat_id,
    name: str,
    username: str | None = None,
) -> tuple[AccessRequest, bool]:
    """Return a pending request and whether managers should be notified now."""
    chat_id = str(chat_id)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, request_code, chat_id, name, username, status, requested_at
            FROM access_requests WHERE chat_id = %s
            """,
            (chat_id,),
        )
        row = await cur.fetchone()
        if row and row[5] == "pending":
            return AccessRequest(
                id=row[0], request_code=row[1], chat_id=row[2], name=row[3],
                username=row[4], status=row[5], requested_at=row[6],
            ), False

        code = new_request_code()
        if row:
            await cur.execute(
                """
                UPDATE access_requests
                SET request_code = %s, name = %s, username = %s, status = 'pending',
                    decided_by = NULL, requested_at = NOW(), decided_at = NULL
                WHERE id = %s
                RETURNING id, request_code, chat_id, name, username, status, requested_at
                """,
                (code, name, username, row[0]),
            )
        else:
            await cur.execute(
                """
                INSERT INTO access_requests (request_code, chat_id, name, username)
                VALUES (%s, %s, %s, %s)
                RETURNING id, request_code, chat_id, name, username, status, requested_at
                """,
                (code, chat_id, name, username),
            )
        created = await cur.fetchone()
    await conn.commit()
    return AccessRequest(
        id=created[0], request_code=created[1], chat_id=created[2], name=created[3],
        username=created[4], status=created[5], requested_at=created[6],
    ), True


async def list_pending_access_requests(conn, limit: int = 50) -> list[AccessRequest]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, request_code, chat_id, name, username, status, requested_at
            FROM access_requests
            WHERE status = 'pending'
            ORDER BY requested_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        AccessRequest(
            id=row[0], request_code=row[1], chat_id=row[2], name=row[3],
            username=row[4], status=row[5], requested_at=row[6],
        )
        for row in rows
    ]


async def get_access_request(conn, code: str) -> AccessRequest | None:
    """One request by its code, whatever state it is in.

    Not filtered to pending, unlike the listing above: this backs the details
    a manager asks for, and a manager looking at a notification for something
    already decided should be told that, not told it does not exist.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, request_code, chat_id, name, username, status, requested_at
            FROM access_requests
            WHERE request_code = %s
            """,
            (code,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return AccessRequest(
        id=row[0], request_code=row[1], chat_id=row[2], name=row[3],
        username=row[4], status=row[5], requested_at=row[6],
    )


async def cancel_access_request(conn, chat_id) -> AccessRequest | None:
    """Withdraw one person's own pending request, if they still have one.

    A single conditional UPDATE, like every other claim in this project: read
    then write would let a manager's decision and the person's withdrawal both
    believe they won. Whoever's statement matches `status = 'pending'` first
    settles it, and the other finds nothing to change.

    `decided_by` stays NULL on purpose. Nobody at the firm decided this, and an
    audit that recorded a withdrawal as a rejection by a manager who never saw
    it would be worse than one that said nothing.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE access_requests
            SET status = 'cancelled', decided_at = NOW(), decided_by = NULL
            WHERE chat_id = %s AND status = 'pending'
            RETURNING id, request_code, chat_id, name, username, status, requested_at
            """,
            (str(chat_id),),
        )
        row = await cur.fetchone()
    await conn.commit()
    if not row:
        return None
    return AccessRequest(
        id=row[0], request_code=row[1], chat_id=row[2], name=row[3],
        username=row[4], status=row[5], requested_at=row[6],
    )


async def decide_access_request(
    conn,
    code: str,
    *,
    approved: bool,
    decided_by: int,
) -> AccessRequest | None:
    """Approve into the intern role or reject a still-pending access request."""
    normalized_code = code.strip().upper()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, request_code, chat_id, name, username, status, requested_at
            FROM access_requests
            WHERE request_code = %s
            FOR UPDATE
            """,
            (normalized_code,),
        )
        row = await cur.fetchone()
        if not row or row[5] != "pending":
            return None
        request = AccessRequest(
            id=row[0], request_code=row[1], chat_id=row[2], name=row[3],
            username=row[4], status=row[5], requested_at=row[6],
        )
        if approved:
            # Access is granted at the lowest rank; an admin promotes from
            # there. On conflict the existing role is deliberately preserved:
            # re-approving somebody who is already an admin must not silently
            # demote them back to the entry rank.
            await cur.execute(
                """
                INSERT INTO staff (name, chat_id, role, active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (chat_id) DO UPDATE
                SET name = EXCLUDED.name, active = TRUE
                """,
                (request.name, request.chat_id, ROLE_INTERN),
            )
        status = "approved" if approved else "rejected"
        await cur.execute(
            """
            UPDATE access_requests
            SET status = %s, decided_by = %s, decided_at = NOW()
            WHERE id = %s
            """,
            (status, decided_by, request.id),
        )
    await conn.commit()
    return request.model_copy(update={"status": status})


async def set_staff_role(conn, staff_id: int, role: str) -> StaffMember | None:
    """Change an existing active user's role; caller enforces owner-only policy."""
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid staff role: {role}")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE staff SET role = %s
            WHERE id = %s AND active = TRUE
            RETURNING id, chat_id, email, name, role, active
            """,
            (role, staff_id),
        )
        row = await cur.fetchone()
    await conn.commit()
    if not row:
        return None
    return StaffMember(
        id=row[0], chat_id=row[1], email=row[2], name=row[3], role=row[4], active=row[5],
    )


async def deactivate_staff(
    conn,
    *,
    actor_id: int,
    target_id: int,
) -> StaffDeactivation:
    """Revoke one non-owner account while preserving its audit references.

    Authorization is repeated here instead of trusting the Telegram handler so
    future buttons or natural-language routes cannot bypass the owner-only rule.
    Row locks make two confirmations idempotent: only the first one changes the
    account and writes an audit event.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, role, active FROM staff WHERE id = %s FOR UPDATE",
            (actor_id,),
        )
        actor = await cur.fetchone()
        if not actor or not actor[2] or normalize_role(actor[1]) != ROLE_OWNER:
            raise StaffDeactivationError("actor_not_owner")

        await cur.execute(
            """
            SELECT id, chat_id, email, name, role, active
            FROM staff WHERE id = %s FOR UPDATE
            """,
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await conn.commit()
            return StaffDeactivation("not_found")

        target = StaffMember(
            id=row[0], chat_id=row[1], email=row[2], name=row[3],
            role=row[4], active=row[5],
        )
        if target.id == actor_id:
            raise StaffDeactivationError("self")
        if normalize_role(target.role) == ROLE_OWNER:
            raise StaffDeactivationError("target_owner")
        if not target.active:
            await conn.commit()
            return StaffDeactivation("already_inactive", target)

        await cur.execute(
            "UPDATE staff SET active = FALSE WHERE id = %s",
            (target.id,),
        )
        await cur.execute(
            """
            INSERT INTO staff_management_log
                (action, actor_staff_id, target_staff_id, target_role, reason)
            VALUES ('deactivate', %s, %s, %s, 'owner_revoked_access')
            """,
            (actor_id, target.id, normalize_role(target.role)),
        )
    await conn.commit()
    return StaffDeactivation("deactivated", target.model_copy(update={"active": False}))


async def erase_person(
    conn,
    *,
    actor_id: int,
    target_id: int,
) -> PersonErasure:
    """Erase everything that identifies one person, and keep what the firm owes.

    LGPD art. 18 V gives a person the right to have their data erased. It does
    not give anyone the right to erase the firm's records: art. 16 I preserves
    what has to be kept to satisfy a legal obligation, and an audit trail saying
    which client documents were read, and when, is exactly that. Deleting the
    staff row would take the trail with it, because every log entry points at it.

    So the row survives with its id and loses its identity. The name becomes a
    marker, the chat id and the email go, and the account is left inactive. The
    audit rows keep pointing at a person who can no longer be named, which is
    what "erased" has to mean here: the events are still countable and nobody
    can be identified from them.

    What is deleted outright is conversation state, which carries a chat id and
    has no audit value once the conversation is over. What is deliberately kept
    is every document the person uploaded: it is the firm's client material, and
    it never belonged to whoever happened to send it.

    Authorization is checked here, not only in the handler, for the same reason
    it is in `deactivate_staff`: a future button or sentence must not be able to
    reach this by another route.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, role, active FROM staff WHERE id = %s FOR UPDATE",
            (actor_id,),
        )
        actor = await cur.fetchone()
        if not actor or not actor[2] or normalize_role(actor[1]) != ROLE_OWNER:
            raise StaffDeactivationError("actor_not_owner")

        await cur.execute(
            """
            SELECT id, chat_id, email, name, role, active
            FROM staff WHERE id = %s FOR UPDATE
            """,
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await conn.commit()
            return PersonErasure("not_found")

        target = StaffMember(
            id=row[0], chat_id=row[1], email=row[2], name=row[3],
            role=row[4], active=row[5],
        )
        if target.id == actor_id:
            raise StaffDeactivationError("self")
        if normalize_role(target.role) == ROLE_OWNER:
            raise StaffDeactivationError("target_owner")
        # Nothing identifies this person any more, so there is nothing to erase.
        # Reporting it as done again would claim work that did not happen.
        if target.chat_id is None and target.email is None and target.name == ERASED_NAME:
            await conn.commit()
            return PersonErasure("already_erased", target)

        chat_id = target.chat_id

        # The staff row survives erasure, so its credential cascades never
        # fire. Remove the second factor and every device in this transaction.
        await web_access.forget_enrolment(conn, target.id, commit=False)

        # Conversation state: deleted, not anonymised. A half-finished step
        # belonging to nobody would be answered by nobody.
        steps = buttons = 0
        if chat_id is not None:
            await cur.execute(
                "DELETE FROM pending_actions WHERE chat_id = %s", (chat_id,)
            )
            steps = cur.rowcount or 0
            await cur.execute(
                "DELETE FROM callback_actions WHERE chat_id = %s", (chat_id,)
            )
            buttons = cur.rowcount or 0

        # What the person followed in the DJEN, and what they were told: an
        # OAB is theirs, and none of it is owed to anyone once they are gone.
        await cur.execute("DELETE FROM watched_oabs WHERE staff_id = %s", (target.id,))
        watches = cur.rowcount or 0
        await cur.execute("DELETE FROM watched_processes WHERE staff_id = %s", (target.id,))
        watches += cur.rowcount or 0
        # What they wrote to the bot and what it answered: theirs, and owed to
        # nobody once they are gone. Deleted, not anonymised.
        await cur.execute("DELETE FROM conversation_turns WHERE staff_id = %s", (target.id,))
        # The panel's side of the same conversation, and the files it offered.
        # Explicit: the staff row stays, so no cascade ever fires.
        # Every attachment, taken by a message or not: an unattached one
        # belongs to no request, so the requests' cascade would miss it.
        await cur.execute("DELETE FROM panel_uploads WHERE staff_id = %s", (target.id,))
        await cur.execute("DELETE FROM panel_requests WHERE staff_id = %s", (target.id,))
        await cur.execute("DELETE FROM panel_files WHERE staff_id = %s", (target.id,))
        await cur.execute("DELETE FROM publication_notices WHERE staff_id = %s", (target.id,))
        # Their private cases go with them (deadlines and alerts by cascade);
        # a team case stays with the team. Explicit, as the row survives.
        await cur.execute("DELETE FROM tracked_processes WHERE created_by = %s "
                          "AND visibility = 'private'", (target.id,))
        await cur.execute("DELETE FROM deadline_alerts WHERE staff_id = %s", (target.id,))

        # The audit trail keeps the event and loses the identifier. staff_id
        # still points here, which is why the row below must survive.
        await cur.execute(
            "UPDATE document_access_log SET chat_id = NULL WHERE staff_id = %s",
            (target.id,),
        )
        entries = cur.rowcount or 0

        # The request that let this person in: who approved it and when is worth
        # keeping, who they were is not. chat_id is UNIQUE and NOT NULL, so it
        # takes a value that can never match a real Telegram chat instead of
        # going empty.
        await cur.execute(
            """
            UPDATE access_requests
               SET name = %s, username = NULL, chat_id = %s
             WHERE chat_id = %s
            """,
            (ERASED_NAME, f"erased:{target.id}", chat_id),
        )
        requests = cur.rowcount or 0

        # The person themselves, last: while the rows above still referenced the
        # chat id, this is the row that made them findable.
        await cur.execute(
            """
            UPDATE staff
               SET name = %s, chat_id = NULL, email = NULL, active = FALSE
             WHERE id = %s
            """,
            (ERASED_NAME, target.id),
        )
        await cur.execute(
            """
            INSERT INTO staff_management_log
                (action, actor_staff_id, target_staff_id, target_role, reason)
            VALUES ('erase', %s, %s, %s, 'lgpd_erasure_request')
            """,
            (actor_id, target.id, normalize_role(target.role)),
        )
    await conn.commit()
    erased = target.model_copy(update={
        "name": ERASED_NAME, "chat_id": None, "email": None, "active": False,
    })
    return PersonErasure(
        "erased",
        erased,
        access_log_entries=entries,
        steps_removed=steps,
        buttons_removed=buttons,
        requests_anonymised=requests,
        watches_removed=watches,
    )


async def ensure_access_schema(conn) -> None:
    """Migrate legacy staff roles and install request approval persistence."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS access_requests (
                id SERIAL PRIMARY KEY,
                request_code VARCHAR(16) UNIQUE NOT NULL,
                chat_id VARCHAR(50) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                username VARCHAR(255),
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                decided_by INTEGER REFERENCES staff(id),
                requested_at TIMESTAMP DEFAULT NOW(),
                decided_at TIMESTAMP,
                CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'))
            )
            """
        )
        # A database created before withdrawal existed carries the old check,
        # and init.sql never runs again on it. Dropping by both names makes this
        # idempotent: the first name is the one PostgreSQL generated for the
        # anonymous constraint, the second is the one this statement installs.
        await cur.execute(
            """
            ALTER TABLE access_requests
            DROP CONSTRAINT IF EXISTS access_requests_status_check
            """
        )
        await cur.execute(
            """
            ALTER TABLE access_requests
            DROP CONSTRAINT IF EXISTS access_requests_status_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE access_requests
            ADD CONSTRAINT access_requests_status_allowed
            CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'))
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_access_requests_status
            ON access_requests(status, requested_at)
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_management_log (
                id BIGSERIAL PRIMARY KEY,
                action VARCHAR(32) NOT NULL,
                actor_staff_id INTEGER NOT NULL REFERENCES staff(id),
                target_staff_id INTEGER NOT NULL REFERENCES staff(id),
                target_role VARCHAR(50) NOT NULL,
                reason VARCHAR(64) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CHECK (action IN ('deactivate', 'erase'))
            )
            """
        )
        # A database created before erasure existed only allows 'deactivate',
        # and init.sql never runs again on it, so the first erasure would be
        # refused by a constraint written months earlier. Dropped by both names
        # for the same reason as the status check above: the generated one and
        # the one this statement installs.
        await cur.execute(
            """
            ALTER TABLE staff_management_log
            DROP CONSTRAINT IF EXISTS staff_management_log_action_check
            """
        )
        await cur.execute(
            """
            ALTER TABLE staff_management_log
            DROP CONSTRAINT IF EXISTS staff_management_log_action_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE staff_management_log
            ADD CONSTRAINT staff_management_log_action_allowed
            CHECK (action IN ('deactivate', 'erase'))
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_staff_management_target_created
            ON staff_management_log(target_staff_id, created_at DESC)
            """
        )
        # Migrate to the four-rank scale. "user" meant a working lawyer, so it
        # becomes one; anything else unrecognized drops to the lowest rank,
        # matching normalize_role — a corrupted value must never gain privilege.
        await cur.execute(
            "UPDATE staff SET role = %s WHERE role = ANY(%s)",
            (ROLE_LAWYER, list(_LEGACY_ROLES)),
        )
        await cur.execute(
            "UPDATE staff SET role = %s WHERE role IS NULL OR NOT (role = ANY(%s))",
            (ROLE_INTERN, list(VALID_ROLES)),
        )
        await cur.execute(
            "SELECT id FROM staff WHERE active = TRUE AND role = %s LIMIT 1",
            (ROLE_OWNER,),
        )
        owner = await cur.fetchone()
        if not owner:
            await cur.execute(
                """
                UPDATE staff SET role = %s
                WHERE id = (
                    SELECT id FROM staff WHERE active = TRUE ORDER BY created_at, id LIMIT 1
                )
                """,
                (ROLE_OWNER,),
            )
    await conn.commit()
