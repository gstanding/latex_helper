"""Unit tests for the SimpleTex page-assembler helper.

SimpletexConverter.stream_latex renders every page via SimpleTex OCR and
then stitches the per-page LaTeX strings into a single document. The
assembler's job is to:
  - return the page verbatim if there is only one page
  - for multi-page results that already include \\documentclass, strip
    the trailing \\end{document} from page 1 and splice the body of
    later pages with \\newpage separators
  - for multi-page results without a preamble, wrap everything in a
    default article preamble with amsmath/amssymb/amsfonts
"""
import unittest

from latex_helper.converter import _assemble_simpletex_document


class EmptyAndSinglePage(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(_assemble_simpletex_document([]), "")

    def test_single_page_with_documentclass_returned_verbatim(self):
        page = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "$E = mc^2$\n"
            "\\end{document}\n"
        )
        self.assertEqual(_assemble_simpletex_document([page]), page)

    def test_single_page_without_documentclass_wraps_in_default_preamble(self):
        page = "x^2 + y^2 = z^2"
        out = _assemble_simpletex_document([page])
        # Default article preamble applied
        self.assertIn("\\documentclass{article}", out)
        self.assertIn("\\usepackage{amsmath}", out)
        self.assertIn("\\begin{document}", out)
        self.assertIn("\\end{document}", out)
        # Body content gets wrapped in \\[...\\] (the auto-displaymath wrap)
        self.assertIn("\\[", out)
        self.assertIn(out[out.index("\\[") + 2:out.index("\\]")].strip(), page)


class MultiPageWithDocumentclass(unittest.TestCase):
    def test_multi_page_with_preamble_strips_end_document_and_splices(self):
        page1 = (
            "\\documentclass{article}\n"
            "\\usepackage{amsmath}\n"
            "\\begin{document}\n"
            "Page 1 content\n"
            "\\end{document}\n"
        )
        page2 = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Page 2 content\n"
            "\\end{document}\n"
        )
        out = _assemble_simpletex_document([page1, page2])
        # Only one \\documentclass preamble
        self.assertEqual(out.count("\\documentclass"), 1)
        # Only one trailing \\end{document}
        self.assertEqual(out.rstrip().count("\\end{document}"), 1)
        # Both page contents present
        self.assertIn("Page 1 content", out)
        self.assertIn("Page 2 content", out)
        # \\newpage separator between pages
        self.assertIn("\\newpage", out)


class MultiPageWithoutPreamble(unittest.TestCase):
    def test_multi_page_no_preamble_uses_default_article(self):
        pages = ["x^2", "y^2"]
        out = _assemble_simpletex_document(pages)
        self.assertIn("\\documentclass{article}", out)
        self.assertIn("\\usepackage{amsmath}", out)
        self.assertIn("\\usepackage{amssymb}", out)
        self.assertIn("\\usepackage{amsfonts}", out)
        self.assertIn("x^2", out)
        self.assertIn("y^2", out)
        # Both pages separated by \\newpage
        self.assertIn("\\newpage", out)

    def test_empty_page_strings_are_skipped(self):
        pages = ["x^2", "", "   \n\n  ", "y^2"]
        out = _assemble_simpletex_document(pages)
        # Empty/whitespace pages dropped, not turned into empty \\[\\]
        self.assertNotIn("\\[\\]", out)
        # Real pages preserved
        self.assertIn("x^2", out)
        self.assertIn("y^2", out)


class AutoWrappingMathEnv(unittest.TestCase):
    def test_bare_formula_gets_display_math_wrap(self):
        # Page that doesn't start with a known math env gets \\[...\\]
        out = _assemble_simpletex_document(["a^2 + b^2 = c^2"])
        self.assertIn("\\[", out)
        self.assertIn("a^2 + b^2 = c^2", out)
        self.assertIn("\\]", out)

    def test_page_starting_with_equation_env_not_double_wrapped(self):
        out = _assemble_simpletex_document(["\\begin{equation}\nx = 1\n\\end{equation}"])
        # The equation is kept intact, no surrounding \\[\\]
        # Find the equation block
        self.assertIn("\\begin{equation}", out)
        self.assertIn("\\end{equation}", out)
        # And the outer \\documentclass preamble is still applied
        self.assertIn("\\documentclass{article}", out)

    def test_page_starting_with_inline_dollar_math_not_double_wrapped(self):
        out = _assemble_simpletex_document(["$$ x = 1 $$"])
        # The $$...$$ is kept, not wrapped in \\[...\\]
        self.assertIn("$$ x = 1 $$", out)
        # Only one set of delimiters around the body
        self.assertEqual(out.count("$$"), 2)


if __name__ == "__main__":
    unittest.main()
