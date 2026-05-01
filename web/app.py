import asyncio
import base64
import binascii
import json
import logging
import os
import re as _re
import shutil
import tempfile

import aiofiles

logger = logging.getLogger("latex_helper")

_PDF_MAGIC = b"%PDF-"


def _is_valid_pdf(path: str) -> bool:
    """Check file starts with PDF magic bytes and ends with %%EOF (properly finalized)."""
    try:
        size = os.path.getsize(path)
        if size < 256:
            return False
        with open(path, "rb") as f:
            if f.read(5) != _PDF_MAGIC:
                return False
            # A properly finalized PDF must contain %%EOF near the end.
            # Incomplete/crashed compilations won't have it.
            f.seek(max(0, size - 1024))
            return b"%%EOF" in f.read()
    except OSError:
        return False
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from latex_helper.converter import get_converter, get_llm_info
from latex_helper.utils import MAX_FILE_SIZE, detect_file_type, extract_pdf_figures, postprocess_latex

_DANGEROUS_LATEX = {r"\write18", r"\immediate\write"}

_CJK_RE = _re.compile(r"[一-鿿぀-ゟ가-힯]")


def _needs_xelatex(src: str) -> bool:
    return "ctex" in src or "xeCJK" in src or bool(_CJK_RE.search(src))


def _parse_latex_log(log: str) -> dict:
    """Extract first fatal error and line number from a pdflatex/xelatex log."""
    error_msg = None
    error_line = None
    for i, line in enumerate(log.splitlines()):
        if line.startswith("!"):
            error_msg = line[1:].strip()
            for j in range(i + 1, min(i + 15, len(log.splitlines()))):
                lm = _re.match(r"^l\.(\d+)", log.splitlines()[j])
                if lm:
                    error_line = int(lm.group(1))
                    break
            break
    return {"error": error_msg, "line": error_line}


class CompileRequest(BaseModel):
    latex: str
    images: dict[str, str] = {}

app = FastAPI(title="LaTeX Helper")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


