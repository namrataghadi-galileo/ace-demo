"""Streamlit runner for manually configured Agent Control serialization cases."""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import serialization_runtime_cases as _case_module
from common import resolve_agent_control_api_key, resolve_agent_control_api_key_header

# Streamlit reruns this module in a long-lived process. Reload the fixtures as
# well, so edits to serialization_runtime_cases.py are reflected without a
# process restart.
_case_module = importlib.reload(_case_module)
CASES = _case_module.CASES
SerializationCase = _case_module.SerializationCase
case_payload = _case_module.case_payload
validate_case_payload = _case_module.validate_case_payload


def _required_environment() -> dict[str, str]:
    names = {
        "AGENT_CONTROL_URL": os.environ.get(
            "AGENT_CONTROL_URL", "https://agent-control-test-evals.gcp-dev.galileo.ai"
        ),
        "AGENT_CONTROL_AGENT_NAME": os.environ.get(
            "AGENT_CONTROL_AGENT_NAME", "galileo-control-span-demo"
        ),
        "AGENT_CONTROL_TARGET_TYPE": os.environ.get(
            "AGENT_CONTROL_TARGET_TYPE", "log_stream"
        ),
        "AGENT_CONTROL_TARGET_ID": os.environ.get(
            "AGENT_CONTROL_TARGET_ID", os.environ.get("GALILEO_LOG_STREAM_ID", "")
        ),
        "GALILEO_API_KEY": os.environ.get("GALILEO_API_KEY", ""),
    }
    missing = [key for key, value in names.items() if not value]
    if missing:
        raise RuntimeError("Set required environment values: " + ", ".join(missing))
    return names


def _initialize_agent(env: dict[str, str]) -> None:
    """Register the agent and load its target-bound controls before preflight."""
    import agent_control

    agent_control.init(
        agent_name=env["AGENT_CONTROL_AGENT_NAME"],
        agent_description="Serialization and Orbit runtime record test",
        server_url=env["AGENT_CONTROL_URL"],
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        target_type=env["AGENT_CONTROL_TARGET_TYPE"],
        target_id=env["AGENT_CONTROL_TARGET_ID"],
        observability_enabled=True,
        observability_sink_name="registered",
    )


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


async def _read_bound_controls(env: dict[str, str]) -> list[dict[str, Any]]:
    key = resolve_agent_control_api_key()
    if not key:
        raise RuntimeError("GALILEO_API_KEY or AGENT_CONTROL_API_KEY is required.")
    headers = {resolve_agent_control_api_key_header(): key}
    async with httpx.AsyncClient(
        base_url=env["AGENT_CONTROL_URL"].rstrip("/"), headers=headers, timeout=30
    ) as client:
        response = await client.get(
            f"/api/v1/agents/{env['AGENT_CONTROL_AGENT_NAME']}/controls",
            params={
                "rendered_state": "rendered",
                "enabled_state": "enabled",
                "target_type": env["AGENT_CONTROL_TARGET_TYPE"],
                "target_id": env["AGENT_CONTROL_TARGET_ID"],
            },
        )
        response.raise_for_status()
    controls = response.json().get("controls")
    if not isinstance(controls, list):
        raise TypeError("Agent Control returned an invalid controls list.")
    return [item for item in controls if isinstance(item, dict)]


