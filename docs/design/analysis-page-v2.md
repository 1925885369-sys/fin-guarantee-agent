# 分析页 v2 设计规格（Agent 分析 · 简约化与美化）

> 设计师：颜好看 ｜ 页面：`财务与交叉核验`（`Checks` 组件，`frontend/src/App.tsx` 524-536 行）
> 设计寄存器：**Product（产品型）** —— 设计服务产品，标杆是"赢得熟悉感"（Linear / Stripe / Bloomberg 式的可信数据界面），不是营销页。
> 三轴刻度：`DESIGN_VARIANCE = 3`（信息型页面，可预测的对称栅格） / `MOTION_INTENSITY = 2`（仅功能性过渡） / `VISUAL_DENSITY = 7`（偏紧凑，浅分隔线 + 无盒子分组）。
> 图标系统：**lucide-react@1.28.0**（Spec 已锁定，`App.tsx` 顶部已导入，全项目统一）。本规格用到的图标已在本项目实测版本**全部存在**：`MinusCircle / PackageSearch / Settings2 / CircleDashed / Table2 / ListFilter / AlertOctagon / AlertTriangle / Check / CheckCircle2 / LoaderCircle / X / ChevronDown / ChevronRight / PanelRightClose / Calculator / ArrowLeft / ShieldCheck / GitCompareArrows / FileText / SearchCheck / RotateCcw / Filter`，无需替换。尺寸沿用现状：行内 13-14，按钮内 15-17，独立 18-20。
> 色彩与字体唯一来源：`frontend/src/styles.css` 的 `:root`（1-38 行）与既有色板值。**本规格不新增任何 `:root` 变量，不新增任何硬编码色值。**

---

## 0. 设计约束基线（不可协商）

