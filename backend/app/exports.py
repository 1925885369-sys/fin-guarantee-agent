from __future__ import annotations

from io import BytesIO

from docx import Document

from .database import json_load, row, rows


def markdown_report(project_id: str) -> str:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    sections = rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,))
    output = [f"# {project['company_name']}融资担保尽调报告", "", "> 本报告由尽调辅助 Agent 生成，须经人工复核，不构成自动审批意见。", ""]
    for section in sections:
        output.extend([f"## {section['title']}", "", section["content"], ""])
        citations = json_load(section["citation_ids"])
        for citation_id in citations:
            citation = row("SELECT * FROM citations WHERE id=?", (citation_id,))
            if citation:
                output.append(f"- [{citation_id}] {citation['label']} · {citation['locator']}: {citation['quote']}")
        output.append("")
    return "\n".join(output)


def docx_report(project_id: str) -> bytes:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    document = Document()
    document.add_heading(f"{project['company_name']}融资担保尽调报告", 0)
    document.add_paragraph("本报告由尽调辅助 Agent 生成，须经人工复核，不构成自动审批意见。")
    for section in rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,)):
        document.add_heading(section["title"], level=1)
        document.add_paragraph(section["content"])
        for citation_id in json_load(section["citation_ids"]):
            citation = row("SELECT * FROM citations WHERE id=?", (citation_id,))
            if citation:
                document.add_paragraph(f"[{citation_id}] {citation['label']} · {citation['locator']}：{citation['quote']}", style="Quote")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()

