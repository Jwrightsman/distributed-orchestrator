"""Tests for reasoning-markup sanitization.

Observed Aug 2 in the MCP end-to-end run: the reviewer's response contained
draft haiku lines followed by an orphan `</think>`, and all of it shipped as
the deliverable. Reasoning models emit these tags even with think=false.
"""

from ollama_client import strip_thinking


def test_removes_complete_block():
    text = "<think>weighing options here</think>The real answer."
    assert strip_thinking(text) == "The real answer."


def test_removes_multiline_block():
    text = "<think>\nline one\nline two\n</think>\n# Heading\nBody text."
    out = strip_thinking(text)
    assert "line one" not in out
    assert out.startswith("# Heading")


def test_orphan_close_drops_preceding_reasoning():
    """The exact shape from the failed run: reasoning, then a stray close tag."""
    text = "many hands work\nno single boss\n</think>\n## Quality Rating\nFAIL"
    out = strip_thinking(text)
    assert "many hands work" not in out
    assert out.startswith("## Quality Rating")


def test_orphan_open_tag_removed_but_content_kept():
    text = "<think>\nThe deliverable content that matters."
    out = strip_thinking(text)
    assert "<think" not in out
    assert "deliverable content" in out


def test_clean_text_untouched():
    text = "# Title\n\nA normal deliverable with no reasoning markup."
    assert strip_thinking(text) == text


def test_code_mentioning_think_is_preserved():
    """Don't damage legitimate content that merely uses the word."""
    text = "def think(x):\n    return x  # I think this is right"
    assert strip_thinking(text) == text


def test_multiple_blocks_all_removed():
    text = "<think>a</think>First.<think>b</think>Second."
    out = strip_thinking(text)
    assert "a" not in out.replace("First.", "").replace("Second.", "")
    assert "First." in out and "Second." in out


def test_case_insensitive():
    assert strip_thinking("<THINK>noise</THINK>Real output.") == "Real output."


def test_empty_after_stripping():
    assert strip_thinking("<think>only reasoning</think>") == ""
