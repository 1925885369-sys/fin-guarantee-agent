"""B1 契约测试：`calculations` / `recommendations` 能穿过解析链，且策略进输入哈希。

守三件事（对应《风控约束分层》方案 §三.1 / §二 line 16）：
1. 字段**不被静默丢弃**——`LlmSection` 不声明就会在 `llm.py` 的 `model_dump()` 里消失；
2. `input_record_ids` **不能**被当成 citation 别名（取名 `source_ids` 会让整稿 `invalid_source`）；
3. 结构化数组的输出契约**只在模式启用时**出现（关时零成本），业务规则层不泄露文件 UUID。
"""
from __future__ import annotations

from backend.app import llm
from backend.app.chapter_prompts import (
    CALCULATION_CONTRACT_HINT, RECOMMENDATION_CONTRACT_HINT, business_rules_sentence,
    compile_structured_prompt, load_chapter_config, structured_array_contract,
)
from backend.app.schemas import LlmSection


def _config(**overrides) -> dict:
    """以**出厂默认**章节为基线（`governance` 仍是 system_only/off）。

    2026-10-17 起 `financial`/`cashflow`/`debt`/`rating` 出厂就是 calculation_mode=model、
    `guarantee`/`counter_guarantee` 是 recommendation_mode=structured——拿它们当"默认值"
    基线，这些用例会随出厂配置一起变，测不出"关时零成本"。
    """
    config = load_chapter_config("governance")
    config.update(overrides)
    return config


# ------------------------------------------------------------------ 解析链 --

def test_section_keeps_structured_arrays():
    parsed = LlmSection.model_validate({
        "content": "正文",
        "citation_ids": ["E01"],
        "calculations": [{"calculation_id": "c1", "formula": "a/b", "input_record_ids": ["fact:x"], "value": 0.4}],
        "recommendations": [{"recommendation_type": "Accept", "content": "建议承保"}],
        "totally_unknown": 1,
    }).model_dump()
    assert len(parsed["calculations"]) == 1
    assert len(parsed["recommendations"]) == 1
    assert "totally_unknown" not in parsed            # 未知键忽略，不报错
    # 数值型 value 统一转字符串（保留模型给的精度）
    assert parsed["calculations"][0]["value"] == "0.4"


def test_input_record_ids_are_not_treated_as_citations():
    """取名 source_ids 会触发 `_unknown_source_ids` → 整稿 invalid_source。"""
    body = {"calculations": [{"calculation_id": "c1", "input_record_ids": ["fact:abc"]}]}
    assert llm._unknown_source_ids(body, {"E01"}) == set()


def test_normalize_fills_ids_dedupes_and_caps():
    raw = LlmSection.model_validate({
        "content": "x",
        "calculations": [{"formula": "1+1", "value": "2"},
                         {"calculation_id": "c1", "formula": "1+2"},
                         {"calculation_id": "c2", "formula": "1+3"}],
        "recommendations": [{"content": "A"}, {"recommendation_id": "r1", "content": "B"}],
    })
    normalized = llm._normalize_structured_arrays(raw)
    assert [item.calculation_id for item in normalized.calculations] == ["c1", "c2"]
    assert [item.recommendation_id for item in normalized.recommendations] == ["r1"]
    assert normalized.recommendations[0].recommendation_type == normalized.recommendations[0].recommendation_type.lower()

    overflow = LlmSection.model_validate({
        "content": "x",
        "calculations": [{"calculation_id": f"c{i}", "formula": "1+1"}
                         for i in range(llm.MAX_CALCULATIONS + 5)],
    })
    assert len(llm._normalize_structured_arrays(overflow).calculations) == llm.MAX_CALCULATIONS


def test_normalize_does_not_touch_content():
    """规整只动数组形状，不得改写正文（业务判定留给 B2 的复算）。"""
    raw = LlmSection.model_validate({
        "content": "毛利率为40%。", "citation_ids": ["E01"],
        "calculations": [{"calculation_id": "", "formula": "1+1", "value": "2"}],
    })
    assert llm._normalize_structured_arrays(raw).content == "毛利率为40%。"


# ------------------------------------------------------------ 输出契约按模式 --

def test_structured_array_contract_is_off_by_default():
    assert structured_array_contract(_config()) == ""


