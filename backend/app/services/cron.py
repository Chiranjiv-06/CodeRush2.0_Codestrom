"""Small 5-field cron parser (minute hour day month weekday).

Supports ``*``, ``*/n``, ranges ``a-b``, lists ``a,b,c`` and ``a-b/n``, plus the
usual ``@hourly`` / ``@daily`` / ``@weekly`` / ``@monthly`` macros — enough for
recurring exchange jobs without pulling in an extra dependency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


class CronError(ValueError):
    pass


def _expand(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError("empty cron field element")
        step = 1
        if "/" in part:
            part, step_raw = part.split("/", 1)
            if not step_raw.isdigit() or int(step_raw) == 0:
                raise CronError(f"bad step '{step_raw}'")
            step = int(step_raw)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < low or end > high or start > end:
            raise CronError(f"cron value out of range: {part}")
        values.update(range(start, end + 1, step))
    return values


def parse(expression: str) -> list[set[int]]:
    expr = MACROS.get(expression.strip().lower(), expression).strip()
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("cron expression must have 5 fields")
    return [_expand(f, lo, hi) for f, (lo, hi) in zip(fields, FIELD_RANGES)]


def matches(expression: str, moment: datetime) -> bool:
    minute, hour, dom, month, dow = parse(expression)
    return (
        moment.minute in minute
        and moment.hour in hour
        and moment.day in dom
        and moment.month in month
        and (moment.weekday() + 1) % 7 in dow  # cron: Sunday = 0
    )


def next_fire_time(expression: str, after: datetime | None = None,
                   horizon_days: int = 400) -> datetime | None:
    after = (after or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    candidate = after + timedelta(minutes=1)
    limit = after + timedelta(days=horizon_days)
    parse(expression)  # validate once, fail fast
    while candidate <= limit:
        if matches(expression, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def describe(expression: str) -> str:
    try:
        parse(expression)
        return f"valid cron '{MACROS.get(expression.strip().lower(), expression)}'"
    except CronError as exc:
        return f"invalid cron: {exc}"
