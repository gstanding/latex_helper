import base64
import re

PDF_PAGE_LIMIT = 100

# ── LaTeX post-processor ──────────────────────────────────────────────────────

_STD_COLORS = {
    "black", "white", "red", "green", "blue", "cyan", "magenta", "yellow",
    "gray", "darkgray", "lightgray", "brown", "lime", "olive", "orange",
    "pink", "purple", "teal", "violet",
}

# TikZ option keywords that are never color names
_TIKZ_KEYS = {
    "thin", "thick", "very", "ultra", "dashed", "dotted", "solid", "rounded",
    "corners", "anchor", "above", "below", "left", "right", "inner", "outer",
    "minimum", "font", "align", "overlay", "remember", "picture", "shift",
    "scale", "opacity", "fill", "draw", "color", "text", "node", "line",
    "width", "xshift", "yshift", "rotate", "xscale", "yscale", "bend",
    "arrows", "start", "angle", "radius", "north", "south", "east", "west",
    "center", "at", "of", "shape", "circle", "rectangle", "baseline", "base",
    "stealth", "latex", "loop", "above", "sloped", "midway", "near", "pos",
    "step", "help", "grid", "major", "minor", "every", "clip", "use",
    "path", "canvas", "current", "page", "transform",
}


def _infer_rgb(name: str) -> str:
    """Guess an RGB value from a color name using keyword heuristics."""
    n = name.lower()
    if "blue" in n:                         return "31,78,121"
    if "orange" in n:                       return "230,120,20"
    if "yellow" in n or "gold" in n:        return "220,185,0"
    if "red" in n:                          return "192,50,50"
    if "green" in n:                        return "50,150,80"
    if "cyan" in n:                         return "0,175,200"
    if "gray" in n or "grey" in n:          return "128,128,128"
    if "brown" in n:                        return "150,90,40"
    if "light" in n or "bg" in n:           return "240,240,240"
    if "dark" in n or "banner" in n:        return "50,50,50"
    return "80,80,80"


def _collect_used_colors(latex: str) -> set[str]:
    """Return all color names referenced in the LaTeX source."""
    used: set[str] = set()

    # \color{name}, \textcolor{name}, \colorbox{name}, \pagecolor{name}
    for m in re.finditer(r"\\(?:color|textcolor|colorbox|fcolorbox|pagecolor)\{(\w+)\}", latex):
        used.add(m.group(1))

    # draw=name, fill=name, color=name, text=name  inside [...] option blocks
    for m in re.finditer(r"(?:draw|fill|color|text)\s*=\s*(\w+)", latex):
        used.add(m.group(1))

    # bare names inside TikZ option blocks, e.g. \draw[headerblue, thin]
    for m in re.finditer(r"\\(?:draw|fill|filldraw|node|path|tikzset)\[([^\]]*)\]", latex):
        for part in m.group(1).split(","):
            token = re.split(r"[=!]", part.strip())[0].strip()
            if re.match(r"^[a-zA-Z][a-zA-Z0-9]{2,}$", token) and token not in _TIKZ_KEYS:
                used.add(token)

    # color modifiers: name!80 inside option blocks
    for m in re.finditer(r"\b([a-zA-Z][a-zA-Z0-9]{2,})!\d", latex):
        used.add(m.group(1))

    return used


def postprocess_latex(latex: str) -> str:
    """
    Fix common model output issues that prevent compilation:
    1. Auto-define any xcolor color names used but not declared.
    2. Remove spurious \\begin{CJK*} / \\end{CJK*} inside ctex documents.
    3. Comment out \\includegraphics lines referencing non-embedded images.
    4. Move any \\tikzset that appears before \\usepackage{tikz} to after it.
    """
    # ── 1. Fix \tikzset before \usepackage{tikz} ────────────────────────────
    tikz_pkg = r"\usepackage{tikz}"
    if tikz_pkg in latex:
        before, after = latex.split(tikz_pkg, 1)
        # Pull any \tikzset lines out of the preamble-before-tikz section
        orphaned = re.findall(r"\\tikzset\{[^}]*(?:\{[^}]*\}[^}]*)?\}", before)
        if orphaned:
            before = re.sub(r"\\tikzset\{[^}]*(?:\{[^}]*\}[^}]*)?\}\n?", "", before)
            after = "\n" + "\n".join(orphaned) + after
        latex = before + tikz_pkg + after

    # ── 2. Remove redundant CJK* environment (ctex handles encoding) ────────
    if r"\documentclass" in latex and ("ctexart" in latex or "ctexbook" in latex or "ctexrep" in latex):
        latex = re.sub(r"\\begin\{CJK\*\}\{[^}]*\}\{[^}]*\}\n?", "", latex)
        latex = re.sub(r"\\end\{CJK\*\}\n?", "", latex)

    # ── 3. Comment out \includegraphics pointing to external/non-existent files
    def _comment_missing_image(m: re.Match) -> str:
        line = m.group(0)
        # Keep if it references a clearly embedded/real path (absolute or relative with subdir)
        filename = re.search(r"\{([^}]+)\}", line)
        if filename:
            p = filename.group(1)
            if "/" in p or "\\" in p:   # absolute or subdir path — leave as-is
                return line
        return "% " + line          # placeholder filename — comment out

    latex = re.sub(r"[^\n]*\\includegraphics[^\n]*\n?", _comment_missing_image, latex)

    # ── 4. Auto-define missing colors ───────────────────────────────────────
    defined = set(re.findall(r"\\definecolor\{(\w+)\}", latex)) | _STD_COLORS
    used    = _collect_used_colors(latex)
    missing = used - defined

    if missing:
        new_defs = "\n".join(
            f"\\definecolor{{{name}}}{{RGB}}{{{_infer_rgb(name)}}}"
            for name in sorted(missing)
        )
        # Insert right after \usepackage{xcolor}, or before \begin{document}
        if r"\usepackage{xcolor}" in latex:
            latex = latex.replace(
                r"\usepackage{xcolor}",
                r"\usepackage{xcolor}" + "\n" + new_defs,
                1,
            )
        else:
            latex = latex.replace(
                r"\begin{document}",
                r"\usepackage{xcolor}" + "\n" + new_defs + "\n" + r"\begin{document}",
                1,
            )

    return latex
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
