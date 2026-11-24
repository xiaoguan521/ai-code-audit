"""Legacy structured-result checks, used only by the offline quality benchmark."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import evidence
import scoring


class OfflineValidationError(ValueError):
    pass


def check_output(result, checklist, repo=None):
    structural = scoring.schema_errors(result)
    if structural:
        raise OfflineValidationError(f"输出不符合 JSON Schema: {structural[:3]}")
    if repo is not None:
        evidence.normalize_references(result, repo)
    problems = scoring.validate(result, checklist)
    if repo is not None:
        problems.extend(evidence.verify(result, repo))
    return problems
