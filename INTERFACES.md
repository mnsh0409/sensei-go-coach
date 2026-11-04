# Interfaces and definitions

`src/sensei/interfaces.py` is the source of truth for types; this file explains them and fixes the math.

Notation: pi_E is the engine policy, `analysis.policy` (the main net's raw policy, renormalized over legal moves);
pi_L is the learner policy (HumanSL policy at a profile, renormalized over legal moves); V(a) is the main net's
post-search winrate **for the side to move at the root** after move a (KataGo queried with
`reportAnalysisWinratesAs = SIDETOMOVE`, see CLAUDE.md); N(a) is the visit count of candidate a.

## Data types

- `Position`: game name, full move history `((color, move), ...)` from the initial position, side to move,
  and `meta` (board size, komi, rules; defaults 19, 7.5 and `chinese`). Positions are immutable; `Game.play`
  returns a new one. `Position` is not hashable (`meta` is a dict), so caches key on
  `(pos.moves, pos.to_move, size, komi, rules)` plus whatever else the cached value depends on.
- `Candidate`: one engine root candidate with `prior` (the main net's raw prior), `value` = V(a),
  `visits` = N(a), optional `score_lead` (side to move) and principal variation `pv`.
- `EngineAnalysis`: candidates sorted by visits, descending (the wrapper sorts; KataGo's `order` field is not
  strictly by visits); `policy` = pi_E over legal moves (sums to 1 within 1e-6); `root_value` = winrate of the
  side to move; for Go, `ownership` in row-major order from the top left, positive = the side to move owns it.
- `Choice`: the selected move, its instructiveness, its value loss, the position class, the instructiveness
  of every admissible candidate and the admissible set.
- `Explanation`: text, model id, token counts and `grounded_on`, the exact numbers given to the model.
- `LearnerEstimate`: numeric rank, its standard deviation and the number of moves it is based on.

Numeric rank scale (`sensei.ranks`): 1d = 1.0, 2d = 2.0, ..., 1k = 0.0, 2k = -1.0, 10k = -9.0, 20k = -19.0.

## Selector (`ConstrainedSelector`)

1. Reliable candidates: N(a) >= max(`min_visits_abs`, `min_visits_frac` * sum of N over candidates).
   Unreliable candidates are dropped entirely: they neither define the best value nor enter the admissible set.
   If no candidate meets the threshold, the most-visited candidate is the only reliable one.
2. V* = max V(a) over reliable candidates. Admissible set A = { a reliable : V* - V(a) <= epsilon }.
   The best reliable candidate is always admissible, so A is never empty.
3. Instructiveness of a in A, with pi_E(a) taken from `analysis.policy` (not `Candidate.prior`):

       I(a) = pi_E(a) * log( (pi_E(a) + delta) / (pi_L(a) + delta) )

   This is the pointwise contribution of a to KL(pi_E || pi_L): high when the engine likes the move and
   the learner would rarely play it. A move missing from pi_L has pi_L(a) = 0.
4. Temperature 0: choose argmax I over A; ties broken by higher V, then by more visits.
   Temperature T > 0: sample from softmax(I / T) over A with the given `numpy.random.Generator`.
5. `value_loss` = V* - V(chosen), so 0 <= value_loss <= epsilon always.
6. Position class from the entropy H of pi_E in nats: H < `entropy_low` is ONLY_MOVE,
   H > `entropy_high` is STRATEGIC, otherwise TACTICAL. The thresholds are runtime settings
   (`selector.entropy_low`, `selector.entropy_high` in `configs/default.yaml`), calibrated in T5.

Epsilon for play and for C2 is the C1 confirmatory epsilon chosen by the pre-registered pilot rule (SCOPE.md).
Runtime temperature for play (not evaluation) comes from the learner's rank band in `configs/default.yaml`:
weaker learners get a warmer, more varied Sensei.

## Learner (`HumanSLLearner`)

- `policy(pos, legal)`: HumanSL policy at the profile on `learner.profile_grid` nearest the current estimate.
- `estimate(history)`: for each profile p on the grid, LL(p) = sum over the learner's moves of
  log(pi_H(m | x, p) + delta). With a uniform prior, posterior(p) = softmax(LL). Rank = posterior mean on
  the numeric scale, `rank_sd` = posterior standard deviation. `sensei.ranks.rank_posterior` already
  implements this from a dict of log-likelihoods; the learner only has to collect them, caching human policies
  by the position key above plus the profile.
- Below `learner.min_moves_for_estimate` moves, return the declared rank with `rank_sd` = 3.0.

## Mistakes and mistake topics (`LdaMistakeTopics`)

A learner move is a mistake when its loss = V* - V(played) exceeds `play.mistake_threshold` (0.05), where
V* comes from the analysis of the position before the move at `play.max_visits`, using the selector's
reliability rule. If the played move is not a reliable candidate, evaluate it with a second query on the
position after it at the same visits: V(played) = 1 - root winrate of that position (the side to move changed).

Every learner move gets a situation bag of tokens:
`phase:{opening,middle,end}` (ply < 50, < 150, else), `region:{corner,edge,center}` of the played move
(a pass produces no region token), `best_region:{...}` of the engine's best move,
`class:{only_move,tactical,strategic}`, and `local:{capture,atari,contact,tenuki}` (tenuki = more than
5 lines from the previous move). The bag has no loss token, so mistakes and non-mistakes are tagged the same way;
the loss severity is stored separately as `small`, `medium` or `large` (0.05 to 0.10, 0.10 to 0.20, above 0.20).
Fitting corpus: `topics.fit_games` public games from the topic-fitting split, analyzed at `topics.fit_visits`;
documents are windows of `topics.window` consecutive mistakes by the same player, so topics describe the
situations in which mistakes happen. This is a runtime model, not a pre-registered claim.
`assign` maps one bag to a topic distribution summing to 1 (unknown tokens are ignored; a bag with no known
token gets the uniform distribution). A move's topic is the argmax. A topic's mistake rate is
mistakes / moves among the learner's moves tagged with it, which is what the curriculum tracks.
`describe(k)` lists the top tokens of topic k; a human checks readability in T7.

## Curriculum (`ThompsonCurriculum`)

Beta-Bernoulli arms, one per topic, prior (alpha0, beta0). A "success" is an observed mistake in that
topic. Each round, `update(observations)` first decays every arm toward the prior,
alpha <- discount * (alpha - alpha0) + alpha0 (same for beta), then adds the observed successes and failures.
`select(rng)` draws theta_k ~ Beta(alpha_k, beta_k) for each arm and returns the argmax.
Discount 1.0 is stationary Thompson sampling; below 1.0 it forgets, which matters because a learner's
mistake rates fall as they improve.

## Session (`sensei.session`)

The app-side loop for one game. After each learner move: run the mistake check above, tag the move with its
topic, and record it in the learner profile; a mistake in the current target topic triggers an immediate
explanation through `MetaExplainer` (grounded on the analysis of the position before the mistake). At the end of
the game: feed per-topic (mistakes, non-mistake moves) counts to `ThompsonCurriculum.update`, update the rank
estimate, select the next target topic, and save the profile. Profiles are JSON files under `profiles/` (gitignored) holding the declared
rank, the rank-estimate history, per-topic counts with one example position each, the curriculum posterior and
the current target topic. The game result comes from the sign of the main net's score lead (SCOPE.md).

## Explainer (`MetaExplainer`) and Ask Sensei

- `MetaExplainer(client, engine, game, cfg)` implements `Explainer` and runs the Ask Sensei tool loop
  (at most `explainer.max_tool_rounds` tool rounds per answer).
- `build_explanation_messages(analysis, choice, learner, lang)` returns chat messages. The system message
  forbids inventing moves or numbers, names Sensei's and the learner's colors ("I" and "you") and bans engine
  internals. The user message (prompt v2, `PROMPT_VERSION`) gives, in plain words: where each point is
  (`describe_point`), what the chosen move does to the stones next to it and its shape relations (`local_facts`),
  the winning chances and the score in points, the comparison with the engine's top move, why the selector chose
  it (from the instructiveness and the position class, without the numbers) and the engine's expected answer. The
  v1 numeric facts stay in `Explanation.grounded_on` for the app and the logs.
- `TOOLS`: OpenAI-format function schemas, each with `additionalProperties: false`:
  - `analyze_position(top_k: int)`: candidates of the current position (move, where, winrate, score lead), with
    the side the numbers are for.
  - `evaluate_move(move: str)`: the engine's evaluation of one legal move (where, winrate and score lead after it,
    its loss against the engine's top move, meaning its most-searched candidate, and what it does to the stones
    next to it).
  - `show_variation(moves: list[str])`: replays the moves with `GoGame` from the current position, rejecting
    illegal ones, returns the engine's evaluation of the end position; the UI draws the sequence.
  - `get_learner_profile()`: rank estimate, top weakness topics, current target topic.
  Tool results are the only source of moves and numbers the model may mention.
- `find_ungrounded_coords(text, allowed, size)` uppercases the text, then returns the on-board coordinates it
  mentions that are not in `allowed` (uppercase, in order). If it returns anything, regenerate once; if it still
  fails, drop the offending sentences.

## Constructors and extra methods the acceptance tests expect

| Class / function | Module | Signature |
|---|---|---|
| `read_game` | `sensei.sgf` | `(data: bytes) -> GameRecord` with `moves` (GTP), `size`, `komi`, `rules`, `handicap`, `black_rank`, `white_rank`, `date` |
| `ConstrainedSelector` | `sensei.selector` | `(delta, min_visits_abs, min_visits_frac, entropy_low, entropy_high)` |
| `instructiveness` | `sensei.selector` | `(pi_e, pi_l, delta) -> dict[move, float]` over the keys of `pi_e` |
| `HumanSLLearner` | `sensei.learner` | `(source, grid, declared_rank, min_moves, delta)`; `source` has `human_policy` |
| `LdaMistakeTopics` | `sensei.topics` | `(n_topics, seed)`; unknown tokens are ignored by `assign` |
| `ThompsonCurriculum` | `sensei.curriculum` | `(arms, discount, prior)`; extra `posterior(arm) -> (alpha, beta)` |
| `GoGame` | `sensei.game_go` | `()`; `initial_position(size, komi)`; extra `board(pos) -> {coord: color}`; `result()` returns None (scoring is delegated to the engine) |
| `KataGoEngine` | `sensei.engine_katago` | `from_env()` reading the `KATAGO_*` variables |
| `MetaExplainer`, `build_explanation_messages`, `TOOLS`, `find_ungrounded_coords` | `sensei.explainer` | see above; `find_ungrounded_coords` builds on `sensei.coords.find_coords` |
| `elo_from_score`, `paired_bootstrap_ci`, `elo_cost_ci` | `eval.stats` | see `eval/README.md` |

`read_game` normalizes `rules` to lowercase (`chinese`, `japanese`, ...), converts sgfmill coordinates
(row 0 at the bottom) to GTP, and represents passes as `pass`. Ranks are kept as the SGF strings (for example `5k`).

## Engine (`KataGoEngine`)

`analyze(pos, max_visits)` returns `EngineAnalysis` from the side to move's perspective;
`human_policy(pos, profile)` returns the HumanSL policy over legal moves (1-visit query); `close()` terminates the
subprocess. One subprocess per process. Both query methods are thread-safe and may run concurrently (queries are
matched on `id`). Rules and komi come from `pos.meta` (defaults `chinese` and 7.5). `from_env()` reads the
`KATAGO_*` variables, checks each path with `Path.is_file()` and fails with the resolved path if one is missing.
