"""Unit tests for the colour heuristics in latex_helper.utils.

The postprocess pipeline uses _infer_rgb to guess an RGB triple for
unrecognised TikZ colour names, and _collect_used_colors to scan the
LaTeX source for every colour reference (so that any that wasn't
\\definecolor'd can be auto-defined).

These are pure, deterministic helpers — ideal for tight unit tests.
"""
import unittest

from latex_helper.utils import _collect_used_colors, _infer_rgb


class InferRgbByKeyword(unittest.TestCase):
    def test_blue_keyword(self):
        self.assertEqual(_infer_rgb("headerblue"), "31,78,121")
        self.assertEqual(_infer_rgb("MyDarkBlue"), "31,78,121")  # case-insensitive

    def test_orange_keyword(self):
        self.assertEqual(_infer_rgb("brandorange"), "230,120,20")

    def test_yellow_and_gold_keywords(self):
        self.assertEqual(_infer_rgb("highlightyellow"), "220,185,0")
        self.assertEqual(_infer_rgb("brandgold"), "220,185,0")

    def test_red_keyword(self):
        self.assertEqual(_infer_rgb("errorred"), "192,50,50")

    def test_green_keyword(self):
        self.assertEqual(_infer_rgb("okgreen"), "50,150,80")

    def test_cyan_keyword(self):
        self.assertEqual(_infer_rgb("accentcyan"), "0,175,200")

    def test_gray_and_grey_keywords(self):
        self.assertEqual(_infer_rgb("textgray"), "128,128,128")
        self.assertEqual(_infer_rgb("textgrey"), "128,128,128")

    def test_brown_keyword(self):
        self.assertEqual(_infer_rgb("darkbrown"), "150,90,40")

    def test_light_or_bg_keyword(self):
        self.assertEqual(_infer_rgb("lightshade"), "240,240,240")
        self.assertEqual(_infer_rgb("pagebg"), "240,240,240")

    def test_dark_or_banner_keyword(self):
        self.assertEqual(_infer_rgb("darknavy"), "50,50,50")
        self.assertEqual(_infer_rgb("topbanner"), "50,50,50")

    def test_unknown_name_falls_back_to_neutral_gray(self):
        self.assertEqual(_infer_rgb("quirkyhue"), "80,80,80")
        self.assertEqual(_infer_rgb("x"), "80,80,80")


class CollectUsedColors(unittest.TestCase):
    def test_color_command(self):
        used = _collect_used_colors(r"\color{headerblue}")
        self.assertIn("headerblue", used)

    def test_textcolor_command(self):
        used = _collect_used_colors(r"\textcolor{highlightyellow}{x}")
        self.assertIn("highlightyellow", used)

    def test_colorbox_command(self):
        used = _collect_used_colors(r"\colorbox{lightgray}{x}")
        self.assertIn("lightgray", used)

    def test_tikz_draw_option(self):
        used = _collect_used_colors(r"\draw[draw=brandred, thick] (0,0)--(1,1);")
        self.assertIn("brandred", used)
        # `thick` is a TikZ key, not a color, so it should NOT appear
        self.assertNotIn("thick", used)

    def test_tikz_fill_option(self):
        used = _collect_used_colors(r"\draw[fill=brandgreen, draw=black] (0,0) circle;")
        self.assertIn("brandgreen", used)
        # `black` is a std color; it's still in the set, just doesn't need
        # \\definecolor. We assert it appears here for completeness.
        self.assertIn("black", used)

    def test_bare_color_name_in_tikz_option(self):
        # \draw[brandblue, thin] — `brandblue` is a bare key (no draw=/fill=)
        used = _collect_used_colors(r"\draw[brandblue, thin] (0,0)--(1,1);")
        self.assertIn("brandblue", used)
        self.assertNotIn("thin", used)

    def test_color_modifier_with_bang(self):
        # brandblue!80 syntax should also surface the colour name
        used = _collect_used_colors(r"\fill[brandblue!80] (0,0) circle;")
        self.assertIn("brandblue", used)

    def test_collects_multiple_colors(self):
        src = (
            r"\draw[draw=alpha, fill=beta] (0,0) -- (1,1);" "\n"
            r"\color{gamma}" "\n"
        )
        used = _collect_used_colors(src)
        self.assertIn("alpha", used)
        self.assertIn("beta", used)
        self.assertIn("gamma", used)

    def test_tikz_keys_excluded(self):
        # Various TikZ keys must not be collected as colour names
        src = r"\draw[very thick, dashed, rounded corners, ->, >=stealth] (0,0)--(1,1);"
        used = _collect_used_colors(src)
        for k in ("very", "thick", "dashed", "rounded", "corners", "stealth"):
            self.assertNotIn(k, used, msg=f"TikZ key {k!r} was wrongly collected as a colour")

    def test_returns_a_set(self):
        used = _collect_used_colors(r"\color{foo}")
        self.assertIsInstance(used, set)


if __name__ == "__main__":
    unittest.main()
