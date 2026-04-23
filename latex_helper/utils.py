import base64

PDF_PAGE_LIMIT = 100
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def detect_file_type(filename: str, content_type: str | None) -> str:
    """Returns 'pdf' or 'image'. Raises ValueError for unsupported types."""
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    mime = (content_type or "").split(";")[0].strip().lower()

    if ext == "pdf" or mime == "application/pdf":
        return "pdf"
    if ext in ("png", "jpg", "jpeg", "gif", "webp") or mime.startswith("image/"):
        return "image"
    raise ValueError(f"Unsupported file type: {filename} ({content_type})")


def prepare_content_blocks(
    file_bytes: bytes,
    file_type: str,
    filename: str,
    use_native_pdf: bool = True,
) -> list[dict]:
    """Build Claude API content blocks for the given file."""
    if file_type == "image":
        return _image_blocks(file_bytes, filename)
    if use_native_pdf:
        return _pdf_native_blocks(file_bytes)
    return _pdf_as_images_blocks(file_bytes)


def _image_blocks(file_bytes: bytes, filename: str) -> list[dict]:
    ext = (filename or "image.png").rsplit(".", 1)[-1].lower()
    mime_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    media_type = mime_map.get(ext, "image/png")
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(file_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": "Convert this image to LaTeX."},
    ]


def _pdf_native_blocks(file_bytes: bytes) -> list[dict]:
    """Use Claude's native PDF document block (Anthropic only)."""
    import fitz  # pymupdf

    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        page_count = len(doc)

    if page_count > PDF_PAGE_LIMIT:
        raise ValueError(
            f"PDF has {page_count} pages; maximum supported is {PDF_PAGE_LIMIT}. "
            "Split the document or use a smaller file."
        )

    return [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(file_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": f"Convert this {page_count}-page PDF document to LaTeX."},
    ]


def _pdf_as_images_blocks(file_bytes: bytes) -> list[dict]:
    """Render each PDF page as PNG and pass as image blocks (MiniMax path)."""
    pages = pdf_to_page_images(file_bytes)
    blocks: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode("ascii"),
            },
        }
        for png in pages
    ]
    blocks.append(
        {
            "type": "text",
            "text": f"This is a {len(pages)}-page PDF rendered as images. Convert all pages to LaTeX.",
        }
    )
    return blocks


def pdf_to_page_images(file_bytes: bytes) -> list[bytes]:
    """Render each PDF page as PNG bytes. Used by MiniMax VLM path."""
    import fitz  # pymupdf

    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        page_count = len(doc)
        if page_count > PDF_PAGE_LIMIT:
            raise ValueError(
                f"PDF has {page_count} pages; maximum supported is {PDF_PAGE_LIMIT}. "
                "Split the document or use a smaller file."
            )
        mat = fitz.Matrix(1.5, 1.5)  # ~110 DPI effective
        return [page.get_pixmap(matrix=mat).tobytes("png") for page in doc]
