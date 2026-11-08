#!/bin/bash
# Engine benchmark on one SLURM GPU node (TASKS.md T10b): CUDA and TensorRT builds of KataGo, same positions,
# visits, nets and analysis config as every other machine. Writes results/latency_<host>_<backend>.json.
#
#   KATAGO_CUDA_EXE=/path/katago-cuda KATAGO_TRT_EXE=/path/katago-trt sbatch scripts/slurm_bench.sh
#
# KATAGO_MODEL, KATAGO_HUMAN_MODEL and KATAGO_CONFIG come from .env (or the environment), as on every machine.
#SBATCH --job-name=sensei-bench
#SBATCH --partition=b6000-al9_long
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=32G
#SBATCH --time=01:00:00
#SBATCH --output=logs/bench_%j.out
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"
source .venv/bin/activate
nvidia-smi
HOST="${BENCH_HOST:-p6000}"
if [[ -n "${KATAGO_CUDA_EXE:-}" ]]; then
  KATAGO_EXE="$KATAGO_CUDA_EXE" python scripts/bench_engine.py --backend cuda --host "$HOST"
fi
if [[ -n "${KATAGO_TRT_EXE:-}" ]]; then
  # The first TensorRT start builds the engine plan (can take minutes); the script's warm-up query absorbs it.
  KATAGO_EXE="$KATAGO_TRT_EXE" python scripts/bench_engine.py --backend trt --host "$HOST"
fi
