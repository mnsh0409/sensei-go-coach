# Runbook: from a fresh checkout to the first results (Windows, PowerShell)

Every command below runs in Windows PowerShell from the repository folder. Paths assume KataGo in `C:\katago`;
`.env` already points there (only `MODEL_API_KEY` is empty). Steps 1 to 5 take about an hour of your time; the game
fetch in step 6 then runs for about three hours on its own, CPU only, and can start as soon as step 1 is done.

## 0. Start a session log

```powershell
cd C:\Users\Martin\Documents\MetaGlobalAIHackathon\sensei
New-Item -ItemType Directory -Force logs | Out-Null
Start-Transcript -Path "logs\runbook_$(Get-Date -Format yyyyMMdd-HHmmss).log"
```

Everything you type and every output goes to that file (`Stop-Transcript` ends it). Never type the API key into
the console: paste it into `.env` with Notepad (step 3), so it stays out of the log.

## 1. Python environment (5 minutes)

```powershell
.\.venv\Scripts\python.exe --version        # must say 3.12.x; if not: py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
.\.venv\Scripts\python.exe -m pytest -q     # expect 445 passed (one page-script test skips without Node.js)
.\.venv\Scripts\python.exe -m sensei.app --fake
```

The last command serves the offline demo at http://127.0.0.1:8000/ (fake engine, no API calls). Play a few moves,
ask a question, then press Ctrl+C.

## 2. KataGo and the two networks (10 minutes, a few hundred MB)

```powershell
$kg = "C:\katago"
New-Item -ItemType Directory -Force $kg | Out-Null
curl.exe -L -o "$kg\katago-v1.18.0.zip" https://github.com/lightvector/KataGo/releases/download/v1.18.0/katago-v1.18.0-cuda12.1-cudnn9.8.0-windows-x64.zip
Expand-Archive -Force "$kg\katago-v1.18.0.zip" "$kg\v1.18.0"
curl.exe -L -o "$kg\kata1-tf3-b11c768-s11003M-d5973M-7gres.bin.gz" https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-tf3-b11c768-s11003M-d5973M-7gres.bin.gz
curl.exe -L -o "$kg\b18c384nbt-humanv0.bin.gz" https://media.katagotraining.org/uploaded/networks/models_extra/b18c384nbt-humanv0.bin.gz
Get-ChildItem -Recurse "$kg\v1.18.0" -Filter katago.exe | Select-Object FullName
nvidia-smi
where.exe cudnn64_9.dll
& "$kg\v1.18.0\katago.exe" version
```

- If `katago.exe` is not directly in `C:\katago\v1.18.0`, change `KATAGO_EXE` in `.env` to the path printed above.
- `where.exe cudnn64_9.dll` must find cuDNN 9 (or the DLL must sit next to `katago.exe`). If it finds nothing, install
  CUDA 12 and cuDNN 9.8 or later from NVIDIA, or use KataGo's OpenCL build from the same release page with
  `KATAGO_BACKEND=opencl` (then pick a non-transformer net, such as a recent b18c384nbt one from
  https://katagotraining.org/networks/, because the main net above is a transformer).
- The main net is the strongest confidently-rated network listed on katagotraining.org on 2026-10-29. If step 4
  shows C1 would take too long on it, switch nets now, before any evaluation run.

## 3. API key (2 minutes)

```powershell
notepad .env
```

Paste your Meta Model API key after `MODEL_API_KEY=`, save, close. The other lines are already filled in.

## 4. Real-engine and API checks (10 minutes)

```powershell
.\.venv\Scripts\python.exe scripts/meta_smoke_test.py
.\.venv\Scripts\python.exe -m pytest -q -m "engine or network"
.\.venv\Scripts\python.exe -m sensei.cli --auto --engine sensei --moves 20
.\.venv\Scripts\python.exe scripts/bench_engine.py --backend cuda
.\.venv\Scripts\python.exe -m pip freeze --exclude-editable | Out-File -Encoding ascii requirements.lock
```

`bench_engine.py` ends with a line `C1 estimate at ... visits/s: pilot 120 games X h, confirmatory 400 games Y h`.
If Y is more than you can afford, lower `openings.n_openings` (at least 100) and `C1.n_games` (twice that) in
`configs/prereg.yaml` now, and log the change in its `revisions` list; never after a C1 game has been played.

## 5. Git repository and the clean C3 result (5 minutes)

```powershell
git init
git add -A
git commit -m "Sensei kit: first commit"
.\.venv\Scripts\python.exe -m eval.run --claim C3 --config configs/prereg.yaml
git add results
git commit -m "C3 simulation from a clean commit"
```

If git asks who you are: `git config --global user.name "Your Name"` and `git config --global user.email "you@example.com"`.

## 6. Public games and the splits (about 3 hours unattended, CPU only)

Run this in a second PowerShell window right after step 1; it does not need KataGo.

```powershell
cd C:\Users\Martin\Documents\MetaGlobalAIHackathon\sensei
.\.venv\Scripts\python.exe scripts/fetch_games.py --target 5 --max-requests 60 --contact "<repository URL or e-mail>"
.\.venv\Scripts\python.exe scripts/fetch_games.py --target 1600 --contact "<repository URL or e-mail>"
```

The first line is a one-minute trial; the second continues it (stop and restart at any time, it resumes). Then:

```powershell
.\.venv\Scripts\python.exe scripts/make_splits.py --dry-run
.\.venv\Scripts\python.exe scripts/make_splits.py
.\.venv\Scripts\python.exe scripts/explainer_set.py --build
git add data/splits.json data/splits_summary.md data/SOURCES.md eval/explainer_set.json
git commit -m "Draw the evaluation splits"
```

If the dry run reports a short band, it prints the `fetch_games.py --target N` to run next. `data/splits.json` is
drawn once: never redraw it after a C1 or C2 run.

## 7. C1 pilot (GPU; the time `bench_engine.py` printed)

```powershell
git status --short            # must print nothing: results from uncommitted code never reach the README
.\.venv\Scripts\python.exe -m eval.run --claim C1 --config configs/prereg.yaml --pilot --concurrency 8
git add results
git commit -m "C1 pilot"
```

The pilot picks epsilon* by the pre-registered rule and writes `results/pilot_C1_<time>.json`. Copy epsilon* into
`configs/default.yaml` `play.epsilon`, commit, and start the confirmatory run (TASKS.md T4b):

```powershell
.\.venv\Scripts\python.exe -m eval.run --claim C1 --config configs/prereg.yaml --concurrency 8
```

Stop the log with `Stop-Transcript`. The remaining steps (T5 to T10) are in TASKS.md; each lists its command.