def test_structured_array_contract_follows_mode():
    calc_only = structured_array_contract(_config(calculation_mode="model"))
    assert CALCULATION_CONTRACT_HINT in calc_only
    assert RECOMMENDATION_CONTRACT_HINT not in calc_only

    both = structured_array_contract(_config(calculation_mode="model", recommendation_mode="structured"))
    assert CALCULATION_CONTRACT_HINT in both and RECOMMENDATION_CONTRACT_HINT in both

    rec_only = structured_array_contract(_config(recommendation_mode="qualitative"))
    assert RECOMMENDATION_CONTRACT_HINT in rec_only
    assert CALCULATION_CONTRACT_HINT not in rec_only


def test_compiled_prompt_and_runtime_agree_on_contract():
    off = compile_structured_prompt("financial", _config())
    assert "calculation_id" not in off

    on = compile_structured_prompt("financial", _config(calculation_mode="model"))
    assert "calculation_id" in on

    # 运行时契约挂在 system prompt 上（`chapter_prompt()` 可能返回库里存量编译文本，
    # 所以不能只依赖编译器）
    system = llm._report_request(
        "财务分析", "正文。", [], None, "financial",
        {"project_id": "p", "prompt_config": _config(calculation_mode="model")},
    )[0]
    assert "calculation_id" in system


# -------------------------------------------------------------- 业务规则层 --

def test_business_rules_layer_renders_scope_without_uuids():
    config = _config(
        material_scope={"source_kinds": ["募集说明书"], "material_roles": [], "document_ids": ["doc-a", "doc-b"]},
        analysis_period={"periods": ["2023", "2024"], "mode": "configured"},
    )
    sentence = business_rules_sentence(config)
    assert "材料类型限定为募集说明书" in sentence
    assert "只使用指定的 2 份材料文件" in sentence
    assert "分析期间限定为2023、2024" in sentence
    assert "doc-a" not in sentence                    # 不泄露内部文件 ID

    compiled = compile_structured_prompt("financial", config)
    assert "业务生成规则" in compiled


def test_business_rules_layer_absent_when_unconfigured():
    assert business_rules_sentence(_config()) == ""
    assert "业务生成规则" not in compile_structured_prompt("financial", _config())


def test_periods_only_apply_when_mode_is_configured():
    sentence = business_rules_sentence(_config(analysis_period={"periods": ["2024"], "mode": "materials"}))
    assert sentence == ""


def test_calculation_contract_states_consequence_and_forbids_ids_in_body():
    """契约必须写清三件事（2026-10-17 真实试跑：模型把差值直接写正文、且把 record_id 写进正文）。

    只写"可以自行计算"不够——模型会认为"已有指标做减法"不需要声明。
    """
    assert "未声明的计算值会被判为" in CALCULATION_CONTRACT_HINT
    assert "整章退回" in CALCULATION_CONTRACT_HINT
    assert "不要**出现 fact:/metric:/table:/calc:" in CALCULATION_CONTRACT_HINT
    assert "v1、v2" in CALCULATION_CONTRACT_HINT
    assert "方向要与正文表述一致" in CALCULATION_CONTRACT_HINT


# --------------------------------------------- 输出形状（2026-10-18 真实试跑）--


def test_json_shape_carries_arrays_into_the_literal_example():
    """开新模式时，数组必须写进**字面示例**，不能只在句尾"追加一段说明"。

    实测：只追加说明时，模型照抄 `{content, citation_ids}` 就收尾，calculations 连续 3 次为 0。
    """
    from backend.app.chapter_prompts import structured_json_shape

    off = structured_json_shape(_config())
    assert "calculations" not in off and "recommendations" not in off

    calc = structured_json_shape(_config(calculation_mode="model"))
    assert '"content"' in calc and '"calculations"' in calc
    assert "禁止省略" in calc

    both = structured_json_shape(_config(calculation_mode="model", recommendation_mode="structured"))
    assert '"calculations"' in both and '"recommendations"' in both


def test_subsection_contract_keeps_extra_keys():
    """小节模式会整句替换输出形状——替换时必须带上 calculations，否则模型无处安放。"""
    from backend.app.llm import apply_subsection_contract
    from backend.app.chapter_prompts import extra_shape_keys, structured_json_shape

    config = _config(calculation_mode="model")
    shape = structured_json_shape(config)
    prompt = apply_subsection_contract(
        "系统提示。" + shape, [{"subsection_id": "s1", "title": "概览"}],
        replace_contract=shape, extra_keys=extra_shape_keys(config),
    )
    assert "subsection_id" in prompt and '"calculations"' in prompt
    assert prompt.count("输出严格 JSON") == 1, prompt


