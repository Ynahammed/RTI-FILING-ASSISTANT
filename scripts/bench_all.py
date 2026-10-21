"""Run every RTI-Bench benchmark and enforce minimum quality gates.

Each benchmark writes its own artifact; this runner executes them in order, then
collects the scores into artifacts/benchmarks.json and checks them against the
thresholds below. A gate exists so a regression fails loudly instead of being
reported as a pass: the exit code is non-zero when any gate fails.

Gates are set well below the measured scores, not at them: they catch a model
that has collapsed or a feature that has started leaking, not normal noise.

Optional: `--with-eval` runs the 16-scenario end-to-end harness too, which needs
both services up (python -m scripts.dev).

Usage (project root, after downloading the dataset to data/rti-bench):
    python -m scripts.bench_all
    python -m scripts.bench_all --with-eval
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
SUMMARY = ARTIFACTS / "benchmarks.json"

STEPS = (
    ("rules_provenance", ["-m", "scripts.check_rules"]),
    ("registry_coverage", ["-m", "scripts.check_registry"]),
    ("personal_data_screen", ["-m", "scripts.bench_rti_bench"]),
    ("exemption_8_1_j", ["-m", "scripts.train_8_1_j_classifier"]),
    ("review_routing", ["-m", "scripts.bench_review_routing"]),
    ("exemptions_multilabel", ["-m", "scripts.bench_exemptions"]),
    ("precedent_retrieval", ["-m", "scripts.bench_retrieval"]),
    ("source_b_coverage", ["-m", "scripts.bench_source_b"]),
    ("citizen_requests", ["-m", "scripts.bench_citizen_requests"]),
)

# gate name -> (artifact, path into the artifact, comparator, threshold)
GATES: dict[str, tuple[str, tuple[str, ...], str, float]] = {
    "8(1)(j) ROC AUC": ("classifier_8_1_j_results.json", ("gate", "roc_auc"), ">=", 0.65),
    "8(1)(j) nested F1 beats keyword rule": ("classifier_8_1_j_results.json", ("gate", "f1_lift"), ">", 0.0),
    "multi-label macro AP beats base rate 2x": ("exemptions_multilabel_results.json", ("gate", "ap_lift"), ">=", 2.0),
    "retrieval P@10 beats chance 1.5x": ("retrieval_results.json", ("gate", "precision_lift"), ">=", 1.5),
    "honest variant selection holds macro AP": ("exemptions_multilabel_results.json", ("nested_variant_selection", "pooled_macro_ap"), ">=", 0.20),
    "calibrated ECE below 0.03": ("exemptions_multilabel_results.json", ("calibration", "ece_after"), "<=", 0.03),
    "binary calibrated ECE below 0.05": ("classifier_8_1_j_results.json", ("gate", "ece_after"), "<=", 0.05),
    "retrieval authority P@10 beats chance 2x": ("retrieval_results.json", ("gate", "authority_precision_lift"), ">=", 2.0),
    "every required rule is cited": ("rules_provenance_results.json", ("gate", "uncited"), "<=", 0),
    "every jurisdiction has an authority": ("registry_coverage_results.json", ("gate", "uncovered"), "<=", 0),
    "registry entries carry provenance": ("registry_coverage_results.json", ("gate", "missing_provenance"), "<=", 0),
    "no rule claims verification without a source": ("rules_provenance_results.json", ("gate", "unverified_claims"), "<=", 0),
    "review routing recall at least 0.35": ("review_routing_results.json", ("gate", "recall"), ">=", 0.35),
    "review routing precision at least 0.28": ("review_routing_results.json", ("gate", "precision"), ">=", 0.28),
    "review routing load at most 16%": ("review_routing_results.json", ("gate", "review_load"), "<=", 0.16),
    "operations completion at least 90%": ("operations_results.json", ("summary", "gate", "completion_rate"), ">=", 0.9),
    "operations no duplicate registrations": ("operations_results.json", ("summary", "duplicate_registrations"), "<=", 0),
    "operations no payment without approval": ("operations_results.json", ("summary", "payments_without_approval"), "<=", 0),
}
OPS_GATES = ("operations completion at least 90%", "operations no duplicate registrations", "operations no payment without approval")


def run_step(name: str, args: list[str]) -> dict[str, Any]:
    print(f"\n=== {name} ===")
    result = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    for line in result.stdout.strip().splitlines():
        print(f"  {line}")
    if result.returncode != 0:
        print(f"  FAILED with exit {result.returncode}", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
    return {"name": name, "exit": result.returncode, "ok": result.returncode == 0}


def read(name: str) -> dict[str, Any]:
    path = ARTIFACTS / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def gates() -> list[dict[str, Any]]:
    out = []
    for label, (artifact, path, op, threshold) in GATES.items():
        value = dig(read(artifact), path)
        if value is None:
            out.append({"gate": label, "value": None, "threshold": threshold, "passed": False})
            continue
        if op == ">=":
            passed = float(value) >= threshold
        elif op == "<=":
            passed = float(value) <= threshold
        else:
            passed = float(value) > threshold
        out.append(
            {"gate": label, "value": round(float(value), 4), "op": op, "threshold": threshold, "passed": passed}
        )
    scenarios = read("eval_results.json").get("results") or []
    if scenarios:
        passing = sum(1 for row in scenarios if row.get("passed"))
        out.append(
            {
                "gate": "end-to-end scenarios all pass",
                "value": f"{passing}/{len(scenarios)}",
                "op": "==",
                "threshold": f"{len(scenarios)}/{len(scenarios)}",
                "passed": passing == len(scenarios),
            }
        )
    return out


def summary() -> dict[str, Any]:
    personal = read("rti_bench_results.json")
    single = read("classifier_8_1_j_results.json")
    multi = read("exemptions_multilabel_results.json")
    retrieval = read("retrieval_results.json")
    return {
        "personal_data_screen": {
            field["field"]: {k: field[k] for k in ("precision", "recall", "f1")}
            for field in personal.get("fields", [])
            if not field.get("leaky")
        },
        "exemption_8_1_j": {
            "rows": single.get("rows_evaluated"),
            "positives": single.get("positives_evaluated"),
            "roc_auc": dig(single, ("gate", "roc_auc")),
            "avg_precision": dig(single, ("gate", "avg_precision")),
            "nested_f1": dig(single, ("nested_threshold_evaluation", "pooled_metrics", "f1")),
            "keyword_f1": dig(single, ("keyword_screen_oof_rows", "f1")),
            "calibrated_ece": dig(single, ("calibration", "ece_after")),
            "selective_precision_at_50": dig(single, ("gate", "selective_precision_at_50")),
        },
        "exemptions_multilabel": {
            "labels": multi.get("labels"),
            "rows": multi.get("rows_evaluated"),
            "macro_ap": dig(multi, ("variants", "char", "macro_ap")),
            "macro_roc_auc": dig(multi, ("variants", "char", "macro_roc_auc")),
            "nested_micro_f1": dig(multi, ("nested_threshold", "pooled_micro_f1")),
            "nested_variant_macro_ap": dig(multi, ("nested_variant_selection", "pooled_macro_ap")),
            "mean_ece": dig(multi, ("variants", "char", "mean_ece")),
            "calibrated_ece": dig(multi, ("calibration", "ece_after")),
        },
        "rules_provenance": dig(read("rules_provenance_results.json"), ("provenance",)),
        "registry": dig(read("registry_coverage_results.json"), ("coverage",)),
        "review_routing": {
            "threshold": dig(read("review_routing_results.json"), ("chosen_threshold",)),
            "rule_only_recall": dig(read("review_routing_results.json"), ("rule_only", "recall")),
            "recall": dig(read("review_routing_results.json"), ("gate", "recall")),
            "precision": dig(read("review_routing_results.json"), ("gate", "precision")),
            "review_load": dig(read("review_routing_results.json"), ("gate", "review_load")),
            "nested_recall": dig(
                read("review_routing_results.json"), ("nested", "pooled_metrics", "recall")
            ),
        },
        "operations": dig(read("operations_results.json"), ("summary",)),
        "citizen_requests": {
            "present": dig(read("citizen_requests_results.json"), ("present",)),
            "rows": dig(read("citizen_requests_results.json"), ("rows",)),
        },
        "source_b": {
            "corpus_total": dig(read("source_b_results.json"), ("corpus", "total_cases")),
            "scored_share": dig(read("source_b_results.json"), ("corpus", "scored_share_of_corpus")),
            "labelled_documents": dig(read("source_b_results.json"), ("source_b", "documents_with_any_section")),
            "transfer_roc_auc": dig(read("source_b_results.json"), ("transfer", "roc_auc")),
        },
        "end_to_end": {
            "passed": sum(1 for row in (read("eval_results.json").get("results") or []) if row.get("passed")),
            "total": len(read("eval_results.json").get("results") or []),
            "generated_at": read("eval_results.json").get("generated_at"),
        },
        "precedent_retrieval": {
            entry.get("relevance"): {
                "precision@10": entry.get("precision_at_k", {}).get("10"),
                "mrr@10": entry.get("mrr_at_10"),
                "ndcg@10": entry.get("ndcg_at_10"),
                "chance_precision@10": entry.get("chance_precision_at_10"),
            }
            for entry in retrieval.get("relevance", [])
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-eval", action="store_true", help="also run the end-to-end scenario harness")
    parser.add_argument(
        "--with-ops", action="store_true",
        help="also run the operational benchmark over real requests (needs both services)",
    )
    args = parser.parse_args()

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    steps = [run_step(name, cmd) for name, cmd in STEPS]
    if args.with_eval:
        steps.append(run_step("end_to_end", ["-m", "scripts.run_eval"]))
    if args.with_ops:
        steps.append(run_step("operations", ["-m", "scripts.bench_operations", "--limit", "40", "--concurrency", "4"]))

    checks = [item for item in gates() if args.with_ops or item["gate"] not in OPS_GATES]
    payload = {
        "steps": steps,
        "gates": checks,
        "scores": summary(),
        "all_gates_passed": all(item["passed"] for item in checks),
    }
    SUMMARY.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== gates ===")
    for item in checks:
        mark = "PASS" if item["passed"] else "FAIL"
        print(f"  {mark}  {item['gate']}: {item['value']} (needs {item.get('op', '>=')} {item['threshold']})")

    failed_steps = [s for s in steps if not s["ok"]]
    print(f"\nWrote {SUMMARY.relative_to(ROOT)}")
    if failed_steps or not payload["all_gates_passed"]:
        print("Benchmark run did not meet its gates.", file=sys.stderr)
        return 1
    print("All benchmarks ran and every gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
