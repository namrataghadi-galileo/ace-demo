from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from banking_multilevel_cases import (
    SCENARIO_BY_KEY,
    SCENARIOS,
    BankingSandbox,
    BankingScenarioInput,
    execute_account_lookup,
    execute_banking_llm,
    execute_policy_search,
    execute_transfer,
    scenario_record,
    step_tree,
)
from banking_multilevel_streamlit_app import (
    _dependency_sources,
    _emit_record_spans,
    _evaluate_parent_scenario,
    _initialize_agent,
    _log_stream_url,
    _run_decorated_span_scenario,
    _scorer_response_from_evaluation,
    _tool_output_for_scorer,
    _verify_manual_controls,
    build_agent_control_step,
    scorer_invoke_request_preview,
)

ROOT = Path(__file__).parents[1]


def test_parent_evaluation_uses_active_galileo_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_evaluate_controls(step_name: str, **kwargs):
        captured["step_name"] = step_name
        captured.update(kwargs)
        return {
            "matches": [],
            "non_matches": [],
            "errors": [],
            "reason": None,
        }

    import agent_control

    monkeypatch.setattr(agent_control, "evaluate_controls", fake_evaluate_controls)
    scenario = SCENARIO_BY_KEY["trace"]
    result = asyncio.run(
        _evaluate_parent_scenario(
            scenario,
            scenario_record("trace"),
            {"AGENT_CONTROL_AGENT_NAME": "banking-demo"},
            trace_id="1d053adf-cee0-4b84-bda8-3b197010e179",
            span_id="cf7a70bf-b9a3-4392-9ffc-61dd957873ac",
        )
    )

    assert captured["trace_id"] == "1d053adf-cee0-4b84-bda8-3b197010e179"
    assert captured["span_id"] == "cf7a70bf-b9a3-4392-9ffc-61dd957873ac"
    assert captured["step_type"] == "trace"
    assert result["evaluation_path"] == "evaluate_controls"


def test_scenario_selection_covers_exactly_five_levels() -> None:
    assert [scenario.key for scenario in SCENARIOS] == [
        "trace",
        "session",
        "llm",
        "tool",
        "retriever",
    ]
    assert {scenario.level for scenario in SCENARIOS} == {
        "trace",
        "session",
        "llm",
        "tool",
        "retriever",
    }
    assert len({scenario.step_name for scenario in SCENARIOS}) == 5
    assert all(scenario.step_name_regex == f"^{scenario.step_name}$" for scenario in SCENARIOS)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.key)
def test_workflow_outputs_are_deterministic(scenario) -> None:
    assert scenario_record(scenario.key) == scenario_record(scenario.key)
    record = scenario_record(scenario.key)
    assert record["type"] == scenario.level
    assert record["name"] == scenario.step_name
    assert record["metadata"]["executed"] is True
    assert record["metadata"]["executed_function"]
    assert record.get("metadata", {}).get("external_call", False) is False


def test_expected_step_names_and_types_are_distinct() -> None:
    standalone_names = {scenario.step_name for scenario in SCENARIOS}
    for key in ("trace", "session"):
        rows = step_tree(scenario_record(key))
        assert rows[0]["type"] == key
        child_rows = rows[1:]
        assert child_rows
        assert not standalone_names.intersection(row["name"] for row in child_rows)
    assert {row["type"] for row in step_tree(scenario_record("trace"))} == {
        "trace",
        "llm",
        "tool",
        "retriever",
    }


def test_trace_and_session_have_nested_child_steps() -> None:
    trace = scenario_record("trace")
    session = scenario_record("session")
    assert {span["type"] for span in trace["spans"]} == {"llm", "tool", "retriever"}
    assert len(session["traces"]) == 2
    assert all({span["type"] for span in trace["spans"]} == {"llm", "tool"} for trace in session["traces"])
    assert len(step_tree(session)) == 7


