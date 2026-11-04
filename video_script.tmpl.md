# Video script (under 3 minutes)

Draft template (TASKS.md T10). `scripts/render_readme.py` writes `video_script.md` from this file; numbers come
only from the results blocks below. Timings are targets; record the live parts on the dev PC.

## 0:00 to 0:15 The problem

Voice: "A Go engine can tell you the best move. It cannot tell you which move you are ready to learn, or why.
Sensei is a coach that teaches by playing."
Screen: the board, Sensei's first move appearing with its explanation panel.

## 0:15 to 1:15 A live game

Voice: "I tell Sensei I am about ten kyu. KataGo proposes candidates; Sensei plays the one a player of my rank
would rarely find, as long as it costs almost nothing in strength. Muse Spark explains it from the engine's
numbers, and only from them."
Screen: two or three Sensei moves with explanations; the strength-cost line under each.
Voice: "When I make a mistake in the topic I am training, Sensei stops me right away."
Screen: a learner mistake flagged with its immediate explanation.

## 1:15 to 1:50 Ask Sensei

Voice: "I can ask anything. The model answers by calling engine tools, and every variation it mentions is drawn
on the board. If it names a point the tools never returned, a guard sends the answer back."
Screen: the question "why not D4?", the tool calls, the variation drawn on the board.

## 1:50 to 2:30 The numbers

<!-- results -->
Voice: "We pre-registered three claims. Teaching instead of winning costs {{C1.elo}} Elo, with a {{C1.ci_pct}}
percent interval up to {{C1.hi}}, over {{C1.n}} games against the raw engine. On recorded human games, the moves
Sensei teaches one rank band are played {{C2.U_pct}} percentage points more often by the next stronger band, a
real but small effect that is no larger than for any good move. In simulation, the curriculum closes
{{C3.gap_pct}} percent of the gap to an oracle that knows the weak topics."
<!-- /results -->
Screen: the evaluation table from the README.

## 2:30 to 2:50 One honest limitation

Voice: "The curriculum result is a simulation, not a study with people. That study is the next step."
Screen: the session summary with the rank estimate and the next topic.

## 2:50 to 3:00 Close

Voice: "Sensei: KataGo finds the moves, Muse Spark explains them, and you learn the ones that matter at your
level."
Screen: repository URL.
