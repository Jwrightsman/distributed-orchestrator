"""Tests for mechanical validation of extracted code (sprint output quality).

The reviewer rates prose quality and will happily PASS Python that doesn't
parse — a real failure observed in the Aug 1 demo run. These checks are the
backstop, and they must never produce false alarms on good code.
"""

from pathlib import Path

from extract import check_code_files

GOOD_PY = '''
import json


def main(items):
    """Return the items as JSON."""
    return json.dumps([{"id": i, "value": v} for i, v in enumerate(items)])


if __name__ == "__main__":
    print(main(["a", "b"]))
'''

# The exact defect qwen3.5 produced: a stray comma inside a comprehension
BAD_PY = '''
def handler(expenses):
    return {"expenses": [
        {"id": e.id, "amount": e.amount},
        for e in expenses
    ]}
'''

GOOD_HTML = """<!DOCTYPE html>
<html><head><style>body { background: #000; }</style></head>
<body><canvas id="c"></canvas><script>const c = document.getElementById('c');</script></body>
</html>"""


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_valid_python_has_no_problems(tmp_path):
    assert check_code_files([_write(tmp_path, "main.py", GOOD_PY)]) == []


def test_syntax_error_detected_with_line_and_source(tmp_path):
    problems = check_code_files([_write(tmp_path, "main.py", BAD_PY)])
    assert len(problems) == 1
    assert "main.py is not valid Python" in problems[0]
    # The message must be actionable for the reviser: line number + the line
    assert "line 4" in problems[0]
    assert "for e in expenses" in problems[0] or "amount" in problems[0]


def test_valid_html_has_no_problems(tmp_path):
    assert check_code_files([_write(tmp_path, "index.html", GOOD_HTML)]) == []


def test_html_fragment_detected(tmp_path):
    fragment = "<div><canvas id='c'></canvas></div>"
    problems = check_code_files([_write(tmp_path, "index.html", fragment)])
    assert any("DOCTYPE" in p for p in problems)
    assert any("</html>" in p for p in problems)


def test_html_truncated_script_detected(tmp_path):
    truncated = GOOD_HTML.replace("</script>", "")
    problems = check_code_files([_write(tmp_path, "index.html", truncated)])
    assert any("unbalanced <script>" in p for p in problems)


def test_unknown_extensions_ignored(tmp_path):
    assert check_code_files([_write(tmp_path, "notes.txt", "not code at all {{{")]) == []


def test_missing_file_ignored(tmp_path):
    assert check_code_files([str(tmp_path / "gone.py")]) == []


def test_multiple_files_all_reported(tmp_path):
    paths = [
        _write(tmp_path, "a.py", BAD_PY),
        _write(tmp_path, "b.py", GOOD_PY),
        _write(tmp_path, "c.html", "<div>fragment</div>"),
    ]
    problems = check_code_files(paths)
    assert any("a.py" in p for p in problems)
    assert not any("b.py" in p for p in problems)
    assert any("c.html" in p for p in problems)


def test_real_world_regression_from_demo_run():
    """The actual file the Aug 1 demo produced, if it's still on disk."""
    real = Path("output/20260801_230041/code/main.py")
    if not real.exists():
        return  # environment-dependent; the synthetic case above covers the logic
    assert check_code_files([str(real)]), "known-broken demo output should be flagged"
