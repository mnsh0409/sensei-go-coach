"""Fill the GTC 2027 poster skeleton from clean results (TASKS.md T10b, poster/TOKENS.md).

    python poster/render_poster.py                    # writes poster/sensei_gtc2027_poster_filled.pptx
    python poster/render_poster.py --allow-dirty --allow-missing --out preview.pptx    # draft preview only

Every `{{source.path}}` token is replaced from results/ through eval.tokens (clean results only), the human
tokens (`{{author_1}}`, `{{affiliation_1}}`, `{{author_2}}`, `{{affiliation_2}}`, `{{repo_url}}`) from
poster/poster_meta.yaml, and each figure slot by its PNG from poster/figures/ (eval/poster_figures.py) at the
slot's exact position and size; the QR slot gets a QR code of repo_url. A hardware-table row whose GPU or
backend was not benchmarked is removed. Text is replaced run by run (never text_frame.text), so formatting stays.
The human exports the filled file to PDF at 300 dpi from PowerPoint. Needs: pip install -e ".[poster]".
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Emu  # noqa: E402

from eval.tokens import TOKEN_RE, Resolver, TokenError  # noqa: E402

TEMPLATE = ROOT / "poster" / "sensei_gtc2027_poster.pptx"
FILLED = ROOT / "poster" / "sensei_gtc2027_poster_filled.pptx"
META = ROOT / "poster" / "poster_meta.yaml"
FIGURES = ROOT / "poster" / "figures"
HUMAN_TOKENS = ("author_1", "affiliation_1", "author_2", "affiliation_2", "repo_url")
HUMAN_RE = re.compile(r"\{\{(" + "|".join(HUMAN_TOKENS) + r")\}\}")
FIGURE_FILES = {
    1: "fig1_system.png",
    2: "fig2_throughput.png",
    3: "fig3_turn_latency.png",
    4: "fig4_c1_cost.png",
    5: "fig5_c2_uplift.png",
}
TABLE_NAME = "Hardware table"


def load_meta(path: Path = META) -> dict[str, str]:
    """Human-supplied names; author_2 and affiliation_2 may be empty (the second author is then dropped)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data = data or {}
    return {k: str(data.get(k) or "").strip() for k in HUMAN_TOKENS}


def _paragraphs(prs: Any) -> Iterator[tuple[str, Any]]:
    """(shape name, paragraph) for every text paragraph, table cells included, in document order."""
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    yield shape.name, para
            if shape.has_table:
                for r, row in enumerate(shape.table.rows):
                    for c, cell in enumerate(row.cells):
                        for para in cell.text_frame.paragraphs:
                            yield f"{shape.name}[{r},{c}]", para


def _drop_second_author(prs: Any) -> None:
    """Remove " {{author_2}} ({{affiliation_2}})" from the authors line when there is no second author."""
    for name, para in _paragraphs(prs):
        if name != "Authors":
            continue
        runs = list(para.runs)
        idx = next((i for i, r in enumerate(runs) if "{{author_2}}" in r.text), None)
        if idx is None:
            continue
        end = next(i for i in range(idx, len(runs)) if runs[i].text.startswith(")"))
        for r in runs[idx : end + 1]:
            r._r.getparent().remove(r._r)
        if idx > 0:
            runs[idx - 1].text = runs[idx - 1].text.rstrip() + "   "


def _drop_unresolved_table_rows(prs: Any, resolve: Callable[[str], str]) -> tuple[list[str], int]:
    """Remove hardware-table rows with a token that does not resolve; return the dropped row labels and the
    height they freed (EMU)."""
    dropped: list[str] = []
    freed = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if not (shape.has_table and shape.name == TABLE_NAME):
                continue
            tbl = shape.table
            keep_h = 0
            for row in list(tbl.rows)[1:]:
                text = " ".join(c.text for c in row.cells)
                try:
                    for m in TOKEN_RE.finditer(text):
                        resolve(m.group(0))
                except TokenError:
                    dropped.append(" ".join(c.text for c in list(row.cells)[:2]))
                    freed += row.height
                    tbl._tbl.remove(row._tr)
                    continue
                keep_h += row.height
            shape.height = Emu(tbl.rows[0].height + keep_h)
    return dropped, freed


def _close_table_gap(prs: Any, freed: int) -> None:
    """Move Table 1's caption up by the freed height and let Figure 2's slot take it over (the figure is built
    taller by the same amount, eval.poster_figures.fig2_size)."""
    if freed <= 0:
        return
    slide = prs.slides[0]
    caption = _shape(slide, "Table 1 caption")
    if caption is not None:
        caption.top = Emu(caption.top - freed)
    for name in ("Figure 2 slot", "Figure 2 label"):
        shape = _shape(slide, name)
        if shape is not None:
            shape.top = Emu(shape.top - freed)
            shape.height = Emu(shape.height + freed)


