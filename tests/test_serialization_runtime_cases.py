from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from serialization_runtime_cases import CASES, case_payload, validate_case_payload
from serialization_runtime_streamlit_app import _verify_manual_controls


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_each_case_has_expected_payload_type_and_deny_control(case) -> None:
    payload = case_payload(case.key)
    validate_case_payload(case, payload)
    assert payload["type"] == case.record_type
    assert payload["name"] == case.step_name
    assert case.stage == "post"
    assert case.selector == "*"
    assert case.expected_action == "deny"
    assert case.step_name_regex == f"^{case.step_name}$"
    assert case.control_name.startswith("serialization-deny-")


def test_all_payloads_have_unique_exact_step_names_and_controls() -> None:
    assert len({case.step_name for case in CASES}) == 5
    assert len({case.control_name for case in CASES}) == 5
    assert [case.control_name for case in CASES] == [
        "serialization-deny-trace-output-pii",
        "serialization-deny-session-action-completion",
        "serialization-deny-llm-tool-selection",
        "serialization-deny-tool-error-rate",
        "serialization-deny-retriever-chunk-relevance",
    ]
    assert [case.scorer_label for case in CASES] == [
        "output_pii",
        "action_completion_luna",
        "tool_selection_quality_luna",
        "tool_error_rate_luna",
        "chunk_relevance_luna",
    ]


def test_trace_case_includes_only_its_related_llm_and_tool_spans() -> None:
    trace = case_payload("trace")
    assert {span["type"] for span in trace["spans"]} == {"llm", "tool"}
    names = {span["name"] for span in trace["spans"]}
    assert names == {
        "serialization_trace_transfer_llm",
        "serialization_trace_submit_transfer",
    }
    assert not names & {case_payload("llm")["name"], case_payload("tool")["name"]}
    llm = next(span for span in trace["spans"] if span["type"] == "llm")
    assert llm["output"]["tool_calls"][0]["function"]["name"] == "submit_transfer"
    assert "alice@example.test" in trace["output"]["message"]
    assert trace["id"].count("-") == 4
    assert trace["created_at"].endswith("Z")


def test_session_has_two_nested_traces_with_llm_tool_retriever() -> None:
    session = case_payload("session")
    assert len(session["input"]["messages"]) == 2
    assert len(session["traces"]) == 2
    second_spans = session["traces"][1]["spans"]
    assert {span["type"] for span in second_spans} == {"llm", "tool", "retriever"}
    retriever = next(span for span in second_spans if span["type"] == "retriever")
    assert all(
        isinstance(value, (str, int, float, bool))
        for doc in retriever["output"]
        for value in doc["metadata"].values()
    )


def test_llm_keeps_chat_tool_schema_and_selected_call_in_canonical_fields() -> None:
    llm = case_payload("llm")
    assert llm["input"] == [
        {"role": "system", "content": "Follow transfer policy."},
        {
            "role": "user",
            "content": "Review transfer 7781 and cite the relevant policy sections.",
        },
    ]
    assert llm["output"]["role"] == "assistant"
    assert llm["output"]["content"] == "I will verify the transfer before submitting it."
    assert llm["output"]["tool_calls"] == [
        {
            "id": "call-22222222",
            "type": "function",
            "function": {
                "name": "submit_transfer",
                "arguments": '{"amount": 375.5, "metadata": {"source": "serialization-suite", "approved": true}}',
            },
        }
    ]
    assert llm["tools"][0]["function"]["name"] == "submit_transfer"
    assert llm["tools"][0]["function"]["parameters"]["properties"] == {
        "amount": {"type": "number"},
        "metadata": {"type": "object"},
    }


def test_tool_error_fixture_uses_canonical_arguments_and_error_text() -> None:
    tool = case_payload("tool")
    serialized = json.dumps(tool, allow_nan=False)
    roundtrip = json.loads(serialized)
    assert roundtrip["input"] == {"code": "print(undefined_variable)"}
    assert roundtrip["output"].startswith("Error: NameError")
    assert roundtrip["metadata"]["redacted_fields"] == ["code"]


def test_retriever_documents_are_canonicalizable_content_and_scalar_metadata() -> None:
    retriever = case_payload("retriever")
    assert retriever["query"] == "What is the capital of France?"
    assert len(retriever["output"]) >= 2
    assert retriever["output"][0]["content"] == "Bananas are a good source of potassium."
    assert retriever["output"][1]["content"] == "The Pacific Ocean is the largest ocean on Earth."
    for document in retriever["output"]:
        assert isinstance(document["content"], str)
        assert set(document["metadata"]) >= {"source", "rank", "score"}
        assert all(
            isinstance(value, (str, int, float, bool))
            for value in document["metadata"].values()
        )


