"""OpenRouter. Target: a model id such as "openai/<model>". Needs OPENROUTER_API_KEY."""

import os

from . import AdapterError
from .openai_compat import OpenAICompatAdapter


class OpenRouterAdapter(OpenAICompatAdapter):
    service = "OpenRouter"
    extra_body = {"reasoning": {"effort": "low"}, "usage": {"include": True}}

    def __init__(self, target="", **options):
        super().__init__(target, **options)
        self.model, self.base = target, "https://openrouter.ai/api/v1"

    def key(self):
        key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPEN_ROUTER_API_KEY")
        if not key:
            raise AdapterError(f"{self.model} needs OPENROUTER_API_KEY to be set (or pass --env path/to/.env).")
        return key
