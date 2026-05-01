import base64
import re

PDF_PAGE_LIMIT = 100

# Pre-compiled regexes used in post-processing (avoid recompiling on every call)
_RE_TIKZSET = re.compile(r"\\tikzset\{[^}]*(?:\{[^}]*\}[^}]*)?\}")
_RE_CJK_BEGIN = re.compile(r"\\begin\{CJK\*\}\{[^}]*\}\{[^}]*\}\n?")
_RE_CJK_END = re.compile(r"\\end\{CJK\*\}\n?")
_RE_INCLUDEGRAPHICS = re.compile(r"[^\n]*\\includegraphics[^\n]*\n?")
_RE_INCLUDEGRAPHICS_FNAME = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_RE_DEFINECOLOR = re.compile(r"\\definecolor\{(\w+)\}")
_RE_USEPACKAGE_BODY = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{[^}]+\}\n?")
_RE_USEPACKAGE_NAME = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]+)\}")
_RE_DEFINECOLOR_FULL = re.compile(r"\\definecolor\{(\w+)\}\{[^}]*\}\{[^}]*\}\n?")

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


def _merge_preamble_packages(latex: str) -> str:
    """Move \\usepackage commands found inside \\begin{document} to the preamble.
    Also deduplicates \\definecolor definitions. Helps MiniMax multi-page output
    where later pages may include package declarations inside their body.
    """
    doc_start_idx = latex.find(r"\begin{document}")
    if doc_start_idx == -1:
        return latex

    preamble = latex[:doc_start_idx]
    body = latex[doc_start_idx:]

    existing_pkgs: set[str] = set(_RE_USEPACKAGE_NAME.findall(preamble))
    extra_pkgs: list[str] = []

    def _remove_body_pkg(m: re.Match) -> str:
        nm = _RE_USEPACKAGE_NAME.search(m.group(0))
        if nm:
            for name in nm.group(1).split(","):
                name = name.strip()
                if name and name not in existing_pkgs:
                    existing_pkgs.add(name)
                    extra_pkgs.append(m.group(0).rstrip("\n"))
        return ""

    body = _RE_USEPACKAGE_BODY.sub(_remove_body_pkg, body)

    if extra_pkgs:
        preamble = preamble.rstrip("\n") + "\n" + "\n".join(extra_pkgs) + "\n"

    # Deduplicate \definecolor definitions across the full document
    seen_colors: set[str] = set()

    def _dedup_color(m: re.Match) -> str:
        name = m.group(1)
        if name in seen_colors:
            return ""
        seen_colors.add(name)
        return m.group(0)

    return _RE_DEFINECOLOR_FULL.sub(_dedup_color, preamble + body)


def postprocess_latex(latex: str) -> str:
    """
    Fix common model output issues that prevent compilation:
    1. Move any \\tikzset that appears before \\usepackage{tikz} to after it.
    2. Remove spurious \\begin{CJK*} / \\end{CJK*} inside ctex documents.
    3. Comment out \\includegraphics lines referencing non-embedded images.
    4. Auto-define any xcolor color names used but not declared.
    5. Move stray \\usepackage from body to preamble and deduplicate \\definecolor.
    """
    # ── 1. Fix \tikzset before \usepackage{tikz} ────────────────────────────
    tikz_pkg = r"\usepackage{tikz}"
    if tikz_pkg in latex:
        before, after = latex.split(tikz_pkg, 1)
        orphaned = _RE_TIKZSET.findall(before)
        if orphaned:
            before = _RE_TIKZSET.sub("", before)
            after = "\n" + "\n".join(orphaned) + after
        latex = before + tikz_pkg + after

    # ── 2. Remove redundant CJK* environment (ctex handles encoding) ────────
    if r"\documentclass" in latex and ("ctexart" in latex or "ctexbook" in latex or "ctexrep" in latex):
        latex = _RE_CJK_BEGIN.sub("", latex)
        latex = _RE_CJK_END.sub("", latex)

    # ── 3. Comment out \includegraphics pointing to non-existent placeholder files
    def _comment_missing_image(m: re.Match) -> str:
        line = m.group(0)
        fname_m = _RE_INCLUDEGRAPHICS_FNAME.search(line)
        if fname_m:
            p = fname_m.group(1).strip()
            # Keep: path with directory component
            if "/" in p or p.startswith("\\"):
                return line
            # Keep: filename with a recognised image extension (screenshot mode or explicit path)
            if re.search(r"\.(png|jpg|jpeg|pdf|eps|svg|gif|webp)$", p, re.IGNORECASE):
                return line
        return "% " + line

    latex = _RE_INCLUDEGRAPHICS.sub(_comment_missing_image, latex)

    # ── 4. Auto-define missing colors ───────────────────────────────────────
    defined = set(_RE_DEFINECOLOR.findall(latex)) | _STD_COLORS
    used    = _collect_used_colors(latex)
    missing = used - defined

    if missing:
        new_defs = "\n".join(
            f"\\definecolor{{{name}}}{{RGB}}{{{_infer_rgb(name)}}}"
            for name in sorted(missing)
        )
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

    # ── 5. Merge stray \usepackage from body; deduplicate \definecolor ───────
    latex = _merge_preamble_packages(latex)

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


def extract_pdf_figures(file_bytes: bytes) -> dict[str, bytes]:
    """Extract embedded images from PDF. Returns {figureN.png: png_bytes} in document order.

    Tries embedded raster XObjects first. Falls back to rendering drawing-heavy page regions
    if no raster images are found. Skips images smaller than 64×64 px (icons/decorations).
    """
    import fitz

    result: dict[str, bytes] = {}
    counter = 1
    MIN_DIM = 64

    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        seen_xrefs: set[int] = set()
        any_embedded = False

        for page in doc:
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.width < MIN_DIM or pix.height < MIN_DIM:
                        continue
                    if pix.colorspace and pix.colorspace.n > 3:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    result[f"figure{counter}.png"] = pix.tobytes("png")
                    counter += 1
                    any_embedded = True
                except Exception:
                    continue

        # Fallback: render each page that has substantial vector drawings
        if not any_embedded:
            mat = fitz.Matrix(2.0, 2.0)
            for page in doc:
                drawings = page.get_drawings()
                if not drawings:
                    continue
                rects = [fitz.Rect(d["rect"]) for d in drawings if d.get("rect")]
                if not rects:
                    continue
                combined = rects[0]
                for r in rects[1:]:
                    combined = combined | r
                if combined.get_area() < 5000:
                    continue
                pix = page.get_pixmap(matrix=mat, clip=combined)
                if pix.width < MIN_DIM or pix.height < MIN_DIM:
                    continue
                result[f"figure{counter}.png"] = pix.tobytes("png")
                counter += 1

    return result
