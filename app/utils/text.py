import html


def escape_html(value: str | None, fallback: str = "Unknown") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(value)


def compact(value: str | None, max_len: int = 80) -> str:
    if not value:
        return "-"
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."
