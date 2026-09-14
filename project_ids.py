"""One portable project identifier contract for every caller and storage path."""

import re


def validate_project_id(value: str) -> str:
    """Preserve safe IDs verbatim; reject path syntax on both Windows and POSIX."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not re.fullmatch(r"[\w-][\w.-]*", value)
        or value.endswith(".")
        or value.split(".", 1)[0].upper()
        in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", value.split(".", 1)[0], re.IGNORECASE)
    ):
        raise ValueError("project_id must be a portable 1-128 character identifier, not a path")
    return value
