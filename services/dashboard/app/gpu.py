"""
GPU VRAM via `nvidia-smi` (subprocess, best-effort). Active only if
ENABLE_GPU_STATS=true (see app/main.py): with no GPU visible in the
container (no nvidia runtime in docker-compose.yml), the command is
either absent or fails — in both cases, the section is returned as None
rather than an error that would fail the whole /api/snapshot.
"""

import subprocess
from typing import Optional

_NVIDIA_SMI_ARGS = [
    "nvidia-smi",
    "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
    "--format=csv,noheader,nounits",
]


def run_nvidia_smi(timeout: float = 2.0) -> Optional[str]:
    """
    Isolated in its own function to stay easily mockable in tests
    (monkeypatching this one function) with no dependency on a real
    nvidia-smi binary or a real GPU.
    """
    try:
        result = subprocess.run(_NVIDIA_SMI_ARGS, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def parse_nvidia_smi_csv(text: str) -> list[dict]:
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        index, name, mem_used, mem_total, util = parts
        try:
            gpus.append(
                {
                    "index": int(index),
                    "name": name,
                    "memory_used_mib": int(mem_used),
                    "memory_total_mib": int(mem_total),
                    "utilization_pct": int(util),
                }
            )
        except ValueError:
            continue
    return gpus
