import base64
import os
from abc import ABC, abstractmethod
from typing import AsyncIterator

import anthropic
import httpx

from latex_helper.prompts import SYSTEM_PROMPT
from latex_helper.utils import pdf_to_page_images, prepare_content_blocks

_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
_MINIMAX_DEFAULT_HOST = "https://api.minimaxi.com"
_MINIMAX_VLM_PATH = "/v1/coding_plan/vlm"


class LatexConverter(ABC):
    @abstractmethod
    async def stream_latex(
        self, file_bytes: bytes, file_type: str, filename: str
    ) -> AsyncIterator[str]: ...


class AnthropicConverter(LatexConverter):
    def __init__(self, client: anthropic.AsyncAnthropic, model: str) -> None:
        self.client = client
        self.model = model

    async def stream_latex(
        self, file_bytes: bytes, file_type: str, filename: str
    ) -> AsyncIterator[str]:
        blocks = prepare_content_blocks(file_bytes, file_type, filename, use_native_pdf=True)
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
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
        async with httpx.AsyncClient(timeout=120.0) as client:
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
        self, file_bytes: bytes, file_type: str, filename: str
    ) -> AsyncIterator[str]:
        if file_type == "pdf":
            pages = pdf_to_page_images(file_bytes)
            for i, page_png in enumerate(pages):
                b64 = base64.standard_b64encode(page_png).decode("ascii")
                result = await self._call_vlm(
                    prompt=SYSTEM_PROMPT,
                    image_url=f"data:image/png;base64,{b64}",
                )
                if i > 0:
                    yield "\n\n% --- Page break ---\n\n"
                yield result
        else:
            # Single image
            ext = (filename or "image.png").rsplit(".", 1)[-1].lower()
            fmt_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}
            fmt = fmt_map.get(ext, "png")
            b64 = base64.standard_b64encode(file_bytes).decode("ascii")
            result = await self._call_vlm(
                prompt=SYSTEM_PROMPT,
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