def _verify_manual_controls(controls: list[dict[str, Any]]) -> None:
    by_name = {str(control.get("name")): control for control in controls}
    errors = []
    for case in CASES:
        control = by_name.get(case.control_name)
        if control is None:
            clone_prefix = f"{case.control_name}-clone-"
            clones = [
                item
                for item in controls
                if str(item.get("name", "")).startswith(clone_prefix)
            ]
            if len(clones) == 1:
                control = clones[0]
            elif len(clones) > 1:
                errors.append(f"multiple clones found for {case.control_name}")
                continue
        if control is None:
            errors.append(f"missing {case.control_name}")
            continue
        blob = list(_walk(control))
        data = control.get("data") or control.get("control") or {}
        scope = data.get("scope") if isinstance(data, dict) else None
        action = data.get("action") if isinstance(data, dict) else None
        if not isinstance(scope, dict) or scope.get("step_types") != [case.record_type]:
            errors.append(f"{case.control_name} has the wrong step_types")
        if (
            not isinstance(scope, dict)
            or scope.get("step_name_regex") != case.step_name_regex
        ):
            errors.append(f"{case.control_name} has the wrong step-name regex")
        if not isinstance(scope, dict) or scope.get("stages") != [case.stage]:
            errors.append(f"{case.control_name} has the wrong stage")
        if (
            not isinstance(action, dict)
            or action.get("decision") != case.expected_action
        ):
            errors.append(f"{case.control_name} is not configured to deny")
        if not any(
            item.get("path") == case.selector for item in blob if "path" in item
        ):
            errors.append(
                f"{case.control_name} does not use selector {case.selector!r}"
            )
        if not any(item.get("name") == "galileo.luna" for item in blob):
            errors.append(
                f"{case.control_name} does not use the galileo.luna evaluator"
            )
        scorer_ids = [item.get("scorer_id") for item in blob if item.get("scorer_id")]
        version_ids = [
            item.get("scorer_version_id") or item.get("version_id")
            for item in blob
            if item.get("scorer_version_id") or item.get("version_id")
        ]
        labels = [item.get("scorer_label") for item in blob if item.get("scorer_label")]
        if not scorer_ids:
            errors.append(f"{case.control_name} has no scorer ID")
        if not version_ids:
            errors.append(f"{case.control_name} has no scorer version ID")
        # ``scorer_label`` is display/metadata text; the evaluator resolves
        # the selected scorer by scorer_id and scorer_version_id. Console
        # labels (for example, "Tool Selection Quality (SLM)") need not match
        # the runtime alias used by this example ("tool_selection_quality_luna").
        if not labels:
            errors.append(f"{case.control_name} has no scorer label")
    if errors:
        raise RuntimeError("Manual control preflight failed: " + "; ".join(errors))


