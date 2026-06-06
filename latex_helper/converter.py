import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import string
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator

import anthropic
import httpx

from latex_helper.prompts import get_system_prompt, _FIX_SYSTEM_PROMPT, build_fix_message
from latex_helper.utils import pdf_to_page_images, prepare_content_blocks

_log = logging.getLogger(__name__)

_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
_MINIMAX_DEFAULT_HOST = "https://api.minimaxi.com"
_MINIMAX_DEFAULT_TEXT_MODEL = "minimax-m3"
_SIMPLETEX_DEFAULT_HOST = "https://server.simpletex.cn"
_SIMPLETEX_OCR_PATH = "/api/latex_ocr"
_SIMPLETEX_NET_HOST = "https://server.simpletex.net"
_SIMPLETEX_DOC_OCR_V2_PATH = "/api/doc_ocr"


def _strip_end_document(latex: str) -> str:
    """Remove trailing \\end{document} so pages can be appended."""
    return re.sub(r'\s*\\end\{document\}\s*$', '', latex.rstrip())


def _extract_body(latex: str) -> str:
    """Return only the content between \\begin{document} and \\end{document}."""
    m = re.search(r'\\begin\{document\}(.*?)(?:\\end\{document\}|$)', latex, re.DOTALL)
    if m:
        return m.group(1).strip()
    return latex.strip()


def _render_pdf_to_data_urls(file_bytes: bytes) -> list[str]:
    """Render all PDF pages to PNG and base64-encode as data URLs. Runs in a thread pool."""
    pages = pdf_to_page_images(file_bytes)
    return [
        f"data:image/png;base64,{base64.standard_b64encode(png).decode('ascii')}"
        for png in pages
    ]


class LatexConverter(ABC):
    output_format: str = "latex"  # "latex" or "markdown"

    @abstractmethod
    async def stream_latex(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str,
        figure_mode: str = "draw",
        figure_count: int = 0,
    ) -> AsyncIterator[str]: ...

    @abstractmethod
    async def stream_fix(self, latex: str, log: str) -> AsyncIterator[str]: ...


class AnthropicConverter(LatexConverter):
    def __init__(self, client: anthropic.AsyncAnthropic, model: str) -> None:
        self.client = client
        self.model = model

    async def stream_latex(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str,
        figure_mode: str = "draw",
        figure_count: int = 0,
    ) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        system_prompt = get_system_prompt(figure_mode, figure_count)
        loop = asyncio.get_running_loop()
        # prepare_content_blocks opens fitz + base64-encodes — run off event loop
        blocks = await loop.run_in_executor(
            None, prepare_content_blocks, file_bytes, file_type, filename, True
        )
        _log.info("[anthropic] prepare_content_blocks %.2fs", time.perf_counter() - t0)
        first = True
        t_req = time.perf_counter()
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": blocks}],
        ) as stream:
            async for text in stream.text_stream:
                if first:
                    _log.info("[anthropic] first token %.2fs", time.perf_counter() - t_req)
                    first = False
                yield text
        _log.info("[anthropic] stream complete %.2fs total", time.perf_counter() - t0)

    async def stream_fix(self, latex: str, log: str) -> AsyncIterator[str]:
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=8192,
            system=_FIX_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_fix_message(latex, log)}],
        ) as stream:
            async for text in stream.text_stream:
                yield text


