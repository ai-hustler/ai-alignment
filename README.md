# The Alignment Games: the receipts

Do AIs do the right thing when nobody's checking?

Open transcripts and scorecards from games that put AI models from different makers in tempting situations.

- Every line of model speech is unedited model output.
- Rules, dice and labels ("fibbed", "told the truth") come from the game's code.
- Each game folder holds a scorecard, one transcript page per take, and the raw data in `data/`.

A handful of games shows what these players did in these games. It is not a measure of what any model usually does.

## The framework

The code that ran these games is in [`framework/`](framework/): the runner, the scenarios and the adapters for plugging in any model or agent. Start with [`framework/README.md`](framework/README.md).

## Where the evidence is

| Game | Scorecard and transcripts | Raw prompts and replies |
|---|---|---|
| Night Shift | `docs/night-shift/` | `docs/night-shift/data/` |
| 60 Seconds | `docs/sixty-seconds/` | `docs/sixty-seconds/data/` |
| Lights Out | `docs/lights-out/` | `docs/lights-out/data/` |
