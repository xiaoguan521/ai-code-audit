"""把每日扫描结果渲染成：单项目报告（Markdown）、每日汇总报告、评分 CSV、历史趋势 CSV、静态看板 HTML。

可单独运行（例如修改模板后重渲染某一天）：
    python3 runner/render.py --date 2026-11-21
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import scoring

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "dashboard_template.html"
LEVELS = ("P0", "P1", "P2", "P3")


def fmt(v, nd=1) -> str:
    return "—" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def esc(s) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def evidence_text(refs: list) -> str:
    """新结果使用结构化引用，历史结果仍可能是旧格式的字符串。"""
    rendered = []
    for ref in refs:
        if isinstance(ref, dict):
            start = ref.get("start_line", "?")
            end = ref.get("end_line", start)
            rendered.append(f"{ref.get('path', '?')}:{start}" + (f"-{end}" if end != start else ""))
        else:
            rendered.append(str(ref))
    return "; ".join(rendered)


def load_day(outdir: Path, date: str) -> dict[str, dict]:
    d = outdir / "results" / date
    if not d.is_dir():
        return {}
    runlog = d / "_run.json"
    failed = {r["project_id"] for r in json.loads(runlog.read_text(encoding="utf-8")).get("projects", [])
              if r.get("status") == "error"} if runlog.exists() else set()
    return {f.stem: json.loads(f.read_text(encoding="utf-8"))
            for f in sorted(d.glob("*.json")) if not f.name.startswith("_") and f.stem not in failed
            and not (d / f"{f.stem}.error.txt").exists()}


def previous_dates(outdir: Path, date: str) -> list[str]:
    base = outdir / "results"
    return sorted(x.name for x in base.iterdir() if x.is_dir() and x.name < date) if base.is_dir() else []


# ---------- 单项目报告 ----------

def project_report(data: dict, checklist: dict) -> str:
    meta, sc, inv = data["meta"], data["score"], data["inventory"]
    proj = meta["project"]
    names = {i["id"]: i["name"] for i in checklist["items"]}
    dim_names = {d["id"]: d["name"] for d in checklist["dimensions"]}
    items = {i["id"]: i for i in data["items"]}
    issues = sorted(data.get("issues", []), key=lambda x: (x["level"], x["item_id"]))
    L = []
    L.append(f"# 《{proj['project_name']} 代码全面质量检查报告》\n")
    L.append(f"报告信息：检查日期 {meta['date']} | 提交 `{meta.get('commit', '')[:10]}`（{proj.get('branch', '')}）| "
             f"代码规模 约 {inv.get('file_count', '—')} 个文件 / {inv.get('loc_estimate', '—')} 行 | "
             f"技术栈 {', '.join(inv.get('languages', []) + inv.get('frameworks', [])) or '—'} | 检查方式 静态只读（AI + 规则） | "
             f"扫描类型 {meta.get('mode')} | 评估覆盖率 {pct(sc['coverage'])}（未评估 {sc['not_evaluated']} 项）\n")
    if meta.get("validation_problems"):
        L.append(f"> ⚠ 本次输出存在 {len(meta['validation_problems'])} 处自洽性问题，评分仅供参考："
                 + "；".join(meta["validation_problems"][:5]) + "\n")

    L.append("## 0. 执行摘要\n")
    for x in data["summary"].get("key_findings", []):
        L.append(f"- {x}")
    L.append(f"- **总评分 {fmt(sc['total'])}（{sc['grade']}）**，基于 {sc['scored_dimensions']}/8 个维度；"
             f"**安全评分 {fmt(sc['security_score'])}**")
    ic = sc["issue_counts"]
    L.append(f"- 问题数：P0 {ic['P0']} / P1 {ic['P1']} / P2 {ic['P2']} / P3 {ic['P3']}")
    if data["summary"].get("top_risks"):
        L.append("- 最需优先处理：" + "；".join(data["summary"]["top_risks"][:3]))
    L.append(f"- 尚未定论：{data['summary'].get('undetermined', '—')}\n")

    L.append("## 1. 项目盘点摘要\n")
    L.append(f"- 语言 / 框架 / 构建：{', '.join(inv.get('languages', []))} / {', '.join(inv.get('frameworks', []))} / {', '.join(inv.get('build_tools', []))}")
    L.append(f"- 入口：{', '.join(inv.get('entrypoints', [])) or '—'}；测试：{'有' if inv.get('has_tests') else '未见'}；"
             f"CI：{'有' if inv.get('has_ci') else '未见'}；锁文件：{', '.join(inv.get('lockfiles', [])) or '未见'}")
    if inv.get("suspected_cycles"):
        L.append(f"- 疑似循环依赖：{'; '.join(inv['suspected_cycles'])}")
    L.append(f"- 实际检查范围：{inv.get('checked_scope', '—')}\n")
    L.append("| 模块 | 职责 | 文件数 |\n|---|---|---|")
    for m in inv.get("modules", []):
        L.append(f"| {esc(m['path'])} | {esc(m['role'])} | {m.get('files', '—')} |")
    if inv.get("unchecked"):
        L.append("\n未检查部分：\n\n| 路径 | 原因 |\n|---|---|")
        for u in inv["unchecked"]:
            L.append(f"| {esc(u['path'])} | {esc(u['reason'])} |")
    L.append("")

    L.append("## 2. 评分总览\n")
    L.append("| 维度 | 得分 | 覆盖率 | PASS/WARN/FAIL/N/A/NEED_INFO | 最高风险 | 一句话结论 |\n|---|---|---|---|---|---|")
    notes = {n["dimension"]: n for n in data.get("dimension_notes", [])}
    for did, d in sc["dimensions"].items():
        c = d["counts"]
        s = fmt(d["score"]) if d["score"] is not None else "证据不足，暂不评分"
        L.append(f"| {did}. {d['name']} | {s} | {pct(d['coverage'])} | {c['PASS']}/{c['WARN']}/{c['FAIL']}/{c['N/A']}/{c['NEED_INFO']} "
                 f"| {d['top_risk']} | {esc(notes.get(did, {}).get('conclusion', ''))} |")
    L.append("")

    L.append("## 3. 风险问题清单\n")
    L.append("| ID | 维度 | 等级 | 问题标题 | 证据 | 影响 | 修复建议 | 工作量 |\n|---|---|---|---|---|---|---|---|")
    for n, i in enumerate(issues, 1):
        i["_id"] = f"Q{n:03d}"
        L.append(f"| {i['_id']} | {dim_names[i['item_id'].split('.')[0]]} {i['item_id']} | {i['level']} | {esc(i['title'])} | "
                 f"{esc(evidence_text(i.get('evidence', [])))} | {esc(i['impact'])} | 建议：{esc(i['suggestion'])} | {i['effort']} |")
    if not issues:
        L.append("| — | — | — | 本次未发现 WARN / FAIL 问题 | — | — | — | — |")
    L.append("")

    L.append("## 4. 分维度检查详述\n")
    for d in checklist["dimensions"]:
        did = d["id"]
        L.append(f"### 4.{did} {d['name']}\n")
        L.append(f"**本节结论**：{notes.get(did, {}).get('conclusion', '—')}\n")
        L.append("| 编号 | 检查项 | 结果 | 证据 / 依据 | 说明与建议 |\n|---|---|---|---|---|")
        for it in checklist["items"]:
            if it["dimension"] != did:
                continue
            r = items.get(it["id"], {})
            extra = ""
            if r.get("need_info"):
                ni = r["need_info"]
                extra = f"（缺：{ni.get('missing', '')}；因此无法判定：{ni.get('blocked_conclusion', '')}）"
            mark = r.get("result", "缺失") + ("·条件" if r.get("conditional") else "") + ("·沿用" if r.get("carried_over") else "")
            L.append(f"| {it['id']} | {it['name']} | {mark} | {esc(evidence_text(r.get('evidence', [])))} | {esc(r.get('conclusion', ''))}{esc(extra)} |")
        if notes.get(did, {}).get("key_analysis"):
            L.append(f"\n**重点问题分析**：{notes[did]['key_analysis']}")
        L.append("")

    L.append("## 5. 优先级修复路线图\n")
    titles = {"P0": "P0 立即", "P1": "P1 本迭代", "P2": "P2 排期", "P3": "P3 优化"}
    for lv in LEVELS:
        grp = [i for i in issues if i["level"] == lv]
        L.append(f"**{titles[lv]}**（{len(grp)}）")
        for i in grp:
            dep = f"；依赖 {', '.join(i['depends_on'])}" if i.get("depends_on") else "；可并行"
            L.append(f"- {i['_id']} {i['title']} —— 建议：{i['suggestion']}{dep}")
        L.append("")

    L.append("## 6. 待补充信息清单\n")
    L.append("| 编号 | 关联检查项 | 缺失信息 | 为什么需要 | 建议提供形式 | 可解锁的结论 | 优先级 |\n|---|---|---|---|---|---|---|")
    order = {"高": 0, "中": 1, "低": 2}
    for n, x in enumerate(sorted(data.get("need_info_list", []), key=lambda x: order.get(x["priority"], 3)), 1):
        L.append(f"| N{n:02d} | {', '.join(x['items'])} | {esc(x['missing'])} | {esc(x['why'])} | {esc(x['form'])} | {esc(x['unlocks'])} | {x['priority']} |")
    L.append("")

    L.append("## 7. 复审说明\n")
    L.append("把补充材料按 S1–S11 编号命名（如 `S1-需求说明.md`、`S3-压测报告.xlsx`）放入项目配置的补充材料目录并提交。"
             "下一次每日扫描检测到材料变化后，自动对相关检查项做增量复审并更新评分；"
             "也可以让维护人执行 `run_daily.py --only <项目ID>` 立即复审。\n")

    L.append("## 8. 报告自检\n")
    for c in data.get("self_check", []):
        L.append(f"- {'✅' if c['ok'] else '❌'} {c['check']}{'：' + c['note'] if c.get('note') else ''}")
    probs = scoring.validate(data, checklist)
    L.append(f"- {'✅' if not probs else '❌'} 程序校验（64 项齐全、WARN/FAIL 均有证据且在问题清单中）" + ("" if not probs else "：" + "；".join(probs[:5])))
    if data.get("prompt_injection_suspects"):
        L.append(f"- ⚠ 仓库中发现疑似针对 AI 的指令文本，已忽略：{'; '.join(data['prompt_injection_suspects'])}")
    if meta.get("evidence_verified"):
        L.append("- 程序已核验引用的路径、行号及所提供的片段；这不代表结论或风险定级已经人工核实。")
    else:
        L.append("- 历史结果未经过当前版本的源码引用核验，结论和风险定级需要人工核实。")
    L.append("- 评分由程序按固定规则计算。\n")

    L.append("## 附录 A：检查项完整索引\n")
    L.append("| 编号 | 维度 | 检查项 | 结果 | 风险 |\n|---|---|---|---|---|")
    for it in checklist["items"]:
        r = items.get(it["id"], {})
        L.append(f"| {it['id']} | {dim_names[it['dimension']]} | {it['name']} | {r.get('result', '缺失')} | {r.get('risk', '')} |")
    return "\n".join(L) + "\n"


# ---------- 汇总、CSV、看板 ----------

def row_of(pid: str, data: dict) -> dict:
    sc, meta = data["score"], data["meta"]
    r = {"project_id": pid, "project_name": meta["project"]["project_name"], "department": meta["project"].get("department", ""),
         "owner": meta["project"].get("owner", ""), "date": meta["date"], "mode": meta.get("mode", ""),
         "commit": meta.get("commit", "")[:10], "total": sc["total"], "grade": sc["grade"],
         "security": sc["security_score"], "coverage": sc["coverage"], "scored_dims": sc["scored_dimensions"],
         **{lv: sc["issue_counts"][lv] for lv in LEVELS},
         "security_p0": sc["security_p0"], "security_p1": sc["security_p1"],
         "cost_usd": meta.get("cost_usd")}
    for did, d in sc["dimensions"].items():
        r[f"d{did}"] = d["score"]
    return r


def render_all(outdir: Path, date: str, projects_csv: Path, settings: dict) -> None:
    checklist = scoring.load_checklist()
    today = load_day(outdir, date)
    prev_list = previous_dates(outdir, date)
    prev = load_day(outdir, prev_list[-1]) if prev_list else {}
    runinfo_f = outdir / "results" / date / "_run.json"
    runinfo = json.loads(runinfo_f.read_text(encoding="utf-8")) if runinfo_f.exists() else {"projects": []}
    errors = {r["project_id"]: r for r in runinfo["projects"] if r["status"] == "error"}

    rep_dir = outdir / "reports" / date
    rep_dir.mkdir(parents=True, exist_ok=True)
    rows, details = [], {}
    for pid, data in today.items():
        (rep_dir / f"{pid}.md").write_text(project_report(data, checklist), encoding="utf-8")
        row = row_of(pid, data)
        p = prev.get(pid)
        row["delta_total"] = round(row["total"] - p["score"]["total"], 1) if p and row["total"] is not None and p["score"]["total"] is not None else None
        row["delta_security"] = round(row["security"] - p["score"]["security_score"], 1) if p and row["security"] is not None and p["score"]["security_score"] is not None else None
        old_fp = {i.get("fingerprint") for i in (p or {}).get("issues", [])}
        new_issues = [i for i in data.get("issues", []) if i.get("fingerprint") not in old_fp] if p else []
        row["new_p0p1"] = sum(1 for i in new_issues if i["level"] in ("P0", "P1"))
        rows.append(row)
        details[pid] = {
            "dims": [{"id": d["id"], "name": d["name"], "score": d["score"], "coverage": d["coverage"]} for d in data["score"]["dimensions"].values()],
            "issues": [{"level": i["level"], "item": i["item_id"], "title": i["title"], "evidence": evidence_text(i.get("evidence", [])[:2]),
                        "new": i in new_issues} for i in sorted(data.get("issues", []), key=lambda x: x["level"])[:30]],
            "need_info": [x["missing"] for x in data.get("need_info_list", [])][:8],
        }
    for pid, e in errors.items():
        if pid not in today:
            rows.append({"project_id": pid, "project_name": e.get("project_name", pid), "department": e.get("department", ""),
                         "owner": e.get("owner", ""), "date": date, "mode": "失败", "error": e.get("error", ""), "total": None,
                         "security": None, "grade": "扫描失败"})

    # 最新评分表（Excel 可直接打开）
    cols = ["project_id", "project_name", "department", "owner", "date", "mode", "commit", "total", "grade", "delta_total",
            "security", "delta_security", "security_p0", "security_p1", "coverage", "scored_dims", *LEVELS, "new_p0p1",
            *[f"d{i}" for i in range(1, 9)], "cost_usd", "error"]
    for f in (rep_dir / "_scores.csv", outdir / "scores_latest.csv"):
        with f.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    # 历史：从各日结果重建，保证可重算
    hist = []
    for d in prev_list[-89:] + [date]:
        for pid, data in (today if d == date else load_day(outdir, d)).items():
            hist.append({"date": d, "project_id": pid, "total": data["score"]["total"], "security": data["score"]["security_score"],
                         **{lv: data["score"]["issue_counts"][lv] for lv in LEVELS}})
    with (outdir / "scores_history.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "project_id", "total", "security", *LEVELS])
        w.writeheader()
        w.writerows(hist)

    # 每日汇总报告
    ok = [r for r in rows if r.get("total") is not None or r.get("security") is not None]
    avg = lambda k: round(sum(r[k] for r in ok if r.get(k) is not None) / max(1, sum(1 for r in ok if r.get(k) is not None)), 1) if ok else None
    S = [f"# 八性代码质量每日检查汇总 · {date}\n",
         f"- 扫描项目 {len(rows)} 个：成功 {len(today)}，失败 {len(errors)}",
         f"- 平均总评分 {fmt(avg('total'))}，平均安全评分 {fmt(avg('security'))}",
         f"- 问题合计：" + " / ".join(f"{lv} {sum(r.get(lv, 0) or 0 for r in rows)}" for lv in LEVELS),
         f"- 较上次新增 P0/P1：{sum(r.get('new_p0p1', 0) or 0 for r in rows)}\n",
         "| 项目 | 部门 | 负责人 | 总评分 | 档位 | 变化 | 安全评分 | 安全 P0/P1 | 覆盖率 | P0/P1/P2/P3 | 扫描类型 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r.get("total") is None, r.get("security") if r.get("security") is not None else 99)):
        S.append(f"| {r['project_name']} | {r.get('department', '')} | {r.get('owner', '')} | {fmt(r.get('total'))} | {r.get('grade', '')} | "
                 f"{fmt(r.get('delta_total'))} | {fmt(r.get('security'))} | {r.get('security_p0', '—')}/{r.get('security_p1', '—')} | "
                 f"{pct(r.get('coverage'))} | {'/'.join(str(r.get(lv, '—')) for lv in LEVELS)} | {r.get('mode', '')} |")
    if errors:
        S.append("\n## 扫描失败\n")
        S += [f"- {pid}：{e.get('error', '')[:300]}" for pid, e in errors.items()]
    S.append("\n> 评分口径：八性 64 项逐项判定，PASS=1 / WARN=0.6 / FAIL=0，覆盖率不足 60% 的维度不评分；安全评分即“安全性”维度得分。")
    (rep_dir / "_summary.md").write_text("\n".join(S) + "\n", encoding="utf-8")

    # 看板
    trend = {}
    for h in hist:
        t = trend.setdefault(h["date"], {"total": [], "security": []})
        if h["total"] is not None:
            t["total"].append(h["total"])
        if h["security"] is not None:
            t["security"].append(h["security"])
    trend_rows = [{"date": d, "total": round(sum(v["total"]) / len(v["total"]), 2) if v["total"] else None,
                   "security": round(sum(v["security"]) / len(v["security"]), 2) if v["security"] else None}
                  for d, v in sorted(trend.items())]
    dim_avg = []
    for d in checklist["dimensions"]:
        vals = [r[f"d{d['id']}"] for r in ok if r.get(f"d{d['id']}") is not None]
        dim_avg.append({"id": d["id"], "name": d["name"], "avg": round(sum(vals) / len(vals), 2) if vals else None, "n": len(vals)})
    payload = {"date": date, "rows": rows, "details": details, "trend": trend_rows, "dims": dim_avg,
               "generated_at": runinfo.get("finished_at", "")}
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    dash = outdir / "dashboard"
    dash.mkdir(parents=True, exist_ok=True)
    (dash / "index.html").write_text(html, encoding="utf-8")
    (dash / f"{date}.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default=str(ROOT / "config" / "settings.json"))
    ap.add_argument("--projects", default=str(ROOT / "config" / "projects.csv"))
    ap.add_argument("--date", required=True)
    a = ap.parse_args()
    st = json.loads(Path(a.settings).read_text(encoding="utf-8"))
    render_all(Path(st["output_dir"]), a.date, Path(a.projects), st)
