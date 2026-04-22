import os
from typing import AsyncIterator

import anthropic

from latex_helper.prompts import SYSTEM_PROMPT
from latex_helper.utils import prepare_content_blocks

_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
_MINIMAX_DEFAULT_MODEL = "MiniMax-M2.7"
_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic/v1"


class LatexConverter:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        use_native_pdf: bool,
    ) -> None:
        self.client = client
        self.model = model
        self.use_native_pdf = use_native_pdf

    async def stream_latex(
        self, file_bytes: bytes, file_type: str, filename: str
    ) -> AsyncIterator[str]:
        blocks = prepare_content_blocks(file_bytes, file_type, filename, self.use_native_pdf)
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": blocks}],
        ) as stream:
            async for text in stream.text_stream:
                yield text


def get_converter() -> LatexConverter:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    model = os.getenv("LLM_MODEL") or None

    if provider == "minimax":
        api_key = os.getenv("MINIMAX_API_KEY")
        if not api_key:
            raise EnvironmentError("MINIMAX_API_KEY environment variable is not set.")
        base_url = os.getenv("MINIMAX_BASE_URL", _MINIMAX_BASE_URL)
        client = anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key)
        return LatexConverter(client, model or _MINIMAX_DEFAULT_MODEL, use_native_pdf=False)

    # Default: Anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    client = anthropic.AsyncAnthropic(api_key=api_key)
    return LatexConverter(client, model or _ANTHROPIC_DEFAULT_MODEL, use_native_pdf=True)


def get_llm_info() -> dict:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    model = os.getenv("LLM_MODEL")
    if provider == "minimax":
        return {"provider": "minimax", "model": model or _MINIMAX_DEFAULT_MODEL}
    return {"provider": "anthropic", "model": model or _ANTHROPIC_DEFAULT_MODEL}