def test_local_galileo_record_factory_preserves_each_supported_shape() -> None:
    factory = pytest.importorskip("agent_control_evaluator_galileo.records")
    models = pytest.importorskip("agent_control_models")
    build_record = factory.build_galileo_record
    records = {}
    for case in CASES:
        payload = case_payload(case.key)
        context = {case.key: payload} if case.key in {"trace", "session"} else {}
        input_value = payload.get("input", payload.get("query", ""))
        step = models.Step(
            type=case.record_type,
            name=case.step_name,
            input=input_value,
            output=payload.get("output"),
            tools=payload.get("tools") if case.record_type == "llm" else None,
            context=context,
        )
        records[case.key] = build_record(step.model_dump(mode="json"), step)

    assert {span.type for span in records["trace"].spans} == {"llm", "tool"}
    assert len(records["session"].traces) == 2
    assert len(records["retriever"].output) == 2
    assert records["retriever"].input == case_payload("retriever")["query"]
    assert all(
        type(document).__name__ == "Document"
        for document in records["retriever"].output
    )
    assert json.loads(records["tool"].input) == case_payload("tool")["input"]
    assert [message.model_dump(mode="json", exclude_none=True) for message in records["llm"].input] == case_payload("llm")["input"]
    llm_output = records["llm"].output.model_dump(mode="json", exclude_none=True)
    assert llm_output["role"] == "assistant"
    assert llm_output["content"] == case_payload("llm")["output"]["content"]
    assert records["llm"].tools[0]["function"]["name"] == "submit_transfer"
    assert records["llm"].output.tool_calls[0].function.name == "submit_transfer"
    assert llm_output["tool_calls"][0]["id"] == case_payload("llm")["tool_calls"][0]["id"]


def test_llm_scorer_invoke_legacy_values_match_structured_record() -> None:
    """Both legacy inputs and the canonical record must encode the same LLM step."""
    from serialization_runtime_streamlit_app import _scorer_invoke_record_preview

    case = next(case for case in CASES if case.key == "llm")
    payload = case_payload(case.key)
    record = _scorer_invoke_record_preview(case, payload)
    # These values are sent in scorer_invoke `inputs.query` and `inputs.response`.
    assert record["type"] == "llm"
    assert record["input"]
    assert record["output"]["role"] == "assistant"
    assert record["output"]["tool_calls"][0]["id"] == payload["tool_calls"][0]["id"]
    assert record["output"]["tool_calls"][0]["function"]["name"] == "submit_transfer"
    assert record["output"]["tool_calls"][0]["function"]["arguments"] == payload["tool_calls"][0]["function"]["arguments"]
    assert record["tools"][0]["function"]["name"] == "submit_transfer"
    assert record["tools"][0]["function"]["parameters"]["properties"] == payload["tools"][0]["function"]["parameters"]["properties"]


def test_local_control_decorator_payload_includes_available_llm_tools() -> None:
    try:
        from agent_control.control_decorators import _create_evaluation_payload
        from agent_control_models import Step
    except ImportError:
        pytest.skip("local Agent Control SDK is not installed")
    if "tools" not in inspect.signature(_create_evaluation_payload).parameters:
        pytest.skip("installed Agent Control SDK predates the tools parameter")

    payload = case_payload("llm")

    async def execute(input):
        return payload["output"]

    serialized = _create_evaluation_payload(
        execute,
        (payload["input"],),
        {},
        output=payload["output"],
        step_name=payload["name"],
        explicit_step_type="llm",
        tools=payload["tools"],
    )
    step = Step.model_validate(serialized)
    assert step.tools == payload["tools"]


def test_all_fixtures_require_deny_and_app_does_not_create_controls() -> None:
    app_path = Path(__file__).parents[1] / "serialization_runtime_streamlit_app.py"
    tree = ast.parse(app_path.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called_names = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
    assert "ensure_demo_controls" not in called_names
    assert "_ensure_control" not in called_names
    assert "_bind_control_to_target" not in called_names
    source = app_path.read_text()
    assert '"expected_result": "deny"' in source
    assert "client.get(" in source
    assert "client.post(" not in source


def test_read_only_preflight_accepts_complete_manual_controls() -> None:
    controls = [
        {
            "name": case.control_name,
            "data": {
                "scope": {
                    "step_types": [case.record_type],
                    "step_name_regex": case.step_name_regex,
                    "stages": [case.stage],
                },
                "condition": {
                    "selector": {"path": case.selector},
                    "evaluator": {
                        "name": "galileo.luna",
                        "config": {
                            "scorer_id": "real-scorer-id",
                            "scorer_version_id": "real-version-id",
                            "scorer_label": case.scorer_label,
                        },
                    },
                },
                "action": {"decision": "deny"},
            },
        }
        for case in CASES
    ]
    _verify_manual_controls(controls)


def test_read_only_preflight_rejects_missing_scorer_version() -> None:
    control = {
        "name": CASES[0].control_name,
        "data": {
            "scope": {
                "step_types": ["trace"],
                "step_name_regex": CASES[0].step_name_regex,
                "stages": ["post"],
            },
            "condition": {
                "selector": {"path": "*"},
                "evaluator": {
                    "name": "galileo.luna",
                    "config": {"scorer_id": "actual", "scorer_label": "output_pii"},
                },
            },
            "action": {"decision": "deny"},
        },
    }
    with pytest.raises(RuntimeError, match="no scorer version ID"):
        _verify_manual_controls([control])
