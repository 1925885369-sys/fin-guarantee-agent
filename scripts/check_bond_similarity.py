"""Detect accidental reuse of long passages from local bond reference PDFs."""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import fitz


FORBIDDEN_IDENTIFIERS = ("北京市基础设施投资有限公司", "中诚信国际", "CCXI-")
REGULATORY_WHITELIST = (
    "中国证监会对本次发行所作的任何决定均不表明其对发行人的偿债能力作出判断",
    "投资者应当认真阅读募集说明书及有关的信息披露文件进行独立的投资判断",
    "本次发行申请文件不存在虚假记载误导性陈述或重大遗漏",
)
MIN_LENGTH = 30
SIMILARITY_LIMIT = 0.90


@dataclass(frozen=True)
class Passage:
    document: str
    page: int
    text: str


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。；：、！？（）()《》【】\[\]“”‘’\-—]", "", text)


def _passages(path: Path) -> list[Passage]:
    output: list[Passage] = []
    with fitz.open(path) as document:
        for page_number, page in enumerate(document, 1):
            for text in re.split(r"[。！？；\n]+", page.get_text("text")):
                text = text.strip()
                if len(_normalize(text)) >= MIN_LENGTH:
                    output.append(Passage(path.name, page_number, text))
    return output


def _whitelisted(text: str) -> bool:
    normalized = _normalize(text)
    return any(_normalize(item) in normalized for item in REGULATORY_WHITELIST)


def find_reused_passages(reference: list[Passage], generated: list[Passage]) -> list[dict]:
    reference_rows = [(item, _normalize(item.text)) for item in reference if not _whitelisted(item.text)]
    findings: list[dict] = []
    for candidate in generated:
        normalized = _normalize(candidate.text)
        if _whitelisted(candidate.text):
            continue
        for source, source_text in reference_rows:
            shorter = min(len(normalized), len(source_text))
            if shorter < MIN_LENGTH:
                continue
            direct = normalized in source_text or source_text in normalized
            # The shared prefix keeps the quadratic comparison bounded for long documents.
            prefix_overlap = normalized[:12] in source_text or source_text[:12] in normalized
            ratio = SequenceMatcher(None, normalized, source_text, autojunk=False).ratio() if (direct or prefix_overlap) else 0.0
            if direct or ratio >= SIMILARITY_LIMIT:
                findings.append({
                    "generated_document": candidate.document,
                    "generated_page": candidate.page,
                    "generated_excerpt": candidate.text[:120],
                    "reference_document": source.document,
                    "reference_page": source.page,
                    "similarity": round(ratio if ratio else 1.0, 4),
                })
                break
    return findings


def check_paths(reference_paths: list[Path], generated_paths: list[Path]) -> dict:
    reference = [item for path in reference_paths for item in _passages(path)]
    generated = [item for path in generated_paths for item in _passages(path)]
    identifiers = []
    for item in generated:
        for identifier in FORBIDDEN_IDENTIFIERS:
            if identifier in item.text:
                identifiers.append({"document": item.document, "page": item.page, "identifier": identifier})
    reused = find_reused_passages(reference, generated)
    return {
        "reference_documents": len(reference_paths),
        "generated_documents": len(generated_paths),
        "forbidden_identifiers": identifiers,
        "reused_passages": reused,
        "passed": not identifiers and not reused,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="检查虚构债券材料是否复用了参考文件的长句或真实标识")
    parser.add_argument("--reference", type=Path, required=True, help="参考 PDF 目录")
    parser.add_argument("--generated-root", type=Path, required=True, help="六套虚构案例根目录")
    args = parser.parse_args()
    references = sorted(args.reference.glob("*.pdf"))
    generated = sorted(path for path in args.generated_root.glob("demo-bond-*/*.pdf"))
    if not references or not generated:
        raise SystemExit("未找到参考 PDF 或虚构案例 PDF")
    result = check_paths(references, generated)
    print(
        f"reference={result['reference_documents']}, generated={result['generated_documents']}, "
        f"identifiers={len(result['forbidden_identifiers'])}, reused={len(result['reused_passages'])}"
    )
    for item in result["forbidden_identifiers"] + result["reused_passages"]:
        print(item)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
