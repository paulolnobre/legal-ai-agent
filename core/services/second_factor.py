"""The second factor, checked the same way wherever it is asked for.

Two places ask: the web login, and the bot when someone wants to start their
authenticator over. They share this so the lock below cannot be missing from
one of them. A six-digit code is only a factor while guessing it is slow: one
code per login link, with links free to ask for, would find it in about a day.
"""
import enum
import logging

from core import totp
from core.integrations import telegram
from core.repositories import web_access
from core.repositories.staff import ROLE_OWNER, get_staff_by_id, list_staff

logger = logging.getLogger(__name__)

# Five tries per quarter hour is about five hundred a day, against three valid
# codes in a million at any moment: well under one chance in a thousand.
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


class Check(enum.Enum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    LOCKED = "locked"


async def check(conn, staff_id: int, code: str) -> Check:
    """Whether this code lets this person in, counting the wrong ones."""
    if await web_access.second_factor_locked(conn, staff_id):
        return Check.LOCKED
    if await _matches(conn, staff_id, code):
        await web_access.clear_failures(conn, staff_id)
        return Check.ACCEPTED
    locked_now = await web_access.record_failure(
        conn, staff_id, limit=MAX_FAILURES, lock_seconds=LOCK_SECONDS,
    )
    if locked_now:
        await tell_about_lock(conn, staff_id)
        return Check.LOCKED
    return Check.REFUSED


async def _matches(conn, staff_id: int, code: str) -> bool:
    """A current authenticator code, or an unused recovery code.

    The authenticator is tried first and its step is claimed, so the same
    code cannot be used twice even inside its window. A recovery code is only
    tried when the input is not a six-digit code, so a mistyped authenticator
    code never spends one of the few recovery codes by accident.
    """
    enrolment = await web_access.enrolment_of(conn, staff_id)
    if enrolment is None:
        return False
    digits = "".join(character for character in code if character.isdigit())
    if len(digits) == totp.DIGITS and len(code.strip().replace(" ", "")) == totp.DIGITS:
        # The row stays locked from reading the secret to recording the step
        # and the confirmation, so an enrolment cannot be written in between
        # and inherit the confirmation this code earned for the old secret.
        # Every path out of check() commits, which releases it.
        secret = await web_access.secret_for_check(conn, staff_id)
        if not secret:
            return False
        counter = totp.matching_counter(secret, digits)
        if counter is None:
            return False
        if not await web_access.claim_counter(conn, staff_id, counter, commit=False):
            return False
        if not enrolment.confirmed:
            await web_access.confirm_enrolment(conn, staff_id, commit=False)
        await conn.commit()
        return True
    # A recovery code stands in for the app only once the app has been shown
    # to work. Accepting one to confirm an enrolment would let somebody sign in
    # without ever having saved the secret, and meet the lock the next day.
    if not enrolment.confirmed:
        return False
    return await web_access.spend_recovery_code(conn, staff_id, code)


async def tell_about_lock(conn, staff_id: int, *, face_id: bool = False) -> None:
    """Tell the person and the owners. The owners are the part that matters.

    Whoever is guessing may be holding this person's Telegram, so a message to
    that account alone could be read and deleted by the guesser. An owner's
    account is a different one.
    """
    person = await get_staff_by_id(conn, staff_id)
    name = person.name if person is not None else f"staff {staff_id}"
    minutes = LOCK_SECONDS // 60
    reason = "tentativas de Face ID recusadas" if face_id else "códigos errados"
    logger.warning("Second factor locked for staff %s after %s refused attempts",
                   staff_id, MAX_FAILURES)
    recipients: list[tuple[object, str]] = []
    if person is not None and person.chat_id:
        recipients.append((
            person.chat_id,
            f"🔒 Seu acesso às páginas ficou travado por {minutes} minutos depois "
            f"de {MAX_FAILURES} {reason} em sequência. Se não foi você, use "
            f"/mfa e avise o responsável pelo escritório.",
        ))
    for owner in await list_staff(conn, roles=(ROLE_OWNER,)):
        if owner.id == staff_id or not owner.chat_id:
            continue
        recipients.append((
            owner.chat_id,
            f"🔒 O acesso às páginas de {name} ficou travado por {minutes} minutos "
            f"depois de {MAX_FAILURES} {reason} em sequência. Se não foi essa "
            f"pessoa, confirme com ela o que aconteceu: "
            f"/revogar {staff_id} tira o acesso; /mfa liberar {staff_id} zera o "
            f"autenticador depois que você falar com ela.",
        ))
    for chat_id, text in recipients:
        try:
            await telegram.send_message(chat_id, text, parse_mode=None)
        except Exception:
            logger.exception("Could not tell chat %s about a second-factor lock", chat_id)


__all__ = ["Check", "LOCK_SECONDS", "MAX_FAILURES", "check"]
