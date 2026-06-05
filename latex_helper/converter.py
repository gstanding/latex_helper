import base64
import hashlib
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


def _strip_end_document(latex: str) -> str:
    """Remove trailing \\end{document} so pages can be appended."""
    return re.sub(r'\s*\\end\{document\}\s*$', '', latex.rstrip())


def _extract_body(latex: str) -> str:
    """Return only the content between \\begin{document} and \\end{document}."""
    m = re.search(r'\\begin\{document\}(.*?)(?:\\end\{document\}|$)', latex, re.DOTALL)
    if m:
        return m.group(1).strip()
    return latex.strip()

_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
_MINIMAX_DEFAULT_HOST = "https://api.minimaxi.com"
_MINIMAX_VLM_PATH = "/v1/coding_plan/vlm"
_MINIMAX_DEFAULT_TEXT_MODEL = "MiniMax-M2.7"
_SIMPLETEX_DEFAULT_HOST = "https://server.simpletex.cn"
_SIMPLETEX_OCR_PATH = "/api/latex_ocr"


class LatexConverter(ABC):
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
        system_prompt = get_system_prompt(figure_mode, figure_count)
        blocks = prepare_content_blocks(file_bytes, file_type, filename, use_native_pdf=True)
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": blocks}],
        ) as stream:
            async for text in stream.text_stream:
                yield text


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
    """VLM endpoint for image→LaTeX; Anthropic-compatible text model for fix."""

    def __init__(self, api_key: str, api_host: str, text_model: str) -> None:
        self.api_key = api_key
        self.api_host = api_host.rstrip("/")
        self.text_model = text_model
        # MiniMax M-series is Anthropic-API-compatible; base_url points at their v1 prefix.
        self._text_client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=f"{self.api_host}/v1",
        )

    async def _call_vlm(self, prompt: str, image_url: str) -> str:
        url = f"{self.api_host}{_MINIMAX_VLM_PATH}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "MM-API-Source": "Minimax-MCP",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=10.0)) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"prompt": prompt, "image_url": image_url},
            )
            resp.raise_for_status()
            data = resp.json()
            base_resp = data.get("base_resp", {})
            if base_resp.get("status_code", 0) != 0:
                raise RuntimeError(
                    f"MiniMax API error {base_resp.get('status_code')}: "
                    f"{base_resp.get('status_msg', 'unknown error')}"
                )
            return data.get("content", "")

    async def stream_latex(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str,
        figure_mode: str = "draw",
        figure_count: int = 0,
    ) -> AsyncIterator[str]:
        system_prompt = get_system_prompt(figure_mode, figure_count)
        if file_type == "pdf":
            pages = pdf_to_page_images(file_bytes)
            if not pages:
                return
            first_b64 = base64.standard_b64encode(pages[0]).decode("ascii")
            first_result = await self._call_vlm(
                prompt=system_prompt,
                image_url=f"data:image/png;base64,{first_b64}",
            )
            if len(pages) == 1:
                yield first_result
                return
            # Multi-page: strip \end{document} from page 1, append subsequent page bodies
            yield _strip_end_document(first_result)
            for page_png in pages[1:]:
                b64 = base64.standard_b64encode(page_png).decode("ascii")
                result = await self._call_vlm(
                    prompt=system_prompt,
                    image_url=f"data:image/png;base64,{b64}",
                )
                yield f"\n\n\\newpage\n\n{_extract_body(result)}"
            yield "\n\\end{document}\n"
        else:
            # Single image
            ext = (filename or "image.png").rsplit(".", 1)[-1].lower()
            fmt_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}
            fmt = fmt_map.get(ext, "png")
            b64 = base64.standard_b64encode(file_bytes).decode("ascii")
            result = await self._call_vlm(
                prompt=system_prompt,
                image_url=f"data:image/{fmt};base64,{b64}",
            )
            yield result


    async def stream_fix(self, latex: str, log: str) -> AsyncIterator[str]:
        async with self._text_client.messages.stream(
            model=self.text_model,
            max_tokens=8192,
            system=_FIX_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_fix_message(latex, log)}],
        ) as stream:
            async for text in stream.text_stream:
                yield text


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

    def _make_headers(self) -> dict:
        random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
        timestamp = str(int(time.time() * 1000))
        sign_src = f"app_id={self.app_id}&random_str={random_str}&timestamp={timestamp}&app_secret={self.app_secret}"
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


def _assemble_simpletex_document(pages: list[str]) -> str:
    """Wrap per-page SimpleTex formula results into a complete LaTeX document."""
    if not pages:
        return ""

    # If the first page already has a document class, merge pages normally
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
        # Bare formula (not wrapped in an environment) → display math
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

    if effective == "simpletex":
        app_id = os.getenv("SIMPLETEX_APP_ID")
        app_secret = os.getenv("SIMPLETEX_APP_SECRET")
        if not app_id or not app_secret:
            raise EnvironmentError("SIMPLETEX_APP_ID and SIMPLETEX_APP_SECRET must be set.")
        api_host = os.getenv("SIMPLETEX_API_HOST", _SIMPLETEX_DEFAULT_HOST)
        return SimpletexConverter(
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
        converters.append({"id": "simpletex", "label": "SimpleTex OCR", "type": "ocr"})

    default = os.getenv("LLM_PROVIDER", "anthropic").lower()
    # If default provider isn't available, fall back to first available
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
