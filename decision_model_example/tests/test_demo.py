from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

from agent_control import ControlViolationError
from decision_model_example.cases import (
    CONTROL_NAME,
    EVALUATOR_NAME,
    SCENARIO_BY_KEY,
    STEP_NAME,
    BankingSandbox,
    request_for_scenario,
    run_banking_workflow,
    transfer_tool_payload,
)
from decision_model_example.streamlit_app import (
    ControlEventCapture,
    normalize_api_root,
    runtime_environment,
)

EXAMPLE_ROOT = Path(__file__).parents[1]


def test_tool_payload_contains_fields_evaluated_by_jev() -> None:
    request = request_for_scenario(SCENARIO_BY_KEY["suspicious_destination"])

    payload = transfer_tool_payload(request)

    assert payload == {
        "user_request": "Send $7,500 to Kestrel Export LLC in Iran for consulting services.",
        "source_account_id": "acct-1001",
        "amount": 7_500.0,
        "currency": "USD",
        "recipient_name": "Kestrel Export LLC",
        "destination_account_id": "dest-7710",
        "destination_country": "IR",
        "purpose": "Consulting services",
        "recipient_verified": True,
        "identity_verified": True,
        "manager_approved": True,
        "sanctions_screened": True,
        "sanctions_check_passed": False,
    }


def test_deny_stops_transfer_before_sandbox_execution() -> None:
    request = request_for_scenario(SCENARIO_BY_KEY["unverified"])
    sandbox = BankingSandbox()
    called = False

    async def denied_tool(**payload):
        nonlocal called
        called = True
        raise ControlViolationError(
            control_id=1,
            control_name=CONTROL_NAME,
            message="Jev result 'deny' eq threshold 'deny': control triggered.",
            metadata={"score": "deny", "scorer_label": "Banking transfer safety"},
        )

    result = asyncio.run(run_banking_workflow(request, denied_tool, sandbox=sandbox))

    assert called is True
    assert result["decision"] == "deny"
    assert result["tool_executed"] is False
    assert result["control"]["name"] == CONTROL_NAME
    assert result["control"]["metadata"]["score"] == "deny"
    assert sandbox.transactions == []


def test_non_denied_transfer_completes_normally() -> None:
    request = request_for_scenario(SCENARIO_BY_KEY["safe"])
    sandbox = BankingSandbox()

    async def allowed_tool(**payload):
        return sandbox.submit_transfer(**payload)

    result = asyncio.run(run_banking_workflow(request, allowed_tool, sandbox=sandbox))

    assert result["decision"] == "allow"
    assert result["tool_executed"] is True
    assert result["tool_output"]["status"] == "completed"
    assert len(sandbox.transactions) == 1


def test_diagnostic_sink_captures_real_control_events() -> None:
    from agent_control_models import ControlExecutionEvent

    capture = ControlEventCapture()
    event = ControlExecutionEvent(
        trace_id="trace-1",
        span_id="span-1",
        agent_name="decision-model-banking-streamlit",
        control_id=1,
        control_name=CONTROL_NAME,
        check_stage="pre",
        applies_to="tool_call",
        action="deny",
        matched=True,
        confidence=0.98,
        evaluator_name=EVALUATOR_NAME,
        selector_path="input",
        metadata={"score": "deny"},
    )

    result = capture.write_events([event])

    assert result.accepted == 1
    assert capture.events == [event]


def test_galileo_api_url_removes_legacy_api_suffix() -> None:
    assert (
        normalize_api_root("https://api-test-jev.gcp-dev.galileo.ai/api/")
        == "https://api-test-jev.gcp-dev.galileo.ai"
    )
    assert (
        normalize_api_root("https://api-test-jev.gcp-dev.galileo.ai")
        == "https://api-test-jev.gcp-dev.galileo.ai"
    )


