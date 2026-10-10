"""Compatibility entry points for the six-case bond evaluation portfolio."""
from __future__ import annotations

from .bond_documents import CASE_SPECS, evaluation_manifest, generate_case_materials, seed_case
from .config import settings


PORTFOLIO_SPECS = tuple(case for case in CASE_SPECS if case.id != "demo-bond-huachen")
CASES = [case.model_dump(mode="python") for case in PORTFOLIO_SPECS]


def generate_bond_portfolio_materials() -> list[dict]:
    output = []
    for case in PORTFOLIO_SPECS:
        output.extend(generate_case_materials(settings.resolved_data_dir / "demo_materials" / case.id, case))
    return output


def seed_bond_portfolio_cases() -> None:
    for case in PORTFOLIO_SPECS:
        seed_case(case, settings.resolved_data_dir / "demo_materials" / case.id)


def bond_evaluation_manifest() -> dict[str, dict]:
    return evaluation_manifest()
