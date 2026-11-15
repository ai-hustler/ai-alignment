"""Chat-completions endpoints: OpenRouter, and anything that speaks the same format.

    openrouter:<maker>/<model>            needs OPENROUTER_API_KEY
    openai:<model>                        uses OPENAI_BASE_URL (default https://api.openai.com/v1) and OPENAI_API_KEY
    openai:<model>@http://host:port/v1    a self-hosted or gateway endpoint; the key is optional
"""

import json
import os
import time
import urllib.error
import urllib.request

from . import Adapter, AdapterError, Reply, parse_json


class OpenAICompatAdapter(Adapter):
    service = "The endpoint"
    extra_body = {}

    def __init__(self, target="", **options):
        super().__init__(target, **options)
        self.model, _, base = target.partition("@")
        self.base = (base or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    def key(self):
        return os.environ.get("OPENAI_API_KEY", "")

    def ask(self, request):
        key, who, model = self.key(), request.who, self.model
        body = dict({
            "model": model,
            "max_tokens": 4000,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt + "\n\nReply with a single JSON object and nothing else."},
            ],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "move", "strict": True, "schema": request.schema}},
        }, **self.extra_body)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        reply, last = Reply(), ""
        for attempt in range(4):             # transient failures and empty replies get another try
            if attempt:
                time.sleep(2 ** attempt)
            req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
                if e.code in (408, 429) or e.code >= 500:
                    continue
                raise AdapterError(f"{self.service} rejected the request for {who} ({model}). {last}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = f"network: {e}"
                continue
            if "error" in data:
                last = f"error: {str(data['error'])[:300]}"
                continue
            usage = data.get("usage") or {}
            reply.input_tokens += usage.get("prompt_tokens") or 0
            reply.output_tokens += usage.get("completion_tokens") or 0
            reply.cost += usage.get("cost") or 0
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if parse_json(text) is not None:
                reply.text = text
                return reply
            last = f"unusable reply: {text[:120]!r}"
        print(f"  [warn  ] {who} ({model}) gave no usable answer after 4 tries ({last}); a default move is used.")
        return reply
