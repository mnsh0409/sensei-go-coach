# SCOPE (frozen 2026-10-27; revised 2026-10-27 after the kit audit, before any evaluation run)

Changes to this file need the human's approval. Numbers referenced here live in `configs/prereg.yaml`.

## The one user journey

1. The learner opens the web app and picks an approximate rank from a dropdown (20k to 5d).
   Board: 19x19, Chinese rules, komi 7.5.
2. They play a game against Sensei. After each Sensei move a side panel shows the move, two to four
   sentences on why it was chosen (grounded on engine numbers), the position type (only-move,
   tactical or strategic) and the strength it cost (winrate loss against the engine's best move).
3. After each learner move, Sensei checks it against the engine (mistake definition in `INTERFACES.md`).
   A mistake in the current target topic triggers an immediate, engine-grounded explanation; other
   mistakes are logged with their topic for the summary.
4. At any point the learner can ask "Ask Sensei" questions ("why not D4?", "what happens if I
   cut here?"). The model answers by calling engine tools; any variation it mentions is drawn on the board.
5. Session summary: game result (from the engine's score lead), rank estimate with uncertainty, the top
   two weakness topics with one example each, and which topic the next game will target.

## In scope (MVP)

- Go 19x19 with KataGo (main net plus HumanSL net) as a persistent subprocess.
- `ConstrainedSelector`: most instructive admissible move (definition in `INTERFACES.md`).
- `MetaExplainer` and "Ask Sensei" through Meta Model API with tool calling. Tools:
  `analyze_position`, `evaluate_move`, `show_variation`, `get_learner_profile`.
  A guard rejects any explanation that mentions a coordinate not present in the tool results.
- `HumanSLLearner`: rank estimate by maximum likelihood over HumanSL profiles; learner policy is the
  HumanSL policy at the nearest profile. A known use of that network (credited in the README), used here as a
  component, not a claim.
- `LdaMistakeTopics`: unsupervised weakness topics over move-situation bags (hand-coded categories
  are the fallback if the topics are unreadable).
- `ThompsonCurriculum`: discounted Thompson sampling over topics; a "success" is an observed mistake,
  so the curriculum targets the topics where the learner errs most. The target topic decides which
  mistakes get immediate explanations (journey step 3).
- `sensei.session`: per-move mistake check, topic tagging, profile JSON, curriculum update.
- `sensei.sgf` and `scripts/fetch_games.py`: public games from the source in `data/SOURCES.md`.
- Minimal web UI (FastAPI plus one HTML page). Demo delivery: a recorded-session replay mode first; live
  hosting second, only if the T0 hosting decision calls for it.
- Evaluation harness for claims C1 to C3 below.

## Stretch (only after T9 is DONE, in this order)

- S1 Explanation language switch (English, Traditional Chinese, Japanese).
- S2 One-ply steering: small selector bonus for moves whose resulting position loads on the target topic.
- S3 Chess plug-in: python-chess, Stockfish MultiPV, a Maia net as learner policy.
- S4 Photo-to-board through image understanding (9x9 first).
- S5 9x9 mode.

## Out of scope

Fine-tuning any model; training a neural network from scratch; full search-level integration of the
teaching bias; accounts or multi-user storage; mobile app; any use of AI CUP data.

## Rules check (human fills in from the official rules in T0)

| Question | Answer from the official rules |
|---|---|
| Meta components that must be used | |
| Open-weight models allowed alongside the API | |
| Third-party engines (KataGo) and nets allowed | |
| Team size | |
| Ownership and license of the submission | |
| Submission items (video length, public repo, description length) | |
| Hosted live demo required, or are a video and the repository enough | |
| Credit expiry | |

## Pre-registered claims

Frozen before any evaluation run. Parameters in `configs/prereg.yaml`. Results go to `results/`.
Any deviation (changed parameter, excluded game, rerun) is reported as a deviation with its reason,
never applied silently. Pilot runs are labelled as pilots and are never pooled with confirmatory runs.

Shared settings: KataGo main net fixed; every result records the KataGo version, the net files and the analysis
config with their SHA-256, and the thread settings. All values are for the side to move
(`engine.winrate_perspective`). `max_visits` for both sides; candidates with fewer than
`max(min_candidate_visits_abs, min_candidate_visits_frac * total visits)` visits are excluded. Openings: the first
`openings.plies` moves of distinct public games from `data/splits.json`; each opening is played twice with colors
swapped; the pilot and confirmatory opening sets are disjoint. Adjudication: a game ends when the main net's
winrate for one side stays above `adjudication.decisive_winrate` for `adjudication.decisive_plies` consecutive
plies; at `adjudication.max_plies` or after two consecutive passes, the winner is the side the main net's score
lead favours at the final position (area scoring, komi 7.5).

The epsilon used by the app and by C2 is the C1 confirmatory epsilon, epsilon*.

### C1 Strength cost (two stages)

Stage 1, pilot (a pre-registered decision rule, not a claim): Sensei at each epsilon in `C1.pilot_epsilons`
(temperature 0) plays `C1.pilot_games_each` games against the raw engine (move with most visits), same visits,
on the pilot openings. epsilon* is the largest pilot epsilon whose score is at least `C1.pilot_min_score`; if
none qualifies, epsilon* is the smallest pilot epsilon and this is reported. No human choice is involved.

Stage 2, confirmatory: `C1.n_games` games (`openings.n_openings` openings x 2 colors) at epsilon*.
Metric: strength cost in Elo from the score rate (positive = Sensei weaker); 95% CI by bootstrap over openings
(games paired by opening). **Success:** the upper end of the CI is below `C1.success_max_cost_elo_upper`.
Also reported: mean winrate loss and mean score-lead loss per Sensei move, and the distribution of
admissible-set sizes.

`openings.n_openings` may be lowered (to no fewer than 100) after the T0 benchmark and before any C1 game is
played, never afterwards; the change is logged in the `revisions` list of `configs/prereg.yaml`.

### C2 Lesson relevance on real human games

Data: public games from `data/splits.json`, played on or after `C2.game_date_min` (after the HumanSL net's
release), 19x19, even games, no handicap, analyzed under each game's own rules and komi. A position is eligible
when the side to move is a player whose rank is in the band, `C2.ply_min` <= ply <= `C2.ply_max`, and the
admissible set has at least `C2.min_admissible` moves. Sample `C2.positions_per_band` eligible positions per
band, at most `C2.max_positions_per_game` per game. Bands in `C2.bands` are ordered from weak to strong; the
learner policy for band k is HumanSL at band k's profile.

In each position: s_k = Sensei's choice at epsilon* with band k's learner policy, b = the engine's best reliable
move, r = a uniformly random admissible move (seeded). Indicators come from the actual game record:
1[played = s_k], 1[played = b], 1[played = r].

Primary metric, cross-band uplift: for each band k except the strongest, compute s_k on band k's positions and on
band k+1's positions; U_k = P_{k+1}(played = s_k) - P_k(played = s_k), the rate at which stronger players play
the move Sensei would teach band k, minus the rate for band k itself. U = mean of U_k. 95% CI by bootstrap
clustered by game. **Success:** U > 0 with the CI excluding 0. Each U_k is reported.

Secondary (descriptive, no success criterion): per band, D_best = P(played = b) - P(played = s_k) and
D_rand = P(played = r) - P(played = s_k) with game-clustered bootstrap CIs, and the mean winrate loss of s_k.

### C3 Curriculum in simulation

A closed-loop simulation check, labelled as a simulation everywhere it appears: the learning dynamics are
assumed, so it shows that the curriculum finds and targets weaknesses under those dynamics, not that humans
learn faster. `C3.n_learners` simulated learners over `C3.n_topics` topics; each has
`C3.weak_topics_per_learner` weak topics with mistake probability `C3.p_weak` and `C3.p_base` elsewhere.
A session targeting topic k multiplies its mistake probability by (1 - `C3.eta_target`) and every other
topic's by (1 - `C3.eta_transfer`). Observation model (as in the MVP, where every learner move is tagged with a
topic): each session produces `C3.moves_per_session` learner moves spread over topics uniformly at random; the
curriculum observes a mistake or not for each, at the topic's current rate. Policies (`C3.policies`):
discounted Thompson sampling (the product), greedy on the posterior mean, round-robin, uniform random, and an
oracle that always targets the topic with the highest true mistake rate.

Metric: area under the mean mistake-rate curve over `C3.sessions` sessions. Headline: gap closure =
(AUC_round_robin - AUC_thompson) / (AUC_round_robin - AUC_oracle), with a bootstrap CI over learners.
**Success:** AUC(Thompson) < AUC(round-robin) with the paired 95% CI over learners excluding 0. Also reported:
greedy against Thompson, and the share of learners whose weak topics are the curriculum's two most-selected arms
by session `C3.identification_session`. Design-stage simulations with seeds 0 to 2 (not the confirmatory seed)
put Thompson's gap closure near 0.8, so this claim checks the implementation rather than discovering anything.

## Submission checklist

- [ ] Public repository with license, `README.md`, one-command setup, `requirements.lock`.
- [ ] README states: what it does, Meta models and API features used, techniques, evaluation table
      (rendered from `results/`), limitations, credits and related work.
- [ ] `scripts/check_numbers.py` passes: every number inside `<!-- results -->` blocks of the README and the
      video script is a `{{token}}` rendered by `scripts/render_readme.py` from clean-state `results/*.json`.
- [ ] Demo: replay mode works from a clean browser; live mode too if the T0 hosting decision requires it.
- [ ] Video under 3 minutes: problem in the first 15 seconds, live run, Ask Sensei, the numbers, one honest limitation.
- [ ] Project description in the judges' terms: creativity, technical ambition, it works.
- [ ] Submitted by the internal deadline.
