"""Compatibility entry points for the flagship fictional bond case."""
from __future__ import annotations

from pathlib import Path

from .bond_documents import CASE_BY_ID, case_facts, generate_case_materials, seed_case
from .config import settings


PROJECT_ID = "demo-bond-huachen"
SPEC = CASE_BY_ID[PROJECT_ID]
COMPANY = SPEC.company
BOND_NAME = SPEC.bond_name


def _materials(root: Path) -> list[dict]:
    return generate_case_materials(root, SPEC)


def _facts(documents: list[dict]) -> list[dict]:
    return case_facts(SPEC, documents)


def seed_bond_case() -> None:
    seed_case(SPEC, settings.resolved_data_dir / "demo_materials" / PROJECT_ID)
