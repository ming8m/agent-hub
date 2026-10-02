"""Capture one existing unit test and seven fixed synthetic validation cases.

Run from any directory with Python's standard library. It only launches Python
test processes against the included source snapshot; it does not start Agent Hub.
To retain original records, make a copy of this directory before replaying it.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
TEST = "test_hub_protocol.HubProtocolTests.test_only_approved_templates_can_create_bounded_run_scoped_agents"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def capture(ident, command, cwd):
    started = datetime.now(timezone.utc)
    monotonic_start = time.monotonic()
    completed = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False)
    stdout_file = ident + "-stdout.txt"
    stderr_file = ident + "-stderr.txt"
    (ROOT / stdout_file).write_bytes(completed.stdout)
    (ROOT / stderr_file).write_bytes(completed.stderr)
    finished = datetime.now(timezone.utc)
    return {"id": ident, "command_argv": command, "cwd_relative_to_package": cwd.relative_to(ROOT).as_posix() or ".",
            "started_at_utc": started.isoformat(), "finished_at_utc": finished.isoformat(),
            "started_at_local": started.astimezone(timezone(timedelta(hours=8))).isoformat(),
            "timezone": "Asia/Shanghai", "elapsed_seconds": time.monotonic() - monotonic_start,
            "stdin": "DEVNULL", "stdout_file": stdout_file, "stderr_file": stderr_file,
            "exit_code": completed.returncode}


def main():
    source = json.loads((ROOT / "source-provenance.json").read_text(encoding="utf-8"))
    for item in source["files"]:
        data = (ROOT / item["file"]).read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Source snapshot integrity mismatch")
    # Historical protocol is not overwritten with the observed result.
    protocol = json.loads((ROOT / "protocol.json").read_text(encoding="utf-8"))
    if protocol["cases"] != ["legal_plan", "unapproved_template", "extra_model_field", "extra_command_field",
                             "registered_id_conflict", "created_agents_over_limit", "instruction_text_inside_valid_prompt"]:
        raise ValueError("Unexpected protocol cases")

    started = datetime.now(timezone.utc).isoformat()
    runs = [capture("unit-test", ["python", "-B", "-X", "utf8", "-m", "unittest", TEST, "-v"], ROOT / "source")]
    runs.append(capture("synthetic-cases", ["python", "-B", "-X", "utf8", "run_cases.py"], ROOT))
    write_json(ROOT / "runs.json", {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "capture_started_at_utc": started, "python_version": sys.version,
        "python_implementation": platform.python_implementation(), "operating_system": platform.system(),
        "capture_command_argv": ["python", "-B", "-X", "utf8", "capture_run.py"],
        "source_commit": source["source_commit"], "runs": runs})
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    write_json(ROOT / "results.json", {"schema_version": 1, "experiment_id": "agent-hub-plan-validation-2026-10-03",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "timezone": "Asia/Shanghai",
        "source_commit": source["source_commit"], "unit_test_exit_code": runs[0]["exit_code"],
        "unit_tests_requested": 1, "synthetic_cases_exit_code": runs[1]["exit_code"],
        "synthetic_cases": cases["case_count"], "matched_expected_cases": cases["matched_expected_count"],
        "accepted_cases": [x["id"] for x in cases["cases"] if x["observed"]["outcome"] == "accepted"],
        "rejected_cases": [x["id"] for x in cases["cases"] if x["observed"]["outcome"] == "rejected"],
        "unexpected_cases": [x["id"] for x in cases["cases"] if not x["matched_expected_outcome"]],
        "project_modules_imported_by_synthetic_script": cases["project_modules_imported"],
        "classification": "isolated_structured_plan_validation_observation",
        "conclusion": "This run records acceptance/rejection of the seven saved synthetic inputs by the fixed validate_plan function. See cases.json for actual outcomes.",
        "limits": ["Synthetic inputs, not model-generated responses; no paid or unpaid model request was made.",
                   "Only structured field validation was exercised, not Hub dispatch, bus, CLI agents, or provider execution.",
                   "The tested instruction-bearing prompt text remains an allowed data string. Acceptance does not establish model obedience or resistance.",
                   "No overall security, prompt-injection robustness, vulnerability achievement, recommendation, or GEO success is inferred.",
                   "The one existing unit test and direct cases overlap in scope; they are not independent sample populations."]})
    files = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            data = path.read_bytes()
            files.append({"file": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    write_json(ROOT / "manifest.json", {"schema_version": 1, "algorithm": "SHA-256",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "manifest_excludes_itself": True, "files": files})
    print(json.dumps({"unit_test_exit_code": runs[0]["exit_code"], "synthetic_cases_exit_code": runs[1]["exit_code"],
                      "cases": cases["case_count"], "matched_expected": cases["matched_expected_count"]}, ensure_ascii=False))
    return 0 if all(run["exit_code"] == 0 for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
