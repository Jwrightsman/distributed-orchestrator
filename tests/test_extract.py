"""Tests for the code extractor."""

from pathlib import Path

from extract import extract_code_blocks, extract_code_files

PY_CODE = "def main():\n    print('hello world from the test suite')\n"
HTML_CODE = "<html><body><h1>A page with enough characters</h1></body></html>"


def test_extract_blocks_basic():
    text = f"Intro\n```python\n{PY_CODE}```\nOutro"
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["lang"] == "python"
    assert "hello world" in blocks[0]["code"]


def test_extract_blocks_skips_tiny_snippets():
    assert extract_code_blocks("```python\nx = 1\n```") == []


def test_extract_blocks_multiple_languages():
    text = f"```python\n{PY_CODE}```\n\n```html\n{HTML_CODE}\n```"
    blocks = extract_code_blocks(text)
    assert [b["lang"] for b in blocks] == ["python", "html"]


def test_extract_files_naming(tmp_path):
    text = (
        f"```python\n{PY_CODE}```\n"
        f"```python\n{PY_CODE}# second file\n```\n"
        f"```html\n{HTML_CODE}\n```\n"
    )
    saved = extract_code_files(text, tmp_path)
    names = [Path(p).name for p in saved]
    # First .py becomes main.py, second gets a numbered name, first .html is index.html
    assert names[0] == "main.py"
    assert names[1] == "output_2.py"
    assert names[2] == "index.html"
    for p in saved:
        assert Path(p).exists()


def test_extract_files_unknown_lang_gets_txt(tmp_path):
    text = "```brainfuck\n++++++++[>++++[>++>+++>+++>+<<<<-]>+>+>->>+[<]<-]>>.\n```"
    saved = extract_code_files(text, tmp_path)
    assert len(saved) == 1
    assert saved[0].endswith(".txt")


def test_extract_files_no_blocks_returns_empty(tmp_path):
    assert extract_code_files("no code here at all", tmp_path) == []
    assert not (tmp_path / "code").exists()


# ── Unfenced Python deliverables ─────────────────────────────────────
#
# Found by scripts/live_smoke.py on Aug 13: a real pitch to the live
# orchestrator came back as good Python opening with `from collections import
# namedtuple` — no fence, no shebang — and the extractor produced ZERO files.
# The deliverable was fine; nothing runnable reached the user. The raw-document
# fallback covered HTML, XML and shebang scripts but not a plain module.

def test_unfenced_python_module_is_extracted():
    text = (
        "from collections import namedtuple\n\n"
        "def celsius_to_fahrenheit(c: float) -> float:\n"
        "    return round((c * 9 / 5) + 32, 2)\n\n"
        "assert celsius_to_fahrenheit(0) == 32.0\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["lang"] == "python"


def test_unfenced_python_starting_with_def_is_extracted():
    text = "def add(a, b):\n    return a + b\n\nassert add(2, 3) == 5\n"
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1 and blocks[0]["lang"] == "python"


def test_prose_is_never_treated_as_python():
    """The guard that keeps a refusal from becoming a .py file."""
    for prose in (
        "I could not complete this task. Please provide more detail about it.",
        "Sorry, the request was ambiguous so nothing was produced this time.",
        "Here is a summary of what a converter would need to do, in words only.",
    ):
        assert extract_code_blocks(prose) == []


def test_bare_valid_expression_is_not_a_python_file():
    """`Hello` parses as Python. Parsing alone is not enough evidence."""
    assert extract_code_blocks("Hello" + " world" * 20) == []


def test_syntactically_broken_python_is_not_extracted():
    text = "import os\n\ndef broken(:\n    return 1\n"
    assert extract_code_blocks(text) == []


def test_fenced_blocks_still_win_over_the_raw_fallback():
    text = (
        "Some explanation first.\n\n"
        "```python\n"
        "import os\n\n\ndef listing():\n    return os.listdir('.')\n"
        "```\n"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert "listing" in blocks[0]["code"]
    assert "explanation" not in blocks[0]["code"]


def test_unfenced_html_still_extracted():
    html = "<!doctype html>\n<html><body><h1>hi</h1></body></html>"
    blocks = extract_code_blocks(html)
    assert len(blocks) == 1 and blocks[0]["lang"] == "html"