def test_runtime_environment_reads_the_example_configuration(monkeypatch) -> None:
    monkeypatch.setenv("GALILEO_API_KEY", "test-key")
    monkeypatch.setenv("GALILEO_CONSOLE_URL", "https://console-stack.example")
    monkeypatch.setenv("GALILEO_API_URL", "https://api-stack.example/api")
    monkeypatch.setenv("GALILEO_PROJECT_ID", "project-id")
    monkeypatch.setenv("GALILEO_PROJECT", "project-name")
    monkeypatch.setenv("GALILEO_LOG_STREAM_ID", "stream-id")
    monkeypatch.setenv("GALILEO_LOG_STREAM", "stream-name")
    monkeypatch.setenv("AGENT_CONTROL_AGENT_NAME", "decision-model-demo")
    monkeypatch.delenv("AGENT_CONTROL_URL", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_TARGET_ID", raising=False)

    env = runtime_environment()

    assert env.console_url == "https://console-stack.example"
    assert env.api_url == "https://api-stack.example"
    assert env.agent_control_url == "https://console-stack.example/api/agent-control"
    assert env.agent_name == "decision-model-demo"
    assert env.log_stream_id == "stream-id"
    assert env.target_id == "stream-id"


def test_runtime_rejects_different_control_and_telemetry_streams(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("GALILEO_API_KEY", "test-key")
    monkeypatch.setenv("GALILEO_CONSOLE_URL", "https://console-stack.example")
    monkeypatch.setenv("GALILEO_API_URL", "https://api-stack.example")
    monkeypatch.setenv("GALILEO_PROJECT_ID", "project-id")
    monkeypatch.setenv("GALILEO_PROJECT", "project-name")
    monkeypatch.setenv("GALILEO_LOG_STREAM_ID", "telemetry-stream")
    monkeypatch.setenv("GALILEO_LOG_STREAM", "stream-name")
    monkeypatch.setenv("AGENT_CONTROL_TARGET_TYPE", "log_stream")
    monkeypatch.setenv("AGENT_CONTROL_TARGET_ID", "control-stream")

    with pytest.raises(RuntimeError, match="must equal GALILEO_LOG_STREAM_ID"):
        runtime_environment()


def test_app_uses_control_decorator_and_never_calls_evaluate_controls() -> None:
    source = (EXAMPLE_ROOT / "streamlit_app.py").read_text()

    assert 'step_name=STEP_NAME, step_type="tool"' in source
    assert "evaluate_controls(" not in source
    assert "client.put(" not in source
    assert "client.post(" not in source
    assert "start_session(" not in source
    assert STEP_NAME == "decision_model_submit_transfer"


def test_jev_project_uses_only_local_agent_control_sources() -> None:
    pyproject_path = EXAMPLE_ROOT / "pyproject.toml"
    pyproject = pyproject_path.read_text()
    sources = tomllib.loads(pyproject)["tool"]["uv"]["sources"]
    expected = {
        "agent-control-sdk",
        "agent-control-models",
        "agent-control-engine",
        "agent-control-telemetry",
        "agent-control-evaluators",
        "agent-control-evaluator-galileo",
    }

    assert set(sources) == expected
    assert all(source["editable"] is True for source in sources.values())
    assert all("../../agent-control/" in source["path"] for source in sources.values())
    assert "galileo.decision_models is being exercised" in pyproject
    assert "local Agent Control feature checkout" in pyproject


def test_scorer_and_control_template_uses_decision_models_with_placeholders() -> None:
    import json

    template = json.loads((EXAMPLE_ROOT / "controls_and_evaluator.json").read_text())
    scorer = template["evaluator"]
    control = template["control"]
    definition = control["definition"]
    evaluator = definition["condition"]["evaluator"]

    assert scorer["name"] == "Banking Transfer Safety"
    assert scorer["scorer_type"] == "decision"
    assert scorer["model"] == "jev-1.13.0"
    assert scorer["scoreable_node_types"] == ["tool"]
    assert set(scorer["answer_options"]) == {"true", "false"}
    assert control["name"] == CONTROL_NAME
    assert definition["scope"] == {
        "step_types": ["tool"],
        "stages": ["pre"],
    }
    assert definition["condition"]["selector"] == {"path": "input"}
    assert evaluator["name"] == EVALUATOR_NAME
    assert evaluator["config"]["scorer_id"].startswith("<populated-by-backend:")
    assert evaluator["config"]["scorer_version_id"].startswith("<populated-by-backend:")
    assert definition["action"] == {"decision": "deny"}
    assert template["binding"]["target_type"] == "log_stream"


def test_example_env_contains_no_real_credentials() -> None:
    env_example = (EXAMPLE_ROOT / ".env.example").read_text()

    assert "GALILEO_API_KEY=<your-api-key>" in env_example
    assert "DECISION_MODEL_" not in env_example
    assert "namrata-ghadi" not in env_example


def test_app_does_not_require_control_or_scorer_identity_configuration() -> None:
    source = (EXAMPLE_ROOT / "streamlit_app.py").read_text()

    assert "DECISION_MODEL_CONTROL_NAME" not in source
    assert "DECISION_MODEL_SCORER_ID" not in source
    assert "DECISION_MODEL_SCORER_VERSION_ID" not in source
    assert "verify_bound_control" not in source
