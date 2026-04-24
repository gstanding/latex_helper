import asyncio
import json
import os
import shutil
import tempfile

import aiofiles
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from latex_helper.converter import get_converter, get_llm_info
from latex_helper.utils import MAX_FILE_SIZE, detect_file_type

app = FastAPI(title="LaTeX Helper")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, detail="File size exceeds 20 MB limit.")

    try:
        file_type = detect_file_type(file.filename or "", file.content_type)
    except ValueError as e:
        raise HTTPException(415, detail=str(e))

    converter = get_converter()

    async def event_generator():
        try:
            async for chunk in converter.stream_latex(content, file_type, file.filename or ""):
                escaped = chunk.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
            yield "event: done\ndata: \n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/compile")
async def compile_latex(payload: dict):
    latex_source: str = payload.get("latex", "")
    if not latex_source.strip():
        raise HTTPException(400, detail="Empty LaTeX source.")

    # Strip markdown code fences that AI models sometimes add
    import re
    latex_source = re.sub(r"^```(?:latex)?\s*\n?", "", latex_source.strip())
    latex_source = re.sub(r"\n?```\s*$", "", latex_source.strip())

    if not shutil.which("pdflatex"):
        raise HTTPException(
            503,
            detail=json.dumps(
                {
                    "error": "pdflatex_not_found",
                    "message": "pdflatex is not installed. Install TeX Live to use this feature.",
                }
            ),
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tex_path = os.path.join(tmpdir, "document.tex")
        pdf_path = os.path.join(tmpdir, "document.pdf")
        log_path = os.path.join(tmpdir, "document.log")

        async with aiofiles.open(tex_path, "w") as f:
            await f.write(latex_source)

        try:
            proc = await asyncio.create_subprocess_exec(
                "pdflatex",
                "-interaction=nonstopmode",
                "-output-directory",
                tmpdir,
                tex_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=30.0)
            except asyncio.TimeoutError:
                proc.kill()
                raise HTTPException(
                    408,
                    detail=json.dumps({"error": "timeout", "message": "pdflatex timed out after 30s."}),
                )
        except FileNotFoundError:
            raise HTTPException(503, detail=json.dumps({"error": "pdflatex_not_found"}))

        if not os.path.exists(pdf_path):
            log_content = ""
            if os.path.exists(log_path):
                async with aiofiles.open(log_path, "r", errors="replace") as f:
                    log_content = await f.read()
            raise HTTPException(
                422,
                detail=json.dumps(
                    {"error": "compilation_failed", "log": log_content[-4000:]}
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
    return {"available": shutil.which("pdflatex") is not None}


@app.get("/health/llm")
async def health_llm():
    return get_llm_info()
