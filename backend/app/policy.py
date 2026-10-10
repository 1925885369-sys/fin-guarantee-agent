from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .database import connect, json_dump, now_iso


class RulePolicy(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    title: str = Field(min_length=2, max_length=80)
    severity: str = Field(pattern=r"^(high|medium|low)$")
    thresholds: dict[str, float] = Field(default_factory=dict)


class PolicyPack(BaseModel):
    profile: str
    version: str
    name: str
    description: str
    rules: list[RulePolicy]
    sha256: str = ""

    @field_validator("rules")
    @classmethod
    def unique_rule_keys(cls, value: list[RulePolicy]) -> list[RulePolicy]:
        keys = [item.key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("政策规则键重复")
        if len(keys) != 19:
            raise ValueError("通用政策必须包含19项规则")
        return value

    def rule(self, key: str) -> RulePolicy:
        return next(item for item in self.rules if item.key == key)

    def public_dict(self) -> dict:
        return self.model_dump()


@lru_cache(maxsize=8)
def load_policy(profile: str | None = None) -> PolicyPack:
    selected = profile or settings.policy_profile
    path = Path(__file__).resolve().parents[1] / "policies" / f"{selected}.yaml"
    if not path.is_file():
        raise RuntimeError(f"政策配置不存在：{selected}")
    raw = path.read_bytes()
    payload = yaml.safe_load(raw.decode("utf-8"))
    payload["sha256"] = hashlib.sha256(raw).hexdigest()
    return PolicyPack.model_validate(payload)


def save_policy_snapshot(project_id: str, run_id: str, policy: PolicyPack) -> str:
    snapshot_id = f"policy-{run_id}"
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO policy_snapshots (id,project_id,run_id,profile,version,sha256,content_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (snapshot_id, project_id, run_id, policy.profile, policy.version, policy.sha256,
             json_dump(policy.public_dict()), now_iso()),
        )
        conn.execute(
            "UPDATE runs SET policy_profile=?,policy_version=?,policy_sha256=? WHERE id=?",
            (policy.profile, policy.version, policy.sha256, run_id),
        )
    return snapshot_id


def policy_json(policy: PolicyPack) -> str:
    return json.dumps(policy.public_dict(), ensure_ascii=False, sort_keys=True)
