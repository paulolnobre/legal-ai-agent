"""Whether an answer was any good, according to the person who asked.

Every other measurement in this system says what happened, not whether it
helped: how many passages were retrieved, how long the model took, what it
cost. None of that separates a good answer from a confident wrong one, and
that is the question the pilot has to answer before ten lawyers arrive.

So this is one row per verdict, written when somebody presses 👍 or 👎 under
an answer, and it is deliberately small. A verdict, who gave it, what was
asked, and which specialist answered. The point is to be able to read the
fifteen worst answers of a month and see what they have in common, which is
a thing a person does, not a dashboard.

**A vote can be changed.** The decision buttons in this system retire their
siblings on the first press, because a decision must happen once. An opinion
is not a decision: somebody who presses 👍 and then reads more carefully must
be able to say so, and the corrected verdict is better data than the first
impulse. The uniqueness is therefore on the answer and the person, and a
second press updates rather than adding a contradicting row.

The question is stored truncated. It is already in `document_access_log` on
the retrieval path, so this adds no new class of data, and without it a 👎 is
a number nobody can act on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# Long enough to recognise what was asked, short enough that this table never
# becomes a second copy of the conversation.
QUESTION_MAX = 500

VERDICTS = ("util", "nao_util")


@dataclass(frozen=True)
class Verdict:
    """One person's opinion of one answer."""

    answer_ref: str
    staff_id: int
    verdict: str
    question: str
    task: str
    created_at: datetime | None = None


async def ensure_feedback_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_feedback (
                answer_ref  VARCHAR(64) NOT NULL,
                staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                chat_id     VARCHAR(64) NOT NULL,
                verdict     VARCHAR(16) NOT NULL,
                question    TEXT NOT NULL DEFAULT '',
                task        VARCHAR(32) NOT NULL DEFAULT '',
                note        TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (answer_ref, staff_id)
            )
            """
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_feedback_created "
            "ON answer_feedback(created_at DESC)"
        )
    await conn.commit()


async def record(conn, *, answer_ref: str, staff_id: int, chat_id,
                 verdict: str, question: str = "", task: str = "",
                 commit: bool = True) -> bool:
    """Store one verdict. Returns whether it changed a vote already cast.

    An unknown verdict is refused rather than stored: a column that quietly
    accepts anything stops being countable, and the caller is our own button.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}; expected one of {VERDICTS}")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO answer_feedback
                (answer_ref, staff_id, chat_id, verdict, question, task)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (answer_ref, staff_id) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                updated_at = NOW()
            RETURNING (xmax <> 0) AS changed
            """,
            (answer_ref, staff_id, str(chat_id), verdict, question[:QUESTION_MAX], task),
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return bool(row and row[0])


async def add_note(conn, *, answer_ref: str, staff_id: int, note: str,
                   commit: bool = True) -> bool:
    """Attach what was wrong to a verdict already given."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE answer_feedback SET note = %s, updated_at = NOW() "
            "WHERE answer_ref = %s AND staff_id = %s",
            (note[:QUESTION_MAX], answer_ref, staff_id),
        )
        attached = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return attached


async def tally(conn, *, days: int = 30) -> dict[str, int]:
    """How many of each verdict in the window, for `/status`."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT verdict, COUNT(*) FROM answer_feedback "
            "WHERE created_at >= NOW() - make_interval(days => %s) "
            "GROUP BY verdict",
            (int(days),),
        )
        rows = await cur.fetchall()
    counts = {verdict: 0 for verdict in VERDICTS}
    for verdict, total in rows:
        counts[verdict] = int(total)
    return counts


async def unhelpful(conn, *, limit: int = 15) -> list[Verdict]:
    """The answers people marked as not helpful, newest first.

    This is the list somebody reads. Bounded, because a review nobody finishes
    is a review that does not happen.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT answer_ref, staff_id, verdict, question, task, created_at
            FROM answer_feedback
            WHERE verdict = 'nao_util'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (max(1, min(int(limit), 100)),),
        )
        rows = await cur.fetchall()
    return [
        Verdict(answer_ref=row[0], staff_id=row[1], verdict=row[2],
                question=row[3], task=row[4], created_at=row[5])
        for row in rows
    ]


__all__ = [
    "QUESTION_MAX", "VERDICTS", "Verdict", "add_note", "ensure_feedback_schema",
    "record", "tally", "unhelpful",
]
