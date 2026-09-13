#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

from analyze_nam_student_diagnostics import evaluate_model, _print_summary
from distiller_v2_data import group_models, load_pairs, read_pair
from distiller_v2_dsp import warm
from distiller_v2_north_star_v2 import build_fit_evidence, evidence_esr
from evaluate_north_star_vs_original import (
    _discover_clo,
    _dump,
    _pair_metrics,
    _safe,
    read_compact_clo,
    render_compact_clo,
)


SLICE_CONTEXT = {
    "fit-real": {
        "role": "fit",
        "title": "NAM-CENTERED EXACT-MANIFEST FIT REAL DIAGNOSTICS",
        "note": (
            "These are the exact real FIT clips recorded in each frozen v2 report. "
            "They may have influenced coefficients."
        ),
    },
    "fit-synthetic": {
        "role": "fit",
        "title": "NAM-CENTERED EXACT-MANIFEST SYNTHETIC FIT DIAGNOSTICS",
        "note": (
            "These are the exact six synthetic probe families recorded in each frozen "
            "v2 report. Their segmented/level-balanced v2 evidence ESR is reported."
        ),
    },
    "selection": {
        "role": "selection",
        "title": "NAM-CENTERED EXACT-MANIFEST SELECTION DIAGNOSTICS",
        "note": (
            "These are the exact real SELECTION clips recorded in each frozen v2 report. "
            "They may have influenced round acceptance/candidate choice."
        ),
    },
    "benchmark": {
        "role": "benchmark",
        "title": "NAM-CENTERED EXACT-MANIFEST BENCHMARK DIAGNOSTICS",
        "note": (
            "These are the exact BENCHMARK clips recorded in each frozen v2 report. "
            "They remain evaluation-only."
        ),
    },
}
DEFAULT_ATTRIBUTION_SLICES = ("fit-real", "fit-synthetic", "selection")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError(f"Cannot read JSON report {path}: {exc}") from exc


