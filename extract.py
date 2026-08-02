"""
Code extractor — pulls runnable code out of pipeline output.

After the reviewer assembles the final output, this module extracts
code blocks and saves them as actual files you can run.

Usage:
    from extract import extract_code_files
    files = extract_code_files(review_text, output_dir)
"""

import ast
import re
from pathlib import Path


# Map language hints to file extensions
LANG_MAP = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "bash": ".sh",
    "sh": ".sh",
    "sql": ".sql",
    "yaml": ".yaml",
    "yml": ".yaml",
    "toml": ".toml",
    "rust": ".rs",
    "go": ".go",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "ruby": ".rb",
    "php": ".php",
}


def extract_code_blocks(text: str) -> list[dict]:
    """Extract fenced code blocks from markdown text.

    Returns list of {"lang": str, "code": str}
    """
    pattern = r"```(\w*)\s*\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)

    blocks = []
    for lang, code in matches:
        lang = lang.strip().lower()
        code = code.strip()
        if code and len(code) > 20:  # skip tiny snippets
            blocks.append({"lang": lang, "code": code})

    return blocks


# Structural markers a self-contained HTML deliverable must have. Small models
# often emit a fragment or drop the script — cheap to detect, expensive to miss.
_HTML_REQUIRED = (
    ("<!doctype html", "missing <!DOCTYPE html> — the file is a fragment, not a document"),
    ("</html>", "missing closing </html> tag — the document is truncated"),
)


def check_code_files(paths: list[str]) -> list[str]:
    """Validate extracted files that we can check cheaply and definitively.

    Python: real parse via ast. HTML: required structural markers, plus a
    balanced check on <script>/<style> which truncation tends to break.

    Returns human-readable problem descriptions — empty list means everything
    we can check looks sound. Never raises; unreadable files are skipped.
    """
    problems: list[str] = []
    for p in paths:
        path = Path(p)
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if path.suffix == ".py":
            try:
                ast.parse(source)
            except SyntaxError as e:
                line = e.text.strip() if e.text else ""
                problems.append(
                    f"{path.name} is not valid Python: {e.msg} (line {e.lineno})"
                    + (f" — the offending line is: {line}" if line else "")
                )
        elif path.suffix == ".html":
            lowered = source.lower()
            for marker, description in _HTML_REQUIRED:
                if marker not in lowered:
                    problems.append(f"{path.name}: {description}")
            for tag in ("script", "style"):
                if lowered.count(f"<{tag}") != lowered.count(f"</{tag}>"):
                    problems.append(
                        f"{path.name}: unbalanced <{tag}> tags — the file is likely cut off"
                    )
    return problems


def extract_code_files(review_text: str, output_dir: Path) -> list[str]:
    """Extract code blocks from review output and save as runnable files.

    Returns list of saved file paths.
    """
    blocks = extract_code_blocks(review_text)
    if not blocks:
        return []

    code_dir = output_dir / "code"
    code_dir.mkdir(exist_ok=True)

    saved = []
    counters: dict[str, int] = {}

    for block in blocks:
        lang = block["lang"]
        ext = LANG_MAP.get(lang, ".txt")

        # Generate filename
        counters[ext] = counters.get(ext, 0) + 1
        if counters[ext] == 1 and ext == ".py":
            filename = "main.py"
        elif counters[ext] == 1 and ext == ".html":
            filename = "index.html"
        elif counters[ext] == 1 and ext == ".js":
            filename = "main.js"
        else:
            filename = f"output_{counters[ext]}{ext}"

        filepath = code_dir / filename
        filepath.write_text(block["code"])
        saved.append(str(filepath))

    return saved
