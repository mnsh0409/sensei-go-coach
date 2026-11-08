// Builds the GTC 2027 poster skeleton: 43" x 24", 1" white margin, four NVIDIA-required sections,
// figure slots sized for eval/poster_figures.py output, and {{tokens}} for every number.
// Run (needs pptxgenjs: npm install pptxgenjs):
//   node poster/build_poster.js && python poster/fix_pptx.py poster/sensei_gtc2027_poster.pptx
"use strict";
const path = require("path");
const pptxgen = require("pptxgenjs");
const { applyTheme } = require(path.join(__dirname, "apply_theme.js")); // writes the theme colors into the file

const OUT = path.join(__dirname, "sensei_gtc2027_poster.pptx");

const THEME = {
  name: "Sensei Poster",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "1C1C1C", lt1: "FFFFFF", dk2: "0F4C5C", lt2: "F2F2F2",
    accent1: "0F4C5C", accent2: "D9480F", accent3: "5C7A86", accent4: "2E6F40",
    accent5: "B5C9D1", accent6: "9A9A9A", hlink: "0F4C5C", folHlink: "5C7A86",
  },
};

const pres = new pptxgen();
pres.defineLayout({ name: "POSTER_43x24", width: 43, height: 24 });
pres.layout = "POSTER_43x24";
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "Sensei project";
pres.title = "Teaching Moves Under a Strength Bound";
const C = pres.SchemeColor;

// Geometry (inches). Design area is x 1..42, y 1..23.
const M = 1.0;
const COLW = 13.13, GUT = 0.8;
const X1 = M, X2 = M + COLW + GUT, X3 = M + 2 * (COLW + GUT);
const BODY = 28, CAP = 22, HEAD = 44, REF = 18;

const slide = pres.addSlide();
slide.background = { color: THEME.colors.lt1 };

// Helpers -----------------------------------------------------------------------------------
function tok(name) {
  return { text: `{{${name}}}`, options: { color: C.accent2, bold: true } };
}
function t(text, opts = {}) {
  return { text, options: Object.assign({ fontSize: BODY }, opts) };
}
function header(num, text, x, y, w) {
  slide.addShape(pres.ShapeType.ellipse, {
    x, y: y + 0.08, w: 0.7, h: 0.7, fill: { color: C.accent1 }, line: { color: C.accent1, width: 0 },
    objectName: `Section ${num} marker`,
  });
  slide.addText(String(num), {
    x, y: y + 0.08, w: 0.7, h: 0.7, align: "center", valign: "middle", fontSize: 30, bold: true,
    color: C.background1, margin: 0, isTextBox: true, objectName: `Section ${num} number`,
  });
  slide.addText(text, {
    x: x + 0.9, y, w: w - 0.9, h: 0.86, fontSize: HEAD, bold: true, color: C.text2,
    fontFace: THEME.headFontFace, margin: 0, valign: "middle", isTextBox: true, objectName: `Section ${num} header`,
  });
}
function bullets(items, x, y, w, h, name, size = BODY) {
  const runs = [];
  items.forEach((item, i) => {
    const parts = Array.isArray(item) ? item : [t(item)];
    parts.forEach((p, j) => {
      const o = Object.assign({}, p.options, { fontSize: p.options.fontSize || size });
      if (j === 0) { o.bullet = { indent: 28 }; o.paraSpaceAfter = 10; }
      if (j === parts.length - 1 && i < items.length - 1) o.breakLine = true;
      runs.push({ text: p.text, options: o });
    });
  });
  slide.addText(runs, { x, y, w, h, color: C.text1, valign: "top", margin: 0, isTextBox: true, objectName: name });
}
function figure(num, title, source, x, y, w, h) {
  slide.addShape(pres.ShapeType.rect, {
    x, y, w, h, fill: { color: C.background2 },
    line: { color: C.accent6, width: 2, dashType: "dash" }, objectName: `Figure ${num} slot`,
  });
  slide.addText(
    [
      { text: `Figure ${num} slot`, options: { fontSize: 34, bold: true, color: C.accent3, breakLine: true } },
      { text: title, options: { fontSize: 26, color: C.accent3, breakLine: true } },
      { text: `${(w).toFixed(1)}" x ${(h).toFixed(1)}", 300 dpi: ${source}`, options: { fontSize: 20, color: C.accent6 } },
    ],
    { x, y, w, h, align: "center", valign: "middle", isTextBox: true, objectName: `Figure ${num} label` }
  );
}
function caption(text, x, y, w, name, h = 0.8) {
  slide.addText(text, {
    x, y, w, h, fontSize: CAP, italic: true, color: C.accent3, margin: 0, valign: "top", isTextBox: true, objectName: name,
  });
}

