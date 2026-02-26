""".deploy-tag history — newline-delimited, newest last, max 10."""

TAG_FILE = ".deploy-tag"
MAX_HISTORY = 10


def _tag_path() -> str:
    return TAG_FILE


def read_tags() -> list[str]:
    """Read tag history. Returns list with oldest first, newest last."""
    path = _tag_path()
    try:
        with open(path) as f:
            tags = [line.strip() for line in f if line.strip()]
        return tags
    except FileNotFoundError:
        return []


def current_tag() -> str | None:
    """Return the most recently deployed tag, or None."""
    tags = read_tags()
    return tags[-1] if tags else None


def previous_tag() -> str | None:
    """Return the tag before the current one, or None."""
    tags = read_tags()
    return tags[-2] if len(tags) >= 2 else None


def write_tag(tag: str) -> None:
    """Append a tag to history, trimming to MAX_HISTORY."""
    tags = read_tags()
    tags.append(tag)
    tags = tags[-MAX_HISTORY:]
    path = _tag_path()
    with open(path, "w") as f:
        for t in tags:
            f.write(t + "\n")