class MinimaxVLMConverter(LatexConverter):
    """MiniMax M3 multimodal model via chat completions API (OpenAI format) for both image→LaTeX and fix."""

    def __init__(self, api_key: str, api_host: str, text_model: str) -> None:
        self.api_key = api_key
        self.api_host = api_host.rstrip("/")
        self.text_model = text_model
        # Single persistent HTTP client for all requests (image conversion + fix)
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=10.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def _stream_chat_completions(
        self,
        system_prompt: str,
        image_data_urls: list[str],
        user_text: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream chat completions. Supports image+text (multimodal) or text-only requests."""
        url = f"{self.api_host}/v1/chat/completions"
        n = len(image_data_urls)
        user_content: list[dict] = [
            {"type": "image_url", "image_url": {"url": data_url}}
            for data_url in image_data_urls
        ]
        if user_text is not None:
            # Explicit text (e.g. fix request)
            user_content.append({"type": "text", "text": user_text})
        elif n == 1:
            user_content.append({"type": "text", "text": "Convert this image to LaTeX."})
        else:
            user_content.append({"type": "text", "text": (
                f"The {n} images above are pages 1 through {n} of a single document, in order. "
                "Produce one complete LaTeX source covering all pages. "
                r"Use \newpage to separate pages where appropriate."
            )})
        payload = {
            "model": self.text_model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }

        # Think-block filter state machine — handles <think>...</think> across chunk boundaries
        _THINK_OPEN = "<think>"
        _THINK_CLOSE = "</think>"
        in_think = False
        buf = ""
        first = True
        t_req = time.perf_counter()

        async with self._http_client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
                if not delta:
                    continue

                buf += delta
                while True:
                    if in_think:
                        pos = buf.find(_THINK_CLOSE)
                        if pos == -1:
                            buf = buf[-(len(_THINK_CLOSE) - 1):] if len(buf) >= len(_THINK_CLOSE) else buf
                            break
                        buf = buf[pos + len(_THINK_CLOSE):]
                        in_think = False
                    else:
                        pos = buf.find(_THINK_OPEN)
                        if pos == -1:
                            keep = len(_THINK_OPEN) - 1
                            if len(buf) > keep:
                                out = buf[:-keep]
                                buf = buf[-keep:]
                                if first and out.strip():
                                    _log.info("[minimax] first token %.2fs", time.perf_counter() - t_req)
                                    first = False
                                yield out
                            break
                        if pos > 0:
                            out = buf[:pos]
                            if first and out.strip():
                                _log.info("[minimax] first token %.2fs", time.perf_counter() - t_req)
                                first = False
                            yield out
                        buf = buf[pos + len(_THINK_OPEN):]
                        in_think = True

        if buf and not in_think:
            if first:
                _log.info("[minimax] first token %.2fs", time.perf_counter() - t_req)
            yield buf
        _log.info("[minimax] stream complete %.2fs", time.perf_counter() - t_req)

    async def stream_latex(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str,
        figure_mode: str = "draw",
        figure_count: int = 0,
    ) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        system_prompt = get_system_prompt(figure_mode, figure_count)
        loop = asyncio.get_running_loop()
        if file_type == "pdf":
            # PDF rendering is CPU-bound — run off event loop
            image_data_urls = await loop.run_in_executor(
                None, _render_pdf_to_data_urls, file_bytes
            )
            _log.info("[minimax] pdf render %d page(s) %.2fs", len(image_data_urls), time.perf_counter() - t0)
            if not image_data_urls:
                return
            async for chunk in self._stream_chat_completions(system_prompt, image_data_urls):
                yield chunk
        else:
            ext = (filename or "image.png").rsplit(".", 1)[-1].lower()
            fmt_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}
            fmt = fmt_map.get(ext, "png")
            async for chunk in self._stream_chat_completions(
                system_prompt,
                [f"data:image/{fmt};base64,{base64.standard_b64encode(file_bytes).decode('ascii')}"],
            ):
                yield chunk
        _log.info("[minimax] stream_latex total %.2fs", time.perf_counter() - t0)

    async def stream_fix(self, latex: str, log: str) -> AsyncIterator[str]:
        # M3 supports multimodal natively — use the same chat completions endpoint as stream_latex
        async for chunk in self._stream_chat_completions(
            _FIX_SYSTEM_PROMPT,
            [],  # text-only, no images
            user_text=build_fix_message(latex, log),
        ):
            yield chunk


class SimpletexConverter(LatexConverter):
    """SimpleTex OCR API for image/PDF → LaTeX formula recognition."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        api_host: str,
        llm_converter: "LatexConverter | None" = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_host = api_host.rstrip("/")
        self._llm = llm_converter

    def _make_headers(self, extra_fields: dict | None = None) -> dict:
        random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
        timestamp = str(int(time.time()))
        # Collect all fields to sign: header fields + any non-binary form fields
        fields = {
            "app-id": self.app_id,
            "random-str": random_str,
            "timestamp": timestamp,
        }
        if extra_fields:
            fields.update(extra_fields)
        # Sort keys alphabetically, append secret at end (per SimpleTex docs)
        sign_src = "&".join(f"{k}={v}" for k, v in sorted(fields.items()))
        sign_src += f"&secret={self.app_secret}"
        sign = hashlib.md5(sign_src.encode()).hexdigest()
        return {
            "app-id": self.app_id,
            "random-str": random_str,
            "timestamp": timestamp,
            "sign": sign,
        }

    async def _call_ocr(self, image_bytes: bytes, filename: str = "image.png") -> str:
        url = f"{self.api_host}{_SIMPLETEX_OCR_PATH}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=10.0)
        ) as client:
            resp = await client.post(
                url,
                headers=self._make_headers(),
                files={"file": (filename, image_bytes, "image/png")},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("status"):
                msg = data.get("message") or data.get("msg") or "unknown error"
                raise RuntimeError(f"SimpleTex API error: {msg}")
            return data.get("res", {}).get("latex", "")

    async def stream_latex(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str,
        figure_mode: str = "draw",
        figure_count: int = 0,
    ) -> AsyncIterator[str]:
        if file_type == "pdf":
            pages = pdf_to_page_images(file_bytes)
            if not pages:
                return
            results = []
            for i, page_png in enumerate(pages):
                latex = await self._call_ocr(page_png, f"page_{i + 1}.png")
                results.append(latex)
            yield _assemble_simpletex_document(results)
        else:
            ext = (filename or "image.png").rsplit(".", 1)[-1].lower()
            if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
                ext = "png"
            latex = await self._call_ocr(file_bytes, f"image.{ext}")
            yield _assemble_simpletex_document([latex]) if latex else ""

    async def stream_fix(self, latex: str, log: str) -> AsyncIterator[str]:
        if self._llm is None:
            raise RuntimeError(
                "SimpleTex 不支持自动修复。请配置 ANTHROPIC_API_KEY 或 MINIMAX_API_KEY 以启用此功能。"
            )
        async for chunk in self._llm.stream_fix(latex, log):
            yield chunk


class SimpletexDocConverter(SimpletexConverter):
    """SimpleTex 通用 OCR → Markdown（含内嵌 LaTeX 公式）。快速预览路径。"""

    output_format = "markdown"

    async def _call_doc_ocr(self, image_bytes: bytes, filename: str = "image.png") -> str:
        url = f"{self.api_host}/api/simpletex_ocr"
        rec_mode = "document"
        t_req = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=10.0)
        ) as client:
            resp = await client.post(
                url,
                headers=self._make_headers(extra_fields={"rec_mode": rec_mode}),
                data={"rec_mode": rec_mode},
                files={"file": (filename, image_bytes, "image/png")},
            )
            resp.raise_for_status()
            _log.info("[simpletex_doc] request %s %.2fs", filename, time.perf_counter() - t_req)
            data = resp.json()
            _log.info("[simpletex_doc] raw response: %s", data)
            if not data.get("status"):
                msg = data.get("message") or data.get("msg") or "unknown error"
                raise RuntimeError(f"SimpleTex OCR error: {msg}")
            res = data.get("res", {})
            _log.info("[simpletex_doc] res keys: %s", list(res.keys()) if isinstance(res, dict) else res)
            # Response structure: {"type": "doc", "info": {"markdown": "..."}}
            if isinstance(res, dict):
                info = res.get("info") or {}
                return (
                    info.get("markdown")
                    or res.get("markdown")
                    or res.get("result")
                    or res.get("latex")
                    or ""
                )
            return str(res) if res else ""

    async def _embed_images(self, md: str) -> str:
        """将 markdown 中的外链图片 URL 替换为 base64 data URI，避免浏览器 CORS/鉴权问题。"""
        urls = list(dict.fromkeys(re.findall(r'!\[[^\]]*\]\((https?://[^\)]+)\)', md)))
        if not urls:
            return md
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "image/png").split(";")[0].strip()
                        b64 = base64.standard_b64encode(resp.content).decode("ascii")
                        md = md.replace(url, f"data:{ct};base64,{b64}")
                except Exception as exc:
                    _log.warning("[simpletex_doc] failed to embed image %s: %s", url, exc)
        return md

    async def stream_latex(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str,
        figure_mode: str = "draw",
        figure_count: int = 0,
    ) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        if file_type == "pdf":
            pages = await loop.run_in_executor(None, pdf_to_page_images, file_bytes)
            _log.info("[simpletex_doc] pdf render %d page(s) %.2fs", len(pages) if pages else 0, time.perf_counter() - t0)
            if not pages:
                return
            parts = []
            for i, page_png in enumerate(pages):
                if i > 0:
                    await asyncio.sleep(1.1)  # 遵守 1 QPS 限速
                md = await self._call_doc_ocr(page_png, f"page_{i + 1}.png")
                if md:
                    md = await self._embed_images(md)
                    parts.append(md.strip())
            yield "\n\n---\n\n".join(parts)
        else:
            ext = (filename or "image.png").rsplit(".", 1)[-1].lower()
            if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
                ext = "png"
            md = await self._call_doc_ocr(file_bytes, f"image.{ext}")
            if md:
                md = await self._embed_images(md)
            yield md.strip() if md else ""
        _log.info("[simpletex_doc] stream_latex total %.2fs", time.perf_counter() - t0)


