# Sensei

A Go coach that teaches by playing. KataGo finds the moves; a Meta model explains them, answers questions
by calling engine tools, and plans what to practice next.

> Work in progress for the Meta Global AI Developer Hackathon. This README is completed in T10;
> every result in it is rendered from `results/` by `scripts/render_readme.py` and checked by
> `scripts/check_numbers.py`.

## What it does

_To be written in T10._

## How it works

_To be written in T10: selector, learner model, weakness topics, curriculum, Ask Sensei tool loop._

## Meta models and API features used

_To be written in T10._

## Evaluation

_Generated from `results/SUMMARY.md` in T10._

## Setup

Requires Python 3.11 or 3.12. Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts/meta_smoke_test.py
.\.venv\Scripts\python.exe -m pytest -q
```

Linux or macOS: `python3.12 -m venv .venv`, then the same commands with `.venv/bin/python`.

## Limitations

_To be written in T10._

## Credits and related work

- KataGo (David J. Wu) and its HumanSL network (July 2024), which supplies rank-conditioned human move
  predictions. KataGo's Extra Networks page suggests using that network for player rank estimation and as a
  mistake filter in teaching tools; Sensei uses rank estimation as a component, not as a contribution.
- Strength estimation from rank-specific policies has its own literature (for example "Policies of Multiple Skill
  Levels for Better Strength Estimation in Games", 2025), and human-policy-guided move selection has related work
  in chess and Go (for example Maia, and MATILDA, 2026). Sensei's distinct element is selecting moves for their
  teaching value under an explicit strength bound, with explanations grounded only on engine output.
