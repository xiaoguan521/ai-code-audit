"""Unit tests for runner/parsers/frontmatter.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from parsers import frontmatter


# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------

SAMPLE_REPORT = """\
---
score: 72.5
tier: B
security_score: 65
scan_type: full
scan_date: "2026-11-24"
dimension_scores: {功能适合性: 80, 性能效率: 70, 可靠性: 75, 稳定性: 68, 易用性: 90, 安全性: 65, 兼容性: 78, 可维护性: 55}
issues: 12
results: {PASS: 30, WARN: 10, FAIL: 8, N/A: 4, NEED_INFO: 12}
coverage: 81.3
dimensions_scored: 8
validation: passed
# This is a comment
---

# 八性代码审查报告
...
"""


def test_parse_basic_scalars():
    fm = frontmatter.parse(SAMPLE_REPORT)
    assert fm is not None
    assert fm["score"] == 72.5
    assert fm["tier"] == "B"
    assert fm["security_score"] == 65
    assert fm["scan_type"] == "full"
    assert fm["scan_date"] == "2026-11-24"
    assert fm["issues"] == 12
    assert fm["coverage"] == 81.3
    assert fm["dimensions_scored"] == 8
    assert fm["validation"] == "passed"


def test_parse_flow_mapping():
    fm = frontmatter.parse(SAMPLE_REPORT)
    assert fm is not None
    dim = fm["dimension_scores"]
    assert isinstance(dim, dict)
    assert dim["功能适合性"] == 80
    assert dim["安全性"] == 65
    assert dim["可维护性"] == 55
    results = fm["results"]
    assert isinstance(results, dict)
    assert results["PASS"] == 30
    assert results["FAIL"] == 8
    assert results["NEED_INFO"] == 12


def test_parse_no_frontmatter():
    assert frontmatter.parse("# Just a heading\nSome content.") is None
    assert frontmatter.parse("") is None


def test_parse_na_null():
    text = "---\nfield1: n/a\nfield2: null\nfield3: ~\n---\nBody\n"
    fm = frontmatter.parse(text)
    assert fm is not None
    assert fm["field1"] is None
    assert fm["field2"] is None
    assert fm["field3"] is None


def test_parse_boolean():
    text = "---\nflag_yes: true\nflag_no: false\n---\nBody\n"
    fm = frontmatter.parse(text)
    assert fm is not None
    assert fm["flag_yes"] is True
    assert fm["flag_no"] is False


def test_parse_quoted_string():
    text = '---\nname: "hello world"\nsingle: \'test\'\n---\nBody\n'
    fm = frontmatter.parse(text)
    assert fm is not None
    assert fm["name"] == "hello world"
    assert fm["single"] == "test"


def test_parse_inline_comment():
    text = "---\nval: 42  # a comment\n---\nBody\n"
    fm = frontmatter.parse(text)
    assert fm is not None
    assert fm["val"] == 42


def test_parse_empty_value():
    text = "---\nempty:\n---\nBody\n"
    fm = frontmatter.parse(text)
    assert fm is not None
    assert fm["empty"] == ""


def test_parse_comment_line_skipped():
    text = "---\n# full comment line\nkey: val\n---\nBody\n"
    fm = frontmatter.parse(text)
    assert fm is not None
    assert "# full comment line" not in fm
    assert fm["key"] == "val"


def test_parse_integer():
    text = "---\ncount: 100\n---\nBody\n"
    fm = frontmatter.parse(text)
    assert fm["count"] == 100
    assert isinstance(fm["count"], int)


# ---------------------------------------------------------------------------
# run_log_fields()
# ---------------------------------------------------------------------------

def test_run_log_fields_full():
    fields = frontmatter.run_log_fields(SAMPLE_REPORT)
    expected_keys = {"score", "tier", "security_score", "dimension_scores",
                     "issues", "results", "coverage", "dimensions_scored",
                     "validation", "scan_type", "scan_date"}
    assert set(fields.keys()) == expected_keys
    assert fields["score"] == 72.5
    assert fields["tier"] == "B"


def test_run_log_fields_empty():
    assert frontmatter.run_log_fields("No front matter here.") == {}


def test_run_log_fields_partial():
    text = "---\nscore: 50\ntier: C\nextra_field: ignored\n---\nBody\n"
    fields = frontmatter.run_log_fields(text)
    assert fields["score"] == 50
    assert fields["tier"] == "C"
    assert "extra_field" not in fields


def test_run_log_fields_no_relevant_keys():
    text = "---\nfoo: bar\nbaz: 42\n---\nBody\n"
    fields = frontmatter.run_log_fields(text)
    assert fields == {}

