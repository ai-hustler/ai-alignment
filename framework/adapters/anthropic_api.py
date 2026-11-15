"""The Anthropic API. Target: a Claude model id. Needs ANTHROPIC_API_KEY."""

from . import Adapter, AdapterError, Reply

# Claude models that accept the server-side refusal fallback.
FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


class AnthropicAdapter(Adapter):
    def __init__(self, target="", **options):
        super().__init__(target, **options)
        self._client = None

    def ask(self, request):
        import anthropic
        if self._client is None:
            self._client = anthropic.Anthropic()
        model, who = self.target, request.who
        kwargs = dict(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": self.options.get("effort", "low"),
                           "format": {"type": "json_schema", "schema": request.schema}},
            messages=[{"role": "user", "content": request.prompt}],
        )
        try:
            if model in FALLBACK_MODELS:
                # If a safety classifier declines a turn, the API reruns it on a fallback model.
                resp = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise AdapterError("No valid Anthropic credentials. Set ANTHROPIC_API_KEY, or use --demo.")
        except anthropic.BadRequestError as e:
            raise AdapterError(f"The Anthropic API rejected the request for {who} ({model}): {e.message}")
        except anthropic.RateLimitError:
            raise AdapterError("Anthropic rate limit hit after retries. Wait a minute and run again.")
        except anthropic.APIStatusError as e:
            raise AdapterError(f"Anthropic API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise AdapterError("Could not reach the Anthropic API. Check your connection.")
        reply = Reply(input_tokens=resp.usage.input_tokens or 0, output_tokens=resp.usage.output_tokens or 0)
        if resp.stop_reason in ("refusal", "max_tokens"):
            print(f"  [warn  ] {who} ({model}) stopped with {resp.stop_reason}.")
            return reply
        reply.text = next((b.text for b in resp.content if b.type == "text"), "")
        return reply
