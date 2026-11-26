"""The one price table. Every cost estimate and meter reads it.

Source: https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-25.
USD per million tokens. Cache writes are the 5-minute rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(input=8.00, output=40.00, cache_write=10.00, cache_read=0.20),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
    "claude-opus-5": Price(input=5.00, output=25.00, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
}


def price_for(model: str) -> Price:
    if model in PRICES:
        return PRICES[model]
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    raise KeyError(f"no price for model {model!r}; add it to goodfeeling/ai/pricing.py")


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    price = price_for(model)
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read * price.cache_read
        + cache_write * price.cache_write
    ) / 1_000_000


def image_tokens(width: int, height: int) -> int:
    """Anthropic's published approximation for image input: width * height / 750."""
    return math.ceil(width * height / 750)


def text_tokens(text: str) -> int:
    """Conservative estimate for the current tokenizer (about 3 characters per token)."""
    return math.ceil(len(text) / 3)
