"""Audit fact conflicts: measure how many are real vs extraction noise.

Root cause of "waiting-for-human-review" floods is that deterministic text/numeric
extraction records garbage sentence fragments / percentages / years as assertions,
which the old conflict logic (raw identity on text) then reports as conflicts.

This script replays both the *old* and the *new* conflict decision over the
assertions already stored in a project and buckets the difference, so the gain
can be measured before/after a code change.  It never writes to the database.

Usage:
    python scripts/conflict_audit.py <project_id>
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

from backend.app.database import rows
from backend.app.facts import text_value_token
from backend.app.professional import _assertion_identity, _semantic_conflict_groups

DEFAULT_PROJECT = "14ea805a-5be3-4d51-aaa9-7584d77bef5c"  # real 京投 case "aa"


def _active(assertions: list[dict]) -> list[dict]:
    return [
        item for item in assertions
        if item.get("assertion_status", "unreviewed") not in {"stale", "conflicted", "insufficient"}
    ]


def audit(project_id: str) -> None:
    assertions = rows(
        "SELECT * FROM fact_assertions WHERE project_id=? ORDER BY fact_key,subject_id,scope,period",
        (project_id,),
    )
    grouped: dict[tuple, list[dict]] = {}
    for item in assertions:
        key = (item["fact_key"], item["subject_id"], item["scope"], item["period"])
        grouped.setdefault(key, []).append(item)

    total_groups = len(grouped)
    old_conflicted: list[tuple[tuple, list[dict]]] = []
    for key, members in grouped.items():
        active = _active(members)
        if not active:
            continue
        has_declared = any(item.get("assertion_status") == "conflicted" for item in members)
        identities = {_assertion_identity(item) for item in active}
        if has_declared or len(identities) > 1:
            old_conflicted.append((key, active))

    resolved_buckets: Counter[str] = Counter()
    kept = 0
    kept_examples: list[str] = []
    garbage_assertions = 0
    for key, active in old_conflicted:
        fact_key = key[0]
        groups = _semantic_conflict_groups(fact_key, active)
        # how many active assertions are garbage text (signature None)?
        garbage = sum(
            1 for item in active
            if item.get("value") is None and text_value_token(fact_key, item.get("text_value")) is None
        )
        garbage_assertions += garbage
        if len(groups) >= 2:
            kept += 1
            if len(kept_examples) < 14:
                kept_examples.append(f"{fact_key}|{key[2]}|{key[3]}")
            continue
        if not groups:
            resolved_buckets["all values are garbage text (no real value)"] += 1
        elif garbage:
            resolved_buckets["garbage text alongside one clean value"] += 1
        else:
            # single clean group after semantic collapse
            sig = groups[0][0]
            if sig[0] == "txt":
                resolved_buckets["text same after semantic token"] += 1
            else:
                resolved_buckets["numeric equal within tolerance"] += 1

    total_conflict_task_noise = sum(resolved_buckets.values()) + kept
    print(f"project           : {project_id}")
    print(f"canonical groups  : {total_groups}")
    print(f"stored conflicted : {len(old_conflicted)}  (garbage text assertions inside: {garbage_assertions})")
    print()
    print("NEW semantic conflict decision replay over the SAME stored assertions")
    print(f"  would STILL be conflicted : {kept}")
    print(f"  would be RESOLVED         : {sum(resolved_buckets.values())}")
    print("  resolved by:")
    for bucket, count in resolved_buckets.most_common():
        print(f"      {count:4}  {bucket}")
    print()
    print("remaining conflicted examples (fact_key|scope|period) — inspect manually:")
    for example in kept_examples:
        print("   -", example)
    print()
    false_share = sum(resolved_buckets.values()) / max(1, total_conflict_task_noise)
    print(f"false-conflict share on this project: {false_share * 100:.0f}%")
    print("(conflict resolution is the A/B backstop; the extraction fix in facts.py "
          "prevents the garbage assertions at the source for future runs.)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", nargs="?", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    audit(args.project_id)


if __name__ == "__main__":
    main()