@pytest.mark.parametrize("scenario_key", ["llm", "tool", "retriever"])
def test_span_level_scenarios_execute_through_control_decorator(
    monkeypatch: pytest.MonkeyPatch, scenario_key: str
) -> None:
    import agent_control

    decorator_calls: list[dict[str, object]] = []

    def fake_control(**configuration):
        decorator_calls.append(configuration)

        def decorate(function):
            async def wrapped(*args, **kwargs):
                return await function(*args, **kwargs)

            return wrapped

        return decorate

    monkeypatch.setattr(agent_control, "control", fake_control)
    scenario = SCENARIO_BY_KEY[scenario_key]
    record, result = asyncio.run(_run_decorated_span_scenario(scenario))

    assert decorator_calls[0]["step_name"] == scenario.step_name
    assert decorator_calls[0]["step_type"] == scenario.level
    assert record["type"] == scenario.level
    assert "metadata" not in record
    assert result["execution_evidence"]["executed"] is True
    assert result["execution_evidence"]["evaluation_path"] == "@control"
    assert result["evaluation_path"] == "@control"
    if scenario_key == "tool":
        assert record["output"].startswith("Error: APPROVAL_REQUIRED - ")
        assert (
            result["execution_evidence"]["raw_sandbox_output"]["error_code"]
            == "APPROVAL_REQUIRED"
        )


def test_tool_error_adapter_preserves_success_and_makes_failures_explicit() -> None:
    completed = {"status": "completed", "transaction_id": "txn-sandbox-001"}
    failed = {
        "status": "error",
        "error_code": "APPROVAL_REQUIRED",
        "message": "Manager approval is required.",
    }

    assert _tool_output_for_scorer(completed) is completed
    assert (
        _tool_output_for_scorer(failed)
        == "Error: APPROVAL_REQUIRED - Manager approval is required."
    )


def test_agent_control_is_initialized_with_registered_observability_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_control

    captured: dict[str, object] = {}
    monkeypatch.setenv("GALILEO_API_KEY", "test-key")
    monkeypatch.setattr(agent_control, "init", lambda **kwargs: captured.update(kwargs))

    _initialize_agent(
        {
            "AGENT_CONTROL_AGENT_NAME": "banking-agent",
            "AGENT_CONTROL_URL": "https://agent-control.example.test",
            "AGENT_CONTROL_TARGET_TYPE": "log_stream",
            "AGENT_CONTROL_TARGET_ID": "log-stream-id",
        }
    )

    assert captured["observability_enabled"] is True
    assert captured["observability_sink_name"] == "registered"
    assert captured["target_type"] == "log_stream"
    assert captured["target_id"] == "log-stream-id"


def test_record_logging_emits_all_trace_child_span_types() -> None:
    class RecordingLogger:
        def __init__(self) -> None:
            self.types: list[str] = []

        def add_llm_span(self, **kwargs) -> None:
            self.types.append("llm")

        def add_tool_span(self, **kwargs) -> None:
            self.types.append("tool")

        def add_retriever_span(self, **kwargs) -> None:
            self.types.append("retriever")

    logger = RecordingLogger()
    _emit_record_spans(logger, scenario_record("trace"))

    assert logger.types == ["retriever", "tool", "llm"]


def test_log_stream_url_targets_the_resolved_project_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GALILEO_CONSOLE_URL",
        "https://console-namrata-ghadi-test-ee.gcp-dev.galileo.ai",
    )

    assert _log_stream_url(project_id="project-id", log_stream_id="stream-id") == (
        "https://console-namrata-ghadi-test-ee.gcp-dev.galileo.ai/"
        "namrata-ghadi-test-ee/project/project-id/agent-streams/stream-id"
    )


def test_trace_children_are_captured_from_executed_functions() -> None:
    trace = scenario_record("trace")
    children = {span["type"]: span for span in trace["spans"]}

    assert children["retriever"]["metadata"]["executed_function"] == "search_policy_documents"
    assert children["tool"]["metadata"]["executed_function"] == "BankingSandbox.lookup_account"
    assert children["llm"]["metadata"]["executed_function"] == "run_banking_model"
    assert all(child["metadata"]["executed"] is True for child in children.values())
    assert trace["output"]["message"] == children["llm"]["output"]["content"]


