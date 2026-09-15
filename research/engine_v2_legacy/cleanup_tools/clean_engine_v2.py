#!/usr/bin/env python3
"""Safely declutter src/engine_v2 around the active V4 research path.

The script is intentionally conservative:

* dry-run by default; pass --apply to change the working tree;
* requires the EngineV2 branch and a clean tracked working tree;
* computes the local Python dependency closure from the active V4 entrypoints;
* protects the North Star and V4 protocol documents;
* refuses to move files that are textually referenced by active Python code;
* moves everything else out of src/engine_v2 into a reproducibility archive;
* runs the active V4 self-tests and CLI import checks before and after cleanup;
* writes a machine-readable cleanup manifest and a small active-folder README.

No fitting/search/DSP code is modified. Historical files are moved, not deleted.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path


EXPECTED_BRANCH = "EngineV2"
ENGINE_REL = Path("src/engine_v2")
DEFAULT_ARCHIVE_REL = Path("research/engine_v2_legacy")

ACTIVE_ENTRYPOINTS = (
    "train_namtoclo_north_star_v4.py",
    "analyze_north_star_v4_stimulus_diagnostics.py",
    "test_north_star_v4.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

PROTECTED_FILES = (
    "ENGINE_V2_RESEARCH_NORTH_STAR.md",
    "NORTH_STAR_V4_STIMULUS_EXPERIMENT.md",
)

ACTIVE_TESTS = (
    "test_north_star_v4.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

CLI_CHECKS = (
    ("train_namtoclo_north_star_v4.py", "--help"),
    ("analyze_north_star_v4_stimulus_diagnostics.py", "--help"),
)

GENERATED_NAMES = {".DS_Store"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}


class CleanupError(RuntimeError):
    pass


def run(cmd: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if proc.returncode != 0:
        detail = ""
        if capture:
            detail = f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        raise CleanupError(f"Command failed ({proc.returncode}): {' '.join(cmd)}{detail}")
    return proc.stdout.strip() if capture else ""


def git(repo: Path, *args: str, capture: bool = True) -> str:
    return run(["git", *args], cwd=repo, capture=capture)


def repo_root() -> Path:
    try:
        root = run(["git", "rev-parse", "--show-toplevel"], capture=True)
    except CleanupError as exc:
        raise CleanupError("Run this script from inside the NamtoClo---GP50 git checkout") from exc
    return Path(root).resolve()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _unknown_untracked(repo: Path, engine: Path) -> list[str]:
    output = git(repo, "ls-files", "--others", "--exclude-standard", ENGINE_REL.as_posix())
    unknown = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        path = repo / line
        if "__pycache__" in path.parts or path.name in GENERATED_NAMES or path.suffix in GENERATED_SUFFIXES:
            continue
        unknown.append(line)
    return unknown


def require_preflight(repo: Path, engine: Path, allow_dirty: bool, allow_untracked: bool) -> None:
    branch = git(repo, "branch", "--show-current")
    if branch != EXPECTED_BRANCH:
        raise CleanupError(
            f"Refusing cleanup on branch {branch!r}; expected {EXPECTED_BRANCH!r}"
        )

    if not engine.is_dir():
        raise CleanupError(f"Missing EngineV2 directory: {engine}")

    for name in (*ACTIVE_ENTRYPOINTS, *PROTECTED_FILES):
        if not (engine / name).exists():
            raise CleanupError(f"Required active file is missing: {engine / name}")

    if not allow_dirty:
        tracked = git(repo, "status", "--porcelain", "--untracked-files=no")
        if tracked:
            raise CleanupError(
                "Tracked working tree is not clean. Commit/stash changes first, "
                "or use --allow-dirty if you deliberately accept the risk.\n" + tracked
            )

    if not allow_untracked:
        unknown = _unknown_untracked(repo, engine)
        if unknown:
            raise CleanupError(
                "Untracked files exist inside src/engine_v2. Move/commit them first, "
                "or use --allow-untracked if they are intentional:\n"
                + "\n".join(unknown)
            )


def root_python_modules(engine: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in engine.glob("*.py"):
        if path.name.startswith("."):
            continue
        modules[path.stem] = path
    return modules


def parse_local_imports(path: Path, modules: dict[str, Path]) -> set[Path]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise CleanupError(f"Cannot parse {path}: {exc}") from exc

    out: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in modules:
                    out.add(modules[top])
        elif isinstance(node, ast.ImportFrom) and node.module:
            # The current EngineV2 modules are flat top-level modules. Relative
            # package imports are not used by the active V4 path today.
            top = node.module.split(".", 1)[0]
            if top in modules:
                out.add(modules[top])
    return out


def dependency_closure(engine: Path) -> tuple[set[Path], dict[str, list[str]]]:
    modules = root_python_modules(engine)
    roots = [engine / name for name in ACTIVE_ENTRYPOINTS]
    keep: set[Path] = set()
    reasons: dict[str, list[str]] = {}
    queue: deque[tuple[Path, str]] = deque((p, "active entrypoint") for p in roots)

    while queue:
        path, reason = queue.popleft()
        path = path.resolve()
        rel = path.relative_to(engine.resolve()).as_posix()
        reasons.setdefault(rel, [])
        if reason not in reasons[rel]:
            reasons[rel].append(reason)
        if path in keep:
            continue
        keep.add(path)
        for dep in sorted(parse_local_imports(path, modules)):
            queue.append((dep, f"imported by {path.name}"))

    return keep, reasons


def tracked_engine_files(repo: Path) -> list[Path]:
    output = git(repo, "ls-files", ENGINE_REL.as_posix())
    files = []
    for line in output.splitlines():
        if line.strip():
            files.append(repo / line.strip())
    return files


def protect_text_references(
    engine: Path,
    tracked: list[Path],
    python_keep: set[Path],
    keep: set[Path],
    reasons: dict[str, list[str]],
) -> None:
    """Conservatively retain tracked files mentioned by active Python source.

    This catches path-by-string/subprocess/dynamic-load dependencies that AST
    import traversal cannot see. Matching uses both a candidate's relative path
    and basename. False positives retain a file rather than break the active path.
    """
    active_text = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in sorted(python_keep)
    )
    engine_resolved = engine.resolve()
    for path in tracked:
        rp = path.resolve()
        if rp in keep:
            continue
        rel = rp.relative_to(engine_resolved).as_posix()
        if rel == ".gitkeep":
            continue
        if rel in active_text or path.name in active_text:
            keep.add(rp)
            reasons.setdefault(rel, []).append("textually referenced by active Python")


def classify_plan(
    repo: Path, engine: Path
) -> tuple[list[Path], list[Path], dict[str, list[str]], set[Path]]:
    python_keep, reasons = dependency_closure(engine)
    tracked = tracked_engine_files(repo)
    keep: set[Path] = set(python_keep)

    for name in PROTECTED_FILES:
        p = (engine / name).resolve()
        keep.add(p)
        reasons.setdefault(name, []).append("protected research document")

    # README.md is generated by this cleanup. If one already exists, preserve it.
    existing_readme = engine / "README.md"
    if existing_readme.exists():
        keep.add(existing_readme.resolve())
        reasons.setdefault("README.md", []).append("active folder index")

    protect_text_references(engine, tracked, python_keep, keep, reasons)

    to_move: list[Path] = []
    to_remove: list[Path] = []
    for path in tracked:
        rp = path.resolve()
        if rp in keep:
            continue
        rel = rp.relative_to(engine.resolve()).as_posix()
        if rel == ".gitkeep":
            to_remove.append(rp)
        else:
            to_move.append(rp)
    return sorted(to_move), sorted(to_remove), reasons, keep


def remove_generated_clutter(engine: Path) -> list[str]:
    removed: list[str] = []
    for path in sorted(engine.rglob("*"), reverse=True):
        try:
            rel = path.relative_to(engine).as_posix()
        except ValueError:
            continue
        if path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
            removed.append(rel + "/")
            continue
        if path.is_file() and (
            path.name in GENERATED_NAMES or path.suffix in GENERATED_SUFFIXES
        ):
            path.unlink()
            removed.append(rel)
    return removed


def verify_active(engine: Path, kept_python: set[Path], phase: str) -> None:
    print(f"\n[{phase}] compiling active Python dependency closure...")
    compile_targets = [str(p) for p in sorted(kept_python) if p.exists()]
    if compile_targets:
        run([sys.executable, "-m", "py_compile", *compile_targets], cwd=engine)

    print(f"[{phase}] running active V4 self-tests...")
    for name in ACTIVE_TESTS:
        run([sys.executable, name], cwd=engine)

    print(f"[{phase}] checking active CLIs import successfully...")
    for command in CLI_CHECKS:
        run([sys.executable, *command], cwd=engine, capture=True)


def render_readme(kept_python: set[Path], engine: Path) -> str:
    names = sorted(p.relative_to(engine).as_posix() for p in kept_python)
    deps = [n for n in names if n not in ACTIVE_ENTRYPOINTS]
    return "\n".join(
        [
            "# EngineV2 active research workspace",
            "",
            "`ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research document.",
            "",
            "The active experiment is V4 stimulus-only NAM -> constrained GP50 distillation.",
            "Historical/unused EngineV2 files were moved to `research/engine_v2_legacy/` by",
            "`scripts/clean_engine_v2.py`; they were not deleted.",
            "",
            "## Active entrypoints",
            "",
            *[f"- `{n}`" for n in ACTIVE_ENTRYPOINTS],
            "",
            "## Current Python dependency closure",
            "",
            *[f"- `{n}`" for n in deps],
            "",
            "## Research documents",
            "",
            *[f"- `{n}`" for n in PROTECTED_FILES],
            "",
            "Do not restore legacy files to this directory merely for convenience. If V4/V4.1",
            "needs shared behaviour, extract the minimum reusable code deliberately and verify",
            "that the fitting/DSP behaviour is unchanged.",
            "",
        ]
    )


def print_plan(
    repo: Path,
    engine: Path,
    archive_root: Path,
    to_move: list[Path],
    to_remove: list[Path],
    reasons: dict[str, list[str]],
    keep: set[Path],
) -> None:
    print("EngineV2 cleanup plan")
    print("=====================")
    print("repo:   ", repo)
    print("active: ", engine)
    print("archive:", archive_root)
    print()

    print(f"KEEP ({len(keep)} tracked/current files):")
    for path in sorted(keep):
        if not path.exists():
            continue
        try:
            rel = path.relative_to(engine.resolve()).as_posix()
        except ValueError:
            continue
        why = "; ".join(reasons.get(rel, [])) or "active dependency"
        print(f"  {rel:<52} {why}")

    print(f"\nMOVE OUT OF src/engine_v2 ({len(to_move)} files):")
    for path in to_move:
        rel = path.relative_to(engine.resolve())
        print(f"  {rel.as_posix()} -> {(archive_root / rel).relative_to(repo).as_posix()}")

    if to_remove:
        print(f"\nREMOVE ({len(to_remove)} obsolete placeholder files):")
        for path in to_remove:
            print(" ", path.relative_to(engine.resolve()).as_posix())

    if not to_move and not to_remove:
        print("\nNo tracked clutter remains in src/engine_v2.")


def apply_plan(
    repo: Path,
    engine: Path,
    archive_root: Path,
    to_move: list[Path],
    to_remove: list[Path],
    kept_python: set[Path],
    reasons: dict[str, list[str]],
    stage: bool,
) -> None:
    if archive_root.resolve().is_relative_to(engine.resolve()):
        raise CleanupError("Archive root must be outside src/engine_v2")

    conflicts = []
    for src in to_move:
        rel = src.relative_to(engine.resolve())
        dest = archive_root / rel
        if dest.exists():
            conflicts.append(dest)
    if conflicts:
        raise CleanupError(
            "Archive destination already contains files that would be overwritten:\n"
            + "\n".join(str(p) for p in conflicts)
        )

    verify_active(engine, kept_python, "before cleanup")

    print("\nApplying git moves...")
    for src in to_move:
        rel = src.relative_to(engine.resolve())
        dest = archive_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", src.relative_to(repo).as_posix(), dest.relative_to(repo).as_posix(), capture=False)

    for src in to_remove:
        git(repo, "rm", src.relative_to(repo).as_posix(), capture=False)

    removed_generated = remove_generated_clutter(engine)

    readme = engine / "README.md"
    readme.write_text(render_readme(kept_python, engine), encoding="utf-8")

    north_star = engine / "ENGINE_V2_RESEARCH_NORTH_STAR.md"
    manifest = {
        "format": 1,
        "purpose": "Declutter active EngineV2 workspace without changing V4 fitting/DSP behaviour",
        "branch": EXPECTED_BRANCH,
        "source_commit": git(repo, "rev-parse", "HEAD"),
        "north_star_sha256": sha256_file(north_star),
        "active_entrypoints": list(ACTIVE_ENTRYPOINTS),
        "kept_python_dependency_closure": sorted(
            p.relative_to(engine.resolve()).as_posix() for p in kept_python
        ),
        "protected_documents": list(PROTECTED_FILES),
        "moved_from_engine_v2": sorted(
            p.relative_to(engine.resolve()).as_posix() for p in to_move
        ),
        "removed_placeholders": sorted(
            p.relative_to(engine.resolve()).as_posix() for p in to_remove
        ),
        "removed_generated_untracked": removed_generated,
        "keep_reasons": reasons,
    }
    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / "cleanup_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    verify_active(engine, kept_python, "after cleanup")
    # Verification/py_compile can regenerate bytecode; remove it again so the
    # active directory is actually clean when the script returns.
    removed_generated.extend(remove_generated_clutter(engine))
    manifest["removed_generated_untracked"] = sorted(set(removed_generated))
    (archive_root / "cleanup_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Hard invariant: every remaining tracked root Python file must be in the
    # computed active closure. This is the "only what we need" check.
    remaining_root_py = {
        p.resolve() for p in engine.glob("*.py") if p.is_file()
    }
    unexpected = sorted(remaining_root_py - kept_python)
    missing = sorted(kept_python - remaining_root_py)
    if unexpected or missing:
        lines = ["Active-root invariant failed after cleanup."]
        if unexpected:
            lines.append("Unexpected Python files: " + ", ".join(p.name for p in unexpected))
        if missing:
            lines.append("Missing Python files: " + ", ".join(p.name for p in missing))
        raise CleanupError("\n".join(lines))

    if stage:
        git(repo, "add", "-A", capture=False)

    print("\nCleanup completed successfully.")
    print("Active EngineV2 now contains only the V4 dependency closure + protected docs/README.")
    print("Historical files are preserved under:", archive_root)
    print("Review with: git status --short && git diff --stat")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Declutter src/engine_v2 around the active V4 dependency closure while "
            "preserving all historical files in a reproducibility archive."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cleanup. Without this flag the script only prints a plan.",
    )
    parser.add_argument(
        "--archive-root",
        default=str(DEFAULT_ARCHIVE_REL),
        help="Repository-relative archive destination (default: research/engine_v2_legacy).",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow tracked working-tree changes (not recommended).",
    )
    parser.add_argument(
        "--allow-untracked",
        action="store_true",
        help="Allow non-generated untracked files inside src/engine_v2 (not recommended).",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage all cleanup changes after successful verification.",
    )
    args = parser.parse_args()

    try:
        repo = repo_root()
        engine = (repo / ENGINE_REL).resolve()
        archive_root = (repo / args.archive_root).resolve()
        require_preflight(repo, engine, args.allow_dirty, args.allow_untracked)
        to_move, to_remove, reasons, keep = classify_plan(repo, engine)
        kept_python = {p for p in keep if p.suffix == ".py" and p.parent == engine}
        print_plan(repo, engine, archive_root, to_move, to_remove, reasons, keep)
        if not args.apply:
            print("\nDry run only. Re-run with --apply after reviewing the plan.")
            return 0
        apply_plan(
            repo,
            engine,
            archive_root,
            to_move,
            to_remove,
            kept_python,
            reasons,
            args.stage,
        )
        return 0
    except CleanupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