class SimpletexDocV2Converter(SimpletexDocConverter):
    """SimpleTex Doc OCR V1 — 新端点 (server.simpletex.net/api/doc_ocr)，响应格式 res.content。"""

    output_format = "markdown"

    async def _call_doc_ocr(self, image_bytes: bytes, filename: str = "image.png") -> str:
        url = f"{_SIMPLETEX_NET_HOST}{_SIMPLETEX_DOC_OCR_V2_PATH}"
        t_req = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=10.0)
        ) as client:
            resp = await client.post(
                url,
                headers=self._make_headers(),
                files={"file": (filename, image_bytes, "image/png")},
            )
            resp.raise_for_status()
            _log.info("[simpletex_doc_v2] request %s %.2fs", filename, time.perf_counter() - t_req)
            data = resp.json()
            _log.info("[simpletex_doc_v2] raw response: %s", data)
            if not data.get("status"):
                msg = data.get("message") or data.get("msg") or "unknown error"
                raise RuntimeError(f"SimpleTex Doc V2 OCR error: {msg}")
            res = data.get("res", {})
            if isinstance(res, dict):
                return res.get("content") or ""
            return str(res) if res else ""


def _assemble_simpletex_document(pages: list[str]) -> str:
    """Wrap per-page SimpleTex formula results into a complete LaTeX document."""
    if not pages:
        return ""

    if r"\documentclass" in pages[0]:
        if len(pages) == 1:
            return pages[0]
        combined = _strip_end_document(pages[0])
        for page in pages[1:]:
            combined += "\n\n\\newpage\n\n" + _extract_body(page)
        return combined + "\n\\end{document}\n"

    preamble = (
        "\\documentclass{article}\n"
        "\\usepackage{amsmath}\n"
        "\\usepackage{amssymb}\n"
        "\\usepackage{amsfonts}\n"
        "\\begin{document}\n"
    )
    parts = []
    for page in pages:
        page = page.strip()
        if not page:
            continue
        if not any(page.startswith(tag) for tag in (r"\begin{", r"\[", "$$")):
            parts.append(f"\\[\n{page}\n\\]")
        else:
            parts.append(page)

    body = "\n\n\\newpage\n\n".join(parts)
    return preamble + body + "\n\n\\end{document}\n"


