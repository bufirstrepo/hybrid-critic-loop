"""Versioned rubrics for judged gates.

A rubric is a fixed, versioned list of scored items on a fixed scale.
Judges score every item and must attach evidence per item (enforced in
judges.py: no rationale, no score). Rubric ids are never reused with
different contents -- a changed rubric is a new version / new id.
"""

from __future__ import annotations

from .errors import GateError

RUBRICS = {
    "code_quality_v1": {
        "version": 1,
        "scale": 10,
        "items": ["correctness", "idiom", "readability"],
    },
    "intent_v1": {
        "version": 1,
        "scale": 10,
        "items": ["conformance", "completeness", "constraint_respect"],
    },
}


def get_rubric(rubric_id) -> dict:
    """Return a copy of the rubric for ``rubric_id`` (with its id).

    Raises GateError on an unknown rubric id: a judged gate that names a
    rubric the system does not have is a gate failure, not a silent
    default.
    """
    rubric = RUBRICS.get(rubric_id)
    if rubric is None:
        raise GateError(f"unknown rubric: {rubric_id!r}")
    result = dict(rubric)
    result["items"] = list(rubric["items"])
    result["id"] = rubric_id
    return result