def _discover_report(report_root: Path, pair) -> tuple[Path, dict]:
    root = Path(report_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    files = [root] if root.is_file() else sorted(root.rglob("report.json"))
    model_id = int(pair.model_id) if pair.model_id is not None else None

    candidates: list[tuple[Path, dict]] = []
    for path in files:
        report = _read_json(path)
        report_model_id = report.get("model_id")
        if model_id is not None and report_model_id is not None:
            try:
                if int(report_model_id) != model_id:
                    continue
            except (TypeError, ValueError):
                continue
        elif report.get("model_key") != pair.model_key:
            continue
        candidates.append((path, report))

    # Prefer the exact model_key when the report root contains historical runs
    # of the same model ID.
    exact_key = [item for item in candidates if item[1].get("model_key") == pair.model_key]
    if exact_key:
        candidates = exact_key

    if len(candidates) != 1:
        names = "\n  ".join(str(path) for path, _ in candidates[:12]) or "<none>"
        raise RuntimeError(
            f"Expected exactly one frozen report.json for {pair.model_name}; found "
            f"{len(candidates)}:\n  {names}"
        )
    return candidates[0]


def _manifest_rows(report: dict, material_slice: str) -> list[dict]:
    if material_slice not in SLICE_CONTEXT:
        raise ValueError(material_slice)
    material = report.get("material")
    if not isinstance(material, dict):
        raise RuntimeError("Frozen report has no material manifest")

    if material_slice in ("fit-real", "fit-synthetic"):
        rows = list(material.get("fit") or [])
        want_synthetic = material_slice == "fit-synthetic"
        rows = [row for row in rows if bool(row.get("synthetic")) == want_synthetic]
    else:
        rows = list(material.get(material_slice) or [])

    if not rows:
        raise RuntimeError(f"Frozen report manifest has no {material_slice} material")
    return rows


def _pairs_from_manifest(
    report: dict,
    material_slice: str,
    pairs_by_task: dict[str, object],
) -> list:
    rows = _manifest_rows(report, material_slice)
    model_id = report.get("model_id")
    out = []
    missing = []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        pair = pairs_by_task.get(task_id)
        if pair is None:
            missing.append(task_id or "<blank>")
            continue
        if model_id is not None and pair.model_id is not None and int(pair.model_id) != int(model_id):
            raise RuntimeError(
                f"Manifest task {task_id} belongs to model {pair.model_id}, expected {model_id}"
            )
        out.append(pair)
    if missing:
        raise RuntimeError(
            "Frozen report references task IDs absent from the teacher dataset: "
            + ", ".join(missing)
        )
    return out


def _verify_report_clo(report: dict, clo_path: Path) -> None:
    clo = read_compact_clo(clo_path)
    required = ("A128", "pk", "B512_device")
    missing = [key for key in required if key not in report]
    if missing:
        raise RuntimeError(
            "Frozen report lacks coefficient fields required for report/CLO parity: "
            + ", ".join(missing)
        )
    expected = {
        "A128": np.asarray(report["A128"], dtype=np.float64),
        "pk": np.asarray(report["pk"], dtype=np.float64),
        "B512_device": np.asarray(report["B512_device"], dtype=np.float64),
    }
    actual = {"A128": clo.a, "pk": clo.pk, "B512_device": clo.b}
    for key in required:
        if expected[key].shape != actual[key].shape or not np.allclose(
            expected[key], actual[key], rtol=2e-6, atol=5e-7
        ):
            delta = (
                float(np.max(np.abs(expected[key] - actual[key])))
                if expected[key].shape == actual[key].shape
                else float("inf")
            )
            raise RuntimeError(
                f"Frozen report/CLO mismatch for {key} in {clo_path}; max |delta|={delta:.6g}"
            )


def _optimizer_relevant_diagnostic(pairs, clo_path: Path, material_slice: str) -> dict:
    clo = read_compact_clo(clo_path)
    audio = []
    preds = []
    targets = []
    direct_esr = []
    for pair in pairs:
        x, target, _ = read_pair(pair)
        pred = render_compact_clo(x, clo)
        audio.append((x, target, pair))
        preds.append(pred)
        targets.append(target)
        direct_esr.append(float(_pair_metrics(pred, target)["esr"]))

    result = {
        "mean_direct_aligned_esr": float(np.mean(direct_esr)),
        "clips": len(pairs),
    }
    if material_slice in ("fit-real", "fit-synthetic"):
        evidence = build_fit_evidence(audio)
        result.update(
            {
                "v2_evidence_esr": float(evidence_esr(preds, targets, evidence)),
                "v2_virtual_examples": int(evidence.virtual_examples),
                "v2_source_units": int(evidence.source_units),
                "interpretation": (
                    "Uses the same v2 segmentation/level-balanced evidence definition as the "
                    "fitter, restricted to this exact manifest slice."
                ),
            }
        )
    elif material_slice == "selection":
        result["interpretation"] = (
            "Mean direct aligned ESR matches the quantity used by the v2 SELECTION round gate, "
            "evaluated on the final frozen device-domain CLO."
        )
    else:
        result["interpretation"] = (
            "Mean direct aligned ESR is evaluation-only on the frozen benchmark material."
        )
    return result


def _print_material(model_name: str, pairs) -> None:
    print(f"  {model_name}")
    for pair in pairs:
        print(
            f"    {pair.dataset}: {Path(pair.source_path).name} @ "
            f"{pair.start_s:.2f}s ({pair.duration_s:.2f}s)"
        )


def _print_optimizer_diagnostic(material_slice: str, diagnostic: dict) -> None:
    if "v2_evidence_esr" in diagnostic:
        print(
            f"optimizer-relevant {material_slice} v2 evidence ESR: "
            f"{diagnostic['v2_evidence_esr']:.6f} "
            f"({diagnostic['v2_virtual_examples']} virtual windows, "
            f"{diagnostic['v2_source_units']} source units)"
        )
    else:
        print(
            f"optimizer-relevant {material_slice} mean direct ESR: "
            f"{diagnostic['mean_direct_aligned_esr']:.6f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NAM-centered diagnostics on the exact material recorded by frozen North Star v2 "
            "report.json manifests. This is attribution only: NAM remains the target and no "
            "coefficients are changed."
        )
    )
    parser.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    parser.add_argument("--north-star-root", required=True)
    parser.add_argument(
        "--report-root",
        required=True,
        help="Frozen v2 run root containing each model's report.json",
    )
    parser.add_argument("--original-root")
    parser.add_argument("--output", default="~/NamtoCloNAMCenteredManifestDiagnostics")
    parser.add_argument("--model-regex", required=True)
    parser.add_argument(
        "--slice",
        choices=("all", *SLICE_CONTEXT.keys()),
        default="all",
        help=(
            "Exact frozen manifest slice. 'all' runs fit-real, fit-synthetic, and selection "
            "separately (default)."
        ),
    )
    args = parser.parse_args()

    teacher_root = Path(args.teacher_root).expanduser()
    all_pairs = load_pairs(teacher_root)
    groups = group_models(all_pairs)
    pairs_by_task = {}
    for pair in all_pairs:
        previous = pairs_by_task.get(pair.task_id)
        if previous is not None and previous.model_key != pair.model_key:
            raise RuntimeError(f"Teacher task ID is not unique: {pair.task_id}")
        pairs_by_task[pair.task_id] = pair

    rx = re.compile(args.model_regex, re.I)
    keys = [key for key in sorted(groups) if rx.search(groups[key][0].model_name)]
    if not keys:
        raise SystemExit("No teacher models matched --model-regex")

    slices = list(DEFAULT_ATTRIBUTION_SLICES) if args.slice == "all" else [args.slice]
    report_root = Path(args.report_root).expanduser()
    reports = {}
    selected = {}
    north_paths = {}
    original_paths = {}

    for key in keys:
        p0 = groups[key][0]
        report_path, report = _discover_report(report_root, p0)
        reports[key] = (report_path, report)
        north = _discover_clo(Path(args.north_star_root), p0, "north_star")
        _verify_report_clo(report, north)
        north_paths[key] = north
        original_paths[key] = (
            _discover_clo(Path(args.original_root), p0, "original")
            if args.original_root
            else None
        )
        for material_slice in slices:
            selected[(key, material_slice)] = _pairs_from_manifest(
                report, material_slice, pairs_by_task
            )

    print("NAM-CENTERED EXACT FROZEN-MANIFEST ATTRIBUTION")
    print("NAM teacher is the behavioral authority.")
    print("Frozen NSv2 is the student under test.")
    print("Each model uses the exact task IDs stored in its own frozen report.json.")
    print("Report/CLO A128, P/K and B512_device parity verified before evaluation.")
    if args.original_root:
        print("Original NamToClo is reported only as a baseline.")
    print("No coefficients are changed.")
    print("warming GP50 renderer...")
    warm()

    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    all_results = {}

    for material_slice in slices:
        context = SLICE_CONTEXT[material_slice]
        print(f"\n{context['title']}")
        print(context["note"])
        print("exact per-model material:")
        for key in keys:
            _print_material(groups[key][0].model_name, selected[(key, material_slice)])

        slice_results = []
        for key in keys:
            p0 = groups[key][0]
            pairs = selected[(key, material_slice)]
            directory = (
                output
                / _safe(material_slice)
                / _safe(p0.model_key + "__" + p0.model_name)
            )
            summary = evaluate_model(
                pairs,
                north_paths[key],
                directory,
                original_path=original_paths[key],
                role=context["role"],
            )
            report_path, report = reports[key]
            diagnostic = _optimizer_relevant_diagnostic(
                pairs, north_paths[key], material_slice
            )
            summary["manifest_slice"] = material_slice
            summary["frozen_report"] = str(report_path)
            summary["report_clo_parity_verified"] = True
            summary["material_source"] = "exact frozen report.json task IDs"
            summary["optimizer_relevant_diagnostic"] = diagnostic
            summary["frozen_report_reference_metrics"] = {
                "optimizer_fit_evidence_metrics": report.get(
                    "optimizer_fit_evidence_metrics"
                ),
                "optimizer_selection_metrics": report.get(
                    "optimizer_selection_metrics"
                ),
            }
            _dump(directory / "summary.json", summary)
            _print_summary(summary)
            _print_optimizer_diagnostic(material_slice, diagnostic)
            slice_results.append(summary)
        all_results[material_slice] = slice_results

    root_summary = {
        "method": "nam-centered-exact-frozen-manifest-attribution-v1",
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "nam_is_behavioral_authority": True,
        "original_converter_is_baseline_only": True,
        "diagnostics_do_not_change_coefficients": True,
        "material_source": "exact frozen report.json task IDs",
        "same_underlying_performances_across_models": False,
        "report_clo_parity_verified": True,
        "slices": slices,
        "results": all_results,
    }
    _dump(output / "summary.json", root_summary)
    print("\nWrote:", output / "summary.json")
    print("NAM remains the target; attribution diagnostics did not change the fitter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
