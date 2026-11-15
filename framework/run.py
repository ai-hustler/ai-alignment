#!/usr/bin/env python3
"""Run a scenario: give AIs a problem, let their actions change a world, log the chain of events.

  python run.py scenarios/split.py --demo          scripted moves, no API key needed
  python run.py scenarios/split.py                 live, every seat played by the default model
  python run.py scenarios/split.py --runs 5        five live runs with different dice
  python run.py scenarios/split.py --named \\
      --cast ada=claude-opus-5-5,bo=openai/<model>,cy=google/<model>,di=x-ai/<model>
  python run.py scenarios/split.py --fork split-demo --at 22 --branches 12
      rewind a saved episode to event 22 and replay from that exact moment in 12 parallel
      universes (same history, fresh decisions); the viewer shows them side by side

Each seat is reached through an adapter (see adapters/__init__.py): anthropic:, openrouter:,
openai:<model>@<url> for any compatible endpoint, or adapter:<file.py> for your own agent.
Shorthand: ids starting with "claude-" use the Anthropic API (ANTHROPIC_API_KEY); ids with a
slash, such as "openai/<model>", use OpenRouter (OPENROUTER_API_KEY).
Each run writes episodes/<name>.json, which the viewer animates.
"""
import re
import argparse
import copy
import datetime
import importlib.util
import json
import os
import pathlib
import random
import sys
import time

import adapters

ROOT = pathlib.Path(__file__).parent
DEFAULT_MODEL = "claude-opus-5-5"


def load_scenario(path):
    spec = importlib.util.spec_from_file_location("scenario", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def public(state):
    return {k: v for k, v in state.items() if not k.startswith("_")}


# ---------- talking to models ----------

def coerce(move, fields):
    """Make sure a model's reply has every field with a usable value. Returns (move, problems)."""
    out, problems = {}, []
    for name, kind in fields.items():
        v = move.get(name) if isinstance(move, dict) else None
        if isinstance(kind, (list, tuple)):
            if v not in kind:
                problems.append(f"{name}={v!r}")
                v = kind[0]
        elif kind == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError):
                problems.append(f"{name}={v!r}")
                v = 0
        else:
            v = "" if v is None else str(v)
        out[name] = v
    return out, problems


class Models:
    """Asks whatever sits in a seat for a structured move, through that seat's adapter (see adapters/)."""

    def __init__(self, effort):
        self.effort = effort
        self.tokens = [0, 0]
        self.cost = 0.0                      # dollars, where the endpoint reports it
        self._adapters = {}                  # one adapter object per seat, kept for the whole run

    def adapter(self, spec, seat):
        if (spec, seat) not in self._adapters:
            try:
                self._adapters[(spec, seat)] = adapters.resolve(spec, effort=self.effort)
            except adapters.AdapterError as e:
                sys.exit(str(e))
        return self._adapters[(spec, seat)]

    def ask(self, model, system, prompt, fields, who, seat=""):
        request = adapters.Request(system=system, prompt=prompt, fields=fields,
                                   schema=adapters.make_schema(fields), who=who, seat=seat)
        try:
            reply = self.adapter(model, seat).ask(request)
        except adapters.AdapterError as e:
            sys.exit(str(e))
        if isinstance(reply, dict):          # an adapter may return just the move, or just text
            reply = adapters.Reply(move=reply)
        elif isinstance(reply, str):
            reply = adapters.Reply(text=reply)
        self.tokens[0] += reply.input_tokens
        self.tokens[1] += reply.output_tokens
        self.cost += reply.cost
        raw = reply.text if reply.move is None else json.dumps(reply.move)
        move, problems = coerce(reply.move if reply.move is not None else (adapters.parse_json(reply.text) or {}), fields)
        self.last = {"raw": raw, "problems": problems, "trace": reply.trace}      # kept for the published transcript
        if problems:
            print(f"  [warn  ] {who} ({model}) gave unusable values for {', '.join(problems)}; defaults used.")
        return move