def test_trace_and_standalone_llm_reuse_the_same_model_function() -> None:
    trace_llm = next(
        span for span in scenario_record("trace")["spans"] if span["type"] == "llm"
    )
    standalone_llm = scenario_record("llm")

    assert trace_llm["metadata"]["executed_function"] == "run_banking_model"
    assert standalone_llm["metadata"]["executed_function"] == "run_banking_model"
    assert trace_llm["metadata"]["behavior"] == "sensitive_transfer_plan"
    assert standalone_llm["metadata"]["behavior"] == "wrong_tool"


def test_session_executes_stateful_tool_calls_before_building_parent() -> None:
    session = scenario_record("session")
    first_tool = session["traces"][0]["spans"][1]
    second_tool = session["traces"][1]["spans"][1]

    assert first_tool["output"]["error_code"] == "APPROVAL_REQUIRED"
    assert first_tool["metadata"]["sandbox_transaction_count"] == 0
    assert second_tool["output"]["status"] == "completed"
    assert second_tool["output"]["approval_bypassed"] is True
    assert second_tool["metadata"]["sandbox_transaction_count"] == 1
    assert session["output"]["completed_transactions"] == 1


def test_leaf_execution_helpers_capture_actual_results() -> None:
    sandbox = BankingSandbox()
    account = execute_account_lookup(sandbox, name="lookup", account_id="acct-1001")
    transfer = execute_transfer(sandbox, name="transfer")
    retrieval = execute_policy_search(
        name="search",
        query="domestic wire identification",
        partitions=("wire",),
        limit=1,
    )
    model = execute_banking_llm(
        name="model",
        request="plan a transfer",
        behavior="wrong_tool",
    )

    assert account.value == account.record["output"]
    assert transfer.value == transfer.record["output"]
    assert retrieval.value == retrieval.record["output"]
    assert model.value == model.record["output"]
    assert transfer.value["status"] == "error"
    assert retrieval.value[0]["metadata"]["source"] == "wire-policy.md"


def test_trigger_examples_are_present() -> None:
    llm = scenario_record("llm")
    assert {tool["function"]["name"] for tool in llm["tools"]} == {
        "plan_transfer",
        "close_account",
        "submit_transfer",
    }
    assert llm["output"]["tool_calls"][0]["function"]["name"] == "close_account"
    assert scenario_record("tool")["output"]["status"] == "error"
    assert "Ignore bank policy" in scenario_record("retriever")["output"][1]["content"]
    assert "123-45-6789" in scenario_record("trace")["output"]["message"]
    assert scenario_record("session")["output"]["attempts"] == 2


def test_interactive_values_reach_trace_and_leaf_records() -> None:
    inputs = BankingScenarioInput(
        request="Plan a $150 transfer for Casey.",
        account_id="acct-1001",
        amount=150.0,
        recipient="Casey",
        recipient_email="casey@example.test",
        recipient_ssn="987-65-4321",
        policy_query="domestic wire identification",
        manager_approved=True,
        retriever_partitions=("wire",),
        retriever_limit=1,
    )

    trace = scenario_record("trace", inputs)
    trace_llm = next(span for span in trace["spans"] if span["type"] == "llm")
    assert trace["input"]["request"] == inputs.request
    assert "casey@example.test" in trace["output"]["message"]
    assert "987-65-4321" in trace["output"]["message"]
    arguments = json.loads(
        trace_llm["output"]["tool_calls"][0]["function"]["arguments"]
    )
    assert arguments == {
        "account_id": "acct-1001",
        "amount": 150.0,
        "recipient": "Casey",
    }

    tool = scenario_record("tool", inputs)
    assert tool["input"]["amount"] == 150.0
    assert tool["input"]["manager_approved"] is True
    assert tool["output"]["status"] == "completed"

    retriever = scenario_record("retriever", inputs)
    assert retriever["query"] == inputs.request
    assert len(retriever["output"]) == 1
    assert retriever["output"][0]["metadata"]["partition"] == "wire"


