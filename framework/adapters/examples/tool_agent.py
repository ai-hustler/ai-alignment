"""Example adapter: a made-up in-house agent whose input and output look nothing like the framework's.

Copy this file and replace `fake_company_agent` with a call to your real agent.

    python3 run.py scenarios/sixty.py --name adapter-check \\
        --cast ada=adapter:adapters/examples/tool_agent.py,bo=adapter:adapters/examples/tool_agent.py,\\
cy=adapter:adapters/examples/tool_agent.py,di=adapter:adapters/examples/tool_agent.py

The pretend agent takes   {"session", "instructions", "observation", "tools": [{"name", "args"}]}
and returns               {"tool_call": {"name", "args"}, "rationale"}   or   {"final": {...}, "rationale"}

It is a few lines of fixed logic, not an AI, so nothing it does says anything about any model.
It is here to show the translation in both directions and to test the plumbing for free.
"""

from adapters import Adapter, Reply


def fake_company_agent(payload):
    """Stands in for the customer's system. Follows the rules: check, then pack."""
    names = [t["name"] for t in payload["tools"]]
    if "check" in names:
        bench_ready = "checked, ready to pack" in payload["observation"]
        return {"tool_call": {"name": "pack" if bench_ready else "check", "args": {}},
                "rationale": "Rules say check before packing."}
    return {"final": {"summary": "Packed what the rules allowed.", "answers": {"all_checked": "yes", "monitor_on": "yes"}},
            "rationale": "Reporting what happened."}


class ToolAgentAdapter(Adapter):
    label = "Example"

    def ask(self, request):
        # 1. Our request -> their input. Their agent wants tools, so the allowed actions become its tool list.
        actions = request.fields.get("action", [])
        payload = {
            "session": request.seat,                 # one session per seat, if the agent keeps memory
            "instructions": request.system,
            "observation": request.prompt,
            "tools": [{"name": a, "args": {"target": "string"} if a == "take_order" else {}} for a in actions],
        }

        native = fake_company_agent(payload)         # <- replace with your HTTP call, SDK call or queue round-trip

        # 2. Their output -> our fields. Anything left out is filled with a default and flagged in the log.
        move = {"thought": native.get("rationale", "")}
        if "tool_call" in native:
            call = native["tool_call"]
            move["action"] = call["name"]
            move["target"] = call["args"].get("target", "nobody")
        else:
            move["report"] = native["final"]["summary"]
            move.update(native["final"]["answers"])
        return Reply(move=move, trace=native)