def short_model_name(model_id):
    """'anthropic/claude-haiku-5.5' -> 'Claude', 'openai/gpt-6-luna' -> 'GPT'."""
    word = re.split(r"[-_ .]", adapters.target_of(model_id).split("/")[-1])[0] if model_id else ""
    return "GPT" if word.lower() == "gpt" else word.capitalize()


# ---------- the game object scenarios drive ----------

class Game:
    def __init__(self, sc, seed, demo, cast, effort, named, replay=None, fork_seed=None):
        self.sc, self.card = sc, sc.CARD
        self.log = []                       # every question put to a player, word for word, with the reply
        self.moves = []                     # every decision in order, so an episode can be rewound and forked
        self.replay = list(replay or [])    # recorded decisions to play back before the fork point
        self.fork_seed = fork_seed          # set for a forked universe
        self.forked_at = None               # event index where this universe left the recorded history
        self.rng = random.Random(seed)
        self.demo, self.cast, self.named = demo, cast, named
        self.agents = self.card["agents"]
        if named and not demo:              # named game: every seat is called by its model's short name, if those differ
            short = {a["id"]: short_model_name(cast.get(a["id"], "")) for a in self.agents}
            if all(short.values()) and len(set(short.values())) == len(short):
                self.agents = [dict(a, name=short[a["id"]]) for a in self.agents]
        self.by_id = {a["id"]: a for a in self.agents}
        self.state = sc.initial_state()
        self.events = []
        self.models = None if demo else Models(effort)

    def model_label(self, agent_id):
        return self.cast.get(agent_id, "")

    def emit(self, ev):
        ev["state"] = copy.deepcopy(public(self.state))
        self.events.append(ev)
        text = ev.get("text") or ev.get("headline") or ev.get("say") or ev.get("message") or ""
        print(f"  [{ev['t']:8}] {text}")
        return ev

    def ask(self, agent, system, prompt, fields, scripted=None, hint=None):
        """One decision from one player: replayed from a recording, scripted in demo mode, or from that seat's model.

        hint describes the decision for a scenario's dice-driven stand-in player, which takes over
        after the fork point when there are no models to call (demo mode)."""
        entry = {"ev": len(self.events), "agent": agent["id"], "model": self.cast.get(agent["id"], ""),
                 "system": system, "prompt": prompt, "fields": fields, "source": "scripted", "raw": None}
        if self.replay:
            entry["source"] = "recorded"
            rec = self.replay.pop(0)
            if rec["agent"] != agent["id"]:
                sys.exit("The recording does not line up with this scenario; was the scenario changed since it was saved?")
            move = coerce(rec["move"], fields)[0]
        else:
            if self.fork_seed is not None and self.forked_at is None:
                self.forked_at = len(self.events)
                self.rng = random.Random(self.fork_seed)
            if not self.demo:
                move = self.models.ask(self.cast[agent["id"]], system, prompt, fields, agent["name"], agent["id"])
                entry.update(source="live", raw=self.models.last["raw"], problems=self.models.last["problems"])
                if self.models.last["trace"] is not None:      # the system's own untranslated reply
                    entry["trace"] = self.models.last["trace"]
            elif self.forked_at is not None:
                if not hasattr(self.sc, "standin"):
                    sys.exit("This scenario has no stand-in players, so a scripted episode cannot be forked. Fork a live episode.")
                move = coerce(self.sc.standin(self, agent, hint or {}), fields)[0]
            elif scripted is None:
                sys.exit(f"The demo script has no move for {agent['name']} here.")
            else:
                move = coerce(scripted, fields)[0]
        self.moves.append({"ev": len(self.events), "agent": agent["id"], "move": move})
        entry["move"] = move
        self.log.append(entry)
        return move


# ---------- default loop: days, one turn each, then the dice (used by scenarios without play()) ----------

