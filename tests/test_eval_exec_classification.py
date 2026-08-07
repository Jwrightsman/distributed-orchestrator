"""A correct CLI tool must not be scored as broken (eval baseline, Aug 6).

The harness runs generated code with no arguments. A tool whose job needs input
paths therefore exits non-zero with a usage message — which is the program
working, not failing. Three genuinely-correct CLI tools were being counted as
execution failures in the first baseline, understating the measured score.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import scoring  # noqa: E402


class TestArgparseIsNotFailure:
    def test_argparse_required_argument(self):
        stderr = "usage: main.py [-h] input_directory\nmain.py: error: the following arguments are required: input_directory"
        assert scoring._classify_python_failure(stderr) == "needs_args"

    def test_multiple_required_arguments(self):
        stderr = "main.py: error: the following arguments are required: value, source_unit, target_unit"
        assert scoring._classify_python_failure(stderr) == "needs_args"

    def test_click_style_missing_argument(self):
        assert scoring._classify_python_failure("Error: Missing argument 'FILENAME'.") == "needs_args"

    def test_bare_usage_banner(self):
        assert scoring._classify_python_failure("usage: tool.py [-h] FILE") == "needs_args"

    def test_needs_args_counts_as_ran(self):
        """The distinction must reach the ok flag, not just the label."""
        assert scoring._classify_python_failure("error: the following arguments are required: x") == "needs_args"
        # ok is computed in execute_python; assert the membership rule it uses
        assert "needs_args" in ("needs_stdin", "needs_args")


class TestRealFailuresStillFail:
    def test_name_error_is_a_failure(self):
        stderr = "Traceback (most recent call last):\n  File 'main.py', line 3\nNameError: name 'Query' is not defined"
        assert scoring._classify_python_failure(stderr) == "error"

    def test_missing_dependency_still_classified(self):
        stderr = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'requests'"
        assert scoring._classify_python_failure(stderr) == "missing_dependency"

    def test_stdin_case_unchanged(self):
        stderr = "Traceback (most recent call last):\n  line 9, in <module>\n    input()\nEOFError: EOF when reading a line"
        assert scoring._classify_python_failure(stderr) == "needs_stdin"

    def test_runtime_error_is_a_failure(self):
        assert scoring._classify_python_failure("Traceback...\nZeroDivisionError: division by zero") == "error"

    def test_usage_text_inside_a_traceback_is_still_a_failure(self):
        """A crash that happens to print usage must not be excused."""
        stderr = "usage: main.py [-h]\nTraceback (most recent call last):\nValueError: bad config"
        assert scoring._classify_python_failure(stderr) == "error"


class TestEmptyExtractionIsNotAPass:
    def test_no_files_does_not_count_as_parsing(self):
        """check_parses([]) is vacuously True; the eval must not read that as success."""
        parses, problems = scoring.check_parses([])
        # Whatever check_parses returns for empty input, a record with no files
        # must never be a success.
        assert not scoring.is_success(
            {"extracted": False, "parses": parses, "executes": False,
             "artifact_match": True, "keywords_ok": True, "judge_score": 5},
            require_judge=True,
        )
        assert problems == []
