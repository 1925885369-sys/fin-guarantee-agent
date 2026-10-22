"""受限数学表达式求值（模型自主计算的**系统复算**底座）。

方案《风控约束分层与模型分析能力调整方案》§三.2：

- 「采用受限数学表达式解析器及 Decimal 运算，**不执行模型代码**；支持四则运算、括号、
  平均、绝对值、最小值和最大值。首版不执行任意函数及预测模型。」
- 「业务输入必须引用冻结记录，不得凭空增加增长假设、折扣率或调整值。」

因此本模块**只做两件事**：把一个受限表达式解析成数值，以及核对它引用的输入是否都来自
本次冻结记录。它不读数据库、不碰模型输出结构，纯计算——这样复算逻辑可以被单独测试，
也便于在章节校验里按"每条 calculation 独立验证"复用。

安全边界（有意保持很窄）：

- 只有 `+ - * / ( ) ,`、数字、标识符、百分号后辍；
- 函数白名单只有 `avg/mean/abs/min/max`；
- **不使用 `eval`/`exec`/`ast.literal_eval`**，也不允许属性访问、下标、字符串、比较运算；
- 未知标识符、未定义函数、括号不配平、除零一律抛 `CalculationError`，绝不"猜一个值"。
"""

from __future__ import annotations

import difflib
import re
from decimal import (Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP, localcontext)

# 表达式里允许出现的字符（归一化后）：数字、四则、括号、逗号、小数点、百分号、下划线、
# 字母（变量名与函数名）、空白。其余一律在分词阶段就被拒——这是"不执行模型代码"的第一道闸。
# 标识符允许中日韩字符：模型常直接把科目名当变量（"营业收入/营业成本"），
# 拒掉它是排版问题而非公式错误。仍然只认"字母/数字/下划线/汉字"，不含任何运算符或引号。
_ALLOWED_CHARS_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fff+\-*/().,%\s]+$")
_TOKEN_RE = re.compile(r"""
    (?P<number>\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)
  | (?P<op>[+\-*/(),%])
  | (?P<space>\s+)
""", re.VERBOSE)

# 允许的聚合函数：名称 → (最少参数, 最多参数, 说明)
FUNCTIONS: dict[str, tuple[int, int | None]] = {
    "avg": (1, None),
    "mean": (1, None),
    "abs": (1, 1),
    "min": (1, None),
    "max": (1, None),
}

# 判定复算通过的相对容差：模型报 4 位小数、系统算 28 位，直接 `==` 必然失败。
# 取 1e-9 相对量级——比展示精度（通常 2~4 位）严格得多，又能吸收末位舍入差异。
_REL_TOLERANCE = Decimal("1e-9")
_ABS_TOLERANCE = Decimal("1e-9")


class CalculationError(ValueError):
    """表达式不合法或不可求值。``code`` 供台账与诊断分类，不面向最终用户渲染原文。"""

    def __init__(self, message: str, code: str = "formula_invalid") -> None:
        super().__init__(message)
        self.code = code


def normalize_expression(text: str) -> str:
    """把材料里常见的中文/全角写法归一为半角，再做字符白名单校验。

    模型经常照抄材料的全角括号与百分号（"（营业收入－营业成本）／营业收入"）。
    不归一化会被分词器拒掉，而这不是"公式错误"——是排版差异。所以先翻译再校验。
    """
    table = str.maketrans({
        "（": "(", "）": ")", "，": ",", "％": "%", "－": "-", "—": "-", "×": "*",
        "÷": "/", "／": "/", "＋": "+", "　": " ", "．": ".",
    })
    # 中文数字不当算式处理（如"三年平均"），由调用方在业务定义里描述，不进表达式。
    return str(text or "").translate(table).strip()


