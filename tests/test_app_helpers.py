"""Unit tests for the small pure-function helpers in web/app.py.

- _is_valid_pdf
- _needs_xelatex
- _parse_latex_log
- _DANGEROUS_LATEX detection (factored as a tiny helper, since the
  module-level check is inlined in the request handler).
"""
import os
import tempfile
import unittest

from web.app import (
    _DANGEROUS_LATEX,
    _is_valid_pdf,
    _needs_xelatex,
    _parse_latex_log,
)


def _contains_dangerous(src: str) -> bool:
    """Mirror of the check in compile_latex: returns True if any dangerous
    LaTeX command appears in the source."""
    return any(cmd in src for cmd in _DANGEROUS_LATEX)


class IsValidPdf(unittest.TestCase):
    def test_nonexistent_path_returns_false(self):
        self.assertFalse(_is_valid_pdf("/nonexistent/path/to/file.pdf"))

    def test_file_too_small_returns_false(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"%PDF-1.4")  # magic, but well under 256 bytes
            path = f.name
        try:
            self.assertFalse(_is_valid_pdf(path))
        finally:
            os.unlink(path)

    def test_wrong_magic_returns_false(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(b"NOTAPDF" + b"x" * 1000 + b"%%EOF")
            path = f.name
        try:
            self.assertFalse(_is_valid_pdf(path))
        finally:
            os.unlink(path)

    def test_truncated_pdf_without_eof_returns_false(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(b"%PDF-1.4\n" + b"a" * 2000)
            path = f.name
        try:
            self.assertFalse(_is_valid_pdf(path))
        finally:
            os.unlink(path)

    def test_valid_pdf_returns_true(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(b"%PDF-1.4\n" + b"a" * 1000 + b"\n%%EOF\n")
            path = f.name
        try:
            self.assertTrue(_is_valid_pdf(path))
        finally:
            os.unlink(path)


class NeedsXelatex(unittest.TestCase):
    def test_ctex_keyword_triggers(self):
        self.assertTrue(_needs_xelatex(r"\documentclass{ctexart}"))

    def test_xecjk_keyword_triggers(self):
        self.assertTrue(_needs_xelatex(r"\usepackage{xeCJK}"))

    def test_cjk_character_triggers(self):
        self.assertTrue(_needs_xelatex(r"\textbf{你好世界}"))

    def test_korean_character_triggers(self):
        self.assertTrue(_needs_xelatex(r"\textbf{안녕하세요}"))

    def test_japanese_character_triggers(self):
        self.assertTrue(_needs_xelatex(r"\textbf{こんにちは}"))

    def test_pure_english_returns_false(self):
        self.assertFalse(_needs_xelatex(r"\documentclass{article}\n\begin{document}hi\end{document}"))

    def test_ctex_substring_match_is_literal(self):
        # The check is `"ctex" in src`. "context" contains the letters c,t,e,x
        # in order only if "ctex" appears as a contiguous substring — and the
        # English word "context" has "tex" but not "ctex", so it is NOT a
        # false positive. A contrived identifier that does embed "ctex"
        # (e.g. a custom command name) does trip the check.
        self.assertFalse(_needs_xelatex(r"the \emph{context} is rich"))
        self.assertTrue(_needs_xelatex(r"\newcommand{\myctexfile}{x}"))


class ParseLatexLog(unittest.TestCase):
    def test_empty_log(self):
        self.assertEqual(_parse_latex_log(""), {"error": None, "line": None})

    def test_log_without_error(self):
        log = "This is pdflatex log\nSome warnings maybe\n"
        self.assertEqual(_parse_latex_log(log), {"error": None, "line": None})

    def test_error_with_line_number(self):
        log = (
            "This is pdflatex log\n"
            "Some warning\n"
            "! Undefined control sequence.\n"
            "l.42 \foo\n"
            "         bar\n"
        )
        result = _parse_latex_log(log)
        self.assertEqual(result["error"], "Undefined control sequence.")
        self.assertEqual(result["line"], 42)

    def test_error_without_line_within_15_lines(self):
        log = (
            "! Something broke.\n"
            "lots of text without an l.NN marker\n"
        )
        result = _parse_latex_log(log)
        self.assertEqual(result["error"], "Something broke.")
        self.assertIsNone(result["line"])

    def test_first_error_wins(self):
        log = (
            "! First error.\n"
            "l.1 \foo\n"
            "\n"
            "! Second error.\n"
            "l.99 \bar\n"
        )
        result = _parse_latex_log(log)
        self.assertEqual(result["error"], "First error.")
        self.assertEqual(result["line"], 1)


class DangerousLatexDetection(unittest.TestCase):
    def test_write18_caught(self):
        self.assertTrue(_contains_dangerous(r"\write18{rm -rf /}"))

    def test_immediate_write_caught(self):
        self.assertTrue(_contains_dangerous(r"\immediate\write\@auxout{evil}"))

    def test_normal_latex_clean(self):
        self.assertFalse(_contains_dangerous(
            r"\documentclass{article}\begin{document}hi\end{document}"
        ))

    def test_wri18_substring_does_not_trigger(self):
        # The substring check is exact-token; "wri18" should not match "\\write18"
        self.assertFalse(_contains_dangerous(r"\newcommand{\wri18}{x}"))


if __name__ == "__main__":
    unittest.main()
