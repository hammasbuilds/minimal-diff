#!/usr/bin/env bash
# The model arm, end to end: qwen2.5-coder:14b repairs a sample of the tasks under three
# prompts (plain / minimal / diff), every reply is cached, scored against the same
# oracle as the classical search, and summarised to results/model_arm_<model>.json.
#
#   scripts/run_models.sh --dry-run   # the job list and call count; touches no model
#   scripts/run_models.sh             # the run (resumable: cached replies are reused)
#
# Environment: MODEL (default qwen2.5-coder:14b), OLLAMA_URL (default
# http://127.0.0.1:11434), PER_SOURCE (problems per benchmark, default 200),
# MIN_FREE_RAM_GB (default 5), MIN_FREE_VRAM_GB (default 11).
set -euo pipefail
cd "$(dirname "$0")/.."
unset VIRTUAL_ENV

MODEL="${MODEL:-qwen2.5-coder:14b}"
URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
PER_SOURCE="${PER_SOURCE:-200}"
MIN_FREE_RAM_GB="${MIN_FREE_RAM_GB:-5}"
MIN_FREE_VRAM_GB="${MIN_FREE_VRAM_GB:-11}"
ARGS=(--model "$MODEL" --url "$URL" --per-source "$PER_SOURCE")

if [[ "${1:-}" == "--dry-run" ]]; then
    exec uv run minimal-diff model plan "${ARGS[@]}"
fi

free_ram_gb() {
    if command -v powershell >/dev/null 2>&1; then
        powershell -NoProfile -Command \
            "[int]((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB)"
    else
        awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo
    fi
}

ram=$(free_ram_gb | tr -d '\r')
if (( ram < MIN_FREE_RAM_GB )); then
    echo "only ${ram} GB RAM free (need ${MIN_FREE_RAM_GB}); not starting" >&2
    exit 1
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    vram_mb=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1 | tr -d '\r ')
    if (( vram_mb < MIN_FREE_VRAM_GB * 1024 )); then
        echo "only ${vram_mb} MB VRAM free (need ${MIN_FREE_VRAM_GB} GB for a 14B at Q4); is something else training?" >&2
        exit 1
    fi
else
    echo "nvidia-smi not found; skipping the VRAM check" >&2
fi

if ! curl -sf "$URL/api/tags" | grep -qF "\"${MODEL}\""; then
    echo "ollama at $URL is not up, or $MODEL is not pulled (ollama pull $MODEL)" >&2
    exit 1
fi

echo "RAM ${ram} GB free; ollama up with ${MODEL}. Plan:"
uv run minimal-diff model plan "${ARGS[@]}"
uv run minimal-diff model run "${ARGS[@]}"