// Title band --------------------------------------------------------------------------------
slide.addText("Teaching Moves Under a Strength Bound: A GPU-Accelerated Go Coaching Agent and Its Pre-Registered Evaluation", {
  x: M, y: 1.0, w: 36.6, h: 1.7, fontSize: 50, bold: true, color: C.text2, fontFace: THEME.headFontFace,
  margin: 0, valign: "middle", isTextBox: true, objectName: "Poster title",
});
slide.addText(
  [
    tok("author_1"), t(" ("), tok("affiliation_1"), t(")   "), tok("author_2"), t(" ("), tok("affiliation_2"), t(")"),
    t("      Code and results: "), tok("repo_url"),
  ],
  { x: M, y: 2.75, w: 36.6, h: 0.75, fontSize: 30, color: C.text1, margin: 0, valign: "middle", isTextBox: true, objectName: "Authors" }
);
slide.addShape(pres.ShapeType.rect, {
  x: 39.5, y: 1.0, w: 2.5, h: 2.5, fill: { color: C.background2 }, line: { color: C.accent6, width: 2, dashType: "dash" },
  objectName: "QR slot",
});
slide.addText("QR to repository\n(2.5\" square)", {
  x: 39.5, y: 1.0, w: 2.5, h: 2.5, align: "center", valign: "middle", fontSize: 18, color: C.accent3, isTextBox: true, objectName: "QR label",
});

// Column 1: problem, approach, system figure ------------------------------------------------
let y = 4.0;
header(1, "Problem Statement and Goal", X1, y, COLW);
bullets([
  "Strong Go engines return the best move. A learner needs the most instructive move, one they would not have found, from a coach that still plays well.",
  [t("Goal: select moves for teaching value under an explicit winrate-loss bound "), t("ε", { italic: true }),
   t(", explain them from engine evidence only, and measure the strength cost and the lesson relevance with a pre-registered protocol.")],
  [t("Compute: one evaluation needs about "), tok("C1.queries"), t(" searches of 200 visits (400 games); every learner move needs 11 rank-profile queries for rank estimation; the engine part of a coaching turn should stay under a second.")],
], X1, y + 1.0, COLW, 4.4, "Problem bullets");

y = 9.6;
header(2, "Approach", X1, y, COLW);
bullets([
  [t("Engine: KataGo main network (policy "), t("π", { italic: true }), t("E", { subscript: true }), t(", winrate "), t("V", { italic: true }),
   t(", ownership) and the HumanSL network (rank-conditioned human policy "), t("π", { italic: true }), t("L", { subscript: true }), t(").")],
  [t("Selector: among reliable candidates with "), t("V* − V(a) ≤ ε", { italic: true }), t(", choose the argmax of "),
   t("I(a) = π", { italic: true }), t("E", { subscript: true, italic: true }), t("(a) · log[(π", { italic: true }), t("E", { subscript: true, italic: true }),
   t("(a) + δ) / (π", { italic: true }), t("L", { subscript: true, italic: true }), t("(a) + δ)]", { italic: true }),
   t(", the pointwise contribution of a to KL(π"), t("E", { subscript: true }), t(" ‖ π"), t("L", { subscript: true }), t(").")],
  "Explanation: a language model narrates from tool results only (candidates, evaluations, variations); a guard rejects any coordinate absent from tool output.",
  "Learner model: rank by maximum likelihood over HumanSL profiles; weakness topics by LDA over move-situation tokens; discounted Thompson sampling chooses the next target topic.",
], X1, y + 1.0, COLW, 5.8, "Approach bullets");

figure(1, "System diagram: engine, selector, learner model, curriculum, explanation guard", "poster/figures/fig1_system.png", X1, 15.9, COLW, 5.5);
caption("Figure 1. One KataGo process serves the selector, the rank estimator and the explanation tools; the language model never chooses moves.", X1, 21.5, COLW, "Figure 1 caption", 0.9);

