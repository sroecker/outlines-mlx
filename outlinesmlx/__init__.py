"""Outlines is a Generative Model Programming Framework."""
import outlinesmlx.generate
import outlinesmlx.models
import outlinesmlx.text.generate
from outlines.base import vectorize
from outlines.caching import clear_cache, disable_cache, get_cache
from outlines.function import Function
from outlines.prompts import prompt

__all__ = [
    "clear_cache",
    "disable_cache",
    "get_cache",
    "prompt",
    "vectorize",
]
