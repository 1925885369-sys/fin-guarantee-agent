from scripts.check_bond_similarity import Passage, find_reused_passages


def test_long_reference_passage_is_detected_but_regulatory_whitelist_is_allowed():
    copied = "发行人主营业务收入主要来自精密制造板块并且报告期内客户集中度持续上升需要重点关注"
    reference = [
        Passage("reference.pdf", 12, copied),
        Passage("reference.pdf", 2, "中国证监会对本次发行所作的任何决定均不表明其对发行人的偿债能力作出判断"),
    ]
    generated = [
        Passage("generated.pdf", 8, copied),
        Passage("generated.pdf", 1, "中国证监会对本次发行所作的任何决定均不表明其对发行人的偿债能力作出判断"),
    ]
    findings = find_reused_passages(reference, generated)
    assert len(findings) == 1
    assert findings[0]["generated_page"] == 8
