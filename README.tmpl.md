# Sensei

A Go coach that teaches by playing. KataGo finds the moves; Muse Spark, through the Meta Model API, explains
them in plain language, answers the learner's questions by calling engine tools, and every explanation is
checked against the engine's own numbers before it reaches the screen.

## What it does

1. Pick an approximate rank (20k to 5d) and play a 19x19 game against Sensei.
2. After each of Sensei's moves, a side panel shows the move, a short explanation grounded on engine numbers,
   the position type (only move, tactical or strategic) and the strength the move cost against the engine's best.
3. After each of your moves, Sensei checks it with the engine. A mistake in your current weakness topic gets an
   immediate explanation; other mistakes are logged for the summary.
4. Ask Sensei anything about the position ("why not D4?", "what if I cut here?"). The model answers by calling
   engine tools, and every variation it mentions is drawn on the board.
5. At the end: the result, your estimated rank with its uncertainty, your two weakest topics with an example
   each, and the topic the next game will train.

## How it works

- **Move selection.** KataGo's reliable candidates within epsilon of the best winrate form the admissible set.
  Sensei plays the admissible move with the largest pointwise contribution to KL(engine policy || learner
  policy): a move the engine likes and a player of your rank would rarely find. The bound epsilon caps the
  strength Sensei gives away; the model never chooses moves.
- **Learner model.** KataGo's HumanSL network predicts human moves at a given rank. Your rank is the maximum
  likelihood profile over your own moves, and the learner policy is the HumanSL policy at that rank.
- **Weakness topics and curriculum.** Every move becomes a bag of situation tokens (phase, region, position type,
  captures, contact, tenuki). Topics are fitted with LDA on windows of mistakes from public games; a discounted
  Thompson-sampling curriculum picks the topic where you err most, and that topic decides which mistakes get an
  immediate explanation.