def _tokenize(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if not match:
            raise CalculationError(f"表达式中出现不支持的字符：{expression[position]!r}", "formula_invalid")
        position = match.end()
        if match.lastgroup == "space":
            continue
        tokens.append((match.lastgroup or "", match.group()))
    if not tokens:
        raise CalculationError("表达式为空", "formula_invalid")
    return tokens


class _Parser:
    """递归下降解析器：expr → term → factor → primary。

    ``variables`` 提供标识符取值；标识符必须已存在，否则报 `input_not_frozen`
    （方案 §三.2：业务输入必须引用冻结记录，不允许凭空取值）。
    """

    def __init__(self, tokens: list[tuple[str, str]], variables: dict[str, Decimal]) -> None:
        self._tokens = tokens
        self._index = 0
        self._variables = {key.upper(): value for key, value in variables.items()}

    def parse(self) -> Decimal:
        value = self._expr()
        if self._index != len(self._tokens):
            raise CalculationError(f"表达式存在多余内容：{self._peek()[1]!r}", "formula_invalid")
        return value

    # --- 词法辅助 ---------------------------------------------------------
    def _peek(self) -> tuple[str, str]:
        return self._tokens[self._index] if self._index < len(self._tokens) else ("", "")

    def _next(self) -> tuple[str, str]:
        token = self._peek()
        if not token[0]:
            raise CalculationError("表达式意外结束", "formula_invalid")
        self._index += 1
        return token

    def _accept(self, char: str) -> bool:
        if self._peek() == ("op", char):
            self._index += 1
            return True
        return False

    # --- 语法 -------------------------------------------------------------
    def _expr(self) -> Decimal:
        value = self._term()
        while True:
            if self._accept("+"):
                value = value + self._term()
            elif self._accept("-"):
                value = value - self._term()
            else:
                return value

    def _term(self) -> Decimal:
        value = self._factor()
        while True:
            if self._accept("*"):
                value = value * self._factor()
            elif self._accept("/"):
                divisor = self._factor()
                if divisor == 0:
                    raise CalculationError("公式除以零", "division_by_zero")
                value = value / divisor
            else:
                return value

    def _factor(self) -> Decimal:
        # 允许前导负号（如 -1.2），也允许多重（--x 视作 x）。
        if self._accept("-"):
            return -self._factor()
        if self._accept("+"):
            return self._factor()
        return self._primary()

    def _primary(self) -> Decimal:
        kind, text = self._peek()
        if kind == "number":
            self._index += 1
            value = Decimal(text)
            if self._accept("%"):
                value = value / Decimal(100)
            return value
        if kind == "ident":
            return self._identifier()
        if kind == "op" and text == "(":
            self._index += 1
            value = self._expr()
            if not self._accept(")"):
                raise CalculationError("括号不配平", "formula_invalid")
            return value
        raise CalculationError(f"表达式中出现意外的记号：{text!r}", "formula_invalid")

    def _identifier(self) -> Decimal:
        _kind, name = self._next()
        if self._peek() == ("op", "("):
            return self._call(name)
        key = name.upper()
        if key not in self._variables:
            # 变量必须来自冻结记录：未登记的标识符不是"算错了"，是"没有依据"。
            raise CalculationError(f"公式引用了未冻结的业务输入：{name}", "input_not_frozen")
        return self._variables[key]

    def _call(self, name: str) -> Decimal:
        lowered = name.lower()
        if lowered not in FUNCTIONS:
            raise CalculationError(f"公式使用了未支持函数：{name}", "formula_invalid")
        minimum, maximum = FUNCTIONS[lowered]
        self._index += 1                     # 吃掉 "("
        args: list[Decimal] = []
        if not self._accept(")"):
            while True:
                args.append(self._expr())
                if self._accept(","):
                    continue
                if self._accept(")"):
                    break
                raise CalculationError("函数参数括号不配平", "formula_invalid")
        if len(args) < minimum or (maximum is not None and len(args) > maximum):
            raise CalculationError(f"函数 {name} 的参数个数不对", "formula_invalid")
        if lowered in ("avg", "mean"):
            return sum(args, Decimal(0)) / Decimal(len(args))
        if lowered == "abs":
            return abs(args[0])
        if lowered == "min":
            return min(args)
        return max(args)


def evaluate(expression: str, variables: dict[str, Decimal] | None = None) -> Decimal:
    """求值。所有变量必须由调用方从**冻结记录**取出后传入。"""
    normalized = normalize_expression(expression)
    if not _ALLOWED_CHARS_RE.match(normalized):
        raise CalculationError("表达式包含不支持的字符", "formula_invalid")
    tokens = _tokenize(normalized)
    with localcontext() as context:
        context.prec = 28
        try:
            return _Parser(tokens, variables or {}).parse()
        except (DivisionByZero, InvalidOperation) as exc:
            raise CalculationError(f"公式无法求值：{exc}", "formula_invalid") from exc


def close_enough(computed: Decimal, declared: Decimal, precision: int | None = None) -> bool:
    """复算值是否等于模型声明值。

    两种口径，按调用方掌握的信息选用：

    - ``precision`` 给出（记录里带了"批准的展示精度"）→ **按该精度四舍五入后比对**。
      这才是方案 §三.2 说的"正文结果须符合复算值和批准的展示精度"：0.333333… 按 4 位
      展示成 0.3333 是合规的，按 2 位展示成 0.33 就不合规——容差式判据分不出这两种。
    - 未给 ``precision`` → 相对/绝对双容差（1e-9），只吸收浮点末位噪声，**不放宽展示精度**。
    """
    if precision is not None:
        quantum = Decimal(1).scaleb(-int(precision))
        try:
            return computed.quantize(quantum, rounding=ROUND_HALF_UP) == declared
        except InvalidOperation:
            return False
    difference = abs(computed - declared)
    if difference <= _ABS_TOLERANCE:
        return True
    scale = max(abs(computed), abs(declared))
    return difference <= scale * _REL_TOLERANCE


# --------------------------------------------------------------------------- 复算核对 --

# 单位族：同族不同量级（元/万元/亿元）**不静默换算**（方案 §三.2「禁止静默替换缺失输入」、
# 项目既有约定「单位不得被模型自行换算」）；跨族（万元 vs 人）才是明确的量纲错配。
_UNIT_FAMILIES = {
    "元": "currency", "千元": "currency", "万元": "currency", "亿元": "currency",
    "%": "ratio", "％": "ratio", "倍": "ratio", "个百分点": "ratio",
    "人": "count", "户": "count", "家": "count", "个": "count", "项": "count",
    "天": "days", "日": "days", "年": "days", "月": "days",
}

# 触发**修复**的错误码（方案 §二 line 22：明确事实错配、伪造来源和计算错误触发修复）。
HARD_ERROR_CODES = frozenset({
    "input_not_frozen", "formula_invalid", "division_by_zero", "value_mismatch",
    "unit_dimension_mismatch",
})
# 只保留**待核验稿**的错误码（方案 §三.3：有来源但定义或输入状态不明确）。
REVIEW_CODES = frozenset({
    "input_needs_review", "unit_scale_mismatch", "subject_mismatch", "period_mismatch",
    "definition_unclear",
})


def _unit_family(unit: str) -> str:
    return _UNIT_FAMILIES.get(str(unit or "").strip(), "")


def _decimal_or_none(value) -> Decimal | None:
    """记录值/声明值 → Decimal。解析失败返回 None（由调用方按"无法核对"处理）。"""
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


_PERCENT_UNITS = {"%", "％", "个百分点"}


def _is_percent_unit(unit: object) -> bool:
    return str(unit or "").strip() in _PERCENT_UNITS


_LABEL_WS_RE = re.compile(r"[\s\u3000]+")
# 报表科目的书写前缀：模型写「其他收益」，材料写「加：其他收益」；
# 模型写「流动资产合计」，材料写「其中：流动资产合计」。
# ⚠️ 数字前缀必须带顿号/点号（`一、` `（一）`），否则「一年内到期的非流动负债」
# 会被截成「年内到期的非流动负债」——这是最容易踩的过度归一。
_LABEL_PREFIX_RES = (
    re.compile(r"^(?:其中|加|减)\s*[：:]\s*"),
    re.compile(r"^[一二三四五六七八九十]+\s*[、.]\s*"),
    re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*"),
)
# 括号说明：「投资收益（损失以“-”号填列）」→「投资收益」。
# 只去掉带这些关键词的尾部括号，普通括号（如「短期借款（银行）」）保持原样，
# 否则会把两个本来不同的科目合成一个。
_LABEL_TAIL_PAREN_RE = re.compile(r"[（(][^）)]{0,24}(?:填列|亏损|损失)[^）)]{0,24}[）)]\s*$")


def normalize_label_for_match(label: str) -> str:
    """科目名的**匹配用**归一化（只用于比对，不改记录本身）。

    2026-10-18 实测：北投 financial 的全量记录里有 `加：其他收益`（6 条）、
    `投资收益（损失以“-”号填列）`（21 条），模型写自然科目名时全部解析不到
    → `input_not_frozen` → 整章回退。归一化只做这两类无害改写（前缀 + 尾部括号说明）。
    """
    text = _LABEL_WS_RE.sub("", str(label or ""))
    changed = True
    while changed:
        changed = False
        for pattern in _LABEL_PREFIX_RES:
            stripped = pattern.sub("", text)
            if stripped != text:
                text, changed = stripped, True
    return _LABEL_TAIL_PAREN_RE.sub("", text)


def resolve_input_token(token: str, frozen_records: list[dict], *,
                        scope: str = "", subject: str = "") -> list[str]:
    """把模型写的输入解析成记录 ID。

    支持两种写法（都要求**能在本次冻结记录里唯一命中**）：

    - ``record_id``：原样匹配（推荐，最精确）；
    - ``科目名@期间`` / 纯 ``科目名``：按 label（可选 period）解析。

    为什么需要后者：记录 ID 形如 ``report-financial-balance:r03c01@table-evidence-v2``，
    模型能记住"财务费用"，但极难记住哪一行是它——2026-10-18 北投 financial 实测
    16 条计算里 7 条 `value_mismatch`，其中多条是**两个输入填了同一个 ID**
    （`[r03c01, r03c01]` → 复算恒等于 1）。把 ID 解析交给系统，模型只需给业务键。

    返回命中的 record_id 列表（0 条 = 查无此记录；>1 条 = 同名歧义，需补 ``@期间``）。
    """
    token = str(token or "").strip()
    if not token:
        return []
    by_id = {str(item.get("record_id")): True for item in (frozen_records or [])}
    if token in by_id:
        return [token]
    label, _, period = token.partition("@")
    label = label.strip()
    period = period.strip()
    if not label:
        return []
    hits = [
        item for item in (frozen_records or [])
        if str(item.get("label") or "").strip() == label
        and (not period or str(item.get("period") or "").strip() == period)
        and item.get("record_id")
    ]
    if not hits:
        # 精确匹配不到 → 按归一化 label 再试（`加：其他收益` ≡ `其他收益`）。
        # 只在**零命中**时才用，避免把本来唯一的精确命中扩成歧义。
        target = normalize_label_for_match(label)
        if target:
            hits = [
                item for item in (frozen_records or [])
                if normalize_label_for_match(item.get("label")) == target
                and (not period or str(item.get("period") or "").strip() == period)
                and item.get("record_id")
            ]
    if len(hits) > 1 and str(scope or "").strip():
        # **口径优先**：同名同期在合并/母公司两套报表里都有，数值不同是正常的。
        # 计算自己声明了 scope，就按它筛——这既解析了歧义，也顺带保证口径一致
        # （否则会拿合并口径的负债去除母公司口径的资产）。
        scoped = [item for item in hits if str(item.get("scope") or "").strip() == str(scope).strip()]
        if scoped:
            hits = scoped
    if len(hits) > 1 and str(subject or "").strip():
        same_subject = [
            item for item in hits if str(item.get("subject") or "").strip() == str(subject).strip()
        ]
        if same_subject:
            hits = same_subject
    if len(hits) > 1:
        # 同一科目常常**跨来源重复**（系统整理表 report-* 与上传表 uploaded-* 各一行，
        # 数值相同）。这种"同名同期间同值"不是歧义——按值去重，优先取系统表那条。
        # 只有值/单位不一致时才是真歧义（需人工确认口径）。
        by_value: dict[tuple[str, str], list[dict]] = {}
        for item in hits:
            key = (str(item.get("value") or ""), str(item.get("unit") or ""))
            by_value.setdefault(key, []).append(item)
        if len(by_value) == 1:
            group = list(by_value.values())[0]
            group.sort(key=lambda item: 0 if str(item.get("record_id")).startswith("report-") else 1)
            return [str(group[0]["record_id"])]
    return [str(item.get("record_id")) for item in hits]


def _missing_input_hint(token: str, frozen_records: list[dict]) -> str:
    """输入 0 命中时给**可操作**的提示：是期间对不上，还是科目名不存在。

    只回一句"引用了未冻结的输入"，模型在唯一一次修复里仍然只能猜。2026-10-22 华辰实测
    两种 0 命中，修法完全不同：

    - `固定票息@2026`：记录里根本没有这个科目（材料写"票面利率"）→ 要改名；
    - `未来12个月到期债务@2025`：科目存在、但 2 条记录的 period 都是空 → 要去掉
      `@期间`，或直接用 record_id。

    提示只用于修复诊断，**不参与任何判定**。
    """
    raw = str(token or "").strip()
    label, _, period = raw.partition("@")
    label = label.strip()
    if not label:
        return ""
    target = normalize_label_for_match(label)
    same_label = [
        item for item in (frozen_records or [])
        if target and normalize_label_for_match(item.get("label")) == target
    ]
    if same_label:
        periods = sorted({str(item.get("period") or "").strip() for item in same_label})
        shown = "、".join(item or "（无期间）" for item in periods[:3])
        return (
            f"该科目有 {len(same_label)} 条记录，期间为{shown}，与你写的 `@{period}` 不符；"
            f"可去掉 `@期间` 或直接改用 record_id；"
        )
    candidates = sorted({
        str(item.get("label") or "").strip() for item in (frozen_records or [])
        if str(item.get("label") or "").strip()
    })
    # 只在**相似度确实高**时才猜名字（"应收账款" vs "应收款项" 这类写法差异）。
    # 不做"共享一个字"的粗筛：实测 `固定票息` 与记录里的 `票面利率` 只共享一个"票"字，
    # 任何字面启发式都只能给出误导性的候选（"利息支出""分配股利…"），还不如直接
    # 让它回去照抄索引——模型手上有完整的 label 列表，缺的只是"名字不对"这个信号。
    close = difflib.get_close_matches(label, candidates, n=3, cutoff=0.6)
    if close:
        return f"记录索引里最接近的是：{'、'.join(close)}；"
    return "记录索引里没有该科目名，请逐字照抄上文“科目索引”里的 label；"


def verify_calculation(record: dict, frozen_records: list[dict]) -> dict:
    """核对模型返回的一条 `calculations[]` 记录（方案 §三.2）。

    ``record`` 期望字段：``calculation_id`` / ``name`` / ``formula`` / ``input_record_ids`` /
    ``value`` / ``unit`` / ``subject`` / ``period`` / ``scope`` / ``precision``（可选）。

    返回::

        {"status": "verified"|"needs_review"|"rejected",
         "reason_code": ""|"input_not_frozen"|...,
         "detail": "面向人工的一句说明",
         "computed": "复算值（字符串，保持 Decimal 精度）",
         "declared": "模型声明值（字符串）",
         "inputs": [{"record_id", "value", "unit", "status"}]}

    - ``rejected``：确定错误（未冻结输入 / 公式非法 / 除零 / 值不符 / 量纲错配）→ 走一次修复。
    - ``needs_review``：来源可追溯但归属、口径、量级或定义不明确 → 保留待核验稿。
    """
    by_id = {str(item.get("record_id")): item for item in (frozen_records or [])}
    input_ids = [str(item) for item in (record.get("input_record_ids") or []) if str(item).strip()]

    def result(status: str, code: str = "", detail: str = "",
               computed: Decimal | None = None, inputs: list[dict] | None = None) -> dict:
        return {
            "status": status, "reason_code": code, "detail": detail,
            "computed": str(computed) if computed is not None else "",
            "declared": str(record.get("value") or ""),
            "inputs": inputs or [],
        }

    if not input_ids:
        return result("rejected", "input_not_frozen", "计算未声明任何输入记录")

    # ① 输入必须全部来自**本次冻结记录**——这是"不执行模型凭空的增长假设"的落点。
    #    允许 `科目名@期间`：模型记不住 record_id（实测会两个输入填同一行），
    #    但系统可以按 label+period 唯一解析。解析不了才算"未冻结"。
    resolved: list[str] = []
    missing: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for token in input_ids:
        hits = resolve_input_token(
            token, frozen_records,
            scope=str(record.get("scope") or ""), subject=str(record.get("subject") or ""),
        )
        if len(hits) == 1:
            resolved.append(hits[0])
        elif len(hits) > 1:
            ambiguous.append((token, hits))
        else:
            missing.append(token)
    if missing:
        return result("rejected", "input_not_frozen",
                      f"计算引用了本次未冻结的输入：{'、'.join(missing[:3])}；"
                      f"{_missing_input_hint(missing[0], frozen_records)}"
                      f"可写 record_id，或 `科目名@期间`（如 `财务费用@2025`）")
    if ambiguous:
        token, hits = ambiguous[0]
        # 把候选的 record_id 与实际值一起给出：模型只有一次修复机会，只告诉它"有 2 条"
        # 它仍然不知道选哪个（2026-10-22 华辰 cashflow：两个"未来12个月到期债务"
        # 都没有期间标注，补 `@期间` 也没用）。
        preview = "、".join(
            f"{str(by_id[hit].get('record_id'))}={by_id[hit].get('value')}"
            f"{by_id[hit].get('unit') or ''}" for hit in hits[:3] if hit in by_id
        )
        return result("needs_review", "input_ambiguous",
                      f"输入 {token} 匹配到 {len(hits)} 条记录（{preview}），"
                      f"请补 `@期间` 或直接用上面的 record_id")
    input_ids = resolved

    variables: dict[str, Decimal] = {}
    inputs: list[dict] = []
    for record_id in input_ids:
        item = by_id[record_id]
        value = _decimal_or_none(item.get("value"))
        if value is None:
            return result("rejected", "formula_invalid", f"输入记录 {record_id} 没有可解析的数值")
        variables[record_id] = value
        # **位置别名 v1..vN**：公式的词法只允许字母/数字/下划线/汉字，记录 ID 里的
        # `:` / `@` / `-` 根本进不了表达式，所以"用 record_id 当变量名"实际上不可用。
        # 而同一科目跨期会有两个同名 label，`setdefault` 会让后一个被静默丢掉。
        # 位置别名同时解决这两件事：input_record_ids 的第 1 个就写作 v1，第 2 个写作 v2。
        # ⚠️ 必须在 label 之前登记并**覆盖**，避免某个科目恰好叫 "v1" 时被反过来遮住。
        variables[f"v{len(inputs) + 1}"] = value
        # 科目名也登记为变量：模型常用"营业收入/营业成本"直接写公式（同名时后者覆盖前者，
        # 因此**记录 ID 优先**，科目名仅作便利别名）。
        label = str(item.get("label") or "").strip()
        if label:
            variables.setdefault(label, value)
        inputs.append({
            "record_id": record_id, "value": str(value), "unit": str(item.get("unit") or ""),
            "status": str(item.get("status") or ""), "label": label,
            "subject": str(item.get("subject") or ""), "period": str(item.get("period") or ""),
            "scope": str(item.get("scope") or ""),
        })

    # ② 求值（除零、非法函数、未冻结变量都在这里暴露成明确错误码）
    try:
        computed = evaluate(record.get("formula") or "", variables)
    except CalculationError as exc:
        code = exc.code if exc.code in HARD_ERROR_CODES else "formula_invalid"
        return result("rejected", code, str(exc), inputs=inputs)

    # ③ 量纲**先于**数值判：量纲错配时算出的数字没有意义，先报"量纲错配"才是可定位的诊断。
    #    跨族（万元 vs 人）→ 明确错配（硬）；同族不同量级（万元 vs 元）→ 不静默换算，待核验。
    families = {_unit_family(item["unit"]) for item in inputs} - {""}
    if len(families) > 1:
        return result("rejected", "unit_dimension_mismatch",
                      f"计算的输入量纲不一致：{'、'.join(sorted(families))}", computed=computed, inputs=inputs)
    units = {item["unit"] for item in inputs} - {""}
    if len(units) > 1:
        return result("needs_review", "unit_scale_mismatch",
                      f"计算的输入单位不同（{'、'.join(sorted(units))}），需确认是否已换算",
                      computed=computed, inputs=inputs)

    # ④ 主体/期间/口径：同主体同期间才是"同口径可比"；跨期须说明（增长率类）。
    subjects = {item["subject"] for item in inputs} - {""}
    if len(subjects) > 1:
        return result("needs_review", "subject_mismatch",
                      f"计算的输入分属不同主体：{'、'.join(sorted(subjects))}", computed=computed, inputs=inputs)
    periods = {item["period"] for item in inputs} - {""}
    if len(periods) > 1 and not str(record.get("cross_period_reason") or "").strip():
        return result("needs_review", "period_mismatch",
                      f"计算的输入跨期间（{'、'.join(sorted(periods))}）且未说明跨期口径",
                      computed=computed, inputs=inputs)

    # ⑤ 值一致（有"批准的展示精度"就按精度对齐判）
    declared = _decimal_or_none(record.get("value"))
    if declared is None:
        return result("rejected", "value_mismatch", "计算声明值不是可解析的数值",
                      computed=computed, inputs=inputs)
    precision = record.get("precision")
    precision_value = int(precision) if precision is not None else None
    matched = close_enough(computed, declared, precision_value)
    if not matched and _is_percent_unit(record.get("unit")):
        # 百分数只是**表示口径**（值×100），不是不同数值：模型常写
        # formula=v1/v2、value="38.41"、unit="%"，而复算得 0.3841。
        # 不认这一种口径，所有占比类计算都会误判 value_mismatch
        # （2026-10-18 北投 financial 实测 c13/c14/debt 均为此）。
        scaled = computed * 100
        if close_enough(scaled, declared, precision_value):
            computed = scaled
            matched = True
    if not matched:
        return result("rejected", "value_mismatch",
                      f"复算值 {computed} 与声明值 {declared} 不符", computed=computed, inputs=inputs)

    # ⑥ 输入状态：任一输入只是待核验，结果也只能待核验（不能借"算对了"洗白）。
    #    放在数值之后——数值明确不符是**确定错误**，应优先按修复处理。
    if any(item["status"] and item["status"] not in ("verified", "derived") for item in inputs):
        return result("needs_review", "input_needs_review",
                      "计算引用了待核验输入，结果亦待核验", computed=computed, inputs=inputs)

    return result("verified", "", "", computed=computed, inputs=inputs)
