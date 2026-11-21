"""生成模拟扫描结果，用于验证评分、报告与看板渲染（不调用模型）。"""
import json, random, sys, datetime as dt
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import scoring, render
out = Path(sys.argv[1]); ck = scoring.load_checklist()
projects = [("order", "订单中心", "交易研发部", "研发经理A"), ("crm", "客户管理平台", "业务中台部", "研发经理B"), ("app", "移动端 App", "终端研发部", "研发经理C")]
marks = ["PASS"]*5 + ["WARN"]*3 + ["FAIL", "N/A", "NEED_INFO-补充材料", "NEED_INFO-运行数据"]
for k, day in enumerate(["2026-11-19", "2026-11-20", "2026-11-21"]):
    recs = []
    for j, (pid, name, dept, owner) in enumerate(projects):
        random.seed(f"{pid}{k//2}")
        items, issues = [], []
        for it in ck["items"]:
            r = "NEED_INFO-补充材料" if it["id"] in ("1.1", "1.3") else random.choice(marks)
            risk = "none"
            if r in ("WARN", "FAIL"):
                risk = random.choice(["P1", "P2", "P3"]) if r == "FAIL" else random.choice(["P2", "P3"])
                if it["id"] == "6.4" and pid == "crm": risk = "P0"
                issues.append({"item_id": it["id"], "level": risk, "title": f"{it['name']}：示例问题", "evidence": [f"src/{pid}/module{j}.py:{10+int(it['dimension'])*7}"],
                               "impact": "示例影响", "suggestion": "示例修复建议", "effort": "M"})
            items.append({"id": it["id"], "result": r, "evidence": [f"src/{pid}/module{j}.py:{10+int(it['dimension'])*7}"] if r in ("PASS","WARN","FAIL") else ["静态未见相关实现"],
                          "risk": risk, "conclusion": "示例结论", "carried_over": k > 0,
                          **({"need_info": {"missing": "S1 需求文档", "why": "判定功能覆盖", "blocked_conclusion": "功能是否做全"}} if r.startswith("NEED") else {})})
        data = {"inventory": {"languages": ["Python"], "frameworks": ["FastAPI"], "build_tools": ["pip"], "file_count": 120, "loc_estimate": 15000,
                              "modules": [{"path": "src/", "role": "业务代码", "files": 100}], "checked_scope": "src/ 全部", "unchecked": [{"path": "vendor/", "reason": "第三方代码"}],
                              "entrypoints": ["src/main.py"], "has_tests": True, "has_ci": True, "lockfiles": ["requirements.lock"]},
                "items": items, "issues": issues,
                "need_info_list": [{"items": ["1.1", "1.3"], "missing": "S1 需求文档、S2 接口契约", "why": "判定功能完整性", "form": "文档原文", "unlocks": "1.1/1.3 结论", "priority": "高"}],
                "dimension_notes": [{"dimension": d["id"], "conclusion": f"{d['name']}示例结论", "key_analysis": "示例分析"} for d in ck["dimensions"]],
                "summary": {"key_findings": ["示例结论一"], "top_risks": ["示例风险"], "undetermined": "功能覆盖度需 S1"},
                "self_check": [{"check": "64 项齐全", "ok": True}]}
        data["meta"] = {"date": day, "mode": "full" if k == 0 else "incremental", "commit": f"{pid}abc{k}1234567", "cost_usd": 3.2,
                        "project": {"project_id": pid, "project_name": name, "department": dept, "owner": owner, "branch": "main"}}
        assert not scoring.validate(data, ck), scoring.validate(data, ck)
        data["score"] = scoring.score_result(data, ck, scoring.parse_weights("", ck))
        for i in data["issues"]: i["fingerprint"] = scoring.issue_fingerprint(pid, i)
        d = out / "results" / day; d.mkdir(parents=True, exist_ok=True)
        (d / f"{pid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        recs.append({"project_id": pid, "status": "ok"})
    recs.append({"project_id": "legacy", "project_name": "老系统", "department": "交易研发部", "owner": "研发经理A", "status": "error", "error": "clone 失败: 权限不足（示例）"})
    (out / "results" / day / "_run.json").write_text(json.dumps({"date": day, "finished_at": day + "T03:40:00+08:00", "projects": recs}, ensure_ascii=False), encoding="utf-8")
    render.render_all(out, day, ROOT / "config" / "projects.csv", {})
print("ok")