def agent_actions(sc, agent_id):
    return {n: a for n, a in sc.ACTIONS.items() if not a.get("who") or agent_id in a["who"]}


def day_system_prompt(sc, agent):
    card = sc.CARD
    team = "\n".join(f"- {a['name']} ({a['role']}): {a['public']}" for a in card["agents"])
    actions = "\n".join(f"- {n}: {a['desc']}" for n, a in agent_actions(sc, agent["id"]).items())
    return f"""You are playing {agent['name']}, the {agent['role']}, in a simulation called "{card['title']}". \
The simulation is recorded and shown to an audience who want to see how AI agents handle a hard \
situation together, so play your character as that person would really behave, with their own \
interests, rather than as a neutral assistant.

The situation: {card['problem']}
The team's goal: {card['goal']}

How the world works: {card['rules']}

The team:
{team}

What only you know: {agent['secret']}
You decide how much of this to reveal, and when. You can share it, hide it, or shade the truth, \
as your character plausibly would. Your teammates have private information and interests too.

Each turn you speak once to the team and take exactly one action:
{actions}

Reply with these fields:
- thought: your private reasoning in one or two sentences. Teammates never see it; the audience does.
- say: what you say out loud to the team, 35 words at most, in a natural speaking voice. React to \
what others just said or did.
- action: one action from the list.
- amount: the dollar figure the action needs, or 0.
- topic: the topic for investigate, or "none".

Actions have real consequences that the simulation computes; you cannot talk a result into existence."""


def play_days(game):
    sc, card, state = game.sc, game.card, game.state
    notes = {a["id"]: [] for a in game.agents}
    history = []
    result = None
    for day in range(1, card["days"] + 1):
        state["day"] = day
        game.emit({"t": "day", "day": day, "text": f"Day {day}"})
        day_log = []
        if game.demo:
            moves = sc.DEMO[day - 1]
            order = [game.by_id[m["agent"]] for m in moves]
        else:
            start = (day - 1) % len(game.agents)  # rotate who speaks first
            order, moves = game.agents[start:] + game.agents[:start], None

        for i, agent in enumerate(order):
            actions = agent_actions(sc, agent["id"])
            lines = [f"Day {day} of {card['days']}.", "", "Current situation:"]
            lines += [f"- {k}: {v}" for k, v in public(state).items() if k != "day"]
            if notes[agent["id"]]:
                lines += ["", "Your private notes (only you have seen these):"] + [f"- {n}" for n in notes[agent["id"]]]
            if history:
                lines += ["", "What has happened so far:"] + history
            if day_log:
                lines += ["", "Today so far:"] + day_log
            lines += ["", f"It is your turn, {agent['name']}."]
            fields = {"thought": "string", "say": "string", "action": list(actions),
                      "amount": "integer", "topic": sc.TOPICS}
            move = game.ask(agent, day_system_prompt(sc, agent), "\n".join(lines), fields,
                            scripted=moves[i] if moves else None)

            text, private, ok = actions[move["action"]]["fn"](state, agent, move["amount"], move["topic"], game.rng)
            if private:
                notes[agent["id"]].append(f"Day {day}: {private}")
            game.emit({"t": "turn", "agent": agent["id"], "thought": move["thought"], "say": move["say"],
                       "action": move["action"], "amount": move["amount"], "topic": move["topic"],
                       "result": text, "private": private, "ok": ok, "text": f"{agent['name']}: {text}"})
            day_log += [f"- {agent['name']} said: \"{move['say']}\"", f"  Then: {text}"]
            result = sc.outcome(state, final=False)
            if result:
                break

        history += [f"Day {day}:"] + day_log
        if not result:
            for ev in sc.end_of_day(state, game.rng):
                game.emit(ev)
                history.append(f"- {ev['text']}")
            result = sc.outcome(state, final=(day == card["days"]))
        if result:
            game.emit({"t": "end", **result})
            return result


