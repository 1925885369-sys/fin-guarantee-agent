from __future__ import annotations

import uuid

from .database import connect, json_dump, json_load, now_iso, row, rows
from .professional import audit_event, risk_severity


def replace_candidates(project_id: str, findings: list[dict]) -> list[dict]:
    previous = {
        (item["taxonomy"], item["title"]): item
        for item in rows("SELECT * FROM candidate_findings WHERE project_id=?", (project_id,))
    }
    with connect() as conn:
        conn.execute("DELETE FROM candidate_findings WHERE project_id=?", (project_id,))
        for finding in findings:
            old = previous.get((finding["taxonomy"], finding["title"]))
            status = old["status"] if old and old["status"] in {"confirmed", "rejected"} else "proposed"
            decision_note = old["decision_note"] if old else ""
            finding_id = old["id"] if old else str(uuid.uuid4())
            severity = risk_severity(finding["likelihood"], finding["impact"])
            conn.execute(
                "INSERT INTO candidate_findings (id,project_id,taxonomy,title,rationale,potential_impact,likelihood,impact,severity,support_source_ids,counter_source_ids,questions,mitigation,status,confidence,decision_note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (finding_id, project_id, finding["taxonomy"], finding["title"], finding["rationale"], finding["potential_impact"],
                 finding["likelihood"], finding["impact"], severity, json_dump(finding["support_source_ids"]),
                 json_dump(finding.get("counter_source_ids", [])), json_dump(finding["questions"]), finding["mitigation"],
                 status, finding.get("confidence", 0.6), decision_note, old["created_at"] if old else now_iso(), now_iso()),
            )
    sync_confirmed_candidate_risks(project_id)
    return decode_candidates(rows("SELECT * FROM candidate_findings WHERE project_id=? ORDER BY severity DESC,created_at", (project_id,)))


def decode_candidates(items: list[dict]) -> list[dict]:
    for item in items:
        for key in ("support_source_ids", "counter_source_ids", "questions"):
            item[key] = json_load(item[key])
    return items


def decide_candidate(project_id: str, finding_id: str, status: str, note: str) -> dict:
    before = row("SELECT * FROM candidate_findings WHERE id=? AND project_id=?", (finding_id, project_id))
    if not before:
        raise ValueError("候选风险不存在")
    with connect() as conn:
        conn.execute("UPDATE candidate_findings SET status=?,decision_note=?,updated_at=? WHERE id=?", (status, note, now_iso(), finding_id))
    after = row("SELECT * FROM candidate_findings WHERE id=?", (finding_id,)) or {}
    audit_event(project_id, "candidate_decision", "candidate_finding", finding_id, before, after, "human")
    # The formal risk matrix reflects the decision immediately. The report is
    # still marked stale, so its prose cannot silently remain outdated.
    sync_confirmed_candidate_risks(project_id)
    from .review_lifecycle import mark_review_changed
    mark_review_changed(project_id, "candidate")
    return decode_candidates([after])[0]


def sync_confirmed_candidate_risks(project_id: str) -> None:
    confirmed = decode_candidates(rows("SELECT * FROM candidate_findings WHERE project_id=? AND status='confirmed'", (project_id,)))
    with connect() as conn:
        conn.execute("DELETE FROM risks WHERE project_id=? AND source_type='candidate'", (project_id,))
        for item in confirmed:
            conn.execute(
                "INSERT INTO risks (id,project_id,severity,category,title,description,impact,action,source_ids,likelihood,impact_score,source_type,candidate_id,condition_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"risk-{item['id']}", project_id, item["severity"], "定性候选", item["title"], item["rationale"],
                 item["potential_impact"], item["mitigation"], json_dump(item["support_source_ids"]), item["likelihood"],
                 item["impact"], "candidate", item["id"], item["mitigation"]),
            )


def _task_key(item: dict) -> tuple[str, str]:
    return item.get("issue_key") or f"{item['task_type']}:{item['title']}", ""