// Column 2: NVIDIA technology, hardware table, throughput and latency figures ---------------
y = 4.0;
header(3, "Application of NVIDIA Technology", X2, y, COLW);
bullets([
  [t("KataGo analysis engine on NVIDIA GPUs with the CUDA and TensorRT backends (b18c384nbt networks); one engine process serves "),
   tok("C1.conc"), t(" concurrent games through asynchronous JSON queries; going from 1 to 8 concurrent queries raised engine throughput "),
   tok("hw.conc_gain"), t("× on the same GPU.")],
  [t("Rank estimation scores each learner move under 11 HumanSL rank profiles, one cached human-policy query each ("), tok("hw.hp_ms"),
   t(" ms per query on the "), tok("C1.gpu"), t(").")],
  [t("The 400 pre-registered games at 200 visits per move finished in "), tok("C1.hours"), t(" h on one "), tok("C1.gpu"),
   t(" with "), tok("C1.conc"), t(" games in flight: "), tok("C1.gph"), t(" games per hour.")],
], X2, y + 1.0, COLW, 5.0, "NVIDIA bullets", 26);

const TH = { bold: true, color: THEME.colors.lt1, fill: { color: THEME.colors.accent1 }, fontSize: 16, align: "center", valign: "middle" };
const TD = { fontSize: 16, align: "center", valign: "middle", color: THEME.colors.dk1 };
const TK = { fontSize: 16, align: "center", valign: "middle", color: THEME.colors.accent2, bold: true };
const rows = [
  ["GPU", "Backend", "Driver / CUDA", "Visits / s (8 concurrent)", "analyze p50, 1 query (ms)"].map((h) => ({ text: h, options: Object.assign({}, TH) })),
];
for (const [gpu, g] of [["RTX 3090", "3090"], ["RTX 4090", "4090"], ["RTX PRO 6000", "p6000"]]) {
  for (const [be, b] of [["CUDA", "cuda"], ["TensorRT", "trt"]]) {
    rows.push([
      { text: gpu, options: Object.assign({}, TD) }, { text: be, options: Object.assign({}, TD) },
      { text: `{{hw.${g}.${b}.cu}}`, options: Object.assign({}, TK) },
      { text: `{{hw.${g}.${b}.vps}}`, options: Object.assign({}, TK) },
      { text: `{{hw.${g}.${b}.p50}}`, options: Object.assign({}, TK) },
    ]);
  }
}
slide.addTable(rows, {
  x: X2, y: 10.2, w: COLW, colW: [2.2, 1.8, 2.9, 3.1, 3.13], rowH: 0.46, fontFace: THEME.bodyFontFace,
  border: { type: "solid", color: THEME.colors.accent5, pt: 1 }, objectName: "Hardware table",
});
caption("Table 1. Engine throughput by GPU and backend from scripts/bench_engine.py: 50 fixed positions, 200 visits, the same nets and analysis config on every GPU.", X2, 13.55, COLW, "Table 1 caption", 0.75);

figure(2, "Engine throughput (visits per second) by GPU and backend, at 1, 4 and 8 concurrent queries", "results/latency_*.json via eval/poster_figures.py", X2, 14.4, COLW, 4.0);
caption("Figure 2. Visits per second at 1, 4 and 8 concurrent queries, by GPU and backend.", X2, 18.45, COLW, "Figure 2 caption", 0.6);
figure(3, "Coaching-turn latency breakdown: engine search, 11-profile human policy, explanation", "logs/turns.jsonl via eval/poster_figures.py", X2, 19.1, COLW, 3.1);
caption("Figure 3. Median and p95 per component of a live coaching turn over the two recorded games; the explanation is a remote language-model call.", X2, 22.25, COLW, "Figure 3 caption", 0.6);

// Column 3: methods, results, takeaways, references ----------------------------------------
y = 4.0;
header(4, "Benchmarking Methods", X3, y, COLW);
bullets([
  "Pre-registered before any run (configs/prereg.yaml); every result file records the commit, net SHA-256, analysis config, GPU, driver, CUDA, backend and concurrency.",
  [t("C1, strength cost: Sensei against the raw engine at equal visits, 200 openings × 2 colors, paired bootstrap over openings; "), t("ε", { italic: true }),
   t(" fixed by a pilot rule (largest pilot "), t("ε", { italic: true }), t(" scoring ≥ 0.35)." )],
  [t("C2, lesson relevance: "), tok("C2.details.n_games"), t(" public games from after the HumanSL release, four rank bands; uplift "),
   t("U", { italic: true }), t("k", { subscript: true, italic: true }), t(" = P"), t("k+1", { subscript: true }), t("(played s"), t("k", { subscript: true }),
   t(") − P"), t("k", { subscript: true }), t("(played s"), t("k", { subscript: true }), t("); game-clustered bootstrap; best move and random admissible move as descriptive baselines.")],
  [t("C3, curriculum in simulation: "), tok("C3.learners"), t(" simulated learners over "), tok("C3.sessions"),
   t(" sessions; Thompson sampling against round robin, greedy, uniform and an oracle.")],
], X3, y + 1.0, COLW, 5.5, "Methods bullets", 23);

