"""Unit tests for latex_helper.utils.postprocess_latex.

Covers the 6 correction branches + a few combined real-world scenarios.
Tests are written as ``unittest.TestCase`` so they run on stdlib unittest
without external dependencies; pytest also picks them up unchanged.
"""
import unittest

from latex_helper.utils import postprocess_latex


class TikzsetOrdering(unittest.TestCase):
    def test_tikzset_before_usepackage_moved_after(self):
        src = (
            r"\tikzset{mystyle/.style={very thick, draw=blue}}" "\n"
            r"\usepackage{tikz}" "\n"
            r"\begin{document}body\end{document}"
        )
        out = postprocess_latex(src)
        # tikzset must now live AFTER \\usepackage{tikz}
        pkg_idx = out.index(r"\usepackage{tikz}")
        ts_idx = out.index(r"\tikzset")
        self.assertGreater(ts_idx, pkg_idx)
        # and not appear twice
        self.assertEqual(out.count(r"\tikzset"), 1)

    def test_tikzset_already_after_usepackage_unchanged(self):
        src = (
            r"\usepackage{tikz}" "\n"
            r"\tikzset{mystyle/.style={very thick, draw=blue}}" "\n"
            r"\begin{document}body\end{document}"
        )
        out = postprocess_latex(src)
        self.assertEqual(out.count(r"\tikzset"), 1)
        pkg_idx = out.index(r"\usepackage{tikz}")
        ts_idx = out.index(r"\tikzset")
        self.assertGreater(ts_idx, pkg_idx)