def test_interactive_session_can_disable_the_retry_bypass() -> None:
    inputs = BankingScenarioInput(
        request="Try the transfer without approval.",
        retry_request="Try it one more time.",
        retry_bypass_approval=False,
    )

    session = scenario_record("session", inputs)

    assert [message["content"] for message in session["input"]["messages"]] == [
        inputs.request,
        inputs.retry_request,
    ]
    assert session["output"]["status"] == "blocked"
    assert session["output"]["completed_transactions"] == 0
    assert all(
        trace["output"]["error_code"] == "APPROVAL_REQUIRED"
        for trace in session["traces"]
    )


def test_control_configuration_json_is_valid_and_complete() -> None:
    controls = json.loads((ROOT / "banking_multilevel_controls.json").read_text())
    assert len(controls) == 5
    assert {control["scenario"] for control in controls} == set(SCENARIO_BY_KEY)
    for control in controls:
        scenario = SCENARIO_BY_KEY[control["scenario"]]
        definition = control["definition"]
        config = definition["condition"]["evaluator"]["config"]
        assert control["name"] == scenario.control_name
        assert control["target_level"] == scenario.level
        assert definition["scope"] == {
            "step_types": [scenario.level],
            "stages": [scenario.stage],
            "step_name_regex": scenario.step_name_regex,
        }
        assert definition["condition"]["selector"] == {"path": scenario.payload_field}
        assert definition["condition"]["evaluator"]["name"] == "galileo.luna"
        assert config["scorer_label"] == scenario.scorer_name
        assert config["operator"] == scenario.operator
        assert config["threshold"] == scenario.comparison_value
        assert config["scorer_id"].startswith("<fill-from-galileo-ui:")
        assert config["scorer_version_id"].startswith("<fill-from-galileo-ui:")
        assert definition["action"] == {"decision": "deny"}


def test_selected_scenario_preflight_is_independent() -> None:
    scenario = SCENARIO_BY_KEY["tool"]
    control = {
        "name": scenario.control_name,
        "data": {
            "scope": {
                "step_types": [scenario.level],
                "stages": [scenario.stage],
                "step_name_regex": scenario.step_name_regex,
            },
            "condition": {
                "selector": {"path": scenario.payload_field},
                "evaluator": {
                    "name": "galileo.luna",
                    "config": {
                        "scorer_label": scenario.scorer_name,
                        "scorer_id": "real-scorer-id",
                        "scorer_version_id": "real-version-id",
                    },
                },
            },
            "action": {"decision": "deny"},
        },
    }
    _verify_manual_controls([control], (scenario,))


@pytest.mark.parametrize(
    ("scenario_key", "display_label"),
    [
        ("trace", "Output PII (SLM)"),
        ("session", "Action Completion (SLM)"),
        ("llm", "Tool Selection Quality (SLM)"),
        ("tool", "Tool Error Rate (SLM)"),
        ("retriever", "Chunk Relevance (SLM)"),
    ],
)
def test_preflight_accepts_deployed_scorer_display_labels(
    scenario_key: str, display_label: str
) -> None:
    scenario = SCENARIO_BY_KEY[scenario_key]
    clone = {
        "name": f"{scenario.control_name}-clone-test",
        "data": {
            "scope": {
                "step_types": [scenario.level],
                "stages": [scenario.stage],
                "step_name_regex": scenario.step_name_regex,
            },
            "condition": {
                "selector": {"path": scenario.payload_field},
                "evaluator": {
                    "name": "galileo.luna",
                    "config": {
                        "scorer_label": display_label,
                        "scorer_id": "real-scorer-id",
                        "scorer_version_id": "real-version-id",
                    },
                },
            },
            "action": {"decision": "deny"},
        },
    }
    _verify_manual_controls([clone], (scenario,))