def test_subsection_contract_replaces_required_sections_and_paragraph_order():
    """小节清单接管内容区块与行文顺序：system prompt 里不得再重复这两句。"""
    from backend.app.llm import apply_subsection_contract

    base = (
        "你是财务分析员。必须保留以下内容区块：财务概览、审查边界。"
        "行文顺序：财务概览→审查边界。行文语气：审慎。"
    )
    prompt = apply_subsection_contract(base, [{"subsection_id": "s1", "title": "财务概览"}])
    assert "必须保留以下内容区块" not in prompt
    assert "行文顺序" not in prompt
    assert "行文语气" in prompt, "只去重结构与顺序，语气等写作要求必须保留"
    assert "s1" in prompt and "财务概览" in prompt


def test_subsection_contract_survives_global_directive(monkeypatch):
    """全局指令在末尾，小节契约不能被它截掉。

    2026-10-27 实测：`chapter_prompt()` 返回的文本已带「【全局强制指令】」，而
    `_report_request()` 在其后追加小节输出契约，末尾 `apply_global_directive()` 从哨兵
    截断重注入 —— 小节契约整段消失，模型收到的提示词里连 `subsections` 字样都没有，
    响应自然不带小节，被判"响应缺少小节内容"整章回退（连续两次单章重生失败的真因）。
    """
    from backend.app.chapter_prompts import GLOBAL_DIRECTIVE_MARK
    from backend.app.llm import _SUBSECTION_SECTION_MARK

    directive = f"{GLOBAL_DIRECTIVE_MARK}以下为最高优先级要求；与前述任何段落冲突时，以本段为准。\n正文不得出现募集说明书字样。"
    monkeypatch.setattr(llm, "chapter_prompt", lambda *a, **k: "你是经营分析员。\n\n" + directive)

    subsections = [
        {"subsection_id": "sub-1", "title": "主营业务概述", "prompt": ""},
        {"subsection_id": "sub-2", "title": "主要业务及稳定性分析", "prompt": "分业务分析"},
    ]
    system, request, *_ = llm._report_request(
        "主营业务及经营稳定性", "正文。", [], None, "operations",
        {"project_id": "p", "prompt_config": _config(), "subsections": subsections},
    )

    assert _SUBSECTION_SECTION_MARK in system, "小节契约被全局指令截断了"
    assert "sub-1" in system and "sub-2" in system
    assert GLOBAL_DIRECTIVE_MARK in system, "全局指令必须在最后一轮重注入"
    # 全局指令仍是最后一段（最高优先级），小节契约在它之前
    assert system.index(_SUBSECTION_SECTION_MARK) < system.index(GLOBAL_DIRECTIVE_MARK)
    assert system.rstrip().endswith("正文不得出现募集说明书字样。")
    assert request["required_subsections"] == subsections


def test_global_directive_kept_when_not_reread(monkeypatch):
    """读库失败时不得连同业务方声明的最高优先级约束一起丢掉。"""
    from backend.app.chapter_prompts import GLOBAL_DIRECTIVE_MARK

    directive = f"{GLOBAL_DIRECTIVE_MARK}最高优先级。\n正文不得出现募集说明书字样。"
    monkeypatch.setattr(llm, "chapter_prompt", lambda *a, **k: "你是分析员。\n\n" + directive)
    monkeypatch.setattr(llm, "apply_global_directive", lambda prompt: str(prompt or "").rstrip())

    system, *_ = llm._report_request(
        "标题", "正文。", [], None, "operations",
        {"project_id": "p", "prompt_config": _config()},
    )
    assert GLOBAL_DIRECTIVE_MARK in system
    assert "正文不得出现募集说明书字样。" in system
    assert system.count(GLOBAL_DIRECTIVE_MARK) == 1


