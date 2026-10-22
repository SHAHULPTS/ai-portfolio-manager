"""The writer's request, checked with a fake Anthropic client (no API calls)."""

from types import SimpleNamespace

import psycopg
import pytest

from agent.sql.schema import load_schema
from agent.sql.writer import FALLBACK_BETA, SqlDraft, SqlWriter, WriterError, WriterRefused

PORTFOLIOS = [{"portfolio_id": 11, "name": "P11", "mandate": "Synthetic", "benchmark": "NIFTY50"}]


class FakeClient:
    def __init__(self, response):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self._response = response

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def ok(draft):
    return SimpleNamespace(stop_reason="end_turn", parsed_output=draft, stop_details=None)


@pytest.fixture(scope="module")
def schema(metrics_db):
    with psycopg.connect(metrics_db[1]) as conn:
        return load_schema(conn)


def make(schema, response):
    client = FakeClient(response)
    return SqlWriter(schema=schema, model="claude-sonnet-5-5", client=client), client


def test_request_shape(schema):
    draft = SqlDraft(answerable=True, sql="SELECT 1", explanation="one")
    writer, client = make(schema, ok(draft))
    assert writer.write("Top holdings?", PORTFOLIOS, "2026-04-08") == draft

    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_format"] is SqlDraft
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "analytics.v_positions" in call["system"][0]["text"]  # live schema
    assert "Worked examples" in call["system"][0]["text"]
    user = call["messages"][0]["content"]
    assert "<question>\nTop holdings?\n</question>" in user
    assert "Data as of: 2026-04-08" in user and "11: P11" in user


def test_system_prompt_is_identical_across_questions(schema):
    # Anything varying in the system prompt would silently break prompt caching.
    draft = SqlDraft(answerable=True, sql="SELECT 1", explanation="")
    writer, client = make(schema, ok(draft))
    writer.write("first question", PORTFOLIOS, "2026-04-08")
    writer.write("a different question", [], "2025-01-01")
    assert client.calls[0]["system"] == client.calls[1]["system"]


def test_retry_includes_the_previous_error(schema):
    writer, client = make(schema, ok(SqlDraft(answerable=True, sql="SELECT 1", explanation="")))
    writer.write("q", PORTFOLIOS, "2026-04-08", previous_sql="SELECT bad", previous_error="no col")
    user = client.calls[0]["messages"][0]["content"]
    assert "SELECT bad" in user and "Error: no col" in user


def test_refusal_and_truncation_raise(schema):
    refused = SimpleNamespace(
        stop_reason="refusal", parsed_output=None, stop_details=SimpleNamespace(category="cyber")
    )
    with pytest.raises(WriterRefused, match="cyber"):
        make(schema, refused)[0].write("q", PORTFOLIOS, "2026-04-08")
    cut = SimpleNamespace(stop_reason="max_tokens", parsed_output=None, stop_details=None)
    with pytest.raises(WriterError):
        make(schema, cut)[0].write("q", PORTFOLIOS, "2026-04-08")


def test_draft_schema_is_strict():
    schema = SqlDraft.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"answerable", "sql", "explanation"}