def test_each_level_builds_the_intended_local_record_shape() -> None:
    records_module = pytest.importorskip("agent_control_evaluator_galileo.records")
    models = pytest.importorskip("agent_control_models")
    built = {}
    for scenario in SCENARIOS:
        record = scenario_record(scenario.key)
        step = build_agent_control_step(record)
        assert isinstance(step, models.Step)
        built[scenario.key] = records_module.build_galileo_record(
            step.model_dump(mode="json"), step
        )
    assert {record.type for record in built.values()} == {
        "trace",
        "session",
        "llm",
        "tool",
        "retriever",
    }
    assert len(built["trace"].spans) == 3
    assert len(built["session"].traces) == 2
    assert len(built["retriever"].output) == 2
    assert built["trace"].spans[0].user_metadata["executed"] == "True"


def test_trace_scorer_invoke_preview_contains_complete_normalized_record() -> None:
    scenario = SCENARIO_BY_KEY["trace"]
    record = scenario_record("trace")
    control = {
        "name": f"{scenario.control_name}-clone-test",
        "data": {
            "condition": {
                "selector": {"path": "*"},
                "evaluator": {
                    "name": "galileo.luna",
                    "config": {
                        "scorer_id": "scorer-id",
                        "scorer_version_id": "version-id",
                        "scorer_label": "Output PII (SLM)",
                        "timeout_ms": 10000,
                        "project_id": ["project-id"],
                    },
                },
            }
        },
    }
    preview = scorer_invoke_request_preview(
        scenario,
        record,
        control,
        {"AGENT_CONTROL_TARGET_ID": "log-stream-id"},
    )
    body = preview["body"]

    assert preview["endpoint_path"] == "/api/v1/scorers/invoke"
    assert body["scorer_id"] == "scorer-id"
    assert body["scorer_version_id"] == "version-id"
    assert body["record"]["type"] == "trace"
    assert {span["type"] for span in body["record"]["spans"]} == {
        "llm",
        "tool",
        "retriever",
    }
    assert json.loads(body["inputs"]["query"]) == record["input"]
    assert json.loads(body["inputs"]["response"]) == record["output"]
    assert body["execution_context"]["project_id"] == "project-id"
    assert body["execution_context"]["run_id"] == "log-stream-id"
    assert body["config"]["request_timeout_seconds"] == 8.0


def test_scorer_response_view_keeps_allow_score_and_status() -> None:
    response = _scorer_response_from_evaluation(
        {
            "control": "trace-control-clone",
            "matched": False,
            "confidence": 1.0,
            "message": "not triggered",
            "error": None,
            "metadata": {
                "score": ["email"],
                "status": "success",
                "execution_time_seconds": 0.25,
                "requested_scorer_version_id": "version-id",
            },
        }
    )

    assert response is not None
    assert response["matched"] is False
    assert response["scorer_response"] == {
        "score": ["email"],
        "status": "success",
        "execution_time_seconds": 0.25,
        "requested_scorer_version_id": "version-id",
    }


def test_banking_project_uses_local_agent_control_sources() -> None:
    sources = _dependency_sources()
    assert all("/code/agent-control/" in path for path in sources.values())


def test_local_dependency_override_and_no_sources_escape_hatch_are_documented() -> None:
    pyproject = (ROOT / "banking-multilevel" / "pyproject.toml").read_text()
    docs = (ROOT / "docs" / "11_banking_multilevel_runtime_e2e.md").read_text()
    assert "[tool.uv.sources]" in pyproject
    assert "../../agent-control/sdks/python" in pyproject
    assert "../../agent-control/evaluators/contrib/galileo" in pyproject
    assert '"galileo[otel]>=2.3.0"' in pyproject
    assert "--no-sources" in docs
    assert "AGENT_CONTROL_URL" in docs


def test_demo_contains_no_real_external_banking_calls() -> None:
    cases_source = (ROOT / "banking_multilevel_cases.py").read_text()
    cases_tree = ast.parse(cases_source)
    imported_roots = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(cases_tree)
        if isinstance(node, ast.Import) and node.names
    }
    imported_from = {
        (node.module or "").split(".")[0]
        for node in ast.walk(cases_tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not ({"httpx", "requests", "urllib", "socket"} & (imported_roots | imported_from))
    assert "external_call\": False" in cases_source
    app_source = (ROOT / "banking_multilevel_streamlit_app.py").read_text()
    assert ".post(" not in app_source