y = 10.6;
header(5, "Results and Outcomes", X3, y, COLW);
const statW = (COLW - 0.6) / 3;
const stats = [
  ["{{C1.elo}}", "Elo strength cost at ε = {{C1.eps}}", "95% CI {{C1.lo}} to {{C1.hi}}"],
  ["{{C2.U_pct:.2f}}", "uplift U, percentage points", "95% CI {{C2.lo_pct:.2f}} to {{C2.hi_pct:.2f}}"],
  ["{{C3.gap_pct}}%", "of the oracle gap closed (simulation)", "95% CI {{C3.details.gap_closure.ci.0:.0%}} to {{C3.details.gap_closure.ci.1:.0%}}"],
];
stats.forEach((s, i) => {
  const x = X3 + i * (statW + 0.3);
  slide.addShape(pres.ShapeType.roundRect, {
    x, y: y + 1.0, w: statW, h: 2.2, rectRadius: 0.15, fill: { color: C.background2 }, line: { color: C.background2, width: 0 },
    objectName: `Stat card ${i + 1}`,
  });
  slide.addText(s[0], {
    x, y: y + 1.05, w: statW, h: 1.0, align: "center", valign: "middle", fontSize: 48, bold: true, color: C.accent2, margin: 0,
    isTextBox: true, objectName: `Stat value ${i + 1}`,
  });
  slide.addText([{ text: s[1], options: { fontSize: 22, color: C.text1, breakLine: true } }, { text: s[2], options: { fontSize: 20, color: C.accent3 } }], {
    x: x + 0.15, y: y + 2.05, w: statW - 0.3, h: 1.1, align: "center", valign: "top", margin: 0, isTextBox: true, objectName: `Stat label ${i + 1}`,
  });
});

const halfW = (COLW - 0.4) / 2;
figure(4, "Strength cost by ε (pilot points) with the confirmatory 95% CI", "results/pilot_C1_*.json, results/C1_*.json", X3, 14.1, halfW, 3.8);
figure(5, "Rise in play rate per band step for the lesson, the best move and a random admissible move", "results/relevance_baselines.json", X3 + halfW + 0.4, 14.1, halfW, 3.8);
caption("Figure 4 (left). Strength cost by ε in the pilot and at ε* in the confirmatory run, with 95% CIs. Figure 5 (right). Rise in play rate from one band to the next, with 95% CIs, for Sensei's lesson and two baselines.", X3, 17.95, COLW, "Figures 4 and 5 caption", 0.95);

bullets([
  [t("Teaching cost no measurable strength ("), tok("C1.elo"), t(" Elo; pre-registered limit "), tok("C1.threshold"), t(").")],
  [t("Lessons rise across bands "), tok("C2base.lesson_vs_rand"), t(" a random admissible move: relevance beyond move quality is not shown.")],
  [t("All 400 games ran on one GPU in "), tok("C1.hours"), t(" h; TensorRT reached "), tok("hw.trt_gain"), t(" the CUDA throughput.")],
], X3, 19.0, COLW, 2.6, "Takeaways", 22);

slide.addText(
  [
    { text: "References. ", options: { bold: true } },
    { text: "D. J. Wu, Accelerating self-play learning in Go, 2019; KataGo Human SL network, katagotraining.org, 2024. " +
      "R. McIlroy-Young et al., Aligning superhuman AI with human behavior: chess as a model system (Maia), KDD 2020. " +
      "MATILDA: engine-agnostic search with human policy guidance, arXiv 2606.25176, 2026. " +
      "Policies of multiple skill levels for better strength estimation in games, arXiv 2505.00279, 2025. " +
      "D. Blei, A. Ng, M. Jordan, Latent Dirichlet allocation, JMLR 2003. D. Russo et al., A tutorial on Thompson sampling, 2018." },
  ],
  { x: X3, y: 21.75, w: COLW, h: 1.25, fontSize: REF, color: C.accent3, margin: 0, valign: "top", isTextBox: true, objectName: "References" }
);

slide.addNotes(
  "Poster skeleton. Every {{token}} is replaced by poster/render_poster.py from results/*.json and logs/turns.jsonl; " +
  "figure slots are replaced by the PNGs that eval/poster_figures.py writes to poster/figures/ at 300 dpi. " +
  "Export: File > Export > PDF, 300 dpi, no compression. No NVIDIA logos or branding on the poster."
);

(async () => {
  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT);
})();
