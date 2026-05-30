"""Calcolo del prossimo run di un cron nella timezone dello scheduler.

L'espressione cron viene interpretata in ORA LOCALE (`SCHEDULER_TZ`, default
`Europe/Rome`), non in UTC: così "0 3 * * *" significa davvero le 03:00 di quella
tz, e il DST è gestito da zoneinfo (l'epoch risultante si sposta da solo tra ora
solare e legale). `next_run` resta un epoch assoluto (UTC), confrontabile con
`get_timestamp()`. Fonte UNICA: usato sia dall'endpoint (`routers/schedules.py`)
sia dal worker (`workers/scheduler.py`) → niente logica tz duplicata.
"""

from datetime import datetime, timezone

from croniter import croniter

from utils.settings import SCHEDULER_TZ
from utils.logger import get_logger

logger = get_logger(__name__)


def _resolve_tz():
    """ZoneInfo(SCHEDULER_TZ), con fallback a UTC se il nome è invalido o manca il
    database tz (container slim senza /usr/share/zoneinfo → si installa `tzdata`)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(SCHEDULER_TZ)
    except Exception as e:  # ZoneInfoNotFoundError, tz mancante, ecc.
        logger.warning(f"SCHEDULER_TZ '{SCHEDULER_TZ}' non utilizzabile ({e}) → UTC")
        return timezone.utc


# Risolta una volta all'import (la tz non cambia a runtime).
SCHEDULER_TZINFO = _resolve_tz()


def cron_next_run(cron: str, base_ts: int) -> int:
    """Prossima esecuzione (epoch) di `cron` dopo `base_ts`, valutato in SCHEDULER_TZ."""
    base = datetime.fromtimestamp(base_ts, tz=SCHEDULER_TZINFO)
    return int(croniter(cron, base).get_next())