def generate_review_tasks(
    project_id: str, gaps: list[dict], conflicts: list[dict], checks: list[dict], candidates: list[dict],
    tie_outs: list[dict] | None = None,
) -> list[dict]:
    generated: list[dict] = []
    # A missing mandatory chapter is one document-level blocker.  Do not turn
    # every field that could have appeared in that chapter into a separate task.
    from .bond_sections import section_location_tasks

    generated.extend(section_location_tasks(project_id))
    for gap in gaps:
        generated.append({"task_type": "material_gap", "priority": "medium", "title": f"补充{gap['group']}",
                          "description": f"当前缺少{gap['group']}类材料。", "related_ids": [], "required_evidence": "、".join(gap.get("expectedKinds", [])),
                          "issue_key": f"material:{gap['group']}", "issue_category": "material_package",
                          "affected_fact_ids": [], "affected_rule_ids": [], "blocking_level": "non_blocking",
                          "source_requirement": "、".join(gap.get("expectedKinds", []))})
    insufficient = [check for check in checks if check["status"] == "insufficient"]
    if insufficient:
        generated.append({
            "task_type": "insufficient_rule", "priority": "medium", "title": "补充规则核验依据",
            "description": f"{len(insufficient)}项核验规则因证据不足尚无法判断。",
            "related_ids": [check["id"] for check in insufficient],
            "required_evidence": "；".join(dict.fromkeys(check.get("formula", "") for check in insufficient if check.get("formula"))),
            "issue_key": "evidence:rule-inputs", "issue_category": "evidence_package",
            "affected_fact_ids": [], "affected_rule_ids": [check.get("rule_key", check["id"]) for check in insufficient],
            "blocking_level": "non_blocking", "source_requirement": "补充受影响规则所需的原始资料",
        })
    for tie_out in tie_outs or []:
        if tie_out["status"] == "warning":
            generated.append({"task_type": "financial_tie_out", "priority": "high", "title": f"核实财务勾稽异常：{tie_out['title']}",
                              "description": tie_out["rationale"], "related_ids": [tie_out["id"]],
                              "required_evidence": "审计报告附注、科目明细及差异调节表",
                              "issue_key": f"tie_out:{tie_out['id']}", "issue_category": "financial_tie_out",
                              "affected_fact_ids": [], "affected_rule_ids": [], "blocking_level": "blocking",
                              "source_requirement": "审计报告附注、科目明细及差异调节表"})
    generated = list({_task_key(task)[0]: task for task in generated}.values())
    previous = {_task_key(item): item for item in rows("SELECT * FROM review_tasks WHERE project_id=?", (project_id,))}
    with connect() as conn:
        conn.execute("DELETE FROM review_tasks WHERE project_id=?", (project_id,))
        for task in generated:
            old = previous.get(_task_key(task))
            conn.execute(
                "INSERT INTO review_tasks (id,project_id,task_type,priority,title,description,status,related_ids,required_evidence,resolution_note,assignee,due_date,created_at,updated_at,issue_key,issue_category,affected_fact_ids,affected_rule_ids,blocking_level,source_requirement) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (old["id"] if old else str(uuid.uuid4()), project_id, task["task_type"], task["priority"], task["title"], task["description"],
                 old["status"] if old else "open", json_dump(task["related_ids"]), task["required_evidence"], old["resolution_note"] if old else "",
                 old["assignee"] if old else "", old["due_date"] if old else "", old["created_at"] if old else now_iso(), now_iso(),
                 task["issue_key"], task["issue_category"], json_dump(task["affected_fact_ids"]), json_dump(task["affected_rule_ids"]),
                 task["blocking_level"], task["source_requirement"]),
            )
    return decode_tasks(rows("SELECT * FROM review_tasks WHERE project_id=? ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,created_at", (project_id,)))


def decode_tasks(items: list[dict]) -> list[dict]:
    for item in items:
        for field in ("related_ids", "affected_fact_ids", "affected_rule_ids"):
            if field in item:
                item[field] = json_load(item[field])
    return items


def review_summary(project_id: str) -> dict:
    conflict_count = row(
        "SELECT COUNT(*) count FROM canonical_facts WHERE project_id=? AND status='conflicted'",
        (project_id,),
    )["count"]
    candidate_count = row(
        "SELECT COUNT(*) count FROM candidate_findings WHERE project_id=? AND status='proposed'",
        (project_id,),
    )["count"]
    action_package_count = row(
        "SELECT COUNT(DISTINCT CASE WHEN issue_key='' THEN task_type || ':' || title ELSE issue_key END) count "
        "FROM review_tasks WHERE project_id=? AND status='open' AND task_type NOT IN ('fact_conflict','candidate_finding')",
        (project_id,),
    )["count"]
    information_notice_count = row(
        "SELECT COUNT(*) count FROM canonical_facts WHERE project_id=? AND status='unreviewed'",
        (project_id,),
    )["count"]
    return {
        "total_unique_issues": conflict_count + candidate_count + action_package_count,
        "conflict_count": conflict_count,
        "candidate_count": candidate_count,
        "action_package_count": action_package_count,
        "information_notice_count": information_notice_count,
    }


def update_task(project_id: str, task_id: str, payload: dict) -> dict:
    before = row("SELECT * FROM review_tasks WHERE id=? AND project_id=?", (task_id, project_id))
    if not before:
        raise ValueError("尽调任务不存在")
    with connect() as conn:
        conn.execute(
            "UPDATE review_tasks SET status=?,resolution_note=?,assignee=?,due_date=?,updated_at=? WHERE id=?",
            (payload["status"], payload.get("resolution_note", ""), payload.get("assignee", ""), payload.get("due_date", ""), now_iso(), task_id),
        )
    after = row("SELECT * FROM review_tasks WHERE id=?", (task_id,)) or {}
    audit_event(project_id, "task_update", "review_task", task_id, before, after, "human")
    if any(before.get(key) != after.get(key) for key in ("status", "resolution_note", "assignee", "due_date")):
        from .review_lifecycle import mark_review_changed
        mark_review_changed(project_id, "task")
    return decode_tasks([after])[0]


def unresolved_items(project_id: str) -> dict:
    conflicts = rows("SELECT id,name,period FROM canonical_facts WHERE project_id=? AND status='conflicted'", (project_id,))
    candidates = rows("SELECT id,title,severity FROM candidate_findings WHERE project_id=? AND status='proposed'", (project_id,))
    tasks = rows("SELECT id,title,priority FROM review_tasks WHERE project_id=? AND status='open'", (project_id,))
    major = conflicts + [item for item in candidates if item["severity"] == "high"] + [item for item in tasks if item["priority"] == "high"]
    return {"has_open_major_items": bool(major), "conflicts": conflicts, "candidates": candidates, "tasks": tasks}
