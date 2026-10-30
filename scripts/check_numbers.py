"""Check that every number in the results blocks comes from clean results (TASKS.md T10, SCOPE.md checklist).

    python scripts/check_numbers.py              # exit 0 when every check passes, 1 otherwise

For each template in scripts/render_readme.py's PAIRS that exists:
1. no number is typed by hand inside a `<!-- results -->` block of the template (every number is a `{{token}}`);
2. every token resolves from a clean result file (git_dirty false, a real commit);
3. the output file (README.md, video_script.md) equals a fresh rendering, so nobody edited a number by hand and
   no newer clean result is missing from it.
Numbers outside the results blocks are not checked. The poster (T10b) is added here when its renderer exists.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from render_readme import PAIRS, render_file  # noqa: E402

from eval.tokens import BLOCK_RE, Resolver, TokenError, literal_numbers  # noqa: E402


def check(root: Path, results: Path) -> list[str]:
    """Problems found (empty when everything passes)."""
    problems: list[str] = []
    resolver = Resolver(results)
    checked = 0
    for template_name, output_name in PAIRS:
        template = root / template_name
        if not template.is_file():
            continue
        checked += 1
        text = template.read_text(encoding="utf-8")
        for i, block in enumerate(BLOCK_RE.findall(text), start=1):
            typed = literal_numbers(block)
            if typed:
                problems.append(f"{template_name} block {i}: numbers typed by hand: {', '.join(typed[:8])}")
        try:
            expected = render_file(template, resolver)
        except TokenError as err:
            problems.append(f"{template_name}: {err}")
            continue
        output = root / output_name
        if not output.is_file():
            problems.append(f"{output_name} is missing: run scripts/render_readme.py")
        elif output.read_text(encoding="utf-8") != expected:
            problems.append(f"{output_name} differs from a fresh rendering of {template_name}: rerun render_readme.py")
    if not checked:
        problems.append("no template found: create README.tmpl.md (TASKS.md T10)")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--results", default=None, help="results directory (default: <root>/results)")
    args = ap.parse_args(argv)
    root = Path(args.root)
    problems = check(root, Path(args.results) if args.results else root / "results")
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        return 1
    print("check_numbers: every results block is rendered from clean results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