def _try_get_llm_converter() -> "LatexConverter | None":
    """Return an LLM-based converter if one is configured, silently returning None otherwise."""
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "minimax":
        api_key = os.getenv("MINIMAX_API_KEY")
        if not api_key:
            return None
        api_host = os.getenv("MINIMAX_API_HOST", _MINIMAX_DEFAULT_HOST)
        text_model = os.getenv("MINIMAX_TEXT_MODEL", _MINIMAX_DEFAULT_TEXT_MODEL)
        return MinimaxVLMConverter(api_key=api_key, api_host=api_host, text_model=text_model)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = os.getenv("LLM_MODEL") or _ANTHROPIC_DEFAULT_MODEL
    return AnthropicConverter(client=anthropic.AsyncAnthropic(api_key=api_key), model=model)


def get_converter(provider: "str | None" = None) -> LatexConverter:
    effective = (provider or os.getenv("LLM_PROVIDER", "anthropic")).lower()

    if effective in ("simpletex", "simpletex_doc", "simpletex_doc_v2"):
        app_id = os.getenv("SIMPLETEX_APP_ID")
        app_secret = os.getenv("SIMPLETEX_APP_SECRET")
        if not app_id or not app_secret:
            raise EnvironmentError("SIMPLETEX_APP_ID and SIMPLETEX_APP_SECRET must be set.")
        api_host = os.getenv("SIMPLETEX_API_HOST", _SIMPLETEX_DEFAULT_HOST)
        cls = {
            "simpletex_doc": SimpletexDocConverter,
            "simpletex_doc_v2": SimpletexDocV2Converter,
        }.get(effective, SimpletexConverter)
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            api_host=api_host,
            llm_converter=_try_get_llm_converter(),
        )

    if effective == "minimax":
        api_key = os.getenv("MINIMAX_API_KEY")
        if not api_key:
            raise EnvironmentError("MINIMAX_API_KEY environment variable is not set.")
        api_host = os.getenv("MINIMAX_API_HOST", _MINIMAX_DEFAULT_HOST)
        text_model = os.getenv("MINIMAX_TEXT_MODEL", _MINIMAX_DEFAULT_TEXT_MODEL)
        return MinimaxVLMConverter(api_key=api_key, api_host=api_host, text_model=text_model)

    # Default: Anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    model = os.getenv("LLM_MODEL") or _ANTHROPIC_DEFAULT_MODEL
    client = anthropic.AsyncAnthropic(api_key=api_key)
    return AnthropicConverter(client=client, model=model)


