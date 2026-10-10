from __future__ import annotations

import argparse
import json
from pathlib import Path


METRICS = (
    "field_extraction_accuracy", "risk_recall", "risk_precision", "risk_f1",
    "candidate_recall", "candidate_precision", "candidate_citation_precision",
    "positive_case_candidate_recall", "negative_case_false_positive_rate",
    "candidate_micro_precision", "candidate_micro_recall",
    "citation_precision", "report_fact_support_rate", "conflict_recall",
    "unsupported_numeric_claims", "invalid_citations", "duplicate_candidate_total",
    "chapter_fallback_rate", "latency_anomaly_count", "latency_ms",
)


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("details"), list):
        raise ValueError(f"{path} 缺少 details")
    return payload


def _mean(payload: dict, key: str):
    value = payload.get("summary", {}).get(key)
    return value if isinstance(value, (int, float)) else None


def _display(value, percent: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%" if percent else f"{value:.4f}"


def _effective_average_cost(payload: dict) -> float | None:
    summary = payload.get("summary", {})
    total = summary.get("total_cost_cny")
    runs = summary.get("runs")
    if not isinstance(total, (int, float)) or not isinstance(runs, (int, float)) or runs <= 0:
        return None
    return float(total) / float(runs)


def _baseline_empty_summary(payload: dict) -> tuple[int, int]:
    """Return completed baseline runs that cannot serve as a fact/rule control."""
    details = payload.get("details") or []
    completed = [item for item in details if item.get("status") == "completed"]
    empty = [
        item for item in completed
        if item.get("empty_baseline_output") is True or item.get("baseline_status") in {"empty", "invalid"}
    ]
    return len(empty), len(completed)


def _percentage_point_delta(current: dict, reference: dict, key: str) -> str:
    current_value, reference_value = _mean(current, key), _mean(reference, key)
    if current_value is None or reference_value is None:
        return "N/A"
    return f"{(current_value - reference_value) * 100:+.1f} 个百分点"


def _ratio(current: float | None, reference: float | None) -> str:
    if current is None or reference is None or reference == 0:
        return "N/A"
    return f"{current / reference:.2f} 倍"


def _version_label(payload: dict, fallback: str) -> str:
    version = str(payload.get("metadata", {}).get("evaluation_version") or "")
    if not version and payload.get("details"):
        version = str(payload["details"][0].get("prompt_version") or "")
    short = version.removeprefix("professional-")
    if short and short != version:
        return f"Baseline {short}" if fallback == "Baseline" else f"Agent {short}"
    return fallback


def comparison(agent_pre: dict, baseline: dict, agent_post: dict | None = None) -> str:
    pre_label = _version_label(agent_pre, "Agent 加固前")
    baseline_label = _version_label(baseline, "Baseline")
    post_label = _version_label(agent_post, "Agent 加固后") if agent_post else ""
    columns = [(pre_label, agent_pre), (baseline_label, baseline)]
    if agent_post:
        columns.append((post_label, agent_post))
    lines = ["# 专业评测对比", "", "所有数据来自虚构案例；N/A 指该模式不适用。", ""]
    empty_baseline, completed_baseline = _baseline_empty_summary(baseline)
    if empty_baseline:
        lines.extend([
            f"> [!WARNING] Baseline 有效性警告：{empty_baseline}/{completed_baseline} 次完成运行返回 0 个事实。",
            "> 该结果仅作为空输出诊断，不作为有效事实抽取或规则能力对照；Agent 相对 Baseline 的提升数值不得用于能力宣称。",
            "",
        ])
    lines.extend([
              "## 总体指标", "", "| 指标 | " + " | ".join(item[0] for item in columns) + " |", "|---|" + "|".join("---:" for _ in columns) + "|"])
    percent_keys = {key for key in METRICS if key not in {
        "unsupported_numeric_claims", "invalid_citations", "duplicate_candidate_total",
        "latency_anomaly_count", "latency_ms",
    }}
    labels = {
        "field_extraction_accuracy": "字段准确率", "risk_recall": "规则风险召回率", "risk_precision": "规则风险精确率",
        "risk_f1": "规则风险 F1", "candidate_recall": "候选风险召回率", "candidate_precision": "候选风险精确率",
        "positive_case_candidate_recall": "正样本候选召回率", "negative_case_false_positive_rate": "负样本误报率",
        "candidate_micro_precision": "候选风险微平均精确率", "candidate_micro_recall": "候选风险微平均召回率",
        "candidate_citation_precision": "候选风险引用精确率", "citation_precision": "来源有效率",
        "report_fact_support_rate": "报告事实支持率", "conflict_recall": "冲突召回率",
        "unsupported_numeric_claims": "无证据数字", "invalid_citations": "无效引用",
        "duplicate_candidate_total": "重复候选项", "chapter_fallback_rate": "章节校验回退率",
        "latency_anomaly_count": "时延异常数",
        "latency_ms": "平均耗时(ms)",
    }
    for key in METRICS:
        lines.append(f"| {labels[key]} | " + " | ".join(_display(_mean(payload, key), key in percent_keys) for _, payload in columns) + " |")
    lines.append("| 有效平均成本(元，含失败重试) | " + " | ".join(
        _display(_effective_average_cost(payload)) for _, payload in columns
    ) + " |")
    lines.append("| 总模型成本(元，含失败重试) | " + " | ".join(
        _display(_mean(payload, "total_cost_cny")) for _, payload in columns
    ) + " |")

    cases = sorted({item["case"] for _, payload in columns for item in payload["details"]})
    lines.extend(["", "## 六案例结果", "", "| 案例 | 模式 | 成功率 | 字段准确率 | 风险召回率 | 候选召回率 | 报告支持率 |", "|---|---|---:|---:|---:|---:|---:|"])
    for case in cases:
        for label, payload in columns:
            items = [item for item in payload["details"] if item["case"] == case]
            successful = [item for item in items if item.get("status") == "completed"]
            def case_mean(key: str):
                values = [float(item[key]) for item in successful if isinstance(item.get(key), (int, float))]
                return sum(values) / len(values) if values else None
            lines.append(f"| {case} | {label} | {_display(len(successful) / max(len(items), 1), True)} | "
                         f"{_display(case_mean('field_extraction_accuracy'), True)} | {_display(case_mean('risk_recall'), True)} | "
                         f"{_display(case_mean('candidate_recall'), True)} | {_display(case_mean('report_fact_support_rate'), True)} |")

    lines.extend(["", "## 稳定性与失败", ""])
    for label, payload in columns:
        failures = [item for item in payload["details"] if item.get("status") != "completed"]
        degraded = sum(int(item.get("degraded_nodes") or 0) for item in payload["details"])
        unstable = []
        for case, values in payload.get("summary", {}).get("per_case_stability", {}).items():
            for metric, stats in values.items():
                if isinstance(stats.get("range"), (int, float)) and stats["range"] > 0.15:
                    unstable.append(f"{case}/{metric}={stats['range'] * 100:.1f}pp")
        lines.append(
            f"- {label}: {len(failures)} 次失败，{degraded} 个降级节点，"
            f"章节回退率 {_display(_mean(payload, 'chapter_fallback_rate'), True)}，"
            f"时延异常 {_display(_mean(payload, 'latency_anomaly_count'))}"
            f"{f'；空 baseline 输出 {empty_baseline}/{completed_baseline} 次' if label == baseline_label and empty_baseline else ''}；"
            f"超过15个百分点波动：{', '.join(unstable) or '无'}。"
        )
        for item in failures:
            lines.append(f"- {label}失败样本：{item['case']} 第{item.get('iteration', '?')}次，{item.get('error_type', 'unknown')}，{item.get('error_message', '')}")

    best = agent_post or agent_pre
    lines.extend(["", "## Agent 相对提升", "",
                  "| 对比 | 字段准确率 | 规则召回率 | 规则精确率 | 规则 F1 | 时延 | 有效平均成本 |",
                  "|---|---:|---:|---:|---:|---:|---:|"])
    best_label = post_label or pre_label
    comparisons = [(f"{best_label} vs {baseline_label}", best, baseline)]
    if agent_post:
        comparisons.append((f"{post_label} vs {pre_label}", agent_post, agent_pre))
    for label, current, reference in comparisons:
        lines.append(
            f"| {label} | {_percentage_point_delta(current, reference, 'field_extraction_accuracy')} | "
            f"{_percentage_point_delta(current, reference, 'risk_recall')} | "
            f"{_percentage_point_delta(current, reference, 'risk_precision')} | "
            f"{_percentage_point_delta(current, reference, 'risk_f1')} | "
            f"{_ratio(_mean(current, 'latency_ms'), _mean(reference, 'latency_ms'))} | "
            f"{_ratio(_effective_average_cost(current), _effective_average_cost(reference))} |"
        )
    lines.extend(["", "## 简历指标摘要", "",
                  f"在六套虚构融资担保案例评测中，字段准确率 {_display(_mean(best, 'field_extraction_accuracy'), True)}，"
                  f"规则风险召回率 {_display(_mean(best, 'risk_recall'), True)}，报告事实支持率 {_display(_mean(best, 'report_fact_support_rate'), True)}，"
                  f"平均单案例成本 {_display(_effective_average_cost(best))} 元。", ""])
    return "\n".join(lines)


def failure_appendix(payloads: list[tuple[str, dict]]) -> str:
    lines = ["# 评测失败样本", "", "按标准错误类型汇总，便于阶段二针对性加固。", ""]
    found = False
    for label, payload in payloads:
        for item in payload["details"]:
            quality_issues = []
            if int(item.get("candidate_false_positive_count") or 0):
                quality_issues.append(f"候选风险误报 {item['candidate_false_positive_count']} 项")
            if int(item.get("candidate_false_negative_count") or 0):
                quality_issues.append(f"候选风险漏报 {item['candidate_false_negative_count']} 项")
            if int(item.get("duplicate_candidate_count") or 0):
                quality_issues.append(f"重复候选 {item['duplicate_candidate_count']} 项")
            if float(item.get("chapter_fallback_rate") or 0) > 0.2:
                quality_issues.append(f"章节回退率 {float(item['chapter_fallback_rate']) * 100:.1f}%")
            fallback_sections = item.get("chapter_fallback_sections") or []
            if fallback_sections:
                descriptions = []
                for section in fallback_sections[:8]:
                    key = section.get("section_key") or "unknown"
                    reason = section.get("reason") or "未记录原因"
                    descriptions.append(f"{key}: {reason}")
                quality_issues.append("章节回退明细：" + "；".join(descriptions))
            if int(item.get("latency_anomaly_count") or 0):
                quality_issues.append(f"时延异常 {item['latency_anomaly_count']} 次")
            if item.get("empty_baseline_output") is True:
                quality_issues.append("baseline 返回空事实，不能作为有效对照")
            if item.get("status") == "completed" and not quality_issues:
                continue
            found = True
            lines.extend([f"## {label} / {item['case']} / 第{item.get('iteration', '?')}次", "",
                          f"- 运行状态：`{item.get('status', 'unknown')}`",
                          f"- 错误类型：`{item.get('error_type') or 'none'}`",
                          f"- 错误：{item.get('error_message', '') or '无程序异常'}",
                          f"- 质量问题：{'；'.join(quality_issues) or '运行失败'}", ""])
    if not found:
        lines.append("未记录失败样本。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-pre", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--agent-post", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/PROFESSIONAL_EVALUATION.md"))
    parser.add_argument("--failures-output", type=Path, default=Path("docs/EVALUATION_FAILURES.md"))
    args = parser.parse_args()
    agent_pre, baseline = _load(args.agent_pre), _load(args.baseline)
    agent_post = _load(args.agent_post) if args.agent_post else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(comparison(agent_pre, baseline, agent_post), encoding="utf-8")
    payloads = [(_version_label(agent_pre, "Agent 加固前"), agent_pre), (_version_label(baseline, "Baseline"), baseline)]
    if agent_post:
        payloads.append((_version_label(agent_post, "Agent 加固后"), agent_post))
    args.failures_output.parent.mkdir(parents=True, exist_ok=True)
    args.failures_output.write_text(failure_appendix(payloads), encoding="utf-8")
    print(f"已生成 {args.output} 和 {args.failures_output}")


if __name__ == "__main__":
    main()