| 项 | 规则 | 来源 |
|---|---|---|
| 图标库 | lucide-react@1.28.0（本规格所用图标实测全部存在，无需替换），**禁止 emoji 作功能图标** | Spec 锁定 |
| 主色 | `--primary: #2f54d4`（蓝白风），**禁止紫→粉渐变** | `styles.css:10` |
| 颜色 | 一律 `var(--*)`；灰度复用 `styles.css` 既有值并注明行号；仅 `#fff`/`#000` 例外 | 需求 D 约束 |
| 字号 | 仅用 `--font-page/section/body/label/caption/micro/metric/metric-sm`；**禁止 8-11px** | `styles.css:27-34` |
| 行高 | 普通文字 `--line-normal`(1.5)，长说明 `--line-long`(1.6) | `styles.css:36-37` |
| 圆角 | 沿用现有 4 / 5 / 6 / 7px 尺度 | `styles.css` 既有 |
| 分隔线 | `var(--line)`(#e5e7eb) 主分隔；`#edf0f5` 行内细分隔（`styles.css:227/254`） | 既有 |
| class 命名 | 全部新 class 前缀 `analysis-`，避免影响其他页面 | 需求 D 约束 |
| 业务代码 | **本规格不改任何 `App.tsx` / `styles.css`**，只给可执行规格 | 交付纪律 |

### 可用 Token 清单（本规格只会引用这些）

```
/* :root 变量（styles.css:6-19） */
--ink #1f2937        --muted #5b6675     --line #e5e7eb      --panel #ffffff
--primary #2f54d4    --primary-dark #2342b6  --primary-light #edf3ff   --cyan #13c2c2
--high #e53935       --high-bg #fff1f0
--medium #c77800     --medium-bg #fff7e6
--low #168247        --low-bg #edf9f1

/* 字号（styles.css:27-34） */
--font-page 24 / --font-section 16 / --font-body 14 / --font-label 14
--font-caption 12 / --font-micro 12 / --font-metric 24 / --font-metric-sm 20
--line-normal 1.5 / --line-long 1.6

/* 复用 styles.css 既有色板值（非 :root，但全站已用，注明行号） */
#f7f9fc 浅底        （252 / 299 / 371）
#edf0f5 行内分隔     （227 / 254）
#f1f3f6 灰底        （297）
#eef1f6 灰底（浅）   （327）
#e8ecf3 进度槽灰     （267）
#dce2ee 连接线灰     （188 / 94）
#dce3f0 卡片边        （182）
#cfd6e4 圆圈边        （96 / 189）
#526079 中性文字      （125 / 218 / 261）
#667085 中性文字      （297 / 485）
#687386 次要文字      （334 / 278）
#697386 表头文字      （252）
#7b8494 弱化文字      （164 / 185）
#8a94a6 最弱文字      （190）
#9ca3af 材料不足灰    （289）
#4b5563 正文灰        （216 / 298）
#e2aaa5 / #8d302a / #fff1ef  错误带边/文字/底（styles.css:521）
#e1c68e/#fff8e8/#765414 警示带（520）
#b8e3ca 成功带边框（244）
（以下既有色值本规格不使用：#fbeeea / #fee2e2 / #fef3c7 / #dcfce7）
```

---

## 1. 信息架构与首屏栅格

### 1.1 三层结构

```
分析页（Checks）
├─ Layer 1  运行重点         .analysis-runfocus        ← 首屏第 1 要素
│   ├─ 状态栏（含运行详情展开）  .analysis-run-bar
│   ├─ 进度行（当前节点/进度/消息）或失败错误带  .analysis-run-progress / .analysis-run-error
│   ├─ 运行结论带（仅 completed 时出现）  .analysis-run-result
│   ├─ 11 节点紧凑步骤条        .analysis-node-strip
│   └─ 选中节点摘要（默认关闭）  .analysis-node-detail
├─ Layer 2  财务汇总         .analysis-finance-summary  ← 首屏第 2 要素（一行，取代 12 张大卡）
└─ Layer 3  核验详情         .analysis-section × 2      ← 首屏第 3 要素（两个分区标题必须可见）
    ├─ 报表勾稽校验  .analysis-section（tieOuts）
    └─ 核验规则      .analysis-section（checks）
        每个分区 = 紧凑标题行 + 可折叠结果区（筛选 + 紧凑结果行 + 分页）
```

### 1.2 1440×900 首屏纵向节奏（硬预算，前端照此实现即可达标）

坐标系：`y=0` 为视口顶部。固定顶栏高 **56px**；`.view-content` 上内距 **22px**；`.page-heading` 高 **48px** + 下距 **16px**。

```
y   0    ├── topbar（fixed，56）
y  56    ├── main-area 起点（padding-top 56）
y  78    ├── view-content padding-top 22
y  78    ├── page-heading 48 → 126 ; margin-bottom 16 → 142
─────────── 视口内剩余高度 = 900 - 142 - 42(底部内距) = 716 ───────────
y 142    │  L1 运行重点 .analysis-runfocus                      高 190（含 margin-bottom 14）
y 332    │  L2 财务汇总 .analysis-finance-summary               高  78（含 margin-bottom 14）
y 410    │  L3a 分区头 报表勾稽校验 .analysis-section-head       高  44
y 454    │  L3a 结果区（展开时，内部滚动）                       高 ≤264
y 718    │  L3a 分页条 .analysis-section-foot                    高  44
y 762    │  分区间距                                         12
y 774    │  L3b 分区头 核验规则 .analysis-section-head           高  44  → y 818
──────────────────────────────────────────────────────────────────────
         818 < 900  OK  两个核验分区标题均在首屏内，余量 82px
```

**L1 高度拆分（190px）**：状态栏 44 + 进度行 40 + 运行结论带 42 + 步骤条 44 + 上下边框 2 + margin-bottom 14 ≈ 186（取 190）。
**步骤条默认折叠**（无选中节点）；节点摘要是**用户主动点击**后才出现（约 +76px），属于交互态，不计入首屏验收。

**关键约束（保证验收）**：`.analysis-section-body` 桌面 ≥1440 下 `max-height: 264px`。这一约束确保即使第一个分区因存在异常而**自动展开**，第二个分区标题仍在首屏内。默认筛选为「异常」，异常条数通常少，多数情况下不触发内部滚动。

### 1.3 水平栅格

| 项 | 值 |
|---|---|
| 侧栏 | `248px` 固定（`styles.css:51`，不改） |
| 内容容器 | `.view-content` `max-width: 1440px`，`padding: 22px 24px 42px`（`styles.css:155`，不改） |
| 1440 视口实际可用内容宽 | `1440 - 248 - 48 = 1144px` |
| L1 / L2 / L3 均为**单列纵向堆叠**，无多列栅格 | — |
| 分区内水平栅格 | 结果行 `grid-template-columns: auto minmax(0,1fr) auto`（状态 / 摘要 / 元数据） |

---

## 2. 逐区块组件规格

> 所有字号一律写成 `var(--font-*)`；所有颜色一律写成 `var(--*)` 或"复用 styles.css:NNNN 既有值"。

### 2.A Layer 1 —— 运行重点

#### 2.A.1 容器 `.analysis-runfocus`
```css
.analysis-runfocus {
  margin-bottom: 14px; overflow: hidden;
  border: 1px solid var(--line); border-radius: 7px; background: var(--panel);
}
```

#### 2.A.2 状态栏 `.analysis-run-bar`（压缩原 `.run-summary` 三块大统计）
- 结构：`flex; align-items:center; gap:12px; min-height:44px; padding:0 14px; border-bottom:1px solid var(--line);`
- 子元素：

| 元素 | 类 | 视觉 |
|---|---|---|
| 运行状态 chip | `.analysis-run-status` | `inline-flex; gap:6px; height:24px; padding:0 9px; border-radius:4px; font:var(--font-caption); font-weight:600` |
| 校验结论 chip（**防误读核心**） | `.analysis-run-verdict` | 同尺寸；见下方状态变体 |
| 事实组 | `.analysis-run-facts` | `display:flex; gap:20px;` 每项 `.analysis-run-fact` 为 `display:grid; gap:1px`：label 用 `var(--font-caption)` `#7b8494`，值用 `var(--font-label)` 600 `var(--ink)` |
| 详情开关 | `.analysis-run-toggle` | 复用 `.secondary-button`（`styles.css:136`）+ `ChevronDown 15`（展开时 `rotate(180deg)`）；文案「运行详情 / 收起详情」；`margin-left:auto` |

- 事实组字段（耗时 / 成本 / Token 常驻；输入·输出 Token 移入详情）：
  - 耗时 `{latency_ms/1000 toFixed(1)} 秒`，label「耗时」
  - 成本 `¥{cost.toFixed(3)}`，label「成本」
  - Token `{(input_tokens+output_tokens).toLocaleString()}`，label「Token」

**`.analysis-run-status` 变体**

| run.status | class | 文字色 | 底色 | 图标 |
|---|---|---|---|---|
| completed | `.is-completed` | `var(--low)` | `var(--low-bg)` | `Check` 14 |
| running / queued | `.is-running` | `var(--primary)` | `var(--primary-light)` | `LoaderCircle` 14（`.spin`） |
| failed | `.is-failed` | `var(--high)` | `var(--high-bg)` | `AlertOctagon` 14 |
| model_required | `.is-model` | `var(--medium)` | `var(--medium-bg)` | `Settings2` 14 |
| 其余（未完成/空） | `.is-pending` | `#8a94a6`（190） | `#f1f3f6`（297） | `CircleDashed` 14 |

文字：`已完成 / 执行中 / 未完成 / 需要模型配置`（沿用现有映射，不加新文案）。

**`.analysis-run-verdict` 变体（校验结论，独立于运行状态）**

| 条件 | class | 文字色 | 底色 | 图标 | 文案 |
|---|---|---|---|---|---|
| tieOuts+checks 异常数 > 0 | `.is-warning` | `var(--medium)` | `var(--medium-bg)` | `AlertTriangle` 14 | `校验 {b} 项异常 · {c} 项材料不足` |
| 有结果且异常=0 | `.is-passed` | `var(--low)` | `var(--low-bg)` | `ShieldCheck` 14 | `校验全部通过` |
| 无任何校验结果 | `.is-empty` | `#8a94a6`（190） | `#f1f3f6`（297） | `MinusCircle` 14 | `暂无校验结果` |

#### 2.A.3 运行详情（展开态）`.analysis-run-detail`
- `border-top:1px solid var(--line); padding:10px 14px; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px 20px; background:#fbfcfe`（330 既有底色）
- 项：输入 Token、输出 Token、模型成本、开始时间、结束时间、运行消息（跨 3 列 `grid-column:1/-1`，`line-height:var(--line-long)`）
- 每项 `.analysis-run-detail-item`：label `var(--font-caption)` `#7b8494`；值 `var(--font-label)` `var(--ink)`；消息为 `var(--font-body)` `#4b5563`（216）

#### 2.A.4 进度行 `.analysis-run-progress`
- `display:flex; align-items:center; gap:12px; min-height:40px; padding:0 14px; border-bottom:1px solid var(--line);`
- 子元素：
  - `.analysis-run-now`：`inline-flex; gap:7px; align-items:center;` 图标 + 文案 `当前节点：{nodeLabels[current_node]}（{i+1}/11）`（`var(--font-body)` `var(--ink)`；节点名用 600）。图标：running→`LoaderCircle` 15 `.spin` `var(--primary)`；非 running 且 completed→`CheckCircle2` 15 `var(--low)`；failed→`AlertTriangle` 15 `var(--high)`。
  - `.analysis-progress-track`：`flex:1; height:6px; overflow:hidden; border-radius:3px; background:#e8ecf3`（267）
  - `.analysis-progress-fill`：`height:100%; background:var(--primary); border-radius:3px; transition:width .3s ease`（`width:{progress}%`）
  - `.analysis-progress-percent`：`var(--font-label)` 600 `var(--primary)`，文案 `{progress}%`
  - `.analysis-run-msg`：`min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;` `var(--font-caption)` `#687386`（334）；显示 `run.message`

**失败态**：进度行整体替换为 `.analysis-run-error`
```css
.analysis-run-error { min-height:42px; padding:0 14px; display:flex; align-items:center; gap:9px;
  border-bottom:1px solid var(--line); color:#8d302a; background:#fff1ef; font-size:var(--font-caption); }
.analysis-run-error button { margin-left:auto; }
```
- 图标 `AlertTriangle 16`；文案 `运行失败：{run.error}`（`--font-body`，允许换行）；右侧 `RotateCcw 15` + 「重新运行」`.text-button`。失败原因**在进度行位置优先展示**，步骤条同步高亮 failed 节点。

#### 2.A.5 运行结论带 `.analysis-run-result`（仅 `completed` 时出现）
```css
.analysis-run-result { min-height:42px; padding:0 14px; display:flex; align-items:center; gap:9px;
  font-size:var(--font-caption); border-bottom:1px solid var(--line); }
```
| 条件 | class | 文字色 | 底色/边框 | 图标 | 文案（防误读） |
|---|---|---|---|---|---|
| 完成且有异常 | `.is-warning` | `#765414`（520） | `#fff8e8`（520）/ 边框 `#e1c68e`（520） | `AlertTriangle` 16 | `运行已完成，但校验存在 {b} 项异常、{c} 项材料不足 —— 运行完成不等于校验通过，请查看下方两个核验分区。` |
| 完成且零异常 | `.is-passed` | `var(--low)` | `var(--low-bg)` / 边框 `#b8e3ca`（244） | `ShieldCheck` 16 | `运行已完成，{t} 项校验全部通过。` |

#### 2.A.6 11 节点紧凑步骤条 `.analysis-node-strip`
```css
.analysis-node-strip { position:relative; display:flex; align-items:center;
  padding:6px 10px; overflow-x:auto; overscroll-behavior-x:contain;
  scrollbar-width:thin; }
/* 单一连接线，画在图标中心高度（6px padding + 11px 半高 = 17px） */
.analysis-node-strip::before { content:""; position:absolute; left:26px; right:26px; top:17px;
  height:1px; background:#dce2ee; }  /* 188 既有 */
```
- 每个节点 `.analysis-node`（`<button>`）：
```css
.analysis-node { position:relative; z-index:1; flex:0 0 auto; min-width:84px; height:44px;
  padding:2px 8px; display:grid; justify-items:center; gap:2px; border:0; border-radius:5px;
  background:transparent; cursor:pointer; text-align:center; }
.analysis-node-icon { width:22px; height:22px; display:grid; place-items:center;
  border:1px solid #cfd6e4; border-radius:50%; background:var(--panel);
  color:#8a94a6; font-size:var(--font-caption); }   /* 未执行显示序号 */
.analysis-node-name { max-width:78px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  color:#4b5563; font-size:var(--font-caption); }
.analysis-node:hover { background:#f7f9fc; }
.analysis-node.is-selected { background:var(--primary-light); box-shadow:inset 0 -2px var(--primary); }
.analysis-node:focus-visible { outline:2px solid var(--primary); outline-offset:1px; }
```
- **五态 + 待执行**配色（与 §3 状态配色表一致）：

| 节点状态 | class | 图标圆：色 / 边框 / 底 | 图标 | 名称文字色 | 额外 |
|---|---|---|---|---|---|
| completed | `.node-completed` | `#fff` / `var(--low)` / `var(--low)` | `Check` 13 | `#4b5563` | — |
| current（running） | `.node-running` | `#fff` / `var(--primary)` / `var(--primary)` | `LoaderCircle` 13 `.spin` | `#4b5563`（当前节点名称 600） | 名称前加 `var(--primary)` 2px 下划线（仅当前） |
| warning | `.node-warning` | `var(--medium)` / `var(--medium)` / `var(--medium-bg)` | `AlertTriangle` 13 | `var(--medium)` | — |
| degraded | `.node-degraded` | `#526079` / `#cfd6e4` / `#f7f9fc` | `MinusCircle` 13 | `#526079` | 图标圆**虚线下边框**表达"部分产出" |
| failed / model_required | `.node-failed` | `#fff` / `var(--high)` / `var(--high)` | `X` 13 | `var(--high)` | — |
| pending | `.node-pending` | `#8a94a6` / `#cfd6e4` / `var(--panel)` | 序号数字 | `#8a94a6` | — |

> degraded 与 warning 均为琥珀语义但**形状不同**（实心三角 vs 空心减号）+ **文字标签不同**（「警告」/「降级」），不依赖颜色单独传达（WCAG 1.4.1）。

#### 2.A.7 选中节点摘要 `.analysis-node-detail`
```css
.analysis-node-detail { padding:10px 14px; display:grid; gap:5px;
  border-top:1px solid var(--line); background:#fbfcfe; }
.analysis-node-detail-head { display:flex; align-items:center; gap:8px; }
.analysis-node-detail-head strong { font-size:var(--font-label); color:var(--ink); }
.analysis-node-detail-head small { font-size:var(--font-caption); color:#7b8494; }
.analysis-node-detail p { margin:0; font-size:var(--font-body); line-height:var(--line-long); color:#4b5563; }
.analysis-node-detail p.is-error { color:var(--high); }
```
- 内容：`[状态 chip] 节点名 · 第 {i+1} / 11 步` + 右侧耗时 `{started_at→finished_at}`（`var(--font-caption)` `#7b8494`）
- 第二行：`output_summary`；若节点有 `error` 则显示 error 且用 `.is-error` 色。

---

### 2.B Layer 2 —— 财务汇总行（取代 `.financial-strip` 12 张大卡）

`.analysis-finance-summary`
```css
.analysis-finance-summary { min-height:64px; margin-bottom:14px; padding:0 14px;
  display:flex; align-items:center; gap:24px; flex-wrap:wrap;
  border:1px solid var(--line); border-radius:7px; background:var(--panel); }
.afs-title { display:flex; align-items:center; gap:8px; color:var(--primary); }
.afs-title strong { color:var(--ink); font-size:var(--font-body); }
.afs-stat { display:grid; gap:1px; }
.afs-stat span { color:var(--muted); font-size:var(--font-caption); }
.afs-stat strong { color:var(--ink); font-size:var(--font-metric-sm); font-weight:700; }
.afs-action { margin-left:auto; }
```

| 元素 | 内容 | 数据口径（视觉需回显） |
|---|---|---|
| 标题 | `Table2` 18 `var(--primary)` + `财务指标` | — |
| 指标种类 | `{distinct metric_key 数} 类` | 全部指标去重计数 |
| 有效计算 | `{value!==null 的最新记录数} / {最新记录总数}` | 有值 / 总数 |
| 期间范围 | `{minPeriod}–{maxPeriod}`，单期时只显示一期；无数据显示 `—` | 全部期间 min~max |
| 入口 | `.primary-button` `查看全部指标` + `ListFilter` 16，`margin-left:auto` | 打开指标抽屉 |
| 提示 | `.afs-note`：`异常数量见下方核验分区` | `var(--font-caption)` `#8a94a6`；**明确排除据指标值推断异常** |

> `.afs-note` 是「避免据指标数值自行推断异常」的显式视觉声明，不可省略。财务汇总行**不出现**"通过/异常/预警"任何字样与颜色。

---

### 2.C Layer 3 —— 两个核验分区

#### 2.C.1 分区容器 `.analysis-section`
```css
.analysis-section { margin-bottom:12px; overflow:hidden;
  border:1px solid var(--line); border-radius:7px; background:var(--panel); }
```

#### 2.C.2 紧凑标题行 `.analysis-section-head`
```css
.analysis-section-head { min-height:44px; padding:0 14px; display:flex; align-items:center;
  gap:10px; background:#fbfcfe; }
.analysis-section-toggle { width:24px; height:24px; flex:0 0 24px; display:grid; place-items:center;
  border:0; border-radius:4px; background:transparent; color:#7b8494; cursor:pointer; }
.analysis-section-toggle svg { transition:transform .15s ease; }
.analysis-section-toggle[aria-expanded="true"] svg { transform:rotate(90deg); }
.analysis-section-head h2 { margin:0; font-size:var(--font-section); }   /* 16，复用 :554 覆盖 */
.analysis-section-head p { margin:0; color:var(--muted); font-size:var(--font-caption); }
.analysis-section-counts { margin-left:auto; display:flex; align-items:center; gap:6px; }
.analysis-section-filter { margin-left:auto; }
```

| 元素 | 内容 |
|---|---|
| 展开箭头 | `.analysis-section-toggle`，`ChevronRight` 16；`aria-expanded` 与 `aria-controls` |
| 图标 | 报表勾稽校验 `GitCompareArrows` 18 `var(--primary)`；核验规则 `ShieldCheck` 18 `var(--primary)` |
| 标题 | `h2` `var(--font-section)`；`p` 说明（报表勾稽：`同一主体、期间及报表口径内核对，缺项不按零值处理`；核验规则：`确定性内核执行的规则校验，不调用模型`）+ 内联 `HelpTip`（复用现有组件） |
| 计数 chips | 见下表 |

**计数 chip `.asc-chip`**
```css
.asc-chip { min-height:22px; padding:0 8px; display:inline-flex; align-items:center; gap:4px;
  border-radius:4px; font-size:var(--font-caption); }
```
| 维度 | class | 文字色 | 底色 | 图标 | 文案 |
|---|---|---|---|---|---|
| 总数 | `.is-total` | `#526079`（125） | `#eef1f6`（327） | `ListChecks` 13 | `共 {t}` |
| 异常 | `.is-warning` | `var(--medium)` | `var(--medium-bg)` | `AlertTriangle` 13 | `异常 {b}` |
| 材料不足 | `.is-insufficient` | `#667085`（297） | `#f1f3f6`（297） | `PackageSearch` 13 | `材料不足 {c}` |
| 通过 | `.is-passed` | `var(--low)` | `var(--low-bg)` | `CheckCircle2` 13 | `通过 {p}` |

- 当某维度计数为 **0** 时切换为 `.is-muted`：文字色 `#8a94a6`（190）、底色 `#f7f9fc`（252），**弱化但不隐藏**，避免"0 异常"被误读为高亮通过。

**筛选器 `.analysis-section-filter`**（仅展开时出现；复用 `.segmented` 样式，`styles.css:354`）
- 选项：`异常 / 材料不足 / 通过 / 全部`（每项带计数，如 `异常 3`）
- 默认选中规则见 §4.3。
- 按钮 `min-height:28px`（沿用 `.segmented button`），选中态 `color:#fff; background:var(--primary)`。
- 提供 `select` 兜底于 ≤600 视口（避免 4 段挤爆）。

#### 2.C.3 结果区 `.analysis-section-body`
```css
.analysis-section-body { max-height:264px; overflow-y:auto; overscroll-behavior:contain;
  border-top:1px solid var(--line); }
@media (min-width:1441px) { .analysis-section-body { max-height:300px; } }
```
> 264px 是首屏验收的硬约束（§1.2），不得放大；`≥1441px` 视口有额外高度可放宽到 300px。

#### 2.C.4 紧凑结果行 `.analysis-result-row`
```css
.analysis-result-row { min-height:52px; padding:8px 14px; display:grid; align-items:center;
  grid-template-columns:auto minmax(0,1fr) auto; gap:10px;
  border-bottom:1px solid #edf0f5; cursor:pointer; }
.analysis-result-row:last-child { border-bottom:0; }
.analysis-result-row:hover { background:#f7f9fc; }
.analysis-result-row.is-expanded { background:#f7f9fc; }
.analysis-result-row:focus-visible { outline:2px solid var(--primary); outline-offset:-2px; }
.arr-status { display:inline-flex; align-items:center; gap:4px; min-height:22px; padding:0 8px;
  border-radius:4px; font-size:var(--font-caption); font-weight:600; white-space:nowrap; }
.arr-main { min-width:0; display:grid; gap:2px; }
.arr-main strong { font-size:var(--font-label); color:var(--ink);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.arr-main small { font-size:var(--font-caption); color:#687386; line-height:var(--line-normal);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.arr-meta { display:inline-flex; align-items:center; gap:8px; color:#7b8494; font-size:var(--font-caption); }
.arr-main .is-insufficient-text { color:#8a94a6; font-style:normal; }
```

- 三列：`状态 chip` / `标题 + 结论摘要（各一行，超出省略）` / `元数据（期间 · 口径）+ ChevronDown`（展开时旋转 180°）。
- 结果行的「结论摘要」来源：tieOuts → `rationale`；checks → `summary`。
- 点击结果行 → 切换 `.is-expanded`，并在其下方渲染 `.arr-detail`。

#### 2.C.5 结果详情 `.arr-detail`（展开态）
```css
.arr-detail { padding:10px 14px 12px; display:grid; gap:8px; background:#f7f9fc;
  border-bottom:1px solid #edf0f5; }
.arr-detail dl { margin:0; display:grid; grid-template-columns:76px minmax(0,1fr); gap:4px 12px; }
.arr-detail dt { color:var(--muted); font-size:var(--font-caption); }
.arr-detail dd { margin:0; color:var(--ink); font-size:var(--font-label); line-height:var(--line-normal);
  overflow-wrap:anywhere; }
```
内容（报表勾稽校验）：
- **数值对比**：`左值 {left_value} ｜ 右值 {right_value} ｜ 差异 {difference} ｜ 容差 {tolerance}`，用 `dl` 两列；差异超容差时数值 `var(--medium)`。
- **公式**：`formula`（`--font-family-mono`，`var(--font-caption)`，`#526079`）。
- **政策版本**：`v{policy_version}`（`.policy-chip` 复用，`styles.css:309`）。
- **证据**：`.evidence-links`（复用，`styles.css:305`）按钮 `FileText` 14 + locator 短名，`onClick → openCitation`。

内容（核验规则）：
- **数值对比**：复用 `.bar-group` + `.bar-row`（`styles.css:299-304`）柱状对比 `values_json`，保持现有视觉。
- **公式 / 阈值 / 政策版本**：同 `HelpTip` 现有内容（`App.tsx:533` 的 `rule-meta`），此处**内联展开**而非 tooltip。
- **严重度**：`SeverityBadge`（复用）置于详情首行。
- **证据**：`.evidence-links`。

#### 2.C.6 分页条 `.analysis-section-foot`
```css
.analysis-section-foot { position:sticky; bottom:0; z-index:1; background:var(--panel);
  border-top:1px solid var(--line); }
```
- 直接复用 `<Pagination>`（`frontend/src/components/common/Pagination.tsx`）：默认 `pageSize=10`，可选 50。
- `pageCount = Math.max(1, Math.ceil(filtered.length / pageSize))`；`total = filtered.length`。
- 仅当 `filtered.length > pageSize` 时渲染分页条；否则隐藏（保持紧凑）。
- `sticky bottom:0` 让分页在结果区内部滚动时始终可见，避免"双层滚动"困惑。

#### 2.C.7 图例 `.analysis-legend`
- 复用现有 `.legend .checks-legend`（`styles.css:286 / 319`），置于两个分区之间，说明：`预警=需人工关注`、`通过`、`材料不足`。

---

### 2.D 财务指标抽屉 `.analysis-metrics-drawer`（沿用 EvidenceDrawer 手法）

> 复用 `App.tsx:1467 EvidenceDrawer` 的遮罩/宽度/关闭按钮/焦点处理范式；**不另起一套**。

```css
.analysis-drawer-mask { position:fixed; inset:0; z-index:37; border:0;
  background:rgba(15,23,42,.32); }          /* 沿用项目 rgba(15,23,42,·) 体系 */
.analysis-metrics-drawer { width:min(760px,94vw); position:fixed; inset:0 0 0 auto; z-index:38;
  overflow-y:auto; transform:translateX(100%); transition:transform .22s ease;
  background:var(--panel); border-left:1px solid #d9dfeb;   /* 490 既有 */
  box-shadow:-16px 0 40px rgba(15,23,42,.12); }
.analysis-metrics-drawer.open { transform:translateX(0); }
@media (prefers-reduced-motion: reduce) { .analysis-metrics-drawer { transition:none; } }
```

> **层级关系（关键）**：证据抽屉 `.evidence-drawer` 的 `z-index:40`（`styles.css:490`）**高于**指标抽屉 38（遮罩 37）。因此从抽屉内点「证据」打开证据抽屉时，证据层正确覆盖在指标抽屉之上，无遮挡冲突。`modal-backdrop` 的 70 不参与本流程（计算链路详情改为**抽屉内视图**，不再用 `.trace-modal`）。

**头部**：复用 `.drawer-head`（`styles.css:492`，`min-height` 收窄到 56）
- 左：`span` 眉标 `财务指标`（`var(--font-caption)` `var(--muted)`）+ `strong` 当前视图标题（`var(--font-section)`；列表态 `共 {n} 类 · {m} 条记录`，详情态 `{metric.name}`）
- 右：`.icon-button` + `PanelRightClose 19`，`title="关闭"` `aria-label="关闭财务指标"` → `close()`

**工具栏 `.analysis-drawer-toolbar`**（仅列表态）
```css
.analysis-drawer-toolbar { min-height:44px; padding:0 14px; display:flex; align-items:center; gap:12px;
  border-bottom:1px solid var(--line); }
.analysis-drawer-toolbar small { color:var(--muted); font-size:var(--font-caption); }
```
- `.segmented`：`最新` / `全部期间`（默认 `最新`）
- 说明：最新态 `仅显示各主体、口径下每类指标的最新记录`；全部态 `显示全部期间记录`。

**紧凑表格 `.analysis-metric-table`**
```css
.amt-head, .amt-row { display:grid; gap:10px; align-items:center;
  grid-template-columns:minmax(0,1.6fr) 108px 92px 96px 84px; }
.amt-head { height:34px; padding:0 14px; color:#697386; background:#f7f9fc;
  border-bottom:1px solid var(--line); font-size:var(--font-caption); font-weight:600; }
.amt-row { min-height:46px; padding:8px 14px; border-bottom:1px solid #edf0f5;
  font-size:var(--font-caption); }
.amt-row strong { font-size:var(--font-label); font-weight:600; color:var(--ink);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.amt-scope { padding:2px 6px; border-radius:3px; color:#526079; background:#eef2fa; }
.amt-link { padding:0; display:inline-flex; align-items:center; gap:4px; color:var(--primary);
  border:0; background:transparent; cursor:pointer; font-size:var(--font-caption); }
```
| 列 | 内容 |
|---|---|
| 指标 | `name`（`.amt-row strong`），下一行 `var(--font-caption)` `#7b8494` 显示 `formula` 截断（可选） |
| 数值 | `value.toLocaleString({maximumFractionDigits:2}){unit}`；`value===null` → `材料不足` `#8a94a6` |
| 期间 | `period`，`#526079` |
| 口径 | `.amt-scope`：`合并` / `单体` |
| 计算链路 | `.amt-link` + `Calculator 14` → 进入详情视图 |

- **最新态逻辑（视觉反馈）**：每个 `(subject_id, scope, metric_key)` 取 `period` 最大的一条；工具栏下方常驻 `small`：`最新期间：{maxPeriod}`，并在每行期间列回显，便于核对。
- **期间排序规则（已实测确认，必须按此实现）**：`period` 为**纯年度字符串或空串**——`financial_metrics` 实测去重 = `['', '2003', '2023', '2024', '2025', '2026', '2087']`；`financial_tie_outs` = `['2003','2023','2024','2025','2026']`。**不存在 `2024-12` / `2024年` 等混合格式**。因此：
  1. 排序键 = `Number(period)` 降序，取第一个；
  2. **空串 `''` 必须恒定排到最后**（`''` 转数值为 0/NaN，若直接降序会排到最前，重现"选了最旧/无效期间"的老 bug）；
  3. 全部记录 `period` 均为空串时，"最新"态视为无有效期间：数值列显示 `材料不足`，期间列显示 `—`，`最新期间` 提示改为 `暂无有效期间`；
  4. `App.tsx:525` 修正后不得再出现 `[...metrics].reverse()`。
- **无 12 项截断**：抽屉渲染后端返回的**全部**指标（最新态按维度去重后仍全量展示，可滚动）。
- 移动端（≤600）：列塌缩为卡片式两行（见 §7）。

**计算链路详情视图 `.analysis-trace-view`**（抽屉内视图，非模态）
```css
.analysis-trace-view { display:grid; }   /* 与列表互斥渲染，不叠加 */
.analysis-trace-back { min-height:40px; padding:0 14px; display:flex; align-items:center; gap:7px;
  border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:1; }
.analysis-trace-result { margin:12px 14px; padding:12px; display:grid;
  grid-template-columns:auto minmax(0,1fr); gap:4px 16px; border-radius:6px;
  border-left:2px solid var(--primary); background:#f7f9ff; }   /* 248 既有 */
.analysis-trace-result strong { grid-row:1/3; align-self:center; font-size:var(--font-metric-sm); color:var(--primary); }
.analysis-trace-result span { font-size:var(--font-body); color:var(--ink); }
.analysis-trace-result small { font-size:var(--font-caption); color:var(--muted); }
.analysis-trace-inputs { margin:0 14px 16px; border:1px solid var(--line); border-radius:6px;
  overflow:hidden; }
.ati-row { min-height:58px; padding:10px 12px; display:grid; gap:6px; }
.ati-row + .ati-row { border-top:1px solid var(--line); }
.ati-row > div:first-child { display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
.ati-row strong { font-size:var(--font-label); color:var(--ink); }
.ati-row span { font-size:var(--font-label); color:var(--ink); }
.ati-row small { color:var(--muted); font-size:var(--font-caption); }
```
- 返回行：`.text-button` + `ArrowLeft 15` + `返回指标列表`（→ 回到列表态）；`position:sticky top:0` 保证长列表中始终可返回。
- 结果带：`strong` = 数值+单位（null → `材料不足`）；`span` = `formula`；`small` = `{period} · {口径} · v{policy_version}`。
- 输入事实列表：每行 `canonical_fact.name` + 值/单位、`assertion.extraction_method`、`sources` 证据按钮（`.evidence-links` 样式，`FileText 13`，`onClick → openCitation`）。
- **返回列表不丢状态**：返回时保留滚动位置、`最新/全部期间` 选择、筛选。

---

## 3. 状态配色表

### 3.1 11 节点步骤条（5+ 状态）

| 状态 | 语义 | 图标形状 | 图标圆 色/边框/底 | 名称文字色 | 与相邻状态的区分手段 |
|---|---|---|---|---|---|
| `completed` | 节点已产出结果 | `Check` 描边勾 | `#fff` / `var(--low)` / `var(--low)` | `#4b5563` | 实心绿 + 勾（**弱化绿**：仅图标着色，名称仍中性） |
| `running`（当前） | 正在执行 | `LoaderCircle` 旋转 | `#fff` / `var(--primary)` / `var(--primary)` | `#4b5563` 600 + 蓝色 2px 下划线 | 唯一带**动效**的节点 |
| `warning` | 产出但需关注 | `AlertTriangle` 实心三角 | `var(--medium)` / `var(--medium)` / `var(--medium-bg)` | `var(--medium)` | 实心三角 |
| `degraded` | 降级产出（继续执行） | `MinusCircle` 空心减号 | `#526079` / `#cfd6e4` / `#f7f9fc` | `#526079` | 空心减号 + 圆底虚线下边 |
| `failed` | 执行失败 | `X` | `#fff` / `var(--high)` / `var(--high)` | `var(--high)` | 实心红 + 叉 |
| `pending` | 尚未执行 | 序号数字 | `#8a94a6` / `#cfd6e4` / `#fff` | `#8a94a6` | 唯一显示数字、无图标 |

### 3.2 分区结果行 / 分区计数（三态）

| 状态 | 文字色 | 底色 | 图标 | 文案 | 说明 |
|---|---|---|---|---|---|
| `passed` | `var(--low)` | `var(--low-bg)` | `CheckCircle2` 13 | `通过` | 绿 |
| `warning` | `var(--medium)` | `var(--medium-bg)` | `AlertTriangle` 13 | `异常` | 琥珀；与「材料不足」明确区分 |
| `insufficient` | `#667085`（297） | `#f1f3f6`（297） | `PackageSearch` 13 | `材料不足` | 中性灰蓝，**不是**失败 |

> **不用左边框条纹**表达状态（避免 AI 味的 `border-left` 彩条）。状态一律由「图标形状 + 文字标签 + 底色」三重表达，颜色只是第三重。

### 3.3 「运行完成 ≠ 校验通过」的视觉手段（强制性，逐条实现）

1. **两个 chip 并列，语义分离**：`.analysis-run-status`（仅表达节点执行）与 `.analysis-run-verdict`（专表达校验结论）在状态栏内**并排等权**。运行 chip 用绿/中性，校验 chip 异常时用琥珀 —— 视线自然落在琥珀上。
2. **完成且有异常 → 强制琥珀结论带**：`.analysis-run-result.is-warning`（`#fff8e8`/`#765414`）内写明文案"运行已完成，但校验存在 N 项异常……运行完成不等于校验通过"。绿色结论带**仅在零异常时**出现。
3. **色彩语境隔离**：步骤条的绿只表示"节点已产出"（名称仍为中性灰，弱化绿）；核验结果行用独立的琥珀/灰状态 chip，绝不复用步骤条的绿。
4. **财务汇总行去结论化**：只给种类数/有效数/期间，附 `.afs-note`「异常数量见下方核验分区」，不出任何通过/异常字样与颜色。
5. **0 值弱化**：分区计数中为 0 的「异常」chip 转为灰（`.is-muted`），避免"0 异常"被当成高亮通过。
6. **三态均含文字标签**：颜色之外必有中文标签（通过/异常/材料不足），色盲可用。

---

## 4. 交互状态机

### 4.1 步骤条：选中与自动定位的优先级

状态：`selectedNodeKey: string | null`、`manualNodeSelection: boolean`（默认 `false`）、`autoLocateEnabled`。

| 触发 | 行为 |
|---|---|
| 首次渲染 / 切换项目 / 切换 run | `selectedNodeKey=null`，`manualNodeSelection=false`，`autoLocateEnabled=true`；步骤条不显示详情面板 |
| run 更新（进度/current_node 变化）且 `manualNodeSelection===false` | 自动把当前节点**滚动到步骤条可视区**（改 `strip.scrollLeft`，不用 `scrollIntoView`，避免连带滚动页面/受 `scroll-behavior:smooth` 影响）；**不自动打开详情面板**（保首屏高度） |
| 用户点击某节点 | `selectedNodeKey=该节点`，`manualNodeSelection=true`；在步骤条下方展示 `.analysis-node-detail`；同时把该节点滚入可视区 |
| `manualNodeSelection===true` 后再有 run 更新 | **不抢焦点、不改选中**：仅更新该节点内的状态/摘要文案；不滚动步骤条 |
| 再次点击已选中节点 | 取消选中（`selectedNodeKey=null`），详情面板收起；`manualNodeSelection` 保持 `true`（不恢复自动跟随，直到重置） |
| 点击节点时该节点为 `pending` | 允许选中，详情面板显示「等待执行」；不报错 |

**优先级规则一句话**：**用户主动选择 > 自动跟随；自动跟随只在用户从未手动选择时生效。**

### 4.2 抽屉：打开 / 关闭 / 返回 / 焦点

| 动作 | 行为 |
|---|---|
| 打开（点「查看全部指标」/ 结果行等触发） | 记录 `document.activeElement` 为 `lastTrigger`；挂载遮罩 + 抽屉，`requestAnimationFrame` 后焦点移到**关闭按钮** |
| 焦点陷阱 | 抽屉开启期间 `Tab/Shift+Tab` 在抽屉内可聚焦元素间循环；`document.body` 加 `overflow:hidden` 防背景滚动（等价 EvidenceDrawer 现状） |
| 关闭：关闭按钮 | `close()` → 复位 `overflow`；焦点**恢复到 `lastTrigger`** |
| 关闭：遮罩点击 | 同关闭按钮路径 |
| 关闭：`Escape` | 若处于详情视图 → **先返回列表**；若处于列表视图 → 关闭抽屉（两级返回，符合"可预测返回"） |
| 详情视图进入 | 点结果行「计算链路」→ 视图切到 `.analysis-trace-view`；焦点移到 `.analysis-trace-back` 的返回按钮；记录进入前滚动位置 |
| 返回列表 | 焦点恢复到刚才那行的「计算链路」按钮；滚动位置复原 |
| 抽屉内打开证据 | 调 `openCitation`；证据抽屉（z 40）覆盖指标抽屉（z 38）；焦点移入证据抽屉；证据关闭后焦点**恢复到指标抽屉内的证据触发按钮** |
| 加载失败 | 列表/详情各自的错误态提供「重试」按钮（见 §5） |

### 4.3 分区折叠：「首次自动 vs 后续保留用户选择」

每分区维护 `{ expanded: boolean, filter: "warning"|"insufficient"|"passed"|"all", userTouched: boolean }`。

| 场景 | 规则 |
|---|---|
| **首次数据到达**（该 `projectId + run.id` 组合第一次拿到数据） | 若 `warning 数 > 0` → `expanded=true`、`filter="warning"`；否则 `expanded=false`、`filter="all"` |
| 用户手动折叠/展开 或 切换筛选 | 置 `userTouched=true`；此后**任何数据刷新都不覆盖** |
| 后续数据刷新（`userTouched===false` 且异常数 > 0） | 保持展开 + `filter="warning"` |
| 后续数据刷新（`userTouched===false` 且异常数归 0） | 自动折叠（`expanded=false`） |
| 切换项目 / 切换 run | **全部分区重置**：`expanded`、`filter`、`userTouched` 全部回默认，再按"首次数据到达"规则重新判定 |

### 4.4 筛选与分页的状态重置时机

| 事件 | 分页 | 筛选 | 展开 |
|---|---|---|---|
| 切换筛选（异常↔材料不足↔通过↔全部） | `page=1` | 应用新值，`userTouched=true` | 保持展开 |
| 改变每页条数（10↔50） | `page=1` | 不变 | 不变 |
| 翻页 | 更新 page | 不变 | 不变 |
| 展开/折叠分区 | 保持 page（若折叠隐藏列表，重新展开时 page 保持之前值） | 不变 | 切换 |
| 切换项目 / 切换 run | 两分区 `page=1`、`pageSize=10` | 重置为默认 | 重置为默认 |
| 关闭抽屉 | 抽屉内部状态（最新/全部期间、滚动、视图层级）重置 | — | — |

---

## 5. 三态设计（空 / 载 / 错）

统一前缀类：`.analysis-empty`、`.analysis-skeleton`、`.analysis-error`。

```css
.analysis-empty { min-height:62px; padding:12px 14px; display:flex; align-items:center; gap:9px;
  color:#687386; font-size:var(--font-caption); }
.analysis-empty svg { flex:0 0 auto; color:#8a94a6; }
.analysis-empty strong { display:block; color:var(--ink); font-size:var(--font-body); }
.analysis-empty button { margin-left:auto; }
.analysis-error { min-height:56px; padding:11px 14px; display:flex; align-items:center; gap:9px;
  color:#8d302a; background:#fff1ef; border-top:1px solid #e2aaa5; font-size:var(--font-caption); }
.analysis-error button { margin-left:auto; }
.analysis-skeleton { padding:8px 14px; display:grid; gap:8px; }
.analysis-skeleton i { height:14px; border-radius:4px; display:block;
  background:linear-gradient(90deg,#eef1f6 25%,#f7f9fc 37%,#eef1f6 63%);
  background-size:400% 100%; animation:analysis-shimmer 1.2s ease-in-out infinite; }
@keyframes analysis-shimmer { 0%{background-position:100% 50%} 100%{background-position:0 50%} }
@media (prefers-reduced-motion: reduce) { .analysis-skeleton i { animation:none; } }
```

| 区块 | 空数据 | 加载中 | 加载失败（可重试） |
|---|---|---|---|
| **L1 运行重点** | `SearchCheck 22` + 「尚未运行 Agent」+ 「核心材料齐备后启动分析，系统将依次完成事实、规则、风险和报告节点」（复用 `.empty-run` 文案） | 状态栏 + 进度行显示骨架（`analysis-skeleton` 3 行），步骤条 11 个 pending 节点 | `.analysis-error`：`AlertTriangle 16` + 「运行状态加载失败：{msg}」+ `RotateCcw` 「重试」 |
| **L2 财务汇总** | `Table2 18` + 「尚无财务指标」+ 「完成指标计算节点后将在此汇总」 | 三个 `.afs-stat` 值位骨架 | `.analysis-error` 整行替换，带「重试」 |
| **L3 报表勾稽校验** | `.analysis-empty` + `GitCompareArrows 20` + 「材料尚不足以执行资产负债表勾稽」 | `.analysis-skeleton` 4 行 | `.analysis-error` + 「重试」 |
| **L3 核验规则** | `.analysis-empty` + `ShieldCheck 20` + 「尚未生成核验规则结果」 | `.analysis-skeleton` 4 行 | `.analysis-error` + 「重试」 |
| **筛选无结果** | `.analysis-empty` + `Filter 18` + 「当前筛选下没有{异常/材料不足/通过}的结果」+ `.text-button`「查看全部」→ 切 `filter="all"` | — | — |
| **结果行计数为 0** | 不显示该状态行；分区头对应计数 chip 转 `.is-muted` | — | — |
| **材料不足（数据本身）** | 属于结果状态之一，正常渲染 `.is-insufficient` 行；数值位显示「材料不足」而非 0 | — | — |
| **指标抽屉（列表）** | `ListFilter 20` + 「该运行尚未产出财务指标」 | `.analysis-skeleton` 6 行 | `.analysis-error` 「指标加载失败」+ 「重试」（重发列表请求） |
| **指标抽屉（计算链路）** | `Calculator 20` + 「该指标缺少可展示的输入事实」 | `.analysis-skeleton` 3 行 | `.analysis-error` 「计算链路加载失败」+ 「重试」（重发 `api.metricTrace`），保留「返回指标列表」 |

---

## 6. Token 映射表（前端据此自检硬编码）

| 新增 class | 用到的变量 / 既有值（附来源） | 字号变量 |
|---|---|---|
| `.analysis-runfocus` | `var(--line)` `var(--panel)` | — |
| `.analysis-run-bar` | `var(--line)` | — |
| `.analysis-run-status` / `.analysis-run-verdict` | `var(--low)` `var(--low-bg)` `var(--primary)` `var(--primary-light)` `var(--high)` `var(--high-bg)` `var(--medium)` `var(--medium-bg)`；灰态 `#8a94a6`(190) `#f1f3f6`(297) | `var(--font-caption)` |
| `.analysis-run-fact` | `var(--ink)` `#7b8494`(164) | `var(--font-label)` `var(--font-caption)` |
| `.analysis-run-detail` | `var(--line)` `#fbfcfe`(330) `#4b5563`(216) `#7b8494`(164) | `var(--font-label)` `var(--font-caption)` `var(--font-body)` |
| `.analysis-run-progress` | `var(--line)` `var(--ink)` `var(--primary)` `#e8ecf3`(267) `#687386`(334) `var(--low)` `var(--high)` | `var(--font-body)` `var(--font-label)` `var(--font-caption)` |
| `.analysis-run-error` | `#8d302a` `#fff1ef`(521) `var(--high)` | `var(--font-caption)` `var(--font-body)` |
| `.analysis-run-result` | `#765414` `#fff8e8` `#e1c68e`(520) / `var(--low)` `var(--low-bg)` `#b8e3ca`(244) | `var(--font-caption)` |
| `.analysis-node-strip` | `#dce2ee`(188) | — |
| `.analysis-node` | `#cfd6e4`(189) `#8a94a6`(190) `var(--panel)` `#4b5563`(216) `#f7f9fc`(252) `var(--primary-light)` `var(--primary)` | `var(--font-caption)` |
| `.node-completed/.node-running/.node-warning/.node-degraded/.node-failed/.node-pending` | `var(--low)` `var(--primary)` `var(--medium)` `var(--medium-bg)` `var(--high)` `#526079`(125) `#cfd6e4`(189) `#f7f9fc`(252) `#8a94a6`(190) | — |
| `.analysis-node-detail` | `var(--line)` `#fbfcfe`(330) `var(--ink)` `#7b8494` `#4b5563` `var(--high)` | `var(--font-label)` `var(--font-caption)` `var(--font-body)` |
| `.analysis-finance-summary` | `var(--line)` `var(--panel)` `var(--primary)` `var(--ink)` `var(--muted)` `#8a94a6`(190) | `var(--font-body)` `var(--font-metric-sm)` `var(--font-caption)` |
| `.analysis-section` | `var(--line)` `var(--panel)` | — |
| `.analysis-section-head` | `#fbfcfe`(330) `#7b8494`(164) `var(--muted)` `var(--primary)` | `var(--font-section)` `var(--font-caption)` |
| `.asc-chip` | `#526079`(125) `#eef1f6`(327) `var(--medium)` `var(--medium-bg)` `#667085`(297) `#f1f3f6`(297) `var(--low)` `var(--low-bg)` `#8a94a6`(190) `#f7f9fc`(252) | `var(--font-caption)` |
| `.analysis-section-body` | `var(--line)` | — |
| `.analysis-result-row` | `#edf0f5`(227) `#f7f9fc`(252) `var(--primary)` `var(--ink)` `#687386`(334) `#7b8494`(164) | `var(--font-label)` `var(--font-caption)` |
| `.arr-status` | 同 `.asc-chip` 三态 | `var(--font-caption)` |
| `.arr-detail` | `#f7f9fc`(252) `#edf0f5`(227) `var(--muted)` `var(--ink)` `var(--medium)` | `var(--font-caption)` `var(--font-label)` |
| `.arr-detail` 复用 | `.bar-group/.bar-row`(299-304) `.evidence-links`(305) `.policy-chip`(309) | 既有 |
| `.analysis-section-foot` | `var(--panel)` `var(--line)` | — |
| `.analysis-drawer-mask` | `rgba(15,23,42,.32)`（沿用项目语义） | — |
| `.analysis-metrics-drawer` | `#d9dfeb`(490) `var(--panel)` `rgba(15,23,42,.12)`(490) | — |
| `.drawer-head`（复用） | `var(--line)` `var(--muted)` | `var(--font-caption)` `var(--font-section)` |
| `.analysis-drawer-toolbar` | `var(--line)` `var(--muted)` | `var(--font-caption)` |
| `.amt-head` | `#697386`(252) `#f7f9fc`(252) `var(--line)` | `var(--font-caption)` |
| `.amt-row` | `#edf0f5`(227) `var(--ink)` `#8a94a6`(190) `#526079`(125) `#eef2fa`(261) | `var(--font-label)` `var(--font-caption)` |
| `.amt-link` | `var(--primary)` | `var(--font-caption)` |
| `.analysis-trace-back` | `var(--line)` `var(--primary)` | `var(--font-caption)` |
| `.analysis-trace-result` | `var(--primary)` `#f7f9ff`(248) `var(--ink)` `var(--muted)` | `var(--font-metric-sm)` `var(--font-body)` `var(--font-caption)` |
| `.analysis-trace-inputs` / `.ati-row` | `var(--line)` `var(--ink)` `var(--muted)` | `var(--font-label)` `var(--font-caption)` |
| `.analysis-empty` | `#687386`(334) `#8a94a6`(190) `var(--ink)` | `var(--font-body)` `var(--font-caption)` |
| `.analysis-error` | `#8d302a` `#fff1ef` `#e2aaa5`(521) | `var(--font-caption)` |
| `.analysis-skeleton i` | `#eef1f6`(327) `#f7f9fc`(252) | — |

**自检**：上表不含任何新增 hex；灰阶全部标注复用行号；字号全部为 `var(--font-*)`；无 8-11px。

---

## 7. 响应式

| 档位 | 布局 | 步骤条 | 结果区 / 分页 | 抽屉 |
|---|---|---|---|---|
| **≥1441** | 三栏纵向；内容 `max-width:1440`；`.analysis-section-body` `max-height:300px` | 单行 11 节点，不滚动（1144px 可容 11×84+20=944） | 结果行三列；分页 sticky | `min(760px,94vw)` |
| **1050–1440** | 同上；`.heading-actions` 从 1050 起转纵向（`styles.css:644`，本页 `.page-heading` 右侧是 `.policy-chip`，需加 `flex-wrap:wrap`，`gap:8px`）；`.analysis-finance-summary` 允许换行（`flex-wrap:wrap; gap:12px 20px`）；`.analysis-section-body` `max-height:264px` | 宽度不足时**仅步骤条内部横向滚动**（`overflow-x:auto`），页面不横滚 | 结果行三列保持；`.arr-main small` 单行省略 | 同 |
| **≤1050**（含 780） | `.analysis-section-head` `flex-wrap:wrap`，计数 chips 换到第二行（`gap:6px`）；`.analysis-finance-summary` 保持换行 | 步骤条横向滚动；`autoLocate` 改 `strip.scrollLeft` | 结果行三列保持；`.analysis-section-body` `max-height:360px`（窄屏首屏验收让位于可用性） | `min(760px,94vw)` |
| **≤780** | 侧栏转抽屉（`styles.css:653`）；`.main-area` 去左边距；`.view-content` `padding:22px 16px 38px` | 步骤条横向滚动；节点 `min-width:76px` | 结果行三列；分页允许换行（`.pagination` 自带 `flex-wrap`，`styles.css:1149`） | `min(760px,96vw)` |
| **≤600** | 移动端；`.analysis-run-bar` `flex-wrap:wrap; padding-block:6px`，事实组换行；进度行「消息」隐藏（`.analysis-run-msg{display:none}`），仅保留节点名+百分比 | **步骤条内部横向滚动是唯一横向滚动**；`overflow-x:auto; -webkit-overflow-scrolling:touch`；页面 `overflow-x:hidden` 兜底 | 结果行改**两行卡片**：`grid-template-columns:auto minmax(0,1fr)`，`.arr-meta` 换到第二行 `grid-column:2`；`.arr-detail dl` 单列；分页条 `flex-direction:column; align-items:stretch` | **全宽抽屉**：`width:100vw`；`border-left:0`；`.analysis-metrics-drawer{transform:translateX(100%)}` 过渡保持不变；表格塌缩为列表（见下） |

**≤600 指标表塌缩**：
```css
@media (max-width:600px){
  .amt-head { display:none; }
  .amt-row { grid-template-columns:minmax(0,1fr) auto; grid-auto-rows:min-content; row-gap:4px;
    min-height:64px; }
  .amt-row > :nth-child(1) { grid-column:1; }      /* 指标名 */
  .amt-row > :nth-child(2) { grid-column:2; text-align:right; }  /* 数值 */
  .amt-row > :nth-child(3), .amt-row > :nth-child(4) { grid-column:1; font-size:var(--font-caption); }
  .amt-row > :nth-child(5) { grid-column:2; justify-self:end; }  /* 计算链路 */
}
```

**滚动行为总原则**：页面（`html/body`）**永不横向滚动**；步骤条与指标表若超宽，只在**自身容器内**横向滚动（`overscroll-behavior-x:contain`）。

---

## 8. 五源对齐自查表（供 Phase 3/4）

| # | 源 | 检查项 | 通过标准 |
|---|---|---|---|
| 1 | **设计变量**（本文档 §6） | 每个新增 class 的颜色是否全部来自 `var(--*)` 或标注行号的既有值 | 无新增 hex；浏览器 DevTools 计算样式无"裸色值" |
| 2 | **设计元数据**（本文档 §1-§5 的数值） | 高度/内距/gap/圆角/字号是否与文档逐项一致 | 关键值：L1 190 / L2 64 / 分区头 44 / 结果行 52 / body 264 / 步骤条 44 |
| 3 | **参考截图**（§9 线框图 + 1440×900 首屏） | 首屏是否同时可见：运行重点、财务汇总、**两个**核验分区标题 | 1440×900 截图内 `报表勾稽校验` 与 `核验规则` 两个标题均在视口内，且 y≤900 |
| 4 | **将生成的代码**（Phase 3） | class 前缀是否均为 `analysis-`；是否复用 `Pagination` / `EvidenceDrawer` 范式；`[...metrics].reverse()` 是否已按期间取最新并取消 12 项截断 | grep 无其他前缀；无 emoji；无 `<div className="trace-modal">` |
| 5 | **渲染截图**（Phase 4） | 五态（completed/running/warning/degraded/failed）+ 三态结果行是否与 §3 逐格一致；空/载/错三态是否齐全；键盘 Tab 与 Escape 是否可用 | 逐行对照 §3 表格；Tab 可达所有交互元素；Escape 两级返回；focus-visible 可见 |

**额外反 AI 味自查**（逐条打勾）：
- [ ] 无 emoji 作功能图标（正则 `[\x{1F300}-\x{1F9FF}\x{2600}-\x{26FF}\x{2700}-\x{27BF}]` 扫描为零）
- [ ] 无紫→粉渐变（本页只有 `--primary #2f54d4` 单色与 `#f7f9ff` 浅底）
- [ ] 状态表达不依赖"彩色左边框条纹"（改用 chip + 图标 + 文字）
- [ ] 无 `Lorem ipsum` / `Welcome to` / 空洞占位（所有空态文案均为具体操作指引）
- [ ] 卡片圆角 ≤7px（无 ≥24px 过度圆滑）
- [ ] 无"幽灵卡片"（1px 边框 + blur≥16 阴影并存）—— 本页阴影仅在抽屉
- [ ] 数字为有机值（沿用后端真实数值，不做"10,000+"式编造）

---

## 9. 1440 线框图（ASCII 参考截图）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ [topbar 56]  财务与交叉核验                                     [v1 · 12 条规则]│
├───────────┬──────────────────────────────────────────────────────────────────┤
│ sidebar   │  财务与交叉核验                                [ShieldCheck v1·规则] │ ← page-heading
│  248px    ├──────────────────────────────────────────────────────────────────┤
│           │ ┌──────────────────────────────────────────────────────────────┐ │
│           │ │ [V 已完成] [! 校验 3 项异常 · 1 项材料不足]  耗时 12.4秒 ¥0.21 1,234│ │ ← L1 bar 44
│           │ │                                                        [运行详情▾]│ │
│           │ ├──────────────────────────────────────────────────────────────┤ │
│           │ │ ◐ 当前节点：交叉核验（5/11）  ▓▓▓▓▓▓▓░░░░ 45%  正在比对左右值   │ │ ← progress 40
│           │ ├──────────────────────────────────────────────────────────────┤ │
│           │ │ ! 运行已完成，但校验存在 3 项异常、1 项材料不足 —— 运行完成不等于校验通过，请查看下方两个核验分区 │ │ ← result 42
│           │ ├──────────────────────────────────────────────────────────────┤ │
│           │ │ 1 2 3 4 5 6 7 8 9 10 11   │  ← 步骤条 44（对勾 / 圆环 / 菱形 / 叉 图标 + 名称）
│           │ └──────────────────────────────────────────────────────────────┘ │
│           │ ┌──────────────────────────────────────────────────────────────┐ │
│           │ │ ▤ 财务指标   指标种类 18 类 │ 有效计算 16/18 │ 期间 2022–2024  [查看全部指标]│ │ ← L2 64
│           │ └──────────────────────────────────────────────────────────────┘ │
│           │ ┌──────────────────────────────────────────────────────────────┐ │
│           │ │ ▸ ⧉ 报表勾稽校验  同一主体…   共6 异常3 材料不足1 通过2    [异常3|材料不足|通过|全部]│ │ ← 分区头 44
│           │ ├──────────────────────────────────────────────────────────────┤ │
│           │ │ [!异常]  资产=负债+所有者权益   2024·合并  左100 右98 差2>容差0.5 │ │ ← 结果行 52
│           │ │ [!异常]  利润表勾稽…                                              │ │
│           │ │ [▪材料不足] …                                                     │ │
│           │ ├──────────────────────────────────────────────────────────────┤ │
│           │ │       [每页10▾]              第 1/1 页 · 共 3 条  ↑ ↓           │ │ ← foot 44
│           │ └──────────────────────────────────────────────────────────────┘ │
│           │ ┌──────────────────────────────────────────────────────────────┐ │
│           │ │ > 核验规则  确定性内核…    共6 异常1 材料不足0 通过5           │ │ ← L3b 分区头 44
│           │ └──────────────────────────────────────────────────────────────┘ │
└───────────┴──────────────────────────────────────────────────────────────────┘
（图例说明「预警=需人工关注」置于两分区之间）
```

---

## 10. 实现提示（给前端 Agent）

1. **只动 `Checks` 组件与其后的样式段**：在 `styles.css` 末尾（`/Analysis and calculation workspace/` 段之后）新增 `/* Analysis page v2 -- */` 区块，全部 class 带 `analysis-` 前缀。不要改 `.run-summary` / `.financial-strip` 等共享类（若确认无他处引用，可保留原类但不再渲染）。
2. **修正 `App.tsx:525`**：`[...metrics].reverse()` 改为按 `(subject_id, scope, metric_key)` 取 `Number(period)` 最大者，**空串 `''` 恒定排到最后**（详见 §2.D 期间排序规则）；**移除 `.slice(0,12)`**。
3. **图标（已确认）**：`lucide-react@1.28.0`，本规格全部图标名实测存在，直接使用，无需降级替换。
4. **计算链路详情迁入抽屉**：删除 `App.tsx:534` 的 `.modal-backdrop` + `.trace-modal` 渲染，改为抽屉内 `.analysis-trace-view` 视图（保留 `loadTrace` 请求与 loading 状态）。
5. **步骤条滚动**：用 `strip.scrollLeft = node.offsetLeft - strip.clientWidth/2 + node.offsetWidth/2`，**不要**用 `scrollIntoView`（会连带滚页面）。
6. **`prefers-reduced-motion`**：抽屉过渡、`.spin`、骨架 shimmer 均需在该媒体查询下关闭（沿用 `styles.css:514` 的做法）。
7. **焦点陷阱**：以一个 `keydown` 监听实现 Tab 循环；关闭时 `lastTrigger.focus()`。与 EvidenceDrawer 保持同一套交互手感。
8. **不要新增 `:root` 变量**；如确实需要语义变量，请在团队评审后统一追加，并更新本文件 §6。

---

# 11. v2.1 修订（四阶段工作流 + 摘要卡片 + 详情弹窗）

> 修订日期：2026-10-13 ｜ 触发：产品侧确认「11 节点平铺 → 四阶段」「页内折叠区 → 摘要卡片 + 详情弹窗」
> 本节的优先级**高于**其覆盖的旧章节；旧章节保留作设计演进记录。

## 11.1 取代范围

| 被取代的旧内容 | 现行为 |
|---|---|
| §1 L1「11 节点紧凑步骤条」 | 四阶段工作流（材料 / 核验 / 风险 / 报告），见 §11.2 |
| §1 L3「两个可折叠核验分区」 | 两张摘要卡片 + 详情弹窗，见 §11.3 / §11.4 |
| §2.A.6 节点步骤条规格 | §11.2 |
| §2.C 核验分区规格 | §11.3 / §11.4 |
| §3.1 步骤条 6 态配色 | §11.5 |
| §4.3「分区首次自动展开 / 保留用户选择」 | §11.6（改为阶段展开 + 弹窗默认筛选） |
| §7 响应式中 `.analysis-node*` / `.analysis-section-*` | §11.7 |
| §9 线框图 | §11.8 |

## 11.2 四阶段工作流

**分组（固定顺序，不随运行状态变化）**

| 阶段 | 节点 |
|---|---|
| 材料 | 材料盘点 / 事实抽取 / 冲突识别 |
| 核验 | 指标计算 / 交叉核验 |
| 风险 | 定性风险 / 复核任务 / 正式风险 |
| 报告 | 报告组装 / 引用校验 / 人工复核 |

**布局**：`.analysis-stages` 为 grid，桌面 `repeat(4, minmax(0,1fr))`、gap 10、padding 12 14；≤780px 改两列。

**阶段卡** `.analysis-stage`：`min-height:62px; padding:9px 11px; gap:10px;` 边框 `1px var(--line)`、圆角 6、背景 `var(--panel)`。
- `.analysis-stage-icon` 26×26 圆形；`.analysis-stage-name` 正文 14px / 600 + 14px 角色图标；`.analysis-stage-state` 12px 状态文案；`.analysis-stage-count` 12px「已执行/总数」等宽数字；`.analysis-stage-chevron` 15px，展开时旋转 180°。

**阶段状态聚合**（优先级从高到低）：`failed > running > warning > degraded > completed > pending`。
**关键**：`已完成数 = completed + warning + degraded`（都执行完了），但只要还有 pending 就停在 `pending` —— **部分完成绝不冒充 completed**，否则"未完成"会被一个绿色的阶段盖掉。

**点击展开** `.analysis-stage-panel`：`padding:4px 14px 14px`；标题行带上分隔线（阶段名 14px + 说明 12px）；节点列表 `.analysis-stage-node` 每条 `padding:9px 11px` + 边框圆角 5 + 背景 `#fbfcfe`；行内为「图标 + 名称 14px + 状态 chip + 第 N 步（右对齐 12px）+ 摘要 12px/1.6」。

**「人工复核」节点语义（必须渲染）**：该节点行必须出现
「该节点完成仅表示系统已交付待人工复核，不代表人工事项已经处理。」
（`.analysis-stage-node-note`，13px 图标 + 12px 文案）——后端把它标成 completed 只代表交付，不代表人工处理完。

## 11.3 摘要卡片

- 容器 `.analysis-cards`：`grid-template-columns: repeat(2, minmax(0,1fr)); gap:14px`；≤780px 单列。
- `.analysis-card`：flex 纵向，边框 `1px var(--line)`、圆角 7、背景 `var(--panel)`。
- 头 `.analysis-card-head`：`min-height:52px; padding:10px 14px;` 背景 `#fbfcfe` + 下分隔线；18px 主色图标 + 标题 14px + 描述 12px `var(--muted)` + 右侧 `.analysis-card-total`（`共 N`）。
- 统计 `.analysis-card-stats`：`padding:12px 14px; gap:10px;`；三个 `.acs-chip`（`min-height:26px; padding:0 10px; gap:6px`）：标签 12px + 数值 14px 粗；**0 值转灰 `.is-muted`**（弱化但不隐藏）。
- 重点结果 `.analysis-card-highlights`：`padding:0 14px 12px; gap:8px;` **最多两条，先异常、后材料不足**；`.ach-row` `padding:9px 11px`、左边框 3px（异常 `var(--medium)` / 材料不足 `#cfd6e4` / 通过 `var(--low)`）。
- 底部 `.analysis-card-foot`：`padding:10px 14px` 右对齐 + 上分隔线，**明确的「详情」按钮**。
- **页内折叠结果区已取消**：卡片内不再渲染结果列表。

## 11.4 详情弹窗

- 遮罩 `.analysis-modal-mask`：`position:fixed; inset:0; z-index:35;` 背景 `rgba(15,23,42,.28)` —— 比旧 `.modal-backdrop`(.52) 更浅，保证**原页面仍清晰可辨**；点击关闭。
- 弹窗 `.analysis-modal`：fixed 居中（`translate(-50%,-50%)`），`width:min(880px,100%)`、`max-height:80vh`、**`z-index:36`（低于证据抽屉 z 40 / 指标抽屉 z 38）**，flex 纵向、圆角 8。
- 头 `.analysis-modal-head` `min-height:56px; padding:10px 14px`：标题 16px + 描述 12px + `共 N` chip + 关闭按钮。
- 工具条 `.analysis-modal-toolbar` `padding:10px 14px` 背景 `#fbfcfe`：四档筛选（异常 / 材料不足 / 通过 / 全部，带计数）。
- 列表 `.analysis-modal-body`：`flex:1; overflow-y:auto; overscroll-behavior:contain;`；行 `.analysis-modal-row` 下分隔线 `#edf0f5`；行头 `.analysis-modal-row-head` `min-height:52px; padding:8px 14px;` 三列网格（状态 chip / 标题+摘要 / 元信息+箭头）。
- 行内展开 `.arr-detail`（沿用既有规格）：**勾稽** 左值 / 右值 / 差异 / 容差 + 公式 + 政策版本 + 证据入口；**核验** 数值对比条 + 公式 / 阈值 / 政策版本 + 证据入口。
- 页脚 `.analysis-modal-foot`：上分隔线 + 既有 `Pagination`（默认 10，可切 50）。

## 11.5 状态配色（取代 §3.1）

| 阶段状态 | 图标底 | 图标色 | **名称色** | 状态文案色 | 形状 |
|---|---|---|---|---|---|
| completed | `var(--low-bg)` `#edf9f1` | `var(--low)` | `var(--ink)` 正常深色 | `#5f7d6c` | 勾 |
| running | `var(--primary)` | `#fff` | `var(--primary)` | `var(--primary)` | 旋转环 + 卡片主色描边与内阴影 |
| warning | `var(--medium-bg)` | `var(--medium)` | `var(--ink)` | `var(--medium)` | 三角 |
| degraded | `#f7f9fc` | `#526079` | `var(--ink)` | `#526079` | 空心减号 + 圆底虚线下边 |
| failed | `var(--high)` | `#fff` | `var(--ink)` | `var(--high)` | 叉 |
| pending | `var(--panel)` | `#8a94a6` | `var(--ink)` | `#7b8494` | 虚线圆环 |

**两条硬规则**：
1. **完成阶段弱化为「浅绿图标 + 正常深色文字」**，名称一律 `var(--ink)`（只有 running 用主色）——"完成"不得与"异常/失败"抢注意力。
2. 颜色只是第二重表达，形状（勾 / 环 / 三角 / 减号 / 叉 / 虚线环）必须能独立区分。

## 11.6 交互状态机（取代 §4.3）

- **阶段展开**：默认全部收起；用 `touched` 标志区分"首次自动"与"用户选择"——用户一旦点过，**运行进度推进不覆盖其选择**；切换项目或运行（`resetKey = projectId:runId` 变化）复位。一次只展开一个阶段。
  ⚠️ `Checks` 在切换项目时**不会重新挂载**（同一组件实例），所以阶段展开的复位必须靠 `resetKey` 显式触发，不能指望卸载。
- **弹窗筛选**：每次打开按统计决定默认值（异常数 > 0 → 「异常」，否则「全部」）；**打开期间保持不变**（含证据查看往返）；关闭后复位；切换项目/运行复位。
- **层级与键盘：只有最上层处理键盘与焦点**。Escape 顺序为「证据层 → 弹窗」：弹窗的 Escape 在 `evidenceOpen` 为真时直接放行。焦点链：打开移入弹窗关闭按钮 → 关闭归还卡片底部「详情」按钮；从弹窗内看证据后，焦点归还**弹窗内的证据触发按钮**。
- **背景锁定**：弹窗打开期间 `body.style.overflow = hidden`，关闭时还原（记 `previousOverflow`，不要硬置空）。

## 11.7 响应式（补充 §7）

| 断点 | 行为 |
|---|---|
| ≤1050px | 卡片头允许换行，`共 N` chip 靠右 |
| ≤780px | **四阶段两列**；**两张卡片上下排列** |
| ≤600px | 弹窗 `width:100%`、`max-height:86vh`；`.analysis-modal-filter` 隐藏、改用 `select`；行头两列（元信息落到第二行） |

## 11.8 线框图（取代 §9）

```
┌ 财务与交叉核验 ─────────────────────────────────── v1.0.0 · 19 条规则 ┐
│                                                                      │
│ 最近一次运行  ✓已完成  ⚠ 校验 1 项异常 · 31 项材料不足   耗时 成本 Token  运行详情▾ │   ← L1 一行
│ 当前节点：人工复核 · 报告阶段（11/11）  ████████████ 100%                │   ← L1 进度另一行
│ ⚠ 运行已完成，但校验存在异常 —— 运行完成不等于校验通过，请查看下方两张核验卡片。 │   ← 结论带（不重复计数）
│ ┌───────────┬───────────┬───────────┬───────────┐                    │
│ │ ✓ 材料 3/3│ ◐ 核验 1/2│ ✓ 风险 3/3│ ⚠ 报告 2/3│                    │   ← L1 四阶段（点击展开）
│ └───────────┴───────────┴───────────┴───────────┘                    │
│ ┌── 展开区（可选）───────────────────────────────┐                    │
│ │ 报告  报告组装、引用校验与人工复核交付            │                    │
│ │  ✓ 报告组装  已完成   第 1 步  完成 46 项…      │                    │
│ │  ⚠ 引用校验  警告     第 2 步  1 处引用未定位     │                    │
│ │  ✓ 人工复核  已完成   第 3 步  …               │                    │
│ │      ⓘ 该节点完成仅表示系统已交付待人工复核，      │                    │
│ │        不代表人工事项已经处理。                  │                    │
│ └───────────────────────────────────────────────┘                    │
│                                                                      │
│ 🌐 财务指标  种类 10 类  有效计算 10/10  期间 2023–2025  [查看全部指标]    │   ← L2 一行
│                                                                      │
│ ┌─ 报表勾稽校验 ────────── 共 24 ─┐ ┌─ 核验规则 ──────────── 共 19 ─┐     │   ← L3 两张卡片
│ │ ⚠异常 0  ▤材料不足 15  ✓通过 9  │ │ ⚠异常 1  ▤材料不足 16  ✓通过 2 │     │
│ │ ⚠ 资产负债表恒等式  2025·合并   │ │ ⚠ 债券担保材料完整性  口径待确认 │     │   ← 重点≤2，先异常
│ │ ▤ 现金流量表勾稽  2024·单体     │ │ ▤ 对外担保余额口径  材料不足     │     │
│ │                    [详情 →]    │ │                    [详情 →]    │     │
│ └────────────────────────────────┘ └────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘

[详情] 打开（遮罩 rgba(15,23,42,.28)，原页面仍清晰可辨；z 36）：
┌ 报表勾稽校验  共 24                                     [✕] ┐
│ 异常 9 | 材料不足 2 | 通过 1 | 全部 12                       │   ← 默认「异常」
│ ⚠ 资产负债表恒等式   2025 · 合并                    ▾      │
│   ├ 左值 100  右值 97  差异 3（超容差）  容差 1              │
│   ├ 公式：资产 = 负债 + 所有者权益                           │
│   └ v1.0.0                                     [查看证据]   │   ← 点证据 → 证据抽屉(z 40)盖在其上
│ ▤ 现金流量表勾稽     2024 · 单体                    ▸      │
│ …                                                          │
│                                    第 1 / 2 页 · 共 12 条    │   ← 复用 Pagination(10/50)
└────────────────────────────────────────────────────────────┘
```

## 11.9 Token 映射（补充 §6）

**新增 class → 取值来源**（全部为既有 `:root` 变量或 `styles.css` 既有字面值，零新增色值）：

- `.analysis-stages / .analysis-stages-wrap` → 仅布局（grid / `1px var(--line)`）
- `.analysis-stage*` → `var(--line)` `var(--panel)` `var(--ink)` `var(--primary)` `var(--low)` `var(--low-bg)` `var(--medium)` `var(--medium-bg)` `var(--high)` `#b8e3ca` `#cfd6e4` `#f7f9fc` `#fbfcfe` `#8a94a6` `#526079` `#5f7d6c` `#7b8494`
- `.analysis-card*` → `var(--line)` `var(--panel)` `var(--primary)` `var(--muted)`，chip 复用 `var(--medium)/var(--medium-bg)`、`var(--low)/var(--low-bg)`、`#667085/#f1f3f6`、`#8a94a6/#f7f9fc`、`#526079/#eef1f6`
- `.ach-row.is-*` → 左边框 `var(--medium)` / `#cfd6e4` / `var(--low)`
- `.analysis-modal*` → `rgba(15,23,42,.28)`（遮罩）、`#d8e0ed`（边框）、`#fbfcfe`、`#edf0f5`、`var(--line)`、`var(--panel)`

## 11.10 已删除的旧类（含本次修复的 bug 根因）

| 旧类 | 原因 |
|---|---|
| `.node-timeline` / `.node-step*` | 旧平铺步骤条，页面已无实例（死 CSS） |
| **`.node-completed > span` / `.node-running > span` / `.node-failed > span` / `.node-model_required > span`** | **本次修复的 bug 根因**：它们是**无作用域**的通用选择器，而新步骤条按钮里有两个直接 `<span>`（图标 + 名称），于是「名称」也被染上图标底色与白字。已整体删除 |
| `.analysis-node-strip` / `.analysis-node*` / `.analysis-node-detail*` | 旧 11 节点条 |
| `.analysis-section*` / `.asc-chip` / `.analysis-result-row` / `.analysis-section-body` / `.analysis-section-foot` | 旧页内折叠分区 |

保留：`.analysis-node-chip`（阶段展开区仍在用的状态 chip）。
