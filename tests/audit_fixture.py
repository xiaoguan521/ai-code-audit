"""将测试数据写成 Harness 的 Markdown 契约，用于完整调度测试。"""
import json


def markdown_from_result(result):
    def block(kind, fields):
        lines = [f"<!-- audit-{kind}", "schema_version: 1"]
        for key, value in fields.items():
            if key == "evidence" and value:
                lines.append("evidence:")
                for ref in value:
                    lines.append(f"  - path: {json.dumps(ref['path'], ensure_ascii=False)}")
                    lines.append(f"    start_line: {ref['start_line']}")
                    if "end_line" in ref:
                        lines.append(f"    end_line: {ref['end_line']}")
                    if "snippet" in ref:
                        lines.append(f"    snippet: {json.dumps(ref['snippet'], ensure_ascii=False)}")
            else:
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        return "\n".join([*lines, "-->"])

    blocks = [block("meta", {})]
    for item in result["items"]:
        blocks.append(block("item", {key: item[key] for key in ("id", "result", "risk", "evidence", "conclusion", "carried_over")
                                      if key in item}))
    for index, issue in enumerate(result.get("issues", []), 1):
        fields = {key: issue[key] for key in ("item_id", "level", "title", "evidence", "impact", "suggestion", "effort")}
        blocks.append(block("issue", {"issue_id": f"Q{index:03d}", **fields}))
    for group in result.get("need_info_list", []):
        blocks.append(block("need-info", group))
    blocks.append(block("complete", {"item_count": len(result["items"])}))
    return "# 审查报告\n\n" + "\n\n".join(blocks) + "\n"
