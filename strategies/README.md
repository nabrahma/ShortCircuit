# strategies/

Private strategy plug-ins for this ShortCircuit deployment.

Everything in this folder **except this README** is git-ignored, so the
strategies you write here never reach the public repository.

## Layout

```
strategies/
├── __init__.py          # ACTIVE = ("my_strategy",)  — which run, in priority order
├── my_strategy/         # one package per strategy
│   ├── __init__.py      # the Strategy subclass (the plug-in)
│   ├── detector.py      # optional: the pattern logic it wraps
│   └── params.py        # optional: its thresholds
└── tests/               # optional: tests for your strategies (run by pytest)
```

A single `my_strategy.py` module works too.

## Writing one

Subclass `shortcircuit.strategy.sdk.Strategy`, give it a unique `name`, and
implement `evaluate(ctx) -> Signal | None`. The engine does everything else:
data, risk gates, orders, stops, exits and logging.

The full guide is [docs/STRATEGIES.md](../docs/STRATEGIES.md). A complete,
working example ships with the engine at
`src/shortcircuit/strategy/examples/vwap_stretch_fade.py`.

## Choosing what runs

- `ACTIVE` in `strategies/__init__.py`, or
- `SHORTCIRCUIT_STRATEGIES=my_strategy,other_one` in the environment (wins).

With neither set, the engine runs with no strategy: it scans and manages open
positions but opens nothing, and says so at startup.
