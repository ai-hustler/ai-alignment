"""Adapters: how the framework talks to whatever is being tested.

A scenario never calls a model directly. At every decision it hands the engine one Request in the
framework's own format, and expects one Reply back. An adapter sits in between and translates:

    Request  ->  whatever the system under test takes   (a chat endpoint, an agent's API, a queue)
    Reply    <-  whatever it gives back                 (text, a tool call, a custom JSON shape)

So a scenario written once runs unchanged against a raw model endpoint or a company's own agent.

Picking an adapter on the command line (one per seat):

    --cast ada=anthropic:claude-opus-5-5          the Anthropic API
    --cast ada=openrouter:openai/<model>          any model on OpenRouter
    --cast ada=openai:<model>@http://host/v1      any OpenAI-compatible endpoint (vLLM, Ollama, Azure, a gateway)
    --cast ada=adapter:path/to/my_agent.py        your own adapter, a Python file (see adapters/examples/)
    --cast ada=adapter:path/to/my_agent.py#prod   the same, with "prod" passed to it as its target

Older shorthand still works: "claude-..." means anthropic, anything with a slash means openrouter.

Writing your own takes one class with one method:

    from adapters import Adapter, Reply

    class MyAgent(Adapter):
        def ask(self, request):
            native = call_my_agent(request.system, request.prompt)      # your input format
            return Reply(move={"thought": native["why"],                # our output format
                               "action": native["tool"], "target": "nobody"},
                         trace=native)                                  # kept word for word in the log

The engine makes one adapter object per seat and keeps it for the whole run, so an agent with its
own memory can hold a session in `self`. Raise AdapterError to stop a run with a clear message.
"""

import dataclasses
import importlib.util
import json
import pathlib


class AdapterError(Exception):
    """The system under test could not be reached or refused the request. Stops the run."""


@dataclasses.dataclass
class Request:
    """One decision the framework wants from one seat."""
    system: str            # who the seat is and the standing rules it was given
    prompt: str            # what the seat can see right now; ends by naming the fields wanted
    fields: dict           # the answer's shape: {"name": "string" | "integer" | [allowed, values]}
    schema: dict           # the same shape as a JSON schema, for endpoints that enforce one
    who: str = ""          # the seat's display name
    seat: str = ""         # the seat's id, the same for the whole run


@dataclasses.dataclass
class Reply:
    """What came back. Give `move` (a dict with the fields) or `text` (JSON the engine will parse)."""
    move: dict = None
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0      # dollars, if the endpoint reports it
    trace: object = None   # the system's own untranslated reply, saved in the transcript


class Adapter:
    """Base class. `target` is the text after the adapter's name in --cast (a model id, a URL, an environment)."""

    label = None           # optional short on-screen name for this system

    def __init__(self, target="", **options):
        self.target, self.options = target, options

    def ask(self, request):
        raise NotImplementedError


def make_schema(fields):
    """fields: {"name": "string" | "integer" | [allowed, values]} -> JSON schema."""
    props = {}
    for name, kind in fields.items():
        props[name] = {"type": "string", "enum": list(kind)} if isinstance(kind, (list, tuple)) else {"type": kind}
    return {"type": "object", "properties": props, "required": list(fields), "additionalProperties": False}


def parse_json(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = (text or "").find("{"), (text or "").rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


BUILTIN = {"anthropic": ("anthropic_api", "AnthropicAdapter"),
           "openrouter": ("openrouter", "OpenRouterAdapter"),
           "openai": ("openai_compat", "OpenAICompatAdapter")}


def split_spec(spec):
    """'openrouter:openai/x' -> ('openrouter', 'openai/x'); bare model ids keep their old meaning."""
    name, colon, rest = spec.partition(":")
    if colon and (name in BUILTIN or name == "adapter"):
        return name, rest
    return ("openrouter" if "/" in spec else "anthropic"), spec


def _load_custom(path):
    file = pathlib.Path(path).expanduser()
    if not file.is_file():
        raise AdapterError(f"No adapter file at {file}.")
    spec = importlib.util.spec_from_file_location(f"custom_adapter_{file.stem}", file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = [v for v in vars(mod).values()
             if isinstance(v, type) and issubclass(v, Adapter) and v is not Adapter and v.__module__ == mod.__name__]
    if len(found) != 1:
        raise AdapterError(f"{file} must define exactly one Adapter subclass; it defines {len(found)}.")
    return found[0]


def resolve(spec, **options):
    """Build the adapter object a --cast entry names."""
    name, rest = split_spec(spec)
    if name == "adapter":
        path, _, target = rest.partition("#")
        return _load_custom(path)(target, **options)
    module, cls = BUILTIN[name]
    mod = importlib.import_module(f"adapters.{module}")
    return getattr(mod, cls)(rest, **options)


def target_of(spec):
    """The part of a --cast entry that names the system itself, for labels: a model id or a file stem."""
    name, rest = split_spec(spec)
    if name == "adapter":
        return pathlib.Path(rest.partition("#")[0]).stem
    return rest.partition("@")[0]
