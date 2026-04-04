"""
Code extractor — pulls runnable code out of pipeline output.

After the reviewer assembles the final output, this module extracts
code blocks and saves them as actual files you can run.

Usage:
    from extract import extract_code_files
    files = extract_code_files(review_text, output_dir)
"""

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
