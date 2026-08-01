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
