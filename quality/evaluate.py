#!/usr/bin/env python3
"""比较真实扫描输出和固定的人工设计样例；不代替人工审计。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import offline_validation  # noqa: E402
import scoring  # noqa: E402
from dataset import fixture_digest  # noqa: E402

DATASET = Path(__file__).resolve().parent


def evidence_matches(refs: list[dict], expected: dict) -> bool:
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("path") != expected["path"]:
            continue
        start = ref.get("start_line", 0)
        end = ref.get("end_line", start)
        if start <= expected["end_line"] and end >= expected["start_line"]:
            return True
    return False


def evaluate(results: Path, manifest: dict) -> dict:
    dataset = json.loads((DATASET / "cases.json").read_text(encoding="utf-8"))
    checklist = scoring.load_checklist()
    cases = []
    passed = total = 0
    for case in dataset["cases"]:
        path = results / f"{case['id']}.json"
        record = {"id": case["id"], "checks": [], "validation_problems": []}
        cases.append(record)
        total += len(case["expectations"])
        pin = manifest.get("cases", {}).get(case["id"], {})
        if (manifest.get("dataset_version") != dataset["version"] or
                manifest.get("dataset_sha256") != hashlib.sha256((DATASET / "cases.json").read_bytes()).hexdigest() or
                pin.get("fixture_sha256") != fixture_digest(DATASET / "fixtures" / case["id"]) or not pin.get("commit")):
            record.update(status="invalid", validation_problems=["样例版本或内容摘要不匹配，请重新准备样例"])
            continue
        if not path.is_file():
            record["status"] = "missing"
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("meta"), dict) or result["meta"].get("commit") != pin["commit"]:
                record.update(status="invalid", validation_problems=["结果提交与样例基线不一致"])
                continue
            problems = offline_validation.check_output(result, checklist, DATASET / "fixtures" / case["id"])
        except (OSError, UnicodeError, json.JSONDecodeError, offline_validation.OfflineValidationError) as e:
            record.update(status="invalid", validation_problems=[str(e)[:500]])
            continue
        record["validation_problems"] = problems[:20]
        record["status"] = "invalid" if problems else "evaluated"
        by_id = {item["id"]: item for item in result["items"]}
        for expected in case["expectations"]:
            item = by_id.get(expected["item_id"], {})
            result_ok = item.get("result") in expected["results"]
            evidence_ok = "evidence" not in expected or evidence_matches(item.get("evidence", []), expected["evidence"])
            ok = result_ok and evidence_ok and not problems
            passed += int(ok)
            record["checks"].append({"item_id": expected["item_id"], "expected": expected["results"],
                                     "actual": item.get("result"), "evidence_matches": evidence_ok, "passed": ok})
    return {"dataset_version": dataset["version"], "description": dataset["description"],
            "passed_checks": passed, "total_checks": total, "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser(description="评估固定合成样例的扫描结果")
    parser.add_argument("--results", type=Path, required=True, help="包含各样例 project_id.json 的结果目录")
    parser.add_argument("--manifest", type=Path, required=True, help="prepare.py 生成的样例提交清单")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = evaluate(args.results, manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed_checks"] == report["total_checks"] else 1


if __name__ == "__main__":
    sys.exit(main())