_VALID_FIGURE_MODES = {"draw", "skip", "screenshot"}


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    figure_mode: str = Form("draw"),
):
    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, detail="File size exceeds 20 MB limit.")

    if figure_mode not in _VALID_FIGURE_MODES:
        figure_mode = "draw"

    try:
        file_type = detect_file_type(file.filename or "", file.content_type)
    except ValueError as e:
        raise HTTPException(415, detail=str(e))

    # Pre-extract figures for screenshot mode (before calling LLM so we know the count)
    figures_b64: dict[str, str] = {}
    figure_count = 0
    if figure_mode == "screenshot" and file_type == "pdf":
        raw_figures = extract_pdf_figures(content)
        figure_count = len(raw_figures)
        figures_b64 = {
            name: base64.standard_b64encode(data).decode("ascii")
            for name, data in raw_figures.items()
        }

    converter = get_converter()

    async def event_generator():
        try:
            full_latex = ""
            char_count = 0
            last_progress = asyncio.get_event_loop().time()

            async for chunk in converter.stream_latex(
                content,
                file_type,
                file.filename or "",
                figure_mode=figure_mode,
                figure_count=figure_count,
            ):
                full_latex += chunk
                char_count += len(chunk)
                now = asyncio.get_event_loop().time()
                if now - last_progress >= 5.0:
                    yield f"event: progress\ndata: {json.dumps({'chars': char_count})}\n\n"
                    last_progress = now

            full_latex = postprocess_latex(full_latex)

            chunk_size = 512
            for i in range(0, len(full_latex), chunk_size):
                yield f"data: {json.dumps(full_latex[i:i + chunk_size], ensure_ascii=False)}\n\n"

            if figures_b64:
                yield f"event: images\ndata: {json.dumps(figures_b64, ensure_ascii=False)}\n\n"

            yield "event: done\ndata: \n\n"
        except Exception as e:
            logger.error("Conversion error: %s", e, exc_info=True)
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/compile")
async def compile_latex(req: CompileRequest):
    latex_source = req.latex
    images = req.images

    if not latex_source.strip():
        raise HTTPException(400, detail="Empty LaTeX source.")

    # Reject dangerous shell-escape commands
    for cmd in _DANGEROUS_LATEX:
        if cmd in latex_source:
            raise HTTPException(400, detail=f"Unsafe LaTeX command detected: {cmd}")

    # Strip markdown code fences that AI models sometimes add
    latex_source = _re.sub(r"^```(?:latex)?\s*\n?", "", latex_source.strip())
    latex_source = _re.sub(r"\n?```\s*$", "", latex_source.strip())
    latex_source = _re.sub(r"%\s*---?\s*Page\s+break\s*---?\s*\n", "\n\\newpage\n", latex_source, flags=_re.IGNORECASE)

    if not shutil.which("pdflatex") and not shutil.which("xelatex"):
        raise HTTPException(
            503,
            detail=json.dumps(
                {
                    "error": "pdflatex_not_found",
                    "message": "pdflatex/xelatex is not installed. Install TeX Live to use this feature.",
                }
            ),
        )

    # Detect required compiler (CJK characters → xelatex)
    compiler = "xelatex" if (_needs_xelatex(latex_source) and shutil.which("xelatex")) else "pdflatex"

    with tempfile.TemporaryDirectory() as tmpdir:
        tex_path = os.path.join(tmpdir, "document.tex")
        pdf_path = os.path.join(tmpdir, "document.pdf")
        log_path = os.path.join(tmpdir, "document.log")

        # Write embedded figure images so \includegraphics can resolve them
        for img_name, img_b64 in images.items():
            # Replace unsafe chars; keep extension
            safe = _re.sub(r"[^a-zA-Z0-9._-]", "_", img_name)
            if not safe:
                continue
            try:
                img_bytes = base64.b64decode(img_b64, validate=True)
                with open(os.path.join(tmpdir, safe), "wb") as f:
                    f.write(img_bytes)
            except binascii.Error:
                logger.warning("Invalid base64 for image %s — skipped", img_name)
            except Exception:
                logger.warning("Failed to write image %s — skipped", img_name, exc_info=True)

        async with aiofiles.open(tex_path, "w") as f:
            await f.write(latex_source)

        try:
            proc = await asyncio.create_subprocess_exec(
                compiler,
                "-interaction=nonstopmode",
                "-output-directory",
                tmpdir,
                tex_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=60.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise HTTPException(
                    408,
                    detail=json.dumps({"error": "timeout", "message": "编译超时（60s）。"}),
                )
        except FileNotFoundError:
            raise HTTPException(503, detail=json.dumps({"error": "pdflatex_not_found"}))

        # Validate by PDF magic bytes rather than exit code (warnings → non-zero exit but valid PDF)
        if not _is_valid_pdf(pdf_path):
            log_content = ""
            if os.path.exists(log_path):
                async with aiofiles.open(log_path, "r", errors="replace") as f:
                    log_content = await f.read()
            parsed = _parse_latex_log(log_content)
            raise HTTPException(
                422,
                detail=json.dumps(
                    {
                        "error": "compilation_failed",
                        "message": parsed["error"] or "编译失败，请查看日志",
                        "line": parsed["line"],
                        "log": log_content[-4000:],
                    }
                ),
            )

        async with aiofiles.open(pdf_path, "rb") as f:
            pdf_bytes = await f.read()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=document.pdf"},
    )


@app.get("/health/pdflatex")
async def health_pdflatex():
    return {
        "available": shutil.which("pdflatex") is not None or shutil.which("xelatex") is not None,
        "pdflatex": shutil.which("pdflatex") is not None,
        "xelatex": shutil.which("xelatex") is not None,
    }


@app.get("/health/llm")
async def health_llm():
    return get_llm_info()