def test_subsection_contract_is_eaten_without_stripping():
    """反面证据：不先摘全局指令，末尾重注入会把小节契约整段截掉。

    钉住失效机制本身——若本用例哪天不再"截断"，说明 `apply_global_directive()` 的
    哨兵语义变了，上面那条正例的保护也就不再必要。
    """
    from backend.app.chapter_prompts import GLOBAL_DIRECTIVE_MARK, apply_global_directive
    from backend.app.llm import _SUBSECTION_SECTION_MARK, apply_subsection_contract

    base = "系统提示。\n\n" + GLOBAL_DIRECTIVE_MARK + "最高优先级。\n正文不得出现募集说明书字样。"
    with_subs = apply_subsection_contract(base, [{"subsection_id": "s1", "title": "概述"}])
    assert _SUBSECTION_SECTION_MARK in with_subs

    final = apply_global_directive(with_subs)
    assert _SUBSECTION_SECTION_MARK not in final, "复现条件失效：哨兵不再截断后续内容"


# ------------------------------------------------- 结构化字段的空值容忍（2026-10-27）--

def test_recommendation_tolerates_null_amount_fields():
    """模型对"不需要额度/费率"的建议返回 null，不得判 schema_error。

    实测（operations 2026-10-27 13:18）：``recommendation_type=accept`` 的一条建议带了
    ``amount_value/amount_unit/rate_value/rate_unit`` 四个 null，Pydantic 直接 ValidationError，
    首稿与修复稿都走同一条 schema，最终整章退回确定性草稿。
    """
    section = LlmSection.model_validate({
        "content": "正文。",
        "citation_ids": [],
        "recommendations": [{
            "recommendation_id": "r1", "recommendation_type": "accept",
            "content": "建议在补充核验后审慎确定担保额度",
            "amount_value": None, "amount_unit": None, "rate_value": None, "rate_unit": None,
            "preconditions": ["完成客户集中度核验"],
        }],
    })
    item = section.recommendations[0]
    assert (item.amount_value, item.amount_unit, item.rate_value, item.rate_unit) == ("", "", "", "")
    assert item.recommendation_type == "accept"


def test_calculation_tolerates_null_text_fields():
    section = LlmSection.model_validate({
        "content": "正文。",
        "citation_ids": [],
        "calculations": [{
            "calculation_id": "c1", "name": "占比", "formula": "v1/v2",
            "input_record_ids": ["有息负债@2025"], "value": 80.0,
            "unit": None, "period": None, "subject": None, "scope": None,
        }],
    })
    item = section.calculations[0]
    assert item.value == "80.0"      # 数值转文本，保留给定精度
    assert (item.unit, item.period, item.subject, item.scope) == ("", "", "", "")


# ------------------------------------------------- 表格指令力度（2026-10-27）--

def test_allowed_table_mode_is_condition_driven():
    """`allowed` 必须是**判断式**：信息丰富且可比才用表，不完整就不出表。

    两版措辞都试过，各错一边：
    - "允许并鼓励…优先用表格" → 模型读成"可以不用"，整章 0 行表；
    - "必须…全文至少一张表" → 逼出凑表，4 行里 2 行整行"材料不足"，还多出
      "数据完备性"这种自我评价列（2026-10-27 operations 实测）。
    """
    from backend.app.chapter_prompts import table_instruction

    allowed = table_instruction("allowed")
    assert "自主判断" in allowed
    assert "不要出表" in allowed          # 明确的"不出表"出口
    assert "优先用表格" in allowed        # 可比时仍要求用表
    assert "数据完备性" in allowed        # 点名的反模式：自我评价列
    assert "填满整列" in allowed          # 点名的反模式：凑表
    assert "至少一张表" not in allowed     # 不再设数量下限
    assert table_instruction("prohibited") == ""
    assert "仅" in table_instruction("limited")


def test_subsection_contract_mentions_table_only_when_allowed():
    """小节契约是模型最后读到的组织指令，放开表格时必须点明它仍然生效。"""
    from backend.app.llm import apply_subsection_contract
    from backend.app.chapter_prompts import table_instruction

    subsections = [{"subsection_id": "s1", "title": "概述"}]
    on = apply_subsection_contract("系统提示。", subsections, table_mode="allowed")
    assert "【表格使用】" in on and "不要把表格拆成段落描述" in on

    off = apply_subsection_contract("系统提示。", subsections, table_mode="prohibited")
    assert "【表格使用】" not in off
    assert "不要把表格拆成段落描述" not in off
