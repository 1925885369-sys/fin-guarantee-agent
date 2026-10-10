"""测试库隔离（并行开发与验收的硬前提）。

背景：`backend/app/config.py` 只在 **import 期**读取 `APP_DATA_DIR`（默认 `.data`），
而 `tests/` 此前没有任何隔离配置，于是每次 `python -m pytest tests` 都会：

1. 触发 `TestClient(app)` → `lifespan`/`startup()` → `seed_bond_case()` **重播种演示项目**，
   删除 `demo-bond-huachen` 的 runs / llm_attempts / report_sections；
2. 直接读写正式的 `.data/agent.db`，把真实的验收数据与人工操作结果一起改掉。

本项目已因此丢失过两次真实运行基线。本文件在**导入任何 backend 模块之前**确定数据目录：

- 已设置 `APP_DATA_DIR` → 用它（各任务各用一个目录）；
- 未设置 → **自动改用独立的临时目录**（`APP_DATA_DIR=.data-testrun-<pid>`），
  并在结束时提示。绝不静默落到正式 `.data`。

注意：不要用"测试结束后恢复数据"替代隔离——恢复发生在事后，中途的失败与并发
都会留下并发的写入。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_DATA_DIR = (ROOT / ".data").resolve()


def _isolate_data_dir() -> None:
    configured = os.environ.get("APP_DATA_DIR", "").strip()
    if configured:
        resolved = (ROOT / configured).resolve() if not Path(configured).is_absolute() else Path(configured)
        if resolved == OFFICIAL_DATA_DIR:
            raise RuntimeError(
                "拒绝在正式库 .data 上运行测试：请设置 APP_DATA_DIR 指向独立目录，"
                "例如 APP_DATA_DIR=.data-taskA python -m pytest tests"
            )
        resolved.mkdir(parents=True, exist_ok=True)
        os.environ["APP_DATA_DIR"] = str(resolved)
        print(f"[conftest] 测试数据目录: {resolved}", file=sys.stderr)
        return

    created = Path(tempfile.mkdtemp(prefix="fin-guarantee-testrun-"))
    os.environ["APP_DATA_DIR"] = str(created)
    print(
        f"[conftest] 未设置 APP_DATA_DIR，已自动改用独立目录 {created}"
        "（正式库 .data 不会被读写）",
        file=sys.stderr,
    )


def _copy_materials(target: Path) -> None:
    """把演示/真实参考**素材**复制进隔离目录（只读正式目录，不复制数据库）。

    `seed_real_reference_projects()` 从 `<数据目录>/demo_materials/` 读取中邮、北投等
    真实公开材料；素材缺失时那些项目不会被播种，相关用例会假失败。种子数据本身由
    `startup()` 在空库上重建，因此不复制 `agent.db` 才能真正与当前库状态解耦。
    """
    for name in ("demo_materials", "uploads"):
        source = OFFICIAL_DATA_DIR / name
        if not source.is_dir():
            continue
        destination = target / name
        if destination.exists():
            continue
        try:
            shutil.copytree(source, destination)
        except OSError as exc:      # 素材被占用时降级为软失败，不阻断测试
            print(f"[conftest] 复制 {name} 失败，跳过：{exc}", file=sys.stderr)


_isolate_data_dir()
_copy_materials(Path(os.environ["APP_DATA_DIR"]))

# 隔离确定之后再导入 backend；此后的 settings 会固化到隔离目录。
from backend.app.config import settings  # noqa: E402

assert str(settings.resolved_data_dir) != str(OFFICIAL_DATA_DIR), "隔离失败：仍指向正式库"


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """自动创建的临时目录在会话结束时清理；显式配置的目录保留供排查。"""
    if os.environ.get("FINGUARANTEE_KEEP_TEST_DATA") == "1":
        return
    configured = os.environ.get("APP_DATA_DIR", "")
    if "fin-guarantee-testrun-" in configured and Path(configured).exists():
        shutil.rmtree(configured, ignore_errors=True)
