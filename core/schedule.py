"""Business-hours decisions shared by the webhook and queue worker."""
from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import settings


OUT_OF_HOURS_MESSAGE = (
    "Olá! Nosso assistente jurídico está fora do horário de expediente no momento. "
    "Deixe sua dúvida ou documento aqui; começaremos a processar tudo a partir das "
    "08:00 de amanhã!"
)


def local_now(now: datetime | None = None) -> datetime:
    timezone = ZoneInfo(settings.office_timezone)
    if now is None:
        return datetime.now(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone)
    return now.astimezone(timezone)


def is_office_hours(now: datetime | None = None) -> bool:
    """Return True inside the configured daily window (08:00–20:01 by default)."""
    current = local_now(now)
    minute = current.hour * 60 + current.minute
    start = settings.office_start_hour * 60 + settings.office_start_minute
    end = settings.office_end_hour * 60 + settings.office_end_minute
    return start <= minute < end
