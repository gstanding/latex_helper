import base64
import os
import re
from abc import ABC, abstractmethod
from typing import AsyncIterator

import anthropic
import httpx

from latex_helper.prompts import get_system_prompt
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


class MinimaxVLMConverter(LatexConverter):
    """Calls MiniMax VLM REST API directly (same endpoint used by the MiniMax MCP server)."""

    def __init__(self, api_key: str, api_host: str) -> None:
        self.api_key = api_key
        self.api_host = api_host.rstrip("/")

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


def get_converter() -> LatexConverter:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

    if provider == "minimax":
        api_key = os.getenv("MINIMAX_API_KEY")
        if not api_key:
            raise EnvironmentError("MINIMAX_API_KEY environment variable is not set.")
        api_host = os.getenv("MINIMAX_API_HOST", _MINIMAX_DEFAULT_HOST)
        return MinimaxVLMConverter(api_key=api_key, api_host=api_host)

    # Default: Anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    model = os.getenv("LLM_MODEL") or _ANTHROPIC_DEFAULT_MODEL
    client = anthropic.AsyncAnthropic(api_key=api_key)
    return AnthropicConverter(client=client, model=model)


def get_llm_info() -> dict:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "minimax":
        return {"provider": "minimax", "model": "minimax-vlm"}
    model = os.getenv("LLM_MODEL") or _ANTHROPIC_DEFAULT_MODEL
    return {"provider": "anthropic", "model": model}
