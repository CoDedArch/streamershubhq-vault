"""Money is integer pesewas everywhere. These helpers format it for emails and audit text only."""


def ghs(minor: int | None) -> str:
    if minor is None:
        return "—"
    sign = "-" if minor < 0 else ""
    cedis, pesewas = divmod(abs(int(minor)), 100)
    return f"{sign}GHS {cedis:,}" + (f".{pesewas:02d}" if pesewas else "")


def cedis(amount: float | int) -> int:
    """GHS to pesewas, for seed data and forms that take whole cedis."""
    return int(round(float(amount) * 100))


def month_key(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def prev_month(key: str) -> str:
    y, m = map(int, key.split("-"))
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def next_month(key: str) -> str:
    y, m = map(int, key.split("-"))
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def month_label(key: str) -> str:
    import calendar

    y, m = map(int, key.split("-"))
    return f"{calendar.month_name[m]} {y}"
