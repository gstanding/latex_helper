"""Unit tests for latex_helper.utils.detect_file_type."""
import unittest

from latex_helper.utils import detect_file_type


class DetectByExtension(unittest.TestCase):
    def test_pdf_extension(self):
        self.assertEqual(detect_file_type("doc.pdf", None), "pdf")

    def test_png_extension(self):
        self.assertEqual(detect_file_type("photo.png", None), "image")

    def test_jpg_extension(self):
        self.assertEqual(detect_file_type("photo.jpg", None), "image")

    def test_jpeg_extension(self):
        self.assertEqual(detect_file_type("photo.jpeg", None), "image")

    def test_gif_extension(self):
        self.assertEqual(detect_file_type("anim.gif", None), "image")

    def test_webp_extension(self):
        self.assertEqual(detect_file_type("img.webp", None), "image")

    def test_uppercase_extension_normalised(self):
        # detect_file_type lowercases via .rsplit(".", 1)[-1].lower()
        self.assertEqual(detect_file_type("PHOTO.PNG", None), "image")
        self.assertEqual(detect_file_type("FILE.PDF", None), "pdf")


class DetectByMime(unittest.TestCase):
    def test_application_pdf_mime(self):
        self.assertEqual(detect_file_type("noext", "application/pdf"), "pdf")

    def test_image_mime(self):
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            with self.subTest(mime=mime):
                self.assertEqual(detect_file_type("noext", mime), "image")

    def test_mime_with_charset_suffix_stripped(self):
        # `text/html; charset=utf-8` style suffix should be ignored for image match
        self.assertEqual(detect_file_type("noext", "image/png; charset=binary"), "image")


class ExtensionTakesPrecedenceOverMime(unittest.TestCase):
    def test_pdf_extension_wins_over_image_mime(self):
        # Some browsers mis-label PDFs as octet-stream. Extension is the tiebreaker.
        self.assertEqual(detect_file_type("doc.pdf", "image/png"), "pdf")

    def test_pdf_mime_overrides_image_extension(self):
        # If either ext or mime says "pdf", the function returns "pdf".
        # The PDF check is evaluated before the image check.
        self.assertEqual(detect_file_type("doc.png", "application/pdf"), "pdf")


class UnsupportedFiles(unittest.TestCase):
    def test_unknown_extension_raises(self):
        with self.assertRaises(ValueError):
            detect_file_type("archive.zip", None)

    def test_no_extension_no_mime_raises(self):
        with self.assertRaises(ValueError):
            detect_file_type("noext", None)

    def test_docx_raises(self):
        with self.assertRaises(ValueError):
            detect_file_type("paper.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_value_error_message_includes_filename(self):
        with self.assertRaises(ValueError) as ctx:
            detect_file_type("evil.exe", None)
        # The error message embeds the filename so users can debug
        self.assertIn("evil.exe", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
