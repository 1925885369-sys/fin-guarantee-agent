# 报告章节与标题层级（论文式）

尽调报告的章节、分部与标题编号由 `backend/app/report_outline.py` 统一计算，Markdown 导出、Word 导出和前端"报告交付"页面共用同一套层级，三处不会各写一套。

## 1. 层级体例（GB/T 7713 论文式）

| 层级 | 内容 | 编号 | Markdown | Word 样式 |
|---|---|---|---|---|
| 报告题名 | 债券发行融资担保尽调报告 | — | `#` | Title（22pt 黑体） |
| 一级 | 报告分部（六大部分） | 一、二、三… | `##` | Heading 1（16pt） |
| 二级 | 报告章节（17 章） | （一）（二）…（每分部内重新计数） | `###` | Heading 2（14pt） |
| 三级 | 章内分块（确定性分块标题） | 1. 2. 3.…（每章内重新计数） | `####` | Heading 3（12pt） |
| 四级 | 更深的标题（模型可能输出） | 不编号 | `#####` | Heading 3 之后的更深层级 |

示例：

```
一、报告摘要及审查范围
  （一）审查范围、材料状态和免责声明
      1. 审查范围
      2. 材料状态
二、发行人与本次债券
  （一）发行人主体、股权和治理
```

## 2. 关键设计：编号不写入正文

正文以 Markdown 分块存储（`### 标题`），**编号在导出/展示时计算**，原因：

1. LLM 受控改写会整体替换章节正文（`llm.rewrite_section`），写死的编号会丢失或被改写；
2. 章节可单独重新生成，写死编号会重复累加；
3. Markdown、Word、前端三个出口必须完全一致。

### 正文标题的层级钳制
`content_blocks()` 取正文中出现的最浅标题层级作为**三级标题**（编号 1. 2. 3.），更深的降为四级；
因此即使模型把标题写成 `##`（与章标题同级），也不会升到章标题之上——此前 Markdown 导出中章标题与
正文分块标题同为 `###`，层级是撞在一起的。

## 3. 分部与标题的来源

- 中文标题：`chapter_prompts.BOND_CHAPTER_TITLES`
- 六大部分分组：`chapter_prompts.BOND_CHAPTER_PARTS`（`报告摘要及审查范围` / `第一部分 发行人与本次债券` / … / `附录`）
- 编号渲染时去掉分部名自带的"第X部分"前缀，避免出现"二、第一部分 发行人与本次债券"这类重复
- 未列入任何分部的历史章节（如旧借款包章节）各自成为一个后续分部，编号顺延，不会丢失

## 4. 各出口的落点

| 出口 | 位置 |
|---|---|
| Markdown 导出 | `exports.markdown_report()` — `## 一、` / `### （一）` / `#### 1.` |
| Word 导出 | `exports.docx_report()`（`grouped_sections` + `_add_content_blocks`）、`_add_contents()` 目录为两级编号列表 |
| 报告 API | `GET /api/projects/{id}/report` 每行附加 `part_label` / `chapter_number` / `chapter_label` / `blocks` |
| 前端报告页 | `App.tsx` 的 `Report` + `ReportBlocks`：按分部插入一级标题、章标题前显示（一）编号、正文按 `blocks` 渲染三级/四级标题、列表、引用与表格 |

## 5. 验证

```bash
PYTHONPATH=. python -m pytest tests/test_report_outline.py tests/test_api.py -q
```

覆盖：中文数字与编号体例、正文标题层级钳制、Markdown 层级（`##`/`###`/`####` 不同级）、
Word 的 Heading 1/2/3 编号前缀、分部与章的连续编号。
