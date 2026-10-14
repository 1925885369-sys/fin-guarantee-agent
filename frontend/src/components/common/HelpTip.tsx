import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { HelpCircle } from "lucide-react";

/**
 * 共用说明按钮：区块标题旁的问号，承载本区块的原理、口径、方法等长说明。
 *
 * 设计约束（来自排版规范）：
 * - 悬停、键盘聚焦、手机点击都能打开；Escape、点击外部关闭；切换页面随组件卸载关闭。
 * - 浮层用 portal 挂到 body + `position: fixed`，不参与父级 overflow 裁剪，
 *   因此放在卡片、表格滚动区、抽屉里也不会被切掉；坐标按视口夹取，保证在屏幕内。
 * - 触发器与浮层通过 `aria-describedby` 关联。
 *
 * 用法：只承载**短说明**；错误、阻塞原因、资料缺口、校验提示、只读状态等必须直接显示，
 * 不要塞进这里。同一区块的多条解释合并成一段，不为每个字段各加一个问号。
 */
export function HelpTip({ label, children, side = "bottom" }: { label: string; children: ReactNode; side?: "top" | "bottom" }) {
  const panelId = useId();
  const anchorRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

  const place = useCallback(() => {
    const anchor = anchorRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;
    const margin = 10;
    const rect = anchor.getBoundingClientRect();
    const size = panel.getBoundingClientRect();
    const maxLeft = Math.max(margin, window.innerWidth - size.width - margin);
    const left = Math.min(Math.max(margin, rect.left + rect.width / 2 - size.width / 2), maxLeft);
    let top = side === "bottom" ? rect.bottom + 8 : rect.top - size.height - 8;
    if (top + size.height > window.innerHeight - margin) top = Math.max(margin, rect.top - size.height - 8);
    if (top < margin) top = Math.min(rect.bottom + 8, window.innerHeight - size.height - margin);
    setPosition({ top, left });
  }, [side]);

  useLayoutEffect(() => {
    if (!open) { setPosition(null); return; }
    place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      setPinned(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (anchorRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
      setPinned(false);
    };
    const reposition = () => place();
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open, place]);

  const hide = () => { if (!pinned) setOpen(false); };

  return <>
    <button
      type="button"
      ref={anchorRef}
      className="help-tip"
      aria-label={label}
      aria-expanded={open}
      aria-describedby={open ? panelId : undefined}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={hide}
      onFocus={() => setOpen(true)}
      onBlur={hide}
      onClick={(event) => {
        event.stopPropagation();
        setPinned((current) => {
          const next = !current;
          setOpen(next);
          return next;
        });
      }}
    >
      <HelpCircle size={14} />
    </button>
    {open && createPortal(
      <div
        className="help-tip-panel"
        id={panelId}
        role="tooltip"
        ref={panelRef}
        style={position ? { top: position.top, left: position.left } : { top: -9999, left: -9999 }}
      >
        {children}
      </div>,
      document.body,
    )}
  </>;
}
