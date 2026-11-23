"""分维度审查必须保留证据校验、失败关闭和全局并发上限。"""
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import run_daily  # noqa: E402
import scoring  # noqa: E402
import segments  # noqa: E402
from test_runner import CK, valid_result  # noqa: E402


def make_part(dimension):
    whole = valid_result()
    ids = {item["id"] for item in CK["items"] if item["dimension"] == dimension}
    return {
        **({"inventory": whole["inventory"]} if dimension == "1" else {}),
        "items": [item for item in whole["items"] if item["id"] in ids],
        "issues": [], "need_info_list": [],
        "dimension_notes": [note for note in whole["dimension_notes"] if note["dimension"] == dimension],
    }


class SegmentContractTest(unittest.TestCase):
    def test_contract_contains_only_target_dimension(self):
        source = (ROOT / "skill/eight-quality-audit/SKILL.md").read_text(encoding="utf-8")
        for d in (str(n) for n in range(1, 9)):
            schema = segments.schema_for_dimension(CK, scoring.load_schema(), d)
            skill = segments.skill_for_dimension(source, CK, d)
            self.assertEqual(scoring.schema_errors(make_part(d), schema), [])
            self.assertEqual(skill.count("### 维度 "), 1)
            self.assertIn(f"### 维度 {d} ·", skill)
            wrong = make_part(d)
            wrong["items"][0]["id"] = "8.1" if d != "8" else "1.1"
            self.assertTrue(scoring.schema_errors(wrong, schema))

    def test_merge_preserves_all_missing_information(self):
        parts = {}
        for dimension in (str(n) for n in range(1, 9)):
            part = make_part(dimension)
            for item in part["items"]:
                item.update(result="NEED_INFO-补充材料", evidence=[])
            halfway = len(part["items"]) // 2
            part["need_info_list"] = [
                {"items": [i["id"] for i in items], "missing": f"材料 {dimension}-{n}", "why": "无法判定",
                 "form": "材料", "unlocks": "结论", "priority": "中"}
                for n, items in enumerate((part["items"][:halfway], part["items"][halfway:]))
            ]
            parts[dimension] = part
        result = segments.merge(CK, parts)
        self.assertEqual(len(result["need_info_list"]), 15)
        self.assertEqual({i for group in result["need_info_list"] for i in group["items"]}, {i["id"] for i in CK["items"]})
        self.assertEqual(scoring.schema_errors(result), [])
        self.assertEqual(scoring.validate(result, CK), [])

    def test_missing_dimension_and_unresolved_injection_never_merge(self):
        parts = {str(n): make_part(str(n)) for n in range(1, 9)}
        incomplete = dict(parts)
        del incomplete["6"]
        with self.assertRaisesRegex(ValueError, "不完整"):
            segments.merge(CK, incomplete)
        parts["2"]["prompt_injection_suspects"] = ["README.md:1"]
        with self.assertRaisesRegex(ValueError, "6.8"):
            segments.merge(CK, parts)


class SegmentRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "a.py").write_text("pass\n", encoding="utf-8")
        self.settings = {"agent": {"type": "pi-docker", "strategy": "dimensions", "dimension_parallelism": 4}}

    def call(self, ctx):
        ctx.mkdir()
        return run_daily.call_agent_dimensions("MODE: batch", self.repo, ctx, self.settings, 10, CK, None, None, False)

    def test_only_invalid_dimension_is_retried_and_merged(self):
        counts = {}
        lock = threading.Lock()
        def fake(_prompt, _repo, ctx, _settings, _timeout):
            dimension = ctx.name
            with lock:
                counts[dimension] = counts.get(dimension, 0) + 1
                count = counts[dimension]
            part = make_part(dimension)
            if dimension == "6" and count == 1:
                part["items"][0]["evidence"][0]["path"] = "missing.py"
            if dimension == "6" and count == 2:
                self.assertTrue((ctx / "previous_attempt.json").exists())
            return {"result": part, "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "call_agent", side_effect=fake):
            out = self.call(self.root / "ctx")
        self.assertEqual(counts, {str(n): (2 if n == 6 else 1) for n in range(1, 9)})
        self.assertEqual(run_daily.check_output(out["result"], CK, self.repo), [])
        self.assertEqual(len(out["result"]["items"]), 64)
        self.assertEqual(out["stats"]["dimensions"]["6"]["attempts"][0]["evidence_check"]["invalid_references"], 1)

    def test_persistently_invalid_dimension_prevents_a_result(self):
        def fake(_prompt, _repo, ctx, _settings, _timeout):
            part = make_part(ctx.name)
            if ctx.name == "6":
                part["items"][0]["evidence"][0]["path"] = "missing.py"
            return {"result": part, "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "call_agent", side_effect=fake):
            with self.assertRaisesRegex(run_daily.AgentOutputError, "不评分") as cm:
                self.call(self.root / "ctx")
        self.assertEqual(cm.exception.stats["dimensions"]["6"]["status"], "error")
        self.assertEqual(cm.exception.stats["dimensions"]["5"]["status"], "ok")
        self.assertFalse(cm.exception.retryable)

    def test_incremental_rechecks_security_without_repeating_other_dimensions(self):
        previous = valid_result()
        ctx = self.root / "ctx"
        ctx.mkdir()
        (ctx / "previous_result.json").write_text(json.dumps(previous), encoding="utf-8")
        counts = {}
        lock = threading.Lock()
        def fake(_prompt, _repo, part_ctx, _settings, _timeout):
            dimension = part_ctx.name
            self.assertTrue((part_ctx / "previous_result.json").exists())
            with lock:
                counts[dimension] = counts.get(dimension, 0) + 1
                count = counts[dimension]
            part = make_part(dimension)
            for item in part["items"]:
                item["carried_over"] = not (dimension == "6" and count > 1)
            return {"result": part, "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "call_agent", side_effect=fake):
            out = run_daily.call_agent_dimensions("增量", self.repo, ctx, self.settings, 10, CK, previous, ["new.py"], False)
        self.assertEqual(counts, {str(n): (2 if n == 6 else 1) for n in range(1, 9)})
        self.assertEqual(run_daily.incremental_problems(out["result"], previous, ["new.py"], False, self.repo), [])

    def test_hard_limit_is_not_retried_unchanged(self):
        counts = {}
        lock = threading.Lock()
        def fake(_prompt, _repo, ctx, _settings, _timeout):
            with lock:
                counts[ctx.name] = counts.get(ctx.name, 0) + 1
            if ctx.name == "6":
                raise run_daily.AgentOutputError("长度上限", retryable=False)
            return {"result": make_part(ctx.name), "cost_usd": None, "session_id": "s"}
        with mock.patch.object(run_daily, "call_agent", side_effect=fake):
            with self.assertRaisesRegex(run_daily.AgentOutputError, "长度上限"):
                self.call(self.root / "ctx")
        self.assertEqual(counts, {str(n): 1 for n in range(1, 9)})

    def test_global_limit_covers_concurrent_projects(self):
        self.settings["agent"].update(network="test", dimension_parallelism=8)
        self.settings["_model_limiter"] = threading.BoundedSemaphore(2)
        active = maximum = 0
        lock = threading.Lock()
        def fake(_prompt, _repo, ctx, _settings, _timeout):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.015)
                return {"result": make_part(ctx.name), "cost_usd": None, "session_id": "s"}
            finally:
                with lock:
                    active -= 1
        with mock.patch.object(run_daily, "call_pi_docker", side_effect=fake):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(self.call, (self.root / "ctx1", self.root / "ctx2")))
        self.assertEqual(maximum, 2)
        self.assertTrue(all(len(result["result"]["items"]) == 64 for result in results))


if __name__ == "__main__":
    unittest.main()
