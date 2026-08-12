"""公开 clone 可离线执行的最小契约测试。"""

import json
from pathlib import Path

from app.rag.chunking import chunk_text, read_utf8_text


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "fixtures" / "public"


def test_public_fixture_is_nonempty_and_chunkable() -> None:
    source = FIXTURE_ROOT / "health_topics.txt"
    text = read_utf8_text(source)

    chunks = chunk_text(text, source, chunk_size=80, overlap=20)

    assert len(chunks) >= 2
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.source_name for chunk in chunks} == {"health_topics.txt"}
    assert chunks[0].text[-20:] == chunks[1].text[:20]


def test_public_evaluation_schema_declares_release_boundaries() -> None:
    schema = json.loads((FIXTURE_ROOT / "evaluation-v2.schema.json").read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert {"case_id", "split", "annotation_status", "provenance"}.issubset(schema["required"])
    assert schema["properties"]["split"]["enum"] == ["tune", "dev", "test"]


def test_public_sample_result_is_explicitly_synthetic_and_redacted() -> None:
    result = json.loads((FIXTURE_ROOT / "sample-result.json").read_text(encoding="utf-8"))

    assert result["result_status"] == "synthetic-only"
    assert result["redaction"]["query"] == "omitted"
    assert result["redaction"]["provider_trace"] == "omitted"
    assert result["observations"]["contract_tests_passed"] == 2


def test_public_agent_run_summary_is_redacted_v1bound_pass_evidence() -> None:
    result = json.loads((FIXTURE_ROOT / "agent-run-summary.json").read_text(encoding="utf-8"))

    assert result["result_status"] == "pass"
    assert result["source_run"]["run_id"] == "live-v2-full-matrix-20260813-v1bound"
    assert result["source_run"]["task_count"] == 26
    assert result["source_run"]["task_success_count"] == 26
    assert result["source_run"]["shared_success_rate"] == 1.0
    assert result["source_run"]["agent_only_success_rate"] == 1.0
    assert result["source_run"]["tool_success_count"] == 12
    assert result["source_run"]["approval_resume_success_count"] == 6
    assert result["source_run"]["corpus_version"] == "evaluation/corpora/v1"
    assert result["source_run"]["safety_gates"] == {
        "side_effect_before_approval": 0,
        "duplicate_writes": 0,
        "illegal_tool_leaks": 0,
        "unresolved_unknown_outcomes": 0,
    }
    assert "scenario_breakdown" in result["source_run"]
    assert any("v1" in item for item in result["source_run"]["known_boundaries"])
    assert all(value == "omitted" for key, value in result["redaction"].items() if key != "personal_data")
    assert result["redaction"]["personal_data"] == "none"


def test_public_reproducibility_doc_exists() -> None:
    doc = PROJECT_ROOT / "docs" / "PUBLIC_REPRODUCIBILITY.md"
    text = doc.read_text(encoding="utf-8")
    assert "fixtures/public" in text
    assert "agent-run-summary.json" in text
