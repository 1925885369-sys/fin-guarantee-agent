from scripts.check_public_release import inspect_content, private_path


def test_empty_example_key_does_not_capture_the_next_configuration_line():
    data = b"LLM_API_KEY=\r\nLLM_MODEL=deepseek-v4-flash\r\n"
    assert inspect_content(data, ".env.example", "working_tree") == []


def test_release_check_reports_credential_location_without_the_value():
    value = b"synthetic" * 8
    data = b"LLM_API_KEY=" + value
    findings = inspect_content(data, "example.txt", "working_tree")
    assert findings == [{"scope": "working_tree", "path": "example.txt", "kind": "credential_assignment", "line": 1}]
    assert value.decode() not in str(findings)


def test_release_check_blocks_private_data_but_allows_source_and_example_configuration():
    for name in (".env", ".env.local", ".data/agent.db", ".data-ci/uploads/source.pdf", "docs/report.docx", ".models/weights.bin"):
        assert private_path(name), name
    for name in (".env.example", "backend/app/real_reference.py", "docs/assets/bond-baseline-evaluation-v4.json"):
        assert not private_path(name), name