def fill_text(prs: Any, resolver: Resolver, meta: Mapping[str, str], allow_missing: bool = False) -> list[str]:
    """Replace every token run by run; return the problems (tokens left in place)."""
    problems: list[str] = []
    if not meta.get("author_2"):
        _drop_second_author(prs)

    def result_token(m: re.Match[str]) -> str:
        try:
            return resolver.value(m.group(1), m.group(2), m.group(3))
        except TokenError as err:
            if not allow_missing:
                problems.append(str(err))
            return m.group(0)

    def human_token(m: re.Match[str]) -> str:
        value = meta.get(m.group(1), "")
        if not value:
            if not allow_missing:
                problems.append(f"{{{{{m.group(1)}}}}}: set {m.group(1)} in poster/poster_meta.yaml")
            return m.group(0)
        return value

    for _name, para in _paragraphs(prs):
        for run in para.runs:
            if "{{" in run.text:
                run.text = TOKEN_RE.sub(result_token, HUMAN_RE.sub(human_token, run.text))
    return problems


def _shape(slide: Any, name: str) -> Any | None:
    return next((s for s in slide.shapes if s.name == name), None)


def _replace_slot(slide: Any, slot_name: str, label_name: str, image: str | io.BytesIO) -> bool:
    slot = _shape(slide, slot_name)
    if slot is None:
        return False
    slide.shapes.add_picture(image, slot.left, slot.top, slot.width, slot.height)
    for name in (slot_name, label_name):
        shape = _shape(slide, name)
        if shape is not None:
            shape._element.getparent().remove(shape._element)
    return True


def _fits(path: Path, width: int, height: int, tolerance_in: float = 0.05) -> bool:
    """True when the PNG, scaled to the slot's width, is the slot's height within the tolerance (no distortion)."""
    from PIL import Image  # installed with matplotlib and qrcode[pil]

    with Image.open(path) as im:
        px_w, px_h = im.size
    return abs(width * px_h / px_w - height) <= tolerance_in * 914400


def place_figures(prs: Any, figures: Path, repo_url: str, allow_missing: bool = False) -> list[str]:
    problems = []
    slide = prs.slides[0]
    manifest = figures / "manifest.json"
    built = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    if not allow_missing and (not built or built.get("allow_dirty")):
        problems.append(
            f"{figures} was not built from clean results: run python -m eval.poster_figures (without --allow-dirty)"
        )
    if not allow_missing and built and built.get("turns") != "results/turns.jsonl":
        problems.append("Figure 3: commit a copy of logs/turns.jsonl as results/turns.jsonl, then rebuild the figures")
    for num, file in FIGURE_FILES.items():
        path = figures / file
        if not path.is_file():
            if not allow_missing:
                problems.append(f"Figure {num}: {path} is missing; run python -m eval.poster_figures")
            continue
        slot = _shape(slide, f"Figure {num} slot")
        if slot is not None and not allow_missing and not _fits(path, slot.width, slot.height):
            problems.append(
                f"Figure {num}: {file} does not match its slot's shape; rebuild with python -m eval.poster_figures"
            )
        _replace_slot(slide, f"Figure {num} slot", f"Figure {num} label", str(path))
    if repo_url:
        import qrcode  # the poster extra

        buf = io.BytesIO()
        qrcode.make(repo_url, border=1, box_size=20).save(buf, format="PNG")
        buf.seek(0)
        _replace_slot(slide, "QR slot", "QR label", buf)
    elif not allow_missing:
        problems.append("QR: set repo_url in poster/poster_meta.yaml")
    return problems


def render(
    template: Path = TEMPLATE,
    out: Path = FILLED,
    results: Path = ROOT / "results",
    meta: Mapping[str, str] | None = None,
    figures: Path = FIGURES,
    allow_dirty: bool = False,
    allow_missing: bool = False,
) -> tuple[Path, list[str], list[str]]:
    """Write the filled poster; return (path, problems, dropped table rows). Nothing is written on a problem."""
    meta = load_meta() if meta is None else meta
    resolver = Resolver(results, allow_dirty)
    prs = Presentation(str(template))
    dropped, freed = _drop_unresolved_table_rows(prs, resolver.render)
    _close_table_gap(prs, freed)
    problems = fill_text(prs, resolver, meta, allow_missing)
    problems += place_figures(prs, figures, meta.get("repo_url", ""), allow_missing)
    if problems:
        return out, problems, dropped
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out, [], dropped


def poster_texts(path: Path) -> list[tuple[str, str]]:
    """(shape name, paragraph text) of a poster, for scripts/check_numbers.py."""
    prs = Presentation(str(path))
    return [(name, "".join(r.text for r in para.runs)) for name, para in _paragraphs(prs)]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(FILLED))
    ap.add_argument("--allow-dirty", action="store_true", help="preview with uncommitted results")
    ap.add_argument("--allow-missing", action="store_true", help="preview: leave unresolved tokens in place")
    args = ap.parse_args(argv)
    out, problems, dropped = render(
        out=Path(args.out), allow_dirty=args.allow_dirty, allow_missing=args.allow_missing
    )
    for row in dropped:
        print(f"removed table row {row!r}: not benchmarked (no clean results/latency_*.json)")
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        return 1
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
