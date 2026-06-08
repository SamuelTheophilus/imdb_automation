from datetime import datetime


def format_date(date_str: str) -> str:
    if not date_str:
        return date_str

    dt = datetime.fromisoformat(date_str)
    return dt.strftime("%b %d, %Y • %H:%M")
