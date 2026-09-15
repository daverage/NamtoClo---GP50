#!/usr/bin/env python3
"""Run several independent V4.2 model trainings concurrently as separate
subprocesses of the existing, unmodified train_namtoclo_north_star_v42.py.

This is pure orchestration -- it does not change the fitting philosophy,
objective, DSP, stimulus, or search mechanics documented in
ENGINE_V2_RESEARCH_NORTH_STAR.md. Each model's optimization is already fully
independent of every other model (separate NAM, separate output directory),
so running N of them at once instead of one-at-a-time in a `for` loop changes
only wall-clock time, not results.

Usage:
    python3 -u run_batch_parallel.py \
        --teacher-root ~/NamtoCloArchive/NamtoCloTeacherDataset \
        --nam-root ~/NamtoCloArchive/NamtoCloNAMCorpus \
        --output ~/NamtoCloArchive/NamtoCloNorthStarV4_2_LineConverged \
        --stimulus-cache-root ~/NamtoCloArchive/NamtoCloNorthStarV4_StimulusOnly/_v4_teacher_cache \
        --v41-root ~/NamtoCloArchive/NamtoCloNorthStarV4_1_Converged \
        --v4-root ~/NamtoCloArchive/NamtoCloNorthStarV4_StimulusOnly \
        --v3-root ~/NamtoCloArchive/NamtoCloNorthStarV3_SearchRobust \
        --jobs 6 \
        --models-file models_subset.txt

--models-file: one exact model name per line (blank lines / lines starting
with # are ignored). Each name is matched with an anchored, escaped regex
(^...$) so it selects exactly that model, the same way you'd hand-pick one
model with --model-regex today.

Alternatively pass --model-regex to select a broader set the normal way (it
is resolved once up front via --list-models, then split into individual
per-model jobs so they can run in parallel).

Every extra CLI argument after `--` is passed through verbatim to each
per-model trainer invocation (e.g. -- --a-controls 24 --polish-cycles 12).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
TRAINER = THIS_DIR / "train_namtoclo_north_star_v42.py"


def _discover_all_model_names(common_args: list[str]) -> list[str]:
    proc = subprocess.run(
        [sys.executable, str(TRAINER), *common_args, "--list-models"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"--list-models failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    names = []
    for line in proc.stdout.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        # format: "<split> <model name> <model_key>" -- model name is
        # everything between the first token and the last token.
        parts = line.split(" ")
        if len(parts) < 3:
            continue
        name = " ".join(parts[1:-1])
        names.append(name)
    return names


def _load_models_file(path: Path) -> list[str]:
    names = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return names


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--teacher-root", default="~/NamtoCloArchive/NamtoCloTeacherDataset")
    p.add_argument("--nam-root", default="~/NamtoCloArchive/NamtoCloNAMCorpus")
    p.add_argument("--output", default="~/NamtoCloArchive/NamtoCloNorthStarV4_2_LineConverged")
    p.add_argument("--stimulus-cache-root")
    p.add_argument("--v41-root")
    p.add_argument("--v4-root")
    p.add_argument("--v3-root")
    p.add_argument("--jobs", type=int, default=6, help="Max concurrent model trainings.")
    p.add_argument("--models-file", help="File with one exact model name per line.")
    p.add_argument("--model-regex", help="Select models the normal way; each match becomes its own parallel job.")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="Passed through to each per-model trainer invocation (put after --).")
    args = p.parse_args()

    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    if not args.models_file and not args.model_regex:
        raise SystemExit("Provide --models-file or --model-regex")

    out_root = Path(args.output).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    log_dir = out_root / "_batch_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    common_args = ["--teacher-root", args.teacher_root, "--nam-root", args.nam_root, "--output", args.output]
    if args.stimulus_cache_root:
        common_args += ["--stimulus-cache-root", args.stimulus_cache_root]
    if args.v41_root:
        common_args += ["--v41-root", args.v41_root]
    if args.v4_root:
        common_args += ["--v4-root", args.v4_root]
    if args.v3_root:
        common_args += ["--v3-root", args.v3_root]

    extra = args.extra
    if extra and extra[0] == "--":
        extra = extra[1:]

    if args.models_file:
        model_names = _load_models_file(Path(args.models_file).expanduser())
    else:
        all_names = _discover_all_model_names(common_args)
        rx = re.compile(args.model_regex, re.I)
        model_names = [n for n in all_names if rx.search(n)]

    if not model_names:
        raise SystemExit("No models selected.")

    print(f"Batch: {len(model_names)} model(s), max {args.jobs} concurrent", flush=True)
    for n in model_names:
        print(f"  - {n}", flush=True)

    sem = threading.Semaphore(args.jobs)
    lock = threading.Lock()
    results: dict[str, int] = {}
    started_at = time.time()

    def slug(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:120]

    def run_one(name: str) -> None:
        with sem:
            log_path = log_dir / f"{slug(name)}.log"
            cmd = [
                sys.executable, "-u", str(TRAINER),
                *common_args,
                "--model-regex", f"^{re.escape(name)}$",
                *extra,
            ]
            with lock:
                print(f"[start] {name} -> {log_path}", flush=True)
            t0 = time.time()
            with open(log_path, "w") as logf:
                proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
            elapsed = time.time() - t0
            with lock:
                results[name] = proc.returncode
                status = "OK" if proc.returncode == 0 else f"FAILED(exit={proc.returncode})"
                print(f"[done]  {name} -> {status} in {elapsed/60:.1f} min", flush=True)

    threads = [threading.Thread(target=run_one, args=(n,), daemon=True) for n in model_names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_elapsed = time.time() - started_at
    ok = sum(1 for v in results.values() if v == 0)
    failed = [n for n, v in results.items() if v != 0]

    print(flush=True)
    print(f"Batch complete in {total_elapsed/60:.1f} min: {ok}/{len(model_names)} succeeded", flush=True)
    if failed:
        print("Failed models:", flush=True)
        for n in failed:
            print(f"  - {n} (see {log_dir / (slug(n) + '.log')})", flush=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