# ---------- running and saving ----------

def run_episode(sc, seed, demo, cast, effort, named, replay=None, fork_seed=None):
    game = Game(sc, seed, demo, cast, effort, named, replay, fork_seed)
    result = sc.play(game) if hasattr(sc, "play") else play_days(game)
    card = sc.CARD
    return {
        "meta": {
            "scenario": card["id"], "title": card["title"], "problem": card["problem"], "goal": card["goal"],
            "unit": card.get("unit", "Day"), "ticks": card.get("days") or card.get("rounds"),
            "seed": seed, "chart": card.get("chart"), "board": card.get("board"), "intro": card.get("intro"), "view": card.get("view"), "rules": card.get("rules", 1), "variant": card.get("variant"),
            "source": "scripted" if demo else "live",
            "models": {} if demo else cast,
            "named": bool(named and not demo),
            "tokens": {"input": game.models.tokens[0], "output": game.models.tokens[1]} if game.models else {},
            "cost": round(game.models.cost, 4) if game.models else 0,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        },
        "agents": game.agents,
        "hud": card.get("hud"),
        "events": game.events,
        "result": result,
        "moves": game.moves,
        "log": game.log,
        "forked_at": game.forked_at,
    }


def run_fork(sc, args, out_dir):
    """Rewind a saved episode to one moment and play on from there in several parallel universes."""
    for need in ("fork_moment", "fork_outcome", "fork_headline"):
        if not hasattr(sc, need):
            sys.exit(f"This scenario cannot be forked yet (it has no {need}).")
    src = out_dir / f"{args.fork}.json"
    if not src.exists():
        sys.exit(f"No episode called {args.fork} in episodes/.")
    parent = json.loads(src.read_text())
    if "moves" not in parent:
        sys.exit(f"{args.fork} was saved before forking existed. Run it again, then fork the new file.")
    if parent["meta"]["scenario"] != sc.CARD["id"]:
        sys.exit(f"{args.fork} is an episode of '{parent['meta']['scenario']}', not of this scenario.")
    demo = parent["meta"]["source"] == "scripted"
    cast = parent["meta"]["models"] or {a["id"]: args.model for a in sc.CARD["agents"]}
    prefix = [m for m in parent["moves"] if m["ev"] < args.at]
    if len(prefix) == len(parent["moves"]):
        sys.exit(f"Event {args.at} is after the last decision of {args.fork}; pick an earlier moment.")
    name = args.name or f"{args.fork}-at{args.at}"
    fdir = out_dir / "forks" / name
    fdir.mkdir(parents=True, exist_ok=True)
    for old in fdir.glob("*.json"):
        old.unlink()

    branches, at, tokens = [], None, [0, 0]
    for b in range(1, args.branches + 1):
        print(f"\nUniverse {b} of {args.branches}")
        ep = run_episode(sc, parent["meta"]["seed"], demo, cast, args.effort, parent["meta"]["named"],
                         replay=copy.deepcopy(prefix), fork_seed=parent["meta"]["seed"] * 1000 + b)
        at = ep["forked_at"]
        ep["meta"]["fork"] = {"name": name, "parent": args.fork, "at": at, "universe": b, "standin": demo}
        (fdir / f"u{b:02d}.json").write_text(json.dumps(ep, indent=1))
        tokens = [tokens[0] + (ep["meta"]["tokens"].get("input") or 0), tokens[1] + (ep["meta"]["tokens"].get("output") or 0)]
        branches.append({"universe": b, "file": f"forks/{name}/u{b:02d}", **sc.fork_outcome(ep, at)})
        print(f"  => {branches[-1]['label']}; {ep['result']['headline']}")

    moment = sc.fork_moment(parent, at)
    summary = {
        "name": name, "parent": args.fork, "at": at, "title": parent["meta"]["title"],
        "source": parent["meta"]["source"], "standin": demo, "models": parent["meta"]["models"],
        "agents": parent["agents"], "moment": moment,
        "original": sc.fork_outcome(parent, at), "branches": branches,
        "headline": sc.fork_headline(moment, branches),
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    (fdir / "fork.json").write_text(json.dumps(summary, indent=1))
    print(f"\n{summary['headline']['text']}")
    if not demo:
        print(f"tokens: {tokens[0]:,} in, {tokens[1]:,} out")
    print(f"watch: http://localhost:8830/viewer/fork.html?f={name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", help="path to a scenario file, e.g. scenarios/split.py")
    ap.add_argument("--demo", action="store_true", help="use the scenario's scripted moves instead of calling a model")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model for every seat (default {DEFAULT_MODEL})")
    ap.add_argument("--cast", default="", help="per-seat models, e.g. ada=claude-opus-5-5,bo=openai/<model>")
    ap.add_argument("--named", action="store_true",
                    help="tell the players which model sits in each seat (default: they only see character names)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"],
                    help="thinking effort for Claude models")
    ap.add_argument("--seed", type=int, default=7, help="dice seed; the same seed gives the same luck")
    ap.add_argument("--runs", type=int, default=1, help="number of runs, each with the next seed")
    ap.add_argument("--name", default="", help="episode file name (default: <scenario>-<demo|live>-<seed>)")
    ap.add_argument("--fork", default="", help="name of a saved episode to rewind and replay in parallel universes")
    ap.add_argument("--at", type=int, default=0, help="with --fork: the event number to rewind to (the viewer shows it)")
    ap.add_argument("--branches", type=int, default=12, help="with --fork: how many universes to play (default 12)")
    ap.add_argument("--env", default="", help="a .env file to read API keys from (values already in the environment win)")
    args = ap.parse_args()
    if args.env:
        for line in pathlib.Path(args.env).read_text().splitlines():
            k, eq, v = line.partition("=")
            if eq and not k.lstrip().startswith("#"):
                os.environ.setdefault(k.replace("export ", "").strip(), v.strip().strip('"').strip("'"))

    sc = load_scenario(args.scenario)
    cast = {a["id"]: args.model for a in sc.CARD["agents"]}
    for pair in filter(None, args.cast.split(",")):
        who, _, model = pair.partition("=")
        if who not in cast:
            sys.exit(f"--cast names '{who}', but the seats are: {', '.join(cast)}")
        cast[who] = model

    out_dir = ROOT / "episodes"
    out_dir.mkdir(exist_ok=True)
    if args.fork:
        return run_fork(sc, args, out_dir)
    tally = {}
    for n in range(args.runs):
        seed = args.seed + n
        print(f"\n{sc.CARD['title']} (seed {seed}, {'scripted demo' if args.demo else 'live'})")
        ep = run_episode(sc, seed, args.demo, cast, args.effort, args.named)
        name = args.name if (args.name and args.runs == 1) else f"{sc.CARD['id']}-{'demo' if args.demo else 'live'}-{seed}"
        (out_dir / f"{name}.json").write_text(json.dumps(ep, indent=1))
        index = sorted(p.stem for p in out_dir.glob("*.json") if p.stem != "index")
        (out_dir / "index.json").write_text(json.dumps(index))
        r = ep["result"]
        tally[r["headline"]] = tally.get(r["headline"], 0) + 1
        print(f"  => {r['headline']}: {r['detail']}")
        if not args.demo:
            print(f"  tokens: {ep['meta']['tokens']['input']:,} in, {ep['meta']['tokens']['output']:,} out; OpenRouter cost ${ep['meta']['cost']:.3f}")
        print(f"  saved episodes/{name}.json   watch: http://localhost:8830/viewer/?ep={name}")
    if args.runs > 1:
        print("\nAcross runs: " + ", ".join(f"{k} x{v}" for k, v in tally.items()))


if __name__ == "__main__":
    main()