- **Grounded explanations.** The model receives only facts computed by code: the engine's numbers, where each
  point is in Go terms ("right side, 2nd line"), and what a move does to the stones around it (captures, ataris,
  liberties, and shapes such as a one-space jump or a knight's move). The prompt names Sensei's color and the
  learner's, so "I" and "you" always point at the right stones, and it bans engine internals such as policy priors
  and visit counts. A guard rejects any reply that names a board coordinate absent from those facts or the tool
  results: it asks once for a correction, then drops offending sentences, then falls back to a template.

## Meta models and API features used

- Muse Spark (`muse-spark-1.3`) through the Meta Model API's OpenAI-compatible chat completions.
- Tool calling with four tools: `analyze_position`, `evaluate_move`, `show_variation`, `get_learner_profile`
  (`tool_choice` auto, with a final round without tools when the tool budget is spent).
- `reasoning_effort` per call; every call is logged with its token counts and estimated cost.

## Evaluation

Three claims were pre-registered before any run (`SCOPE.md`, `configs/prereg.yaml`); every number below is
rendered from a result file in `results/` written by `eval/run.py` from a clean git commit.

<!-- results -->
| Claim | Result | Pre-registered criterion | Verdict |
|---|---|---|---|
| C1 Strength cost of teaching | strength cost {{C1.elo}} Elo, positive meaning Sensei is weaker ({{C1.ci_pct}}% CI {{C1.lo}} to {{C1.hi}}), score rate {{C1.score_pct}}% over {{C1.n}} games against the raw engine, epsilon {{C1.eps}} | upper end of the CI below {{C1.threshold}} Elo | {{C1.verdict}} |
| C2 Lesson relevance on human games | uplift {{C2.U_pct:.2f}} percentage points ({{C2.ci_pct}}% CI {{C2.lo_pct:.2f}} to {{C2.hi_pct:.2f}}) over {{C2.n}} positions | CI above zero | {{C2.verdict}} |
| C3 Curriculum (simulation) | mistake-rate AUC of round robin minus Thompson {{C3.auc_diff}} ({{C3.ci_pct}}% CI {{C3.lo}} to {{C3.hi}}) over {{C3.learners}} simulated learners; Thompson closes {{C3.gap_pct}}% of the gap to an oracle ({{C3.ci_pct}}% CI {{C3.details.gap_closure.ci.0:.0%}} to {{C3.details.gap_closure.ci.1:.0%}}) | Thompson below round robin, CI excluding zero | {{C3.verdict}} |

C1 ran {{C1.queries}} engine searches in {{C1.hours}} hours on one {{C1.gpu}} with {{C1.conc}} games in flight.
Deviations from the pre-registration, each listed with its reason in the result file: C1 {{C1.n_deviations}},
C2 {{C2.n_deviations}}, C3 {{C3.n_deviations}}.

C2 in context (descriptive, added after C2 ran, not pre-registered; `eval/relevance_baselines.py`): from one band
to the next, players play the engine's best move {{C2base.best_x}} times as often and a random admissible move
{{C2base.rand_x}} times as often. Sensei's lesson is played {{C2base.lesson_x}} times as often, rising
{{C2base.lesson_vs_rand}} a random admissible move (ratio {{C2base.lesson_over_rand}}, {{C2base.ci_pct}}% CI
{{C2base.lesson_over_rand_lo}} to {{C2base.lesson_over_rand_hi}}). The lesson Sensei picks for
{{C2base.weakest_band}} players is played by {{C2base.weakest_lesson_own_pct}}% of them and by
{{C2base.weakest_lesson_strongest_pct}}% of {{C2base.strongest_band}} players; over the same range the engine's best
move goes from {{C2base.best_weakest_pct}}% to {{C2base.best_strongest_pct}}%.
<!-- /results -->

C1 asks how much playing strength Sensei gives up by teaching instead of winning. C2 asks whether the moves
Sensei would teach a band of players are moves that the next stronger band actually plays; its pre-registered
test compares that rate with zero. Stronger players play good moves more often in general, so the context
paragraph compares the lesson with the engine's best move and with a random admissible move on the same
positions, which shows how much of the uplift is specific to Sensei's choice. C3 is a closed-loop
simulation with assumed learning dynamics: it shows that the curriculum finds and targets weaknesses under those
dynamics, not that people learn faster.

## Setup

Requires Python 3.11 or 3.12, KataGo with a main network and the HumanSL network, and a Meta Model API key.
Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
Copy-Item .env.example .env   # then fill in MODEL_API_KEY and the KATAGO_* paths
.\.venv\Scripts\python.exe scripts/meta_smoke_test.py
.\.venv\Scripts\python.exe -m pytest -q
```

Linux or macOS: `python3.12 -m venv .venv`, then the same commands with `.venv/bin/python`.

Run the coach at http://127.0.0.1:8000/:

```powershell
.\.venv\Scripts\python.exe -m sensei.app            # live: KataGo and the Meta Model API from .env
.\.venv\Scripts\python.exe -m sensei.app --fake     # offline demo: fake engine, canned explanations, no API calls
.\.venv\Scripts\python.exe -m sensei.app --replay   # play back recorded games from sessions/ and results/
```

Every game is recorded to `sessions/`; replay mode needs neither a GPU nor an API key, so it is the mode to host.

## Limitations

- C3 is a simulation, not a study with people; C2 measures relevance on recorded games, not learning gains.
- C2's uplift is small and, compared on the same positions with the engine's best move and a random admissible
  move, not specific to Sensei's choice: stronger players play every good move more often (the context paragraph
  under the evaluation table).
- The rank estimate is close for dan players and off by several ranks for kyu players in a held-out check
  (`results/learner_check.json`, descriptive), so Sensei can start too strong for a weaker player until it has
  seen enough of their moves.
- Rank labels come from the game server's scale, which need not match the scale of the HumanSL training data.
- Explanations can only be as good as the engine numbers they are grounded on, and the guard checks
  coordinates, not reasoning.
- 19x19 only, Chinese rules and komi 7.5 in play.

## Credits and related work

- KataGo (David J. Wu) and its HumanSL network (July 2024), which supplies rank-conditioned human move
  predictions. KataGo's Extra Networks page suggests using that network for player rank estimation and as a
  mistake filter in teaching tools; Sensei uses rank estimation as a component, not as a contribution.
- Strength estimation from rank-specific policies has its own literature (for example "Policies of Multiple Skill
  Levels for Better Strength Estimation in Games", 2025), and human-policy-guided move selection has related work
  in chess and Go (for example Maia, and MATILDA, 2026). Sensei's distinct element is selecting moves for their
  teaching value under an explicit strength bound, with explanations grounded only on engine output.
