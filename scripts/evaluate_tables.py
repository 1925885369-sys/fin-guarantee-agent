"""表格解析评测：虚构案例对金标计分，参考材料只做兼容性定位。

用法::

    PYTHONPATH=. python scripts/evaluate_tables.py [--json out.json]

分层规则（见 ``PLAN_material.md`` Phase 3.3）：

- 华辰及其他虚构案例：计入表格召回率与单元格准确率；
- 中邮、北投：只验证「表格能否被定位」，未逐格人工标注前不计入准确率。

本脚本只读材料，不写数据库。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.bond_documents import CASE_SPECS, case_table_gold, generate_case_materials  # noqa: E402
from backend.app.config import settings  # noqa: E402
from backend.app.parsers import parse_document  # noqa: E402
from backend.app.table_extraction import TABLE_PARSER_VERSION, compare_with_gold, extract_tables  # noqa: E402


REFERENCE_MATERIALS = (
    "demo-北投/募集说明书.pdf",
    "demo-北投/信用评级报告.pdf",
    "real-bond-zhongyou-2026-02/中邮03募集说明书.pdf",
    "real-bond-zhongyou-2026-02/中邮03信用评级报告.pdf",
)


def score_fictional_cases() -> list[dict]:
    output = []
    with tempfile.TemporaryDirectory(prefix="table-eval-") as workspace:
        for case in CASE_SPECS:
            root = Path(workspace) / case.short
            materials = generate_case_materials(root, case)
            extracted = []
            for material in materials:
                extracted.extend(parse_document(material["path"]).tables)
            result = compare_with_gold(extracted, case_table_gold(case, materials)).summary()
            output.append({"case": case.id, **result})
    return output


def locate_reference_materials() -> list[dict]:
    output = []
    for name in REFERENCE_MATERIALS:
        path = settings.resolved_data_dir / "demo_materials" / name
        if not path.exists():
            output.append({"material": name, "status": "missing"})
            continue
        tables = extract_tables(path)
        output.append({
            "material": name, "status": "located",
            "tables": len(tables),
            "with_caption": sum(1 for table in tables if table.caption),
            "with_unit": sum(1 for table in tables if table.unit),
            "multi_page": sum(1 for table in tables if len(table.pages) > 1),
            "needs_review": sum(1 for table in tables if table.status == "needs_review"),
        })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="表格解析评测")
    parser.add_argument("--json", type=Path, default=None, help="把结果写入 JSON 文件")
    arguments = parser.parse_args()

    scored = score_fictional_cases()
    print(f"表格解析器版本：{TABLE_PARSER_VERSION}")
    print("\n== 虚构案例（计入准确率）==")
    for item in scored:
        print(
            f"  {item['case']:24s} 表 {item['matched_tables']}/{item['expected_tables']} "
            f"召回={item['table_recall']:.4f} 单元格 {item['cell_exact']}/{item['cell_total']} "
            f"准确率={item['cell_accuracy']:.4f} 表名={item['caption_matches']} "
            f"单位={item['unit_matches']} 续表={item['continuation_found']}/{item['continuation_expected']} "
            f"待确认={item['needs_review']}"
        )

    located = locate_reference_materials()
    print("\n== 参考材料（只验证定位，不计入准确率）==")
    for item in located:
        if item["status"] == "missing":
            print(f"  {item['material']:48s} 缺失")
            continue
        print(
            f"  {item['material']:48s} 表={item['tables']} 含表名={item['with_caption']} "
            f"含单位={item['with_unit']} 跨页={item['multi_page']} 待确认={item['needs_review']}"
        )

    if arguments.json:
        arguments.json.write_text(
            json.dumps(
                {"parser_version": TABLE_PARSER_VERSION, "fictional_cases": scored, "reference_materials": located},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {arguments.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
