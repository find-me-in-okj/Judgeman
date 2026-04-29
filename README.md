# Judgeman — OSINT Analytical Reasoning Engine

An epistemic discipline engine for OSINT investigations. Models beliefs about information, enforces structured reasoning, and surfaces uncertainty.

## Quick start

```bash
bash install.sh        # install CLI + GUI
jm-gui                 # open the GUI in your browser
# or
jm init "Investigation Name"  # use the CLI
```

## GUI

```bash
jm-gui                         # opens http://127.0.0.1:7432
jm-gui --port 8080             # custom port
python3 jm-gui --no-browser    # headless
```

## CLI

```bash
jm init "Operation Thornfield" --analyst alice
jm hypothesis add
jm source add
jm evidence add
jm claim create
jm claim link <claim_id> <evidence_id> supports
jm claim challenge <claim_id>
jm claim address <counterclaim_id>
jm claim confidence <claim_id>
jm verify
jm report generate
jm export
```

## Confidence ceilings & auto-lift

| Impact | Base ceiling | Auto-lift ceiling | Criteria to lift |
|---|---|---|---|
| low | 100% | — | — |
| medium | 85% | 100% | ≥2 independent high-cred sources + ≥1 addressed counter-claim |
| high | 75% | 90% | ≥3 independent high-cred sources + ≥1 addressed counter-claim + what_if_wrong |

The system never outputs "this is true", legal judgments, or enforcement recommendations.

## Requirements

- Python 3.11+
- click, colorama, flask (installed by install.sh)

## Architecture

```
judgeman/
├── jm                    CLI runner
├── jm-gui                GUI launcher (opens browser)
├── install.sh            Installs CLI + GUI system-wide
├── judgeman/             Core engine
│   ├── db.py             SQLite schema + connection
│   ├── models.py         Data models + impact ceilings
│   ├── confidence.py     Rule-based confidence engine + auto-lift
│   ├── audit.py          Immutable audit log
│   ├── chainhash.py      Tamper-evident export hash
│   ├── resolve.py        Prefix resolution for all commands
│   ├── output.py         Terminal output primitives
│   ├── cli.py            CLI entry point
│   └── commands/         One file per command group
└── gui/
    ├── app.py            Flask REST API (wraps the engine)
    └── templates/
        └── index.html    Death Note themed SPA
```