class CJKEnvironmentRemoval(unittest.TestCase):
    def test_cjk_removed_in_ctexart(self):
        src = (
            r"\documentclass{ctexart}" "\n"
            r"\begin{document}" "\n"
            r"\begin{CJK*}{UTF8}{gbsn}你好\end{CJK*}" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertNotIn(r"\begin{CJK*}", out)
        self.assertNotIn(r"\end{CJK*}", out)
        self.assertIn("你好", out)

    def test_cjk_removed_in_ctexbook_and_ctexrep(self):
        for cls in ("ctexbook", "ctexrep"):
            src = (
                r"\documentclass{" + cls + "}" "\n"
                r"\begin{document}" "\n"
                r"\begin{CJK*}{UTF8}{gbsn}x\end{CJK*}" "\n"
                r"\end{document}"
            )
            out = postprocess_latex(src)
            self.assertNotIn(r"\begin{CJK*}", out, msg=f"failed for {cls}")

    def test_cjk_preserved_outside_ctex(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\begin{document}" "\n"
            r"\begin{CJK*}{UTF8}{gbsn}你好\end{CJK*}" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertIn(r"\begin{CJK*}", out)
        self.assertIn(r"\end{CJK*}", out)


class IncludegraphicsSanitisation(unittest.TestCase):
    def test_unknown_local_filename_is_commented(self):
        src = r"\includegraphics[width=0.5\linewidth]{placeholder}"
        out = postprocess_latex(src)
        self.assertTrue(out.lstrip().startswith("%"), out)
        self.assertIn(r"\includegraphics", out)

    def test_screenshot_filename_with_extension_preserved(self):
        src = r"\includegraphics[width=0.8\linewidth]{figure1.png}"
        out = postprocess_latex(src)
        self.assertNotIn("% \\includegraphics", out)
        self.assertIn("figure1.png", out)

    def test_absolute_path_preserved(self):
        src = r"\includegraphics{/abs/path/foo.pdf}"
        out = postprocess_latex(src)
        self.assertNotIn("% \\includegraphics", out)
        self.assertIn("/abs/path/foo.pdf", out)

    def test_backslash_path_preserved(self):
        # \path\foo.pdf — a Windows-style path that starts with backslash
        src = r"\includegraphics{\path\foo.pdf}"
        out = postprocess_latex(src)
        self.assertNotIn("% \\includegraphics", out)

    def test_other_image_extensions_preserved(self):
        for ext in ("jpg", "jpeg", "pdf", "eps", "svg", "gif", "webp"):
            src = r"\includegraphics{img." + ext + "}"
            out = postprocess_latex(src)
            with self.subTest(ext=ext):
                self.assertNotIn(
                    "% \\includegraphics",
                    out,
                    msg=f"image with .{ext} was incorrectly commented",
                )


class ColorAutoDefinition(unittest.TestCase):
    def _doc_with(self, body_latex: str) -> str:
        return (
            r"\documentclass{article}" "\n"
            r"\usepackage{xcolor}" "\n"
            r"\usepackage{tikz}" "\n"
            r"\begin{document}" "\n"
            + body_latex + "\n"
            r"\end{document}"
        )

    def test_undefined_color_gets_definecolor(self):
        out = postprocess_latex(self._doc_with(r"\color{headerblue}"))
        self.assertIn(r"\definecolor{headerblue}", out)

    def test_already_defined_color_not_duplicated(self):
        src = self._doc_with(
            r"\definecolor{headerblue}{RGB}{31,78,121}" "\n" r"\color{headerblue}"
        )
        out = postprocess_latex(src)
        self.assertEqual(out.count(r"\definecolor{headerblue}"), 1)

    def test_tikz_option_color_gets_definecolor(self):
        out = postprocess_latex(
            self._doc_with(r"\draw[draw=brandred, thick] (0,0) -- (1,1);")
        )
        self.assertIn(r"\definecolor{brandred}", out)

    def test_tikz_known_keys_not_treated_as_colors(self):
        out = postprocess_latex(
            self._doc_with(r"\draw[very thick, dashed, thin] (0,0) -- (1,1);")
        )
        for name in ("very", "thick", "dashed", "thin"):
            with self.subTest(name=name):
                self.assertNotIn(r"\definecolor{" + name + r"}", out)


class PreambleMergeAndDedup(unittest.TestCase):
    def test_usepackage_in_body_moved_to_preamble(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\usepackage{amsmath}" "\n"
            r"\begin{document}" "\n"
            r"Some text." "\n"
            r"\usepackage{geometry}" "\n"
            r"More text." "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertEqual(out.count(r"\usepackage{amsmath}"), 1)
        self.assertEqual(out.count(r"\usepackage{geometry}"), 1)
        doc_idx = out.index(r"\begin{document}")
        geo_idx = out.index(r"\usepackage{geometry}")
        self.assertLess(geo_idx, doc_idx)

    def test_duplicate_usepackage_in_body_not_moved(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\usepackage{amsmath}" "\n"
            r"\begin{document}" "\n"
            r"\usepackage{amsmath}" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertEqual(out.count(r"\usepackage{amsmath}"), 1)

    def test_duplicate_definecolor_deduped(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\usepackage{xcolor}" "\n"
            r"\definecolor{myred}{RGB}{200,50,50}" "\n"
            r"\begin{document}" "\n"
            r"\definecolor{myred}{RGB}{200,50,50}" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertEqual(out.count(r"\definecolor{myred}"), 1)


class ThinkBlockAndFenceStripping(unittest.TestCase):
    def test_full_think_pair_stripped(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\begin{document}" "\n"
            "<think>let me think about the equation</think>" "\n"
            r"$E = mc^2$" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertNotIn("<think>", out)
        self.assertNotIn("</think>", out)
        self.assertIn("$E = mc^2$", out)

    def test_orphan_think_open_line_stripped(self):
        src = (
            r"\documentclass{article}" "\n"
            r"\begin{document}" "\n"
            "<think>" "\n"
            r"$E = mc^2$" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)
        self.assertNotIn("<think>", out)

    def test_markdown_fence_stripped(self):
        src = (
            "```latex" "\n"
            r"\documentclass{article}" "\n"
            r"\begin{document}body\end{document}" "\n"
            "```" "\n"
        )
        out = postprocess_latex(src)
        self.assertNotIn("```", out)
        self.assertIn(r"\documentclass{article}", out)


class CombinedRealWorld(unittest.TestCase):
    def test_multi_issue_document(self):
        src = (
            r"\tikzset{gridstyle/.style={dashed, draw=brandblue}}" "\n"
            r"\documentclass{ctexart}" "\n"
            r"\usepackage{xcolor}" "\n"
            r"\usepackage{tikz}" "\n"
            "<think>Plan: a 2x2 grid</think>" "\n"
            r"\begin{document}" "\n"
            r"\begin{CJK*}{UTF8}{gbsn}段落\end{CJK*}" "\n"
            r"\includegraphics{placeholder_figure}" "\n"
            r"\includegraphics[width=0.8\linewidth]{figure1.png}" "\n"
            r"\draw[draw=brandblue] (0,0) -- (1,1);" "\n"
            r"\end{document}"
        )
        out = postprocess_latex(src)

        # 1) tikzset moved after \usepackage{tikz}
        pkg_idx = out.index(r"\usepackage{tikz}")
        ts_idx = out.index(r"\tikzset{gridstyle")
        self.assertGreater(ts_idx, pkg_idx)

        # 2) think block fully gone
        self.assertNotIn("<think>", out)
        body = out.split(r"\begin{document}")[1].split(r"\end{document}")[0]
        self.assertNotIn("Plan:", body)

        # 3) CJK env gone, body text preserved
        self.assertNotIn(r"\begin{CJK*}", body)
        self.assertIn("段落", body)

        # 4) placeholder figure commented, screenshot figure preserved
        placeholder_line = [ln for ln in body.splitlines() if "placeholder_figure" in ln][0]
        self.assertTrue(placeholder_line.lstrip().startswith("%"))
        self.assertIn("figure1.png", body)
        screenshot_line = [ln for ln in body.splitlines() if "figure1.png" in ln][0]
        self.assertFalse(screenshot_line.lstrip().startswith("%"))

        # 5) brandblue auto-defined
        self.assertIn(r"\definecolor{brandblue}", out)


if __name__ == "__main__":
    unittest.main()
