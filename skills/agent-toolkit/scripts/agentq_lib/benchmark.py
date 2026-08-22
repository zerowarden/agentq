from __future__ import annotations

import json
import math
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .common import AgentQError, compact_line, find_executable, run_cmd


def benchmark_data(root: Path, commands: list[str], warmup: int, runs: int, prepare: str | None = None) -> dict[str, Any]:
    if not commands:
        raise AgentQError("at least one --command is required")
    hyperfine = find_executable("hyperfine")
    if hyperfine:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            output = Path(handle.name)
        try:
            args = [hyperfine, "--warmup", str(warmup), "--runs", str(runs), "--export-json", str(output), "--style", "basic"]
            if prepare:
                args += ["--prepare", prepare]
            args += commands
            result = run_cmd(args, cwd=root, timeout=max(120, runs * 120))
            if result.returncode != 0:
                raise AgentQError(compact_line(result.stderr or result.stdout or "hyperfine failed", 600))
            obj = json.loads(output.read_text(encoding="utf-8"))
            results = []
            for item in obj.get("results", []):
                results.append({
                    "command": item.get("command"), "mean": item.get("mean"), "stddev": item.get("stddev"),
                    "median": item.get("median"), "min": item.get("min"), "max": item.get("max"),
                    "times": item.get("times", []),
                })
            return {"engine": "hyperfine", "warmup": warmup, "runs": runs, "results": results}
        finally:
            output.unlink(missing_ok=True)

    results = []
    for command in commands:
        if prepare:
            subprocess.run(prepare, cwd=root, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(warmup):
            subprocess.run(command, cwd=root, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        times = []
        for _ in range(runs):
            if prepare:
                subprocess.run(prepare, cwd=root, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            start = time.perf_counter()
            proc = subprocess.run(command, cwd=root, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elapsed = time.perf_counter() - start
            if proc.returncode != 0:
                raise AgentQError(f"benchmark command failed: {command}")
            times.append(elapsed)
        results.append({
            "command": command, "mean": statistics.mean(times), "stddev": statistics.stdev(times) if len(times) > 1 else 0.0,
            "median": statistics.median(times), "min": min(times), "max": max(times), "times": times,
        })
    return {"engine": "python-fallback", "warmup": warmup, "runs": runs, "results": results}


def render_benchmark(data: dict[str, Any]) -> str:
    lines = [f"benchmark engine: {data['engine']}; warmup={data['warmup']}; runs={data['runs']}"]
    fastest = min((r["mean"] for r in data["results"]), default=None)
    for result in data["results"]:
        ratio = result["mean"] / fastest if fastest else 1.0
        lines.append(
            f"  {result['command']}: mean={result['mean']:.6f}s ±{result['stddev']:.6f}s "
            f"median={result['median']:.6f}s min={result['min']:.6f}s max={result['max']:.6f}s ({ratio:.2f}× fastest)"
        )
    lines.append("Treat results as empirical measurements under the current machine/load/cache conditions, not universal performance claims.")
    return "\n".join(lines)
