# Video script (under 3 minutes)

`scripts/render_readme.py` writes `video_script.md` from this file; the numbers in the results block come from
`results/`. The game segments are replay-mode captures of `results/demo_game.json` (a game played live on the dev
PC; replay removes the wait for each explanation), labelled on screen as a recorded live game.

## 0:00 to 0:15 The problem

Voice: "A Go engine tells you the best move. It cannot tell you which move you are ready to learn, or why it
matters. Sensei is a coach that teaches by playing."
Screen: the board, a Sensei move appearing with its explanation panel.

## 0:15 to 1:15 A game against Sensei

Voice: "This is a game I played live, played back here. I told Sensei I am about two kyu. KataGo proposes
candidate moves; among those that cost almost nothing in strength, Sensei plays the one a player of my rank would
rarely find. Muse Spark explains it, but the reason comes from the engine: if Sensei had passed here, I could play
Q5 and put its stone at Q4 in atari."
Screen: Sensei's R3 (move 30) with its explanation.
Voice: "Sometimes the reason is timing. This move is sente: Sensei plays it first and still gets the big point
next."
Screen: Sensei's E17 (move 56).
Voice: "Sometimes it is life and death: without this move, I would capture two of its stones."
Screen: Sensei's E15 (move 88).

## 1:15 to 1:50 Ask Sensei

Voice: "I can ask anything. Muse Spark answers by calling engine tools, and every variation it mentions is drawn
on the board. Asked why it played C16 and not D17, it answers from the engine: C16 was its top move, and D17 is now
my best reply. Asked whether my corner will die, it says the tools cannot show that for certain, then shows the
line they do show. A guard rejects any point the tools never returned."
Screen: the question at move 18 ("Why did you play C16 not D17?"), then the one at move 74 ("Will the corner
die?") with its variation drawn on the board.

## 1:50 to 2:30 The numbers

<!-- results -->
Voice: "We pre-registered three claims. Teaching instead of winning stays within the strength bound we set:
{{C1.elo}} Elo, with a {{C1.ci_pct}} percent interval from {{C1.lo}} to {{C1.hi}}, over {{C1.n}} games against the
raw engine. On recorded human games, the moves Sensei teaches one rank band are played {{C2.U_pct}} percentage
points more often by the next stronger band, a real but small effect that is no larger than for any good move. In
simulation, the curriculum closes {{C3.gap_pct}} percent of the gap to an oracle that knows the weak topics."
<!-- /results -->
Screen: the evaluation table from the README.

## 2:30 to 2:50 One honest limitation

Voice: "The curriculum result is a simulation, and the explanations were graded by one player: me. A study with
learners is the next step."
Screen: the session summary: the result, the rank estimate, the weakest topics and the next topic.

## 2:50 to 3:00 Close

Voice: "Sensei: KataGo finds the moves, Muse Spark explains them, and you learn the ones that matter at your
level."
Screen: repository URL.
