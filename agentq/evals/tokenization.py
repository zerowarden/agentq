"""Pinned, evaluation-only token accounting of the actual rendered text."""

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

from agentq.core import ContractError

TOKENIZER_VERSION = "0.12.0"
TOKENIZER_ENCODING = "cl100k_base"
TOKENIZER_ID = f"tiktoken=={TOKENIZER_VERSION}/{TOKENIZER_ENCODING}"


@lru_cache(maxsize=1)
def encoding():
    try:
        installed = version("tiktoken")
    except PackageNotFoundError as exc:
        raise ContractError("token accounting requires uv sync --group eval") from exc
    if installed != TOKENIZER_VERSION:
        raise ContractError(
            f"tokenizer changed: expected {TOKENIZER_ID}, found {installed}"
        )
    import tiktoken

    return tiktoken.get_encoding(TOKENIZER_ENCODING)


def count_tokens(text: str) -> int:
    # Source may contain strings which look like special tokens; they are text.
    return len(encoding().encode_ordinary(text))
