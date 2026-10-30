"""Meta Model API smoke test.

Step 1: list models. Step 2: tool-calling round trip. Step 3: image input (reads stones off a rendered 9x9 board).
Usage:  python scripts/meta_smoke_test.py [--skip-image]
Reads MODEL_API_KEY, META_BASE_URL and META_MODEL from the environment or .env.
Exit code 0 only if every step that ran passed. Token counts are appended to logs/api_calls.jsonl.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is a declared dependency; this only guards odd environments
    pass

from openai import OpenAI

BASE_URL = os.environ.get("META_BASE_URL", "https://api.meta.ai/v1")
MODEL = os.environ.get("META_MODEL", "muse-spark-1.3")
ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "api_calls.jsonl"
RESULTS = ROOT / "results"

TOOL = {
    "type": "function",
    "function": {
        "name": "get_engine_candidates",
        "description": (
            "Return the engine's top candidate moves for the current Go position, with policy prior "
            "and winrate for the side to move."
        ),
        "parameters": {
            "type": "object",
            "properties": {"top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["top_k"],
            "additionalProperties": False,
        },
    },
}

FAKE_RESULT = {
    "to_move": "B",
    "candidates": [
        {"move": "Q16", "prior": 0.41, "winrate": 0.470},
        {"move": "R14", "prior": 0.22, "winrate": 0.465},
        {"move": "C3", "prior": 0.05, "winrate": 0.462},
    ],
}

BOARD_SIZE = 9
BLACK_STONES = ["C3", "D4", "E5", "G7"]
WHITE_STONES = ["C7", "F3", "G4"]


def log_call(step: str, resp: object, t0: float) -> dict:
    usage = getattr(resp, "usage", None)
    rec = {
        "ts": round(time.time(), 3),
        "step": step,
        "model": MODEL,
        "latency_s": round(time.time() - t0, 3),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def step_models(client: OpenAI) -> bool:
    print("[1/3] list models")
    models = [m.id for m in client.models.list().data]
    print(f"  {len(models)} models: {', '.join(models[:8])}")
    if MODEL not in models:
        print(f"  FAIL: {MODEL} not in the model list; set META_MODEL to one of the above")
        return False
    print("  ok")
    return True


def step_tools(client: OpenAI) -> bool:
    print("[2/3] tool-calling round trip")
    messages: list[dict] = [
        {
            "role": "system",
            "content": "You are a Go coach. Never invent engine numbers; always get them from the tool first.",
        },
        {
            "role": "user",
            "content": (
                "Call get_engine_candidates with top_k=3. Then tell me which of those moves would surprise "
                "a 10k player most, and why."
            ),
        },
    ]
    t0 = time.time()
    r1 = client.chat.completions.create(model=MODEL, messages=messages, tools=[TOOL], tool_choice="auto")
    print(f"  call 1: {log_call('tools_1', r1, t0)}")
    msg = r1.choices[0].message
    calls = msg.tool_calls or []
    if not calls:
        print("  FAIL: the model did not call the tool")
        return False
    args = json.loads(calls[0].function.arguments or "{}")
    print(f"  tool call: {calls[0].function.name}({args})")
    messages.append(
        {
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in calls
            ],
        }
    )
    for c in calls:
        messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(FAKE_RESULT)})
    t0 = time.time()
    r2 = client.chat.completions.create(model=MODEL, messages=messages, tools=[TOOL])
    print(f"  call 2: {log_call('tools_2', r2, t0)}")
    text = r2.choices[0].message.content or ""
    print(f"  answer: {text[:300]}")
    if not any(m in text for m in ("Q16", "R14", "C3")):
        print("  FAIL: the answer does not mention any candidate returned by the tool")
        return False
    print("  ok")
    return True


def render_board_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    from sensei.coords import GO_COLUMNS, gtp_to_rc

    size_px, margin = 480, 48
    step = (size_px - 2 * margin) / (BOARD_SIZE - 1)
    img = Image.new("RGB", (size_px, size_px), (220, 179, 92))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    for i in range(BOARD_SIZE):
        p = margin + i * step
        draw.line([(margin, p), (size_px - margin, p)], fill=(0, 0, 0), width=2)
        draw.line([(p, margin), (p, size_px - margin)], fill=(0, 0, 0), width=2)
        draw.text((p - 4, size_px - margin + 14), GO_COLUMNS[i], fill=(0, 0, 0), font=font)
        draw.text((margin - 30, p - 6), str(BOARD_SIZE - i), fill=(0, 0, 0), font=font)
    radius = step * 0.45
    for color, stones in (((15, 15, 15), BLACK_STONES), ((245, 245, 245), WHITE_STONES)):
        for s in stones:
            row, col = gtp_to_rc(s, BOARD_SIZE)
            x, y = margin + col * step, margin + row * step
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color, outline=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def step_image(client: OpenAI) -> bool:
    print("[3/3] image input")
    b64 = base64.b64encode(render_board_png()).decode("ascii")
    prompt = (
        "This is a 9x9 Go board with coordinate labels (columns A-J without I, rows 1-9 from the bottom). "
        'List every stone. Reply with JSON only: {"black": ["C3", ...], "white": [...]}'
    )
    t0 = time.time()
    r = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
    )
    print(f"  call: {log_call('image', r, t0)}")
    text = r.choices[0].message.content or ""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        print(f"  FAIL: no JSON in reply: {text[:200]}")
        return False
    try:
        got = json.loads(match.group(0))
    except json.JSONDecodeError:
        print(f"  FAIL: unparseable JSON: {match.group(0)[:200]}")
        return False
    truth = {("B", s) for s in BLACK_STONES} | {("W", s) for s in WHITE_STONES}
    pred = {("B", str(s).upper()) for s in got.get("black", [])} | {
        ("W", str(s).upper()) for s in got.get("white", [])
    }
    tp = len(truth & pred)
    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(truth)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = {
        "model": MODEL,
        "board_size": BOARD_SIZE,
        "n_stones": len(truth),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "raw_reply": text,
        "ts": round(time.time(), 3),
    }
    (RESULTS / "smoke_image.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"  stone reading: precision {precision:.2f}, recall {recall:.2f} (no threshold; informs stretch S4)")
    print("  ok")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-image", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        print("MODEL_API_KEY is not set (put it in .env)")
        return 2
    client = OpenAI(base_url=BASE_URL, api_key=key)
    print(f"base_url={BASE_URL} model={MODEL}")
    results = [step_models(client), step_tools(client)]
    if not args.skip_image:
        results.append(step_image(client))
    ok = all(results)
    print("ALL PASSED" if ok else "SOME STEPS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