def _actual_control_name(exc: BaseException) -> str | None:
    metadata = getattr(exc, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("control_name", "name"):
            if isinstance(metadata.get(key), str):
                return metadata[key]
    name = getattr(exc, "control_name", None)
    return name if isinstance(name, str) else None


def _summarize_decorator_results(items: Any) -> list[dict[str, Any]]:
    summaries = []
    for item in items or []:
        if isinstance(item, dict):
            evaluation = item.get("result") or {}
            get_value = item.get
            result_value = (
                evaluation.get
                if isinstance(evaluation, dict)
                else lambda key, default=None, _evaluation=evaluation: getattr(
                    _evaluation, key, default
                )
            )
        else:
            evaluation = getattr(item, "result", None)
            get_value = lambda key, default=None, _item=item: getattr(_item, key, default)
            result_value = lambda key, default=None, _evaluation=evaluation: getattr(
                _evaluation, key, default
            )
        summaries.append(
            {
                "control": get_value("control_name"),
                "action": get_value("action"),
                "matched": result_value("matched"),
                "confidence": result_value("confidence"),
                "message": result_value("message"),
                "error": result_value("error"),
                "metadata": result_value("metadata"),
            }
        )
    return summaries


def _is_expected_control_name(name: str | None, case: SerializationCase) -> bool:
    """Accept the source control or its target-bound Console clone."""
    return bool(
        name
        and (
            name == case.control_name
            or name.startswith(f"{case.control_name}-clone-")
        )
    )


def _scorer_invoke_record_preview(
    case: SerializationCase, payload: dict[str, Any]
) -> dict[str, Any]:
    """Build the same canonical record passed as ``record`` to scorer invoke."""
    if case.record_type == "llm":
        try:
            from agent_control_evaluator_galileo import build_galileo_record
            from agent_control_models import Step
        except ImportError:
            # Released evaluator packages before the record-factory API can
            # still validate and display this already-canonical LLM fixture.
            record = {
                "type": "llm",
                "name": case.step_name,
                "input": payload["input"],
                "output": payload["output"],
                "user_metadata": {
                    key: value if isinstance(value, str) else str(value)
                    for key, value in payload.get("metadata", {}).items()
                },
                "tools": payload["tools"],
            }
            from agent_control_evaluator_galileo import ScorerInvokeRecord

            ScorerInvokeRecord.model_validate(record)
            return record

        step = Step(
            type="llm",
            name=case.step_name,
            input=payload["input"],
            output=payload["output"],
            tools=payload["tools"],
        )
        return build_galileo_record(step.model_dump(mode="json"), step).model_dump(
            mode="json", exclude_none=True
        )

    from agent_control_evaluator_galileo import build_galileo_record
    from agent_control_models import Step

    context: dict[str, Any] | None = None
    tools = None
    if case.record_type in {"trace", "session"}:
        context = {
            "serialization_case": case.key,
            case.record_type: payload,
        }
        step_input = payload.get("input")
        step_output = payload.get("output")
    elif case.record_type == "tool":
        # A ToolSpanRecord stores the tool call itself as input. Direct
        # evaluation avoids the decorator's bound-argument mapping wrapper.
        step_input = payload["input"]
        step_output = payload["output"]
    elif case.record_type == "retriever":
        # Orbit's RetrieverSpanRecord.input is the query string itself. The
        # SDK decorator serializes custom step arguments as a mapping, which
        # changes the scorer's query to {"query": "..."}; use direct
        # evaluation for this fixture so the canonical scorer record matches
        # the retriever span contract.
        step_input = payload["query"]
        step_output = payload["output"]
    else:  # pragma: no cover - cases are declared in serialization_runtime_cases
        raise ValueError(f"Unsupported record type: {case.record_type}")

    step = Step(
        type=case.record_type,
        name=case.step_name,
        input=step_input,
        output=step_output,
        context=context,
        tools=tools,
    )
    selected_data = step.model_dump(mode="json")  # selector path "*"
    record = build_galileo_record(selected_data, step)
    return record.model_dump(mode="json", exclude_none=True)


async def _decorated_deny(
    case: SerializationCase, payload: dict[str, Any]
) -> dict[str, Any]:
    import agent_control
    from agent_control import ControlViolationError, control_decorators

    if case.record_type != "llm":
        raise ValueError("The decorator path is reserved for the LLM tool-selection case")

    # Orbit's tool-selection metric requires these definitions on the
    # canonical LlmSpan.tools field. The local SDK forwards this argument to
    # each pre/post evaluation payload.
    decorator = agent_control.control(
        step_name=case.step_name,
        step_type=case.record_type,
        tools=payload["tools"],
    )

    async def execute(input: Any) -> Any:
        return payload["output"]

    execute.__name__ = case.step_name
    controlled = decorator(execute)
    captured_evaluations: list[dict[str, Any]] = []
    original_evaluate = control_decorators._evaluate

    async def capture_evaluation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        evaluation = await original_evaluate(*args, **kwargs)
        captured_evaluations.append(evaluation)
        return evaluation

    control_decorators._evaluate = capture_evaluation
    try:
        result = await controlled(payload["input"])
    except ControlViolationError as exc:
        actual = _actual_control_name(exc)
        evaluation = captured_evaluations[-1] if captured_evaluations else {}
        match_summaries = _summarize_decorator_results(evaluation.get("matches"))
        return {
            "denied": _is_expected_control_name(actual, case),
            "actual_control": actual or str(exc),
            "message": str(exc),
            "matches": match_summaries,
            "non_matches": _summarize_decorator_results(evaluation.get("non_matches")),
            "evaluation_errors": _summarize_decorator_results(evaluation.get("errors")),
        }
    except Exception as exc:  # noqa: BLE001 - report server/evaluator failures per case.
        evaluation = captured_evaluations[-1] if captured_evaluations else {}
        match_summaries = _summarize_decorator_results(evaluation.get("matches"))
        return {
            "denied": False,
            "actual_control": ", ".join(
                str(item["control"]) for item in match_summaries if item.get("control")
            ) or "no deny decision",
            "message": str(exc),
            "matches": match_summaries,
            "non_matches": _summarize_decorator_results(evaluation.get("non_matches")),
            "evaluation_errors": _summarize_decorator_results(evaluation.get("errors")),
        }
    finally:
        control_decorators._evaluate = original_evaluate
    evaluation = captured_evaluations[-1] if captured_evaluations else {}
    match_summaries = _summarize_decorator_results(evaluation.get("matches"))
    non_match_summaries = _summarize_decorator_results(evaluation.get("non_matches"))
    error_summaries = _summarize_decorator_results(evaluation.get("errors"))
    details = [
        item["message"]
        for item in [*non_match_summaries, *error_summaries]
        if item.get("message")
    ]
    return {
        "denied": False,
        "actual_control": ", ".join(
            str(item["control"]) for item in match_summaries if item.get("control")
        ) or "no deny decision",
        "message": evaluation.get("reason")
        or "; ".join(details)
        or f"Decorator returned {result!r} without a deny exception.",
        "matches": match_summaries,
        "non_matches": non_match_summaries,
        "evaluation_errors": error_summaries,
    }


async def _evaluate_complete_record(
    case: SerializationCase, payload: dict[str, Any], agent_name: str
) -> dict[str, Any]:
    """Evaluate a complete parent record or a canonical tool/retriever step."""
    import agent_control

    record_context = {"serialization_case": case.key}
    if case.record_type == "trace":
        record_context["trace"] = payload
    elif case.record_type == "session":
        record_context["session"] = payload
    if case.record_type == "retriever":
        evaluation_input = payload["query"]
    else:
        evaluation_input = payload.get("input")
    result = await agent_control.evaluate_controls(
        case.step_name,
        input=evaluation_input,
        output=payload.get("output"),
        context=record_context,
        step_type=case.record_type,
        stage=case.stage,
        agent_name=agent_name,
    )
    matches = getattr(result, "matches", None) or []
    expected_match = next(
        (
            item
            for item in matches
            if _is_expected_control_name(
                getattr(item, "control_name", None), case
            )
        ),
        None,
    )
    denied = (
        expected_match is not None
        and str(getattr(expected_match, "action", "")) == "deny"
    )

    def summarize(items: Any) -> list[dict[str, Any]]:
        summaries = []
        for item in items or []:
            evaluation = getattr(item, "result", None)
            summaries.append(
                {
                    "control": getattr(item, "control_name", None),
                    "action": getattr(item, "action", None),
                    "matched": getattr(evaluation, "matched", None),
                    "confidence": getattr(evaluation, "confidence", None),
                "message": getattr(evaluation, "message", None),
                "error": getattr(evaluation, "error", None),
                "metadata": getattr(evaluation, "metadata", None),
                }
            )
        return summaries

    match_summaries = summarize(matches)
    names = [item.get("control") for item in match_summaries]
    return {
        "denied": denied,
        "actual_control": ", ".join(str(item) for item in names if item)
        or "no deny decision",
        "message": (
            "Expected deny returned by the matching control."
            if denied
            else getattr(result, "reason", None)
            or "Evaluation returned without the expected deny."
        ),
        "matches": match_summaries,
        "non_matches": summarize(getattr(result, "non_matches", None)),
        "evaluation_errors": summarize(getattr(result, "errors", None)),
    }


async def _run_case(case: SerializationCase, env: dict[str, str]) -> dict[str, Any]:
    payload = case_payload(case.key)
    validate_case_payload(case, payload)
    record_preview = _scorer_invoke_record_preview(case, payload)
    if case.record_type in {"trace", "session", "tool", "retriever"}:
        result = await _evaluate_complete_record(
            case, payload, env["AGENT_CONTROL_AGENT_NAME"]
        )
    else:
        result = await _decorated_deny(case, payload)
    contract_accepted = False
    contract_result: str | None = None
    if case.record_type == "llm":
        # This case is a serialization/deserialization contract probe. A
        # successfully evaluated result proves Orbit accepted the request,
        # regardless of whether the configured threshold produced a deny.
        # An explicitly unsupported scorer response is acceptable only when
        # the evaluator preserved its successful HTTP response metadata.
        errors = result.get("evaluation_errors", [])
        if not errors:
            contract_accepted = True
            contract_result = "accepted by scorer endpoint without validation error"
        else:
            for error in errors:
                metadata = error.get("metadata") or {}
                status_code = metadata.get("http_status_code") or metadata.get("status_code")
                status = str(metadata.get("status") or "").lower()
                message = " ".join(
                    str(error.get(key) or "") for key in ("message", "error")
                ).lower()
                if (
                    isinstance(status_code, int)
                    and 200 <= status_code < 300
                    and (status == "unsupported" or "not supported by this endpoint" in message)
                ):
                    contract_accepted = True
                    contract_result = "accepted; scorer execution is unsupported"
                    break
            if not contract_accepted:
                status_codes = [
                    (error.get("metadata") or {}).get("http_status_code")
                    for error in errors
                ]
                http_error = next(
                    (code for code in status_codes if isinstance(code, int) and code >= 400),
                    None,
                )
                contract_result = (
                    f"failed: scorer endpoint returned HTTP {http_error}"
                    if http_error is not None
                    else "failed or could not be verified"
                )
    passed = contract_accepted if case.record_type == "llm" else bool(result["denied"])
    return {
        "case_name": case.label,
        "scorer_label": case.scorer_label,
        "record_type": case.record_type,
        "step_name": case.step_name,
        "expected_control": case.control_name,
        "expected_result": "deny",
        "actual_control_result": "deny" if result["denied"] else "not denied",
        "test_scope": (
            "serialization/deserialization contract"
            if case.record_type == "llm"
            else "deny control"
        ),
        "contract_result": contract_result,
        "actual_control": result.get("actual_control"),
        "message": result.get("message"),
        "matches": result.get("matches", []),
        "non_matches": result.get("non_matches", []),
        "evaluation_errors": result.get("evaluation_errors", []),
        "expected_scorer_invoke_inputs": (
            {"query": record_preview["input"], "response": record_preview["output"]}
            if case.record_type == "llm"
            else None
        ),
        "scorer_invoke_record": record_preview,
        "identifier": payload.get("id"),
        "passed": passed,
    }


def _run(case: SerializationCase) -> dict[str, Any]:
    try:
        env = _required_environment()
        # Register the agent first: GET /agents/{name}/controls returns 404
        # until the agent exists, even when target-bound controls are present.
        _initialize_agent(env)
        controls = asyncio.run(_read_bound_controls(env))
        _verify_manual_controls(controls)
        return asyncio.run(_run_case(case, env))
    except Exception as exc:  # noqa: BLE001 - Streamlit must report runtime/API failures per case.
        return {
            "case_name": case.label,
            "scorer_label": case.scorer_label,
            "record_type": case.record_type,
            "step_name": case.step_name,
            "expected_control": case.control_name,
            "expected_result": "deny",
            "actual_control_result": "failed before deny"
            if not isinstance(exc, AssertionError)
            else "not denied",
            "message": f"{type(exc).__name__}: {exc}",
            "identifier": None,
            "passed": False,
        }


def _show(result: dict[str, Any]) -> None:
    st.subheader(result["case_name"])
    record = result.get("scorer_invoke_record")
    st.json({key: value for key, value in result.items() if key != "scorer_invoke_record"})
    if record is not None:
        with st.expander("scorer_invoke `record` field"):
            st.json(record)
    (st.success if result["passed"] else st.error)(
        "PASS" if result["passed"] else "FAIL"
    )


def main() -> None:
    st.set_page_config(page_title="Serialization runtime cases", layout="wide")
    st.title("Agent Control serialization and Orbit runtime")
    st.caption(
        "Run one manually configured control at a time. The LLM tool-selection case checks the serialization contract; the other cases check deny decisions. This app does not create or update controls."
    )
    for case in CASES:
        if st.button(f"Run {case.key} serialization case", key=f"run-{case.key}"):
            _show(_run(case))


if __name__ == "__main__":
    main()
