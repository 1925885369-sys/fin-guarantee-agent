import { useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, ArrowRight, Check, FileText, LoaderCircle, Search, X } from "lucide-react";
import { api, DuplicateDocumentError } from "../../api";
import { ErrorState, ReadinessBar, StatusBadge } from "../common";
import type { EntityProfileSummary, Health, ProjectCreatePayload } from "../../types";

type ProjectRef = { id: string; token?: string };
type CounterStatus = "exists" | "not_applicable" | "undetermined";
type UploadKey = "prospectus" | "ratingReport";
type UploadState = "pending" | "uploading" | "uploaded" | "parse_failed" | "failed";
const stepTitles = ["建项方式", "配置交易主体", "填写债券信息", "上传核心材料", "确认并创建"];

export function ProjectIntakeWizard({ profiles, profilesError, initialIssuerProfileId = "", limits, close, created }: { profiles: EntityProfileSummary[]; profilesError?: string; initialIssuerProfileId?: string; limits?: Health["uploadLimits"]; close: () => void; created: (project: ProjectRef) => void }) {
  const [step, setStep] = useState(0);
  const [profileMode, setProfileMode] = useState(Boolean(initialIssuerProfileId));
  const [search, setSearch] = useState("");
  const [issuerId, setIssuerId] = useState(initialIssuerProfileId);
  const [issuerName, setIssuerName] = useState("");
  const [guarantorName, setGuarantorName] = useState("");
  const [counterNamesText, setCounterNamesText] = useState("");
  const [counterStatus, setCounterStatus] = useState<CounterStatus>("undetermined");
  const [prospectus, setProspectus] = useState<File | null>(null);
  const [ratingReport, setRatingReport] = useState<File | null>(null);
  const [bond, setBond] = useState({ bond_name: "", amount: 0, term_months: 36, purpose: "", data_consent: false });
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [createdProject, setCreatedProject] = useState<ProjectRef | null>(null);
  const [uploadStates, setUploadStates] = useState<Record<UploadKey, UploadState>>({ prospectus: "pending", ratingReport: "pending" });
  const [parseWarnings, setParseWarnings] = useState<Partial<Record<UploadKey, string>>>({});
  const issuer = profiles.find((item) => item.id === issuerId);
  const directName = issuerName.trim();
  const counterNames = counterNamesText.split(/[\n,，、]/).map((item) => item.trim()).filter(Boolean);
  const filtered = useMemo(() => profiles.filter((item) => `${item.name}${item.unified_code}`.toLowerCase().includes(search.toLowerCase())), [profiles, search]);
  const issuerNameError = !profileMode && (directName.length < 2 || directName.length > 80) ? "发行人名称需为 2–80 个字符" : "";
  const amountError = !Number.isFinite(bond.amount) || bond.amount <= 0 ? "请输入大于 0 的有限数值，单位为万元" : "";
  const termError = !Number.isInteger(bond.term_months) || bond.term_months < 1 || bond.term_months > 120 ? "期限需为 1–120 个月的整数" : "";
  const purposeError = bond.purpose.trim().length < 2 || bond.purpose.length > 300 ? "募集资金用途需为 2–300 个字符" : "";
  const bondNameError = bond.bond_name.length > 160 ? "债券名称不能超过 160 个字符" : "";
  const guarantorError = guarantorName.trim().length > 120 ? "保证人名称不能超过 120 个字符" : "";
  const counterNamesError = counterNames.length > 20 ? "反担保方最多填写 20 个" : "";
  const fileLabel = (key: UploadKey) => key === "prospectus" ? "募集说明书" : "评级报告";
  const setFile = (key: UploadKey, file: File | null) => {
    if (createdProject && ["uploaded", "parse_failed"].includes(uploadStates[key])) return;
    if (key === "prospectus") setProspectus(file); else setRatingReport(file);
    setUploadStates((current) => ({ ...current, [key]: "pending" }));
    setParseWarnings((current) => ({ ...current, [key]: undefined }));
    setError("");
  };
  const validateFile = (key: UploadKey, file: File | null): string => {
    if (!file) return `${fileLabel(key)}尚未选择`;
    const formats = limits?.formats || [".pdf", ".docx", ".xlsx"];
    const suffix = `.${file.name.split(".").pop()?.toLowerCase() || ""}`;
    if (!formats.includes(suffix)) return `${fileLabel(key)}格式不支持：${suffix}`;
    if ((limits?.maxFiles ?? 8) < 2) return "当前环境每个项目允许的文件数不足以容纳两份核心材料";
    const maxBytes = (limits?.maxFileMb || 15) * 1024 * 1024;
    if (file.size > maxBytes) return `${fileLabel(key)}超过单文件 ${limits?.maxFileMb || 15} MB 限制`;
    return "";
  };
  const canNext = step === 0 ? (profileMode ? Boolean(issuer) : !issuerNameError)
    : step === 1 ? !guarantorError && !counterNamesError
    : step === 2 ? !amountError && !termError && !purposeError && !bondNameError && !guarantorError && !counterNamesError && bond.purpose.trim().length >= 2 && bond.data_consent
    : step === 3 ? Boolean(prospectus && ratingReport) && !validateFile("prospectus", prospectus) && !validateFile("ratingReport", ratingReport) : true;
  const dismiss = () => createdProject ? created(createdProject) : close();
  const uploadMaterial = async (project: ProjectRef, key: UploadKey, file: File, kind: "募集说明书" | "信用评级报告") => {
    setUploadStates((current) => ({ ...current, [key]: "uploading" }));
    try {
      const uploaded = await api.uploadOne(project.id, file, kind, project.token);
      if (["failed", "ocr_required"].includes(uploaded.parseStatus)) {
        const warning = uploaded.warning || `${fileLabel(key)}解析失败，请在材料页查看并处理`;
        setUploadStates((current) => ({ ...current, [key]: "parse_failed" }));
        setParseWarnings((current) => ({ ...current, [key]: warning }));
        return;
      }
      setUploadStates((current) => ({ ...current, [key]: "uploaded" }));
    } catch (reason) {
      if (reason instanceof DuplicateDocumentError) {
        try {
          await api.classifyDocument(project.id, reason.documentId, kind, project.token);
          setUploadStates((current) => ({ ...current, [key]: "uploaded" }));
          return;
        } catch (classificationError) {
          reason = classificationError;
        }
      }
      setUploadStates((current) => ({ ...current, [key]: "failed" }));
      const message = reason instanceof Error ? reason.message : "未知错误";
      throw new Error(`${fileLabel(key)}上传失败：${message}`);
    }
  };
  const submit = async () => {
    if (!canNext || (profileMode && !issuer)) return;
    const fileErrors = (["prospectus", "ratingReport"] as UploadKey[]).filter((key) => !createdProject || !["uploaded", "parse_failed"].includes(uploadStates[key])).map((key) => validateFile(key, key === "prospectus" ? prospectus : ratingReport)).filter(Boolean);
    if (fileErrors.length) { setError(fileErrors.join("；")); return; }
    setBusy(true); setError("");
    const payload: ProjectCreatePayload = { company_name: profileMode ? issuer!.name : directName, amount: bond.amount, term_months: bond.term_months, purpose: bond.purpose, bond_name: bond.bond_name, data_consent: bond.data_consent, project_type: "bond_guarantee", issuer_profile_id: profileMode ? issuer!.id : null, guarantor_name: guarantorName.trim(), counter_guarantor_names: counterStatus === "exists" ? counterNames : [], counter_guarantee_status: counterStatus, analysis_target: "full_due_diligence" };
    try {
      let project = createdProject;
      if (!project) {
        try {
          const result = await api.createProject(payload);
          project = { id: result.id, token: result.accessToken };
          setCreatedProject(project);
        } catch (reason) {
          throw new Error(`创建项目失败：${reason instanceof Error ? reason.message : "请求失败"}`);
        }
      }
      for (const [key, file, kind] of [
        ["prospectus", prospectus, "募集说明书"],
        ["ratingReport", ratingReport, "信用评级报告"],
      ] as const) {
        if (["uploaded", "parse_failed"].includes(uploadStates[key])) continue;
        await uploadMaterial(project, key, file!, kind);
      }
      created(project);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "上传材料失败"); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop intake-backdrop"><section className="project-intake" role="dialog" aria-modal="true" aria-labelledby="intake-title">
    <header className="intake-header"><div><span>新建债券担保项目</span><h2 id="intake-title">{stepTitles[step]}</h2></div><button className="icon-button" onClick={dismiss} aria-label="关闭建项向导"><X size={18} /></button></header>
    <ol className="intake-steps">{stepTitles.map((title, index) => <li className={index === step ? "active" : index < step ? "done" : ""} key={title}><span>{index < step ? <Check size={13} /> : index + 1}</span><strong>{title}</strong></li>)}</ol>
    <div className="intake-body">
      {step === 0 && <div className="intake-form"><div className="target-options"><label className={profileMode ? "selected" : ""}><input type="radio" checked={profileMode} onChange={() => setProfileMode(true)} /><span><strong>使用主体资料库</strong><small>冻结发行人主体事实，并与本次材料核对。</small></span></label><label className={!profileMode ? "selected" : ""}><input type="radio" checked={!profileMode} onChange={() => { setProfileMode(false); setIssuerId(""); }} /><span><strong>直接使用发行材料</strong><small>无需主体档案，上传募集说明书和评级报告后抽取发行人事实。</small></span></label></div>{profileMode ? <><div className="intake-search"><Search size={16} /><input aria-label="搜索发行人" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索企业名称或统一社会信用代码" /></div><div className="intake-profile-grid">{filtered.map((profile) => { const completeness = profile.completeness <= 1 ? profile.completeness * 100 : profile.completeness; return <button className={issuerId === profile.id ? "selected" : ""} onClick={() => setIssuerId(profile.id)} key={profile.id}><div><strong>{profile.name}</strong><span>{profile.unified_code || "统一社会信用代码待补充"}</span></div><StatusBadge tone={profile.status === "ready" ? "success" : profile.status === "conflicted" ? "danger" : "warning"}>{profile.status === "ready" ? "可用于建项" : profile.status === "stale" ? "资料待更新" : profile.status === "conflicted" ? "存在冲突" : "档案不完整"}</StatusBadge><ReadinessBar label="档案完整度" value={profile.completeness} detail={`基准日 ${profile.baseline_date || "待确认"}`} tone={completeness >= 80 ? "success" : "warning"} /></button>; })}</div>{!profiles.length && <ErrorState message={profilesError ? `主体档案功能当前不可用：${profilesError}` : "主体资料库暂无可选发行人，可切换为直接使用发行材料。"} />}</> : <label>发行人名称<input aria-invalid={Boolean(issuerNameError)} value={issuerName} onChange={(event) => setIssuerName(event.target.value)} placeholder="可填写材料中披露的发行人名称" />{issuerNameError && <small className="field-error" role="alert">{issuerNameError}</small>}<small className="form-hint">材料解析后，系统会以募集说明书和评级报告中的主体事实为准。</small></label>}</div>}
      {step === 1 && <div className="intake-form"><label>保证人（项目级信息，可暂缺）<input maxLength={120} aria-invalid={Boolean(guarantorError)} value={guarantorName} onChange={(event) => setGuarantorName(event.target.value)} placeholder="输入本次债券拟议保证人" />{guarantorError && <small className="field-error" role="alert">{guarantorError}</small>}</label><p className="form-hint">保证人不写入发行人主体档案，最终责任边界以本次保证合同为准。</p><fieldset><legend>反担保安排</legend><div className="choice-row">{[["exists", "存在"], ["not_applicable", "不适用"], ["undetermined", "暂未确定"]].map(([value, label]) => <label key={value}><input type="radio" name="counter-status" value={value} checked={counterStatus === value} onChange={() => { setCounterStatus(value as CounterStatus); if (value === "not_applicable") setCounterNamesText(""); }} />{label}</label>)}</div></fieldset>{counterStatus === "exists" && <label>反担保方（项目级，可多项）<textarea value={counterNamesText} onChange={(event) => setCounterNamesText(event.target.value)} placeholder="每行填写一个反担保方；暂未确定时可以留空" />{counterNamesError && <small className="field-error" role="alert">{counterNamesError}</small>}{!counterNames.length && <span className="form-hint warning"><AlertTriangle size={14} />尚未明确反担保方，将生成非阻塞核验提示。</span>}</label>}{counterStatus === "undetermined" && <p className="form-hint">暂未确定不会被系统解释为不存在，后续将生成非阻塞核验提示。</p>}</div>}
      {step === 2 && <div className="intake-form"><label>债券名称（可暂缺）<input aria-invalid={Boolean(bondNameError)} maxLength={160} value={bond.bond_name} onChange={(event) => setBond({ ...bond, bond_name: event.target.value })} placeholder="后续可从募集说明书抽取" />{bondNameError && <small className="field-error" role="alert">{bondNameError}</small>}</label><div className="form-grid"><label>预计本期规模（万元）<input aria-invalid={Boolean(amountError)} type="number" min={0.01} step="any" value={bond.amount || ""} onChange={(event) => setBond({ ...bond, amount: Number(event.target.value) })} /><small className="form-hint">单位为万元，不设金额上限；20 亿元填写 200000。</small>{amountError && <small className="field-error" role="alert">{amountError}</small>}</label><label>预计期限（月）<input aria-invalid={Boolean(termError)} type="number" min={1} max={120} step={1} value={bond.term_months} onChange={(event) => setBond({ ...bond, term_months: Number(event.target.value) })} />{termError && <small className="field-error" role="alert">{termError}</small>}</label></div><label>募集资金用途<textarea aria-invalid={Boolean(purposeError)} maxLength={300} value={bond.purpose} onChange={(event) => setBond({ ...bond, purpose: event.target.value })} placeholder="可填写预计用途，后续以募集说明书披露为准" />{purposeError && <small className="field-error" role="alert">{purposeError}</small>}</label><label className="consent"><input type="checkbox" checked={bond.data_consent} onChange={(event) => setBond({ ...bond, data_consent: event.target.checked })} /><span>我确认本项目材料符合内部数据处理要求，并知悉材料将发送至已配置的模型服务。</span></label></div>}
      {step === 3 && <div className="intake-form"><p className="form-hint">两份核心材料会在项目创建后立即上传并按选择的类型入库。项目不会自动启动 Agent。单文件限制：{limits?.maxFileMb || 15} MB。</p><label><span><FileText size={15} />募集说明书</span><input type="file" disabled={createdProject != null && ["uploaded", "parse_failed"].includes(uploadStates.prospectus)} accept={(limits?.formats || [".pdf", ".docx", ".xlsx"]).join(",")} onChange={(event) => setFile("prospectus", event.target.files?.[0] || null)} />{prospectus && <small>{prospectus.name} · {uploadStates.prospectus}</small>}{prospectus && validateFile("prospectus", prospectus) && <small className="field-error" role="alert">{validateFile("prospectus", prospectus)}</small>}</label><label><span><FileText size={15} />信用评级报告</span><input type="file" disabled={createdProject != null && ["uploaded", "parse_failed"].includes(uploadStates.ratingReport)} accept={(limits?.formats || [".pdf", ".docx", ".xlsx"]).join(",")} onChange={(event) => setFile("ratingReport", event.target.files?.[0] || null)} />{ratingReport && <small>{ratingReport.name} · {uploadStates.ratingReport}</small>}{ratingReport && validateFile("ratingReport", ratingReport) && <small className="field-error" role="alert">{validateFile("ratingReport", ratingReport)}</small>}</label></div>}
      {step === 4 && <><div className="snapshot-confirm"><section><span>发行人</span><strong>{profileMode ? issuer?.name : directName}</strong><small>{profileMode ? `主体档案 v${issuer?.version}` : "发行材料模式 · 不绑定主体档案"}</small></section><section><span>核心材料</span><strong>募集说明书、信用评级报告</strong><small>{prospectus?.name} · {ratingReport?.name}</small></section><section><span>保证人</span><strong>{guarantorName.trim() || "暂未确定"}</strong><small>反担保：{counterStatus === "not_applicable" ? "不适用" : counterStatus === "undetermined" ? "暂未确定" : counterNames.length ? `${counterNames.length} 个项目主体` : "主体待核验"}</small></section><section><span>报告目标</span><strong>完整尽调</strong><small>{profileMode ? "主体档案、募集说明书和评级报告共同进入报告" : "募集说明书和评级报告进入报告事实链"}</small></section></div>{Object.entries(parseWarnings).filter(([, warning]) => warning).map(([key, warning]) => <p className="form-hint warning" role="status" key={key}>{fileLabel(key as UploadKey)}已上传，但解析未通过：{warning}</p>)}{createdProject && <p className="form-hint" role="status">上传状态：募集说明书 {uploadStates.prospectus}；评级报告 {uploadStates.ratingReport}</p>}</>}
      {error && <><ErrorState message={error} />{createdProject && <div className="form-actions"><button className="secondary-button" onClick={() => created(createdProject)}>进入项目材料页继续处理</button><button className="text-button" onClick={() => setError("")}>关闭提示</button></div>}</>}
    </div><footer className="intake-footer"><button className="secondary-button" onClick={() => step ? setStep(step - 1) : dismiss()}>{step ? <><ArrowLeft size={15} />上一步</> : "取消"}</button><span>{step === 3 ? `必须上传两份核心材料；单文件不超过 ${limits?.maxFileMb ?? 15} MB` : createdProject ? "项目已创建，失败材料可以单独重试。" : "创建项目后进入材料盘点，不会自动启动 Agent。"}</span>{step < 4 ? <button className="primary-button" disabled={!canNext} onClick={() => setStep(step + 1)}>下一步<ArrowRight size={15} /></button> : <button className="primary-button" disabled={busy} onClick={submit}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}{createdProject ? "重试未完成上传" : "确认并创建"}</button>}</footer>
  </section></div>;
}
