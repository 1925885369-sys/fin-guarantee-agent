from __future__ import annotations

from dataclasses import dataclass, field

from .database import json_load
from .facts import canonical_key
from .metrics import calculate_metrics
from .policy import PolicyPack, load_policy


@dataclass
class RuleResult:
    key: str
    title: str
    severity: str
    status: str
    summary: str
    values: list[dict]
    source_ids: list[str]
    formula: str
    fact_ids: list[str]
    thresholds: dict[str, float] = field(default_factory=dict)
    result_value: float | None = None
    rationale: str = ""
    policy_version: str = ""


RISK_GUIDANCE = {
    "material_completeness": ("材料完整性", "关键材料缺失可能导致风险识别不充分", "补齐缺失材料并完成真实性核验后再形成正式审查意见"),
    "fact_conflict": ("数据可靠性", "相同口径数据冲突会影响财务判断和担保方案", "逐项核对原件、编制差异调节表并由项目经理确认采用值"),
    "revenue_tax": ("收入真实性", "报表收入与税务口径差异可能反映确认口径或真实性问题", "取得纳税申报表、开票清单并编制收入差异调节表"),
    "revenue_bank": ("回款质量", "销售收入与经营流入不匹配可能影响收入真实性和现金回收", "核查主要账户流水、非经营性往来及回款对应合同"),
    "revenue_trend": ("经营趋势", "收入明显下滑可能削弱第一还款来源", "核查在手订单、期后销售和收入下降原因"),
    "margin_volatility": ("盈利质量", "毛利率显著波动可能反映成本转嫁或收入成本错配", "核查主要产品价格、采购成本和收入成本结转"),
    "ar_growth": ("营运资金", "应收增长快于收入会占用现金并增加坏账风险", "核查账龄、期后回款、客户信用和坏账准备"),
    "overdue_receivables": ("应收质量", "逾期应收占比较高可能造成实际资产价值折损", "取得函证和期后回款，审慎调整可回收价值"),
    "inventory_growth": ("存货质量", "存货增长过快可能形成积压或跌价", "实施盘点并核查库龄、订单覆盖和跌价准备"),
    "cash_profit": ("现金流", "利润未转化为经营现金流会削弱偿债来源", "核查应收、存货、关联往来及现金流持续性"),
    "leverage": ("偿债能力", "高杠杆或流动性不足会压缩债务承受空间", "控制新增负债并设置财务指标持续约束"),
    "dscr": ("偿债覆盖", "经营现金流对短期债务覆盖不足", "落实还款来源、到期安排和必要的偿债资金监管"),
    "customer_concentration": ("客户集中", "客户依赖会放大单一客户流失和账期变化影响", "核查合作稳定性并设置回款归集或客户分散要求"),
    "supplier_concentration": ("供应链", "供应商集中可能影响采购稳定性和议价能力", "核查替代供应渠道、采购协议和结算条件"),
    "related_occupancy": ("关联交易", "关联资金占用可能导致资金挪用和资产虚增", "穿透核查商业背景并要求归还或设置资金使用监管"),
    "purpose_coverage": ("融资用途", "用途依据不足可能导致贷款挪用", "补齐合同并采用受托支付和逐笔用途核验"),
    "debt_maturity": ("债务期限", "短期债务集中到期可能形成续贷依赖", "取得授信续作安排并设置还款资金计划"),
    "external_guarantee": ("或有负债", "对外担保和或有事项可能形成代偿压力", "取得被担保方资料并设置新增对外担保限制"),
    "collateral": ("反担保", "抵押覆盖、估值或权利问题会削弱代偿后追偿保障", "更新评估、核验权属顺位并补充反担保措施"),
    "bond_term_consistency": ("发行条款", "核心发行条款冲突会影响担保责任和偿债计划判断", "逐项核对募集说明书、评级报告和正式发行文件并形成差异表"),
    "rating_consistency": ("评级增信", "评级差异或增信逻辑不清可能误判债项信用提升来源", "取得评级模型说明并核对保证责任对债项评级的实际支持"),
    "proceeds_coverage": ("募集资金", "用途闭环不足可能导致资金挪用或专户监管失效", "补齐用途清单并执行专户、受托支付和逐笔凭证核验"),
    "financial_tie_out": ("财务口径", "评级与审计财务口径不一致会削弱偿债分析可靠性", "取得评级调整明细并编制与审计报表的勾稽表"),
    "cashflow_debt_service": ("偿债来源", "经营现金流对债务偿付覆盖不足会提高代偿概率", "落实偿债资金归集、备用流动性和分期资金准备计划"),
    "bullet_maturity": ("本金集中到期", "到期一次性还本形成大额集中偿付压力", "设置本金偿债准备金计划并按期核验资金归集进度"),
    "leverage_liquidity": ("杠杆与流动性", "高杠杆或流动性不足会削弱债券本息偿付能力", "约束新增有息负债并持续监测流动性指标"),
    "interest_coverage": ("利息偿付", "现金流对利息覆盖不足可能形成持续偿付压力", "落实利息偿付专户和现金流改善计划"),
    "revenue_cash_quality": ("现金回收", "收入未充分转化为经营现金流会削弱第一偿债来源", "核验客户回款、应收账款和银行流水对应关系"),
    "receivable_quality": ("应收质量", "逾期应收占比较高可能造成资产价值折损和回款延期", "取得函证、期后回款并审慎调整可回收金额"),
    "related_party": ("关联交易", "关联资金占用可能导致募集资金挪用和现金流外流", "穿透核验商业背景并落实归还、审批和资金监管"),
    "guarantee_scope": ("保证责任", "保证范围或责任上限不足会留下未覆盖债券风险敞口", "核对签署版保证合同、责任上限、保证期间和除外责任"),
    "counter_guarantee": ("反担保", "反担保覆盖或权利状态不足会削弱代偿后追偿能力", "更新估值、核验在先权利并完成抵质押登记"),
    "investor_protection": ("投资者保护", "保护和违约救济条款不足会延迟风险处置", "完善资信维持、信息披露、交叉违约和持有人会议安排"),
    "legal_issuance": ("发行合规", "法律意见或发行授权缺失会影响交易文件有效性", "补齐法律意见、董事会及股东会授权并核对签署权限"),
}


def risks_from_checks(checks: list[RuleResult]) -> list[dict]:
    output = []
    for check in checks:
        if check.status != "warning":
            continue
        category, impact, action = RISK_GUIDANCE[check.key]
        likelihood = 3 if check.severity == "high" else 2
        impact_score = 3 if check.severity == "high" else 2
        output.append({
            "severity": check.severity, "category": category, "title": check.title,
            "description": check.summary, "impact": impact, "action": action,
            "condition_text": action, "source_ids": check.source_ids,
            "likelihood": likelihood, "impact_score": impact_score, "source_type": "rule", "candidate_id": "",
        })
    return output