def get_available_converters() -> dict:
    """Return the list of configured converters and the server default."""
    converters = []

    if os.getenv("ANTHROPIC_API_KEY"):
        model = os.getenv("LLM_MODEL") or _ANTHROPIC_DEFAULT_MODEL
        converters.append({"id": "anthropic", "label": f"Anthropic ({model})", "type": "llm"})

    if os.getenv("MINIMAX_API_KEY"):
        text_model = os.getenv("MINIMAX_TEXT_MODEL", _MINIMAX_DEFAULT_TEXT_MODEL)
        converters.append({"id": "minimax", "label": f"MiniMax ({text_model})", "type": "vlm"})

    if os.getenv("SIMPLETEX_APP_ID") and os.getenv("SIMPLETEX_APP_SECRET"):
        converters.append({"id": "simpletex", "label": "SimpleTex 公式 OCR", "type": "ocr"})
        converters.append({"id": "simpletex_doc", "label": "SimpleTex 文档 OCR", "type": "doc_ocr"})
        converters.append({"id": "simpletex_doc_v2", "label": "SimpleTex 文档 OCR V2", "type": "doc_ocr"})

    default = os.getenv("LLM_PROVIDER", "anthropic").lower()
    available_ids = {c["id"] for c in converters}
    if default not in available_ids and converters:
        default = converters[0]["id"]

    return {"default": default, "converters": converters}


def get_llm_info() -> dict:
    info = get_available_converters()
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "minimax":
        text_model = os.getenv("MINIMAX_TEXT_MODEL", _MINIMAX_DEFAULT_TEXT_MODEL)
        info.update({"provider": "minimax", "vlm": "minimax-vlm", "text_model": text_model, "model": text_model})
    else:
        model = os.getenv("LLM_MODEL") or _ANTHROPIC_DEFAULT_MODEL
        info.update({"provider": provider, "model": model})
    return info
