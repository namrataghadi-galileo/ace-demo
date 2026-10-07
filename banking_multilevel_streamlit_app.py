#!/usr/bin/env python3
"""Interactive five-level banking demo for Agent Control and Orbit scorers."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from banking_multilevel_cases import (
    SCENARIOS,
    TOOL_DEFINITIONS,
    BankingSandbox,
    BankingScenario,
    BankingScenarioInput,
    default_scenario_input,
    run_banking_model,
    scenario_record,
    search_policy_documents,
    step_tree,
)
from common import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_LOG_STREAM,
    DEFAULT_PROJECT,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

CONTROL_CATALOG = Path(__file__).with_name("banking_multilevel_controls.json")
DEFAULT_AGENT_NAME = "banking-multilevel-runtime-demo"


def _environment() -> dict[str, str]:
    values = {
        "AGENT_CONTROL_URL": os.environ.get("AGENT_CONTROL_URL", DEFAULT_AGENT_CONTROL_URL),
        "AGENT_CONTROL_AGENT_NAME": os.environ.get(
            "AGENT_CONTROL_AGENT_NAME", DEFAULT_AGENT_NAME
        ),
        "AGENT_CONTROL_TARGET_TYPE": os.environ.get(
            "AGENT_CONTROL_TARGET_TYPE", "log_stream"
        ),
        "AGENT_CONTROL_TARGET_ID": os.environ.get(
            "AGENT_CONTROL_TARGET_ID", os.environ.get("GALILEO_LOG_STREAM_ID", "")
        ),
        "GALILEO_API_KEY": os.environ.get("GALILEO_API_KEY", ""),
    }
    missing = [
        name
        for name in ("GALILEO_API_KEY",)
        if not values[name]
    ]
    if missing:
        raise RuntimeError("Set required environment values: " + ", ".join(missing))
    return values


def _initialize_agent(env: dict[str, str]) -> None:
    import agent_control

    agent_control.init(
        agent_name=env["AGENT_CONTROL_AGENT_NAME"],
        agent_description="Deterministic five-level banking runtime scorer demo",
        server_url=env["AGENT_CONTROL_URL"],
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        target_type=env["AGENT_CONTROL_TARGET_TYPE"],
        target_id=env["AGENT_CONTROL_TARGET_ID"],
        observability_enabled=True,
        observability_sink_name="registered",
    )


async def _read_bound_controls(env: dict[str, str]) -> list[dict[str, Any]]:
    api_key = resolve_agent_control_api_key()
    if not api_key:
        raise RuntimeError("GALILEO_API_KEY or AGENT_CONTROL_API_KEY is required")
    async with httpx.AsyncClient(
        base_url=env["AGENT_CONTROL_URL"].rstrip("/"),
        headers={resolve_agent_control_api_key_header(): api_key},
        timeout=30,
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
        raise TypeError("Agent Control returned an invalid controls list")
    return [control for control in controls if isinstance(control, dict)]


def _nested_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nested_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_dicts(item)


def _control_data(control: dict[str, Any]) -> dict[str, Any]:
    value = control.get("data") or control.get("control") or control.get("definition")
    return value if isinstance(value, dict) else {}


def _scenario_control(
    controls: Iterable[dict[str, Any]], scenario: BankingScenario
) -> dict[str, Any]:
    candidates = [
        control
        for control in controls
        if str(control.get("name")) == scenario.control_name
        or str(control.get("name", "")).startswith(f"{scenario.control_name}-clone-")
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one effective control for {scenario.control_name}, found {len(candidates)}"
        )
    return candidates[0]


def _scorer_key(label: Any) -> str:
    """Normalize Orbit slugs and Agent Control display labels for preflight checks."""
    value = re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
    for suffix in ("_luna", "_slm"):
        if value.endswith(suffix):
            value = value.removesuffix(suffix)
    return value


def _verify_manual_controls(
    controls: list[dict[str, Any]], scenarios: Iterable[BankingScenario] = SCENARIOS
) -> None:
    """Require one correctly scoped, version-pinned control for every scenario."""
    by_name = {str(control.get("name")): control for control in controls}
    failures: list[str] = []
    for scenario in scenarios:
        control = by_name.get(scenario.control_name)
        if control is None:
            clones = [
                item
                for name, item in by_name.items()
                if name.startswith(f"{scenario.control_name}-clone-")
            ]
            if len(clones) == 1:
                control = clones[0]
        if control is None:
            failures.append(f"missing {scenario.control_name}")
            continue
        data = _control_data(control)
        scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        nodes = list(_nested_dicts(data))
        expected_scope = {
            "step_types": [scenario.level],
            "stages": [scenario.stage],
            "step_name_regex": scenario.step_name_regex,
        }
        for key, expected in expected_scope.items():
            if scope.get(key) != expected:
                failures.append(f"{scenario.control_name} has wrong scope.{key}")
        if action.get("decision") not in {"deny", "steer"}:
            failures.append(f"{scenario.control_name} must deny or steer")
        if not any(node.get("name") == "galileo.luna" for node in nodes):
            failures.append(f"{scenario.control_name} does not use galileo.luna")
        if not any(node.get("scorer_id") for node in nodes):
            failures.append(f"{scenario.control_name} has no scorer_id")
        if not any(node.get("scorer_version_id") for node in nodes):
            failures.append(f"{scenario.control_name} has no scorer_version_id")
        if not any(
            _scorer_key(node.get("scorer_label")) == _scorer_key(scenario.scorer_name)
            for node in nodes
        ):
            failures.append(f"{scenario.control_name} has wrong scorer_label")
        if not any(node.get("path") == scenario.payload_field for node in nodes):
            failures.append(f"{scenario.control_name} has wrong payload field")
    if failures:
        raise RuntimeError("Manual control preflight failed: " + "; ".join(failures))


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _summarize_matches(items: Any) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in items or []:
        result = _value(item, "result", {})
        summaries.append(
            {
                "control": _value(item, "control_name"),
                "action": _value(item, "action"),
                "matched": _value(result, "matched"),
                "confidence": _value(result, "confidence"),
                "message": _value(result, "message"),
                "error": _value(result, "error"),
                "metadata": _value(result, "metadata"),
            }
        )
    return summaries


def _matches_control(name: Any, scenario: BankingScenario) -> bool:
    return isinstance(name, str) and (
        name == scenario.control_name or name.startswith(f"{scenario.control_name}-clone-")
    )


def build_agent_control_step(record: dict[str, Any]):
    """Recursively translate completed function executions into Agent Control Steps."""
    from agent_control_models import Step

    record_type = str(record["type"])
    input_value = (
        record.get("input", record.get("query"))
        if record_type == "retriever"
        else record.get("input")
    )
    child_records = record.get("traces") if record_type == "session" else record.get("spans")
    children = (
        [build_agent_control_step(child) for child in child_records]
        if isinstance(child_records, list)
        else None
    )
    metadata = record.get("metadata")
    # The Galileo record factory reads user metadata from this canonical key.
    context = {"metadata": metadata} if isinstance(metadata, dict) else None
    return Step(
        type=record_type,
        name=str(record["name"]),
        input=input_value,
        output=record.get("output"),
        context=context,
        tools=record.get("tools") if record_type == "llm" else None,
        children=children,
    )


def canonical_record_preview(scenario: BankingScenario, record: dict[str, Any]) -> dict[str, Any]:
    """Build the canonical Galileo record that the local evaluator sends to Orbit."""
    from agent_control_evaluator_galileo import build_galileo_record
    step = build_agent_control_step(record)
    canonical = build_galileo_record(step.model_dump(mode="json"), step)
    return canonical.model_dump(mode="json", exclude_none=True)


def _json_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def scorer_invoke_request_preview(
    scenario: BankingScenario,
    record: dict[str, Any],
    control: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    """Reconstruct the scorer-invoke body created by the deployed Galileo evaluator.

    Authentication identity is injected inside Agent Control and is intentionally
    unavailable to the browser app, so those two values are visibly redacted.
    """
    step = build_agent_control_step(record)
    canonical = canonical_record_preview(scenario, record)
    data = _control_data(control)
    evaluator = next(
        (
            node.get("evaluator")
            for node in _nested_dicts(data)
            if isinstance(node.get("evaluator"), dict)
            and node["evaluator"].get("name") == "galileo.luna"
        ),
        None,
    )
    if not isinstance(evaluator, dict):
        raise TypeError(f"{scenario.control_name} has no galileo.luna evaluator")
    config = evaluator.get("config")
    if not isinstance(config, dict):
        raise TypeError(f"{scenario.control_name} has no Galileo evaluator config")

    inputs: dict[str, Any] = {
        "query": _json_text(step.input),
        "response": _json_text(step.output),
    }
    if step.ground_truth is not None:
        inputs["ground_truth"] = step.ground_truth
    if step.tools is not None:
        inputs["tools"] = step.tools

    request_timeout = float(config.get("timeout_ms", 10000)) / 1000
    invoke_config = dict(config.get("config") or {})
    invoke_config.setdefault("request_timeout_seconds", request_timeout * 0.8)
    project_id = config.get("project_id")
    if isinstance(project_id, list):
        project_id = project_id[0] if project_id else None

    body: dict[str, Any] = {
        "scorer_id": config.get("scorer_id"),
        "scorer_version_id": config.get("scorer_version_id"),
        "scorer_label": config.get("scorer_label"),
        "inputs": inputs,
        "record": canonical,
        "execution_context": {
            "organization_id": "<injected by Agent Control>",
            "user_id": "<injected by Agent Control>",
            "project_id": project_id or "<resolved from target>",
            "run_id": env["AGENT_CONTROL_TARGET_ID"],
        },
        "config": invoke_config,
    }
    return {
        "method": "POST",
        "endpoint_path": "/api/v1/scorers/invoke",
        "selector_path": scenario.payload_field,
        "body": body,
        "note": (
            "This is the body the deployed evaluator is expected to send after record "
            "construction succeeds. Authentication identity is added server-side and is "
            "redacted here."
        ),
    }


def _scorer_response_from_evaluation(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    response_keys = (
        "scorer_label",
        "score",
        "status",
        "execution_time_seconds",
        "error_message",
        "requested_scorer_version_id",
        "http_method",
        "http_endpoint_path",
        "http_status_code",
        "http_response_content_type",
        "http_response_body",
        "http_response_body_truncated",
    )
    error = item.get("error")
    has_http_evidence = any(key.startswith("http_") for key in metadata)
    transport_status = (
        "HTTP response received"
        if has_http_evidence or "status" in metadata
        else "No scorer HTTP response; evaluator failed before or during transport"
        if error
        else "No scorer transport evidence was propagated"
    )
    return {
        "control": item.get("control"),
        "matched": item.get("matched"),
        "confidence": item.get("confidence"),
        "message": item.get("message"),
        "error": error,
        "transport_status": transport_status,
        "scorer_response": {key: metadata.get(key) for key in response_keys if key in metadata},
        "note": "These are the scorer response fields propagated by the Galileo evaluator.",
    }


def _dependency_sources() -> dict[str, str]:
    import agent_control
    import agent_control_evaluator_galileo
    import agent_control_models

    return {
        "agent_control": str(Path(agent_control.__file__).resolve()),
        "agent_control_evaluator_galileo": str(
            Path(agent_control_evaluator_galileo.__file__).resolve()
        ),
        "agent_control_models": str(Path(agent_control_models.__file__).resolve()),
    }


def _json_text_for_log(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _log_metadata(value: Any) -> dict[str, str | bool | int | float | None] | None:
    if not isinstance(value, dict):
        return None
    return {
        str(key): item
        if item is None or isinstance(item, str | bool | int | float)
        else _json_text_for_log(item)
        for key, item in value.items()
    }


def _emit_record_spans(logger: Any, record: dict[str, Any]) -> None:
    """Emit executed record leaves beneath the active Galileo workflow."""
    record_type = str(record.get("type"))
    metadata = _log_metadata(record.get("metadata"))
    if record_type == "session":
        for trace in record.get("traces", []) or []:
            if not isinstance(trace, dict):
                continue
            logger.add_workflow_span(
                input=_json_text_for_log(trace.get("input")),
                name=str(trace.get("name") or "session-turn"),
                metadata=_log_metadata(trace.get("metadata")),
            )
            _emit_record_spans(logger, trace)
            logger.conclude(output=_json_text_for_log(trace.get("output")))
        return
    if record_type == "trace":
        for span in record.get("spans", []) or []:
            if isinstance(span, dict):
                _emit_record_spans(logger, span)
        return
    if record_type == "llm":
        logger.add_llm_span(
            input=record.get("input", ""),
            output=record.get("output", ""),
            model=str((record.get("metadata") or {}).get("model") or "banking-demo"),
            tools=record.get("tools"),
            name=str(record.get("name") or "llm"),
            metadata=metadata,
        )
        return
    if record_type == "tool":
        logger.add_tool_span(
            input=_json_text_for_log(record.get("input")),
            output=_json_text_for_log(record.get("output")),
            name=str(record.get("name") or "tool"),
            metadata=metadata,
        )
        return
    if record_type == "retriever":
        retriever_input = record.get("query", record.get("input"))
        logger.add_retriever_span(
            input=_json_text_for_log(retriever_input),
            output=record.get("output"),
            name=str(record.get("name") or "retriever"),
            metadata=metadata,
        )


def _log_stream_url(*, project_id: str, log_stream_id: str) -> str | None:
    console_url = os.environ.get("GALILEO_CONSOLE_URL", DEFAULT_CONSOLE_URL).rstrip("/")
    hostname = urlparse(console_url).hostname or ""
    if not hostname.startswith("console-"):
        return None
    organization = hostname.removeprefix("console-").split(".gcp-dev.", 1)[0]
    if not organization:
        return None
    return (
        f"{console_url}/{organization}/project/{project_id}"
        f"/agent-streams/{log_stream_id}"
    )


async def _evaluate_parent_scenario(
    scenario: BankingScenario,
    record: dict[str, Any],
    env: dict[str, str],
    *,
    trace_id: str,
    span_id: str,
) -> dict[str, Any]:
    """Evaluate a parent record and attach its events to the active Galileo span."""
    import agent_control

    if scenario.level not in {"trace", "session"}:
        raise ValueError("Only trace and session parents use evaluate_controls()")

    step = build_agent_control_step(record)
    result = await agent_control.evaluate_controls(
        scenario.step_name,
        input=step.input,
        output=step.output,
        context=step.context,
        tools=step.tools,
        children=step.children,
        step_type=scenario.level,
        stage=scenario.stage,
        agent_name=env["AGENT_CONTROL_AGENT_NAME"],
        # The Galileo bridge deliberately drops events whose context does not
        # match its active hierarchy. Pass the concrete UUIDs instead of
        # relying on implicit context discovery for parent-level evaluations.
        trace_id=trace_id,
        span_id=span_id,
    )
    matches = _summarize_matches(_value(result, "matches", []))
    non_matches = _summarize_matches(_value(result, "non_matches", []))
    errors = _summarize_matches(_value(result, "errors", []))
    expected = next(
        (item for item in matches if _matches_control(item.get("control"), scenario)),
        None,
    )
    evaluated = next(
        (
            item
            for item in (*matches, *non_matches, *errors)
            if _matches_control(item.get("control"), scenario)
        ),
        None,
    )
    action = expected.get("action") if expected else None
    outcome = "denied" if action == "deny" else "steered" if action == "steer" else "allowed"
    steering = (expected.get("metadata") or {}).get("steering_context") if expected else None
    raw_response = (
        result
        if isinstance(result, dict)
        else result.model_dump(mode="json", exclude_none=True)
    )
    return {
        "decision": action or "allow",
        "outcome": outcome,
        "expected_control_matched": expected is not None,
        "evaluator_result": evaluated,
        "scorer_invoke_response": _scorer_response_from_evaluation(evaluated),
        "agent_control_response": raw_response,
        "steering_context": steering,
        "matches": matches,
        "non_matches": non_matches,
        "errors": errors,
        "reason": _value(result, "reason"),
        "evaluation_path": "evaluate_controls",
    }


def _decorator_result(
    scenario: BankingScenario,
    *,
    output: Any,
    violation: Exception | None,
) -> dict[str, Any]:
    """Present the enforcement result exposed by the @control decorator."""
    if violation is None:
        return {
            "decision": "allow",
            "outcome": "allowed",
            "expected_control_matched": False,
            "evaluator_result": None,
            "scorer_invoke_response": None,
            "agent_control_response": None,
            "steering_context": None,
            "matches": [],
            "non_matches": [],
            "errors": [],
            "reason": "Decorated function returned without a blocking action.",
            "function_output": output,
            "evaluation_path": "@control",
        }

    control_name = getattr(violation, "control_name", None)
    metadata = getattr(violation, "metadata", {}) or {}
    message = getattr(violation, "message", str(violation))
    action = "steer" if violation.__class__.__name__ == "ControlSteerError" else "deny"
    item = {
        "control": control_name,
        "action": action,
        "matched": True,
        "confidence": metadata.get("confidence"),
        "message": message,
        "error": None,
        "metadata": metadata,
    }
    return {
        "decision": action,
        "outcome": "steered" if action == "steer" else "denied",
        "expected_control_matched": _matches_control(control_name, scenario),
        "evaluator_result": item,
        "scorer_invoke_response": _scorer_response_from_evaluation(item),
        "agent_control_response": None,
        "steering_context": getattr(violation, "steering_context", None),
        "matches": [item],
        "non_matches": [],
        "errors": [],
        "reason": message,
        "function_output": output,
        "evaluation_path": "@control",
    }


def _tool_output_for_scorer(output: Any) -> Any:
    """Render a failed sandbox result in the form used by Tool Error Rate fixtures."""
    if not isinstance(output, dict) or output.get("status") != "error":
        return output
    error_code = output.get("error_code", "UNKNOWN_TOOL_ERROR")
    message = output.get("message", "The tool call failed.")
    return f"Error: {error_code} - {message}"


async def _run_decorated_span_scenario(
    scenario: BankingScenario,
    inputs: BankingScenarioInput | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a real leaf function protected by @control and capture its Step shape."""
    import agent_control
    from agent_control import ControlSteerError, ControlViolationError

    inputs = inputs or default_scenario_input(scenario)
    captured: dict[str, Any] = {}

    if scenario.level == "llm":
        messages = [
            {
                "role": "system",
                "content": "Help with banking requests without exposing private data.",
            },
            {"role": "user", "content": inputs.request},
        ]

        @agent_control.control(
            step_name=scenario.step_name,
            step_type="llm",
            tools=TOOL_DEFINITIONS,
        )
        async def invoke_model(input: list[dict[str, Any]]) -> dict[str, Any]:
            output = run_banking_model(
                input[-1]["content"],
                behavior="wrong_tool",
                context={"account_id": inputs.account_id},
            )
            captured["output"] = output
            return output

        call_input: Any = messages
        protected_function = invoke_model
        call_args = (messages,)
        call_kwargs: dict[str, Any] = {}
        metadata = {
            "executed": True,
            "executed_function": "run_banking_model",
            "model": "deterministic-local-banking-model",
            "behavior": "wrong_tool",
            "external_call": False,
            "evaluation_path": "@control",
        }
        tools = TOOL_DEFINITIONS
    elif scenario.level == "tool":
        sandbox = BankingSandbox()
        tool_input = {
            "account_id": inputs.account_id,
            "amount": inputs.amount,
            "recipient": inputs.recipient,
            "manager_approved": inputs.manager_approved,
            "bypass_approval": inputs.bypass_approval,
        }

        @agent_control.control(step_name=scenario.step_name, step_type="tool")
        async def submit_transfer(
            account_id: str,
            amount: float,
            recipient: str,
            manager_approved: bool,
            bypass_approval: bool,
        ) -> dict[str, Any]:
            sandbox_output = sandbox.submit_transfer(
                account_id=account_id,
                amount=amount,
                recipient=recipient,
                manager_approved=manager_approved,
                bypass_approval=bypass_approval,
            )
            captured["raw_sandbox_output"] = sandbox_output
            output = _tool_output_for_scorer(sandbox_output)
            captured["output"] = output
            return output

        call_input = tool_input
        protected_function = submit_transfer
        call_args = ()
        call_kwargs = tool_input
        metadata = {
            "executed": True,
            "executed_function": "BankingSandbox.submit_transfer",
            "sandbox_transaction_count": 0,
            "external_call": False,
            "evaluation_path": "@control",
        }
        tools = None
    elif scenario.level == "retriever":
        partitions = list(inputs.retriever_partitions)
        limit = inputs.retriever_limit

        @agent_control.control(step_name=scenario.step_name, step_type="retriever")
        async def retrieve_policy(
            query: str, partitions: list[str], limit: int
        ) -> list[dict[str, Any]]:
            output = search_policy_documents(
                query, partitions=partitions, limit=limit
            )
            captured["output"] = output
            return output

        # Custom step types intentionally receive the complete bound argument map.
        call_input = {
            "query": inputs.request,
            "partitions": list(partitions),
            "limit": limit,
        }
        protected_function = retrieve_policy
        call_args = (inputs.request, partitions, limit)
        call_kwargs = {}
        metadata = {
            "executed": True,
            "executed_function": "search_policy_documents",
            "partitions": list(partitions),
            "external_call": False,
            "evaluation_path": "@control",
        }
        tools = None
    else:
        raise ValueError(f"{scenario.level} is not a decorator-level scenario")

    violation: Exception | None = None
    returned_output: Any = None
    try:
        returned_output = await protected_function(*call_args, **call_kwargs)
    except (ControlViolationError, ControlSteerError) as exc:
        violation = exc

    output = captured.get("output", returned_output)
    # Keep this identical to the Step built by @control. Execution evidence is
    # displayed separately because the decorator does not add demo metadata.
    record: dict[str, Any] = {
        "type": scenario.level,
        "name": scenario.step_name,
        "input": call_input,
        "output": output,
    }
    if tools is not None:
        record["tools"] = tools
    evaluation = _decorator_result(
        scenario,
        output=output,
        violation=violation,
    )
    if scenario.level == "tool":
        metadata["raw_sandbox_output"] = captured.get("raw_sandbox_output")
    evaluation["execution_evidence"] = metadata
    return record, evaluation


async def _run_async(
    scenario: BankingScenario, inputs: BankingScenarioInput | None = None
) -> dict[str, Any]:
    from galileo.logger.logger import GalileoLogger

    inputs = inputs or default_scenario_input(scenario)
    if not inputs.request.strip():
        raise ValueError("Request cannot be empty")
    env = _environment()
    logger = GalileoLogger(
        project=os.environ.get("GALILEO_PROJECT", DEFAULT_PROJECT),
        log_stream=os.environ.get("GALILEO_LOG_STREAM", DEFAULT_LOG_STREAM),
        mode="batch",
    )
    # Galileo normally enables this bridge automatically. Register explicitly
    # so the example guarantees Agent Control events become typed control spans.
    logger.enable_agent_control()
    import agent_control

    registered_sink_count = len(agent_control.get_registered_control_event_sinks())
    if registered_sink_count == 0:
        logger.terminate()
        raise RuntimeError("Galileo did not register an Agent Control control-event sink")
    session_id = logger.start_session(
        name="banking-multilevel-streamlit",
        external_id=f"banking-multilevel-{uuid4()}",
        metadata={"scenario": scenario.key, "record_level": scenario.level},
    )
    if logger.project_id is None or logger.log_stream_id is None:
        logger.terminate()
        raise RuntimeError("Galileo logger did not resolve project/log stream IDs")

    # The Galileo logger is the source of truth for the stream receiving both
    # application records and Agent Control control spans.
    env["AGENT_CONTROL_TARGET_ID"] = logger.log_stream_id
    os.environ["GALILEO_PROJECT_ID"] = logger.project_id
    os.environ["GALILEO_LOG_STREAM_ID"] = logger.log_stream_id
    os.environ["AGENT_CONTROL_TARGET_ID"] = logger.log_stream_id

    trace_input = asdict(inputs)
    trace = None
    completed = False
    try:
        _initialize_agent(env)
        controls = await _read_bound_controls(env)
        _verify_manual_controls(controls, (scenario,))
        control = _scenario_control(controls, scenario)

        trace = logger.start_trace(
            input=trace_input,
            name=f"{scenario.step_name}_run",
            metadata={"scenario": scenario.key, "record_level": scenario.level},
        )
        workflow = logger.add_workflow_span(
            input=_json_text_for_log(trace_input),
            name=scenario.step_name,
            metadata={
                "evaluation_path": (
                    "evaluate_controls"
                    if scenario.level in {"trace", "session"}
                    else "@control"
                )
            },
        )

        if scenario.level in {"trace", "session"}:
            record = scenario_record(scenario.key, inputs)
            evaluation = await _evaluate_parent_scenario(
                scenario,
                record,
                env,
                trace_id=str(trace.id),
                span_id=str(workflow.id),
            )
        else:
            record, evaluation = await _run_decorated_span_scenario(scenario, inputs)

        _emit_record_spans(logger, record)
        logger.conclude(
            output=_json_text_for_log(record.get("output")),
            conclude_all=True,
        )
        logger.flush()
        completed = True

        step = build_agent_control_step(record)
        control_span_count = sum(
            getattr(span, "type", None) == "control"
            for span in getattr(workflow, "spans", [])
        )
        return {
            "scenario": scenario.key,
            "selected_scenario": scenario.label,
            "request": inputs.request,
            "record_level": scenario.level,
            "step_name": scenario.step_name,
            "step_name_regex": scenario.step_name_regex,
            "expected_control": scenario.control_name,
            "expected_action": scenario.action,
            "record": record,
            "agent_control_step": step.model_dump(mode="json", exclude_none=True),
            "canonical_record": canonical_record_preview(scenario, record),
            "scorer_invoke_request": scorer_invoke_request_preview(
                scenario, record, control, env
            ),
            "dependency_sources": _dependency_sources(),
            "step_tree": step_tree(record),
            "project_id": logger.project_id,
            "log_stream_id": logger.log_stream_id,
            "session_id": session_id,
            "trace_id": str(trace.id),
            "workflow_span_id": str(workflow.id),
            "control_span_count": control_span_count,
            "registered_control_sinks": registered_sink_count,
            "log_stream_url": _log_stream_url(
                project_id=logger.project_id,
                log_stream_id=logger.log_stream_id,
            ),
            **evaluation,
        }
    except Exception as exc:
        if trace is not None and not completed:
            logger.conclude(
                output=f"{type(exc).__name__}: {exc}",
                status_code=2,
                conclude_all=True,
            )
            logger.flush()
        raise
    finally:
        logger.terminate()


def run_scenario(
    scenario: BankingScenario, inputs: BankingScenarioInput | None = None
) -> dict[str, Any]:
    inputs = inputs or default_scenario_input(scenario)
    try:
        return asyncio.run(_run_async(scenario, inputs))
    except Exception as exc:  # noqa: BLE001 - render actionable E2E failures in Streamlit.
        record = scenario_record(scenario.key, inputs)
        return {
            "scenario": scenario.key,
            "selected_scenario": scenario.label,
            "request": inputs.request,
            "record_level": scenario.level,
            "step_name": scenario.step_name,
            "step_name_regex": scenario.step_name_regex,
            "expected_control": scenario.control_name,
            "expected_action": scenario.action,
            "record": record,
            "step_tree": step_tree(record),
            "decision": "error",
            "outcome": "evaluation failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _render_result(result: dict[str, Any]) -> None:
    st.subheader(result["selected_scenario"])
    summary_columns = st.columns(4)
    summary_columns[0].metric("Record level", result["record_level"])
    summary_columns[1].metric("Control decision", result["decision"])
    summary_columns[2].metric("Outcome", result["outcome"])
    summary_columns[3].metric("Step count", len(result["step_tree"]))
    if result.get("trace_id"):
        st.success("The application trace and spans were flushed to Galileo.")
        telemetry_columns = st.columns(4)
        telemetry_columns[0].metric("Project", str(result["project_id"])[:8])
        telemetry_columns[1].metric("Log stream", str(result["log_stream_id"])[:8])
        telemetry_columns[2].metric("Trace", str(result["trace_id"])[:8])
        telemetry_columns[3].metric("Control spans", result.get("control_span_count", 0))
        st.caption(
            "Agent Control registered sinks: "
            f"{result.get('registered_control_sinks', 0)}"
        )
        if result.get("control_span_count", 0) == 0:
            st.error(
                "The application trace succeeded, but no Agent Control event was "
                "attached. The trace's success status describes application "
                "execution; it does not mean control telemetry was emitted."
            )
        else:
            st.success("Agent Control telemetry is attached to this trace.")
        if result.get("log_stream_url"):
            st.markdown(f"[Open this Galileo log stream]({result['log_stream_url']})")
    st.markdown("**Request**")
    st.code(result["request"], language=None)
    st.markdown("**Step tree**")
    st.dataframe(result["step_tree"], use_container_width=True, hide_index=True)
    st.markdown("**Evaluator and control result**")
    st.json(
        {
            key: result.get(key)
            for key in (
                "scenario",
                "record_level",
                "evaluation_path",
                "execution_evidence",
                "project_id",
                "log_stream_id",
                "session_id",
                "trace_id",
                "workflow_span_id",
                "control_span_count",
                "registered_control_sinks",
                "step_name",
                "step_name_regex",
                "expected_control",
                "expected_control_matched",
                "decision",
                "outcome",
                "evaluator_result",
                "steering_context",
                "matches",
                "non_matches",
                "errors",
                "reason",
                "error",
            )
            if key in result
        }
    )
    with st.expander("Generated Agent Control record", expanded=True):
        st.json(result["record"])
    if "agent_control_step" in result:
        with st.expander("Agent Control Step sent for evaluation", expanded=True):
            st.json(result["agent_control_step"])
    if "scorer_invoke_request" in result:
        with st.expander("Reconstructed POST /api/v1/scorers/invoke request", expanded=True):
            st.json(result["scorer_invoke_request"])
    if result.get("scorer_invoke_response") is not None:
        with st.expander("Scorer invoke response", expanded=True):
            st.json(result["scorer_invoke_response"])
    if result.get("agent_control_response") is not None:
        with st.expander("Raw Agent Control evaluation response"):
            st.json(result["agent_control_response"])
    if "canonical_record" in result:
        with st.expander("Canonical Galileo record sent to the scorer"):
            st.json(result["canonical_record"])
    if "dependency_sources" in result:
        with st.expander("Loaded Agent Control package paths"):
            st.json(result["dependency_sources"])


def _interactive_inputs(scenario: BankingScenario) -> BankingScenarioInput:
    """Render scenario-specific controls and return executable banking inputs."""
    defaults = default_scenario_input(scenario)
    request = st.text_area(
        "Banking request",
        value=defaults.request,
        height=100,
        key=f"request::{scenario.key}",
    )

    account_id = defaults.account_id
    amount = defaults.amount
    recipient = defaults.recipient
    recipient_email = defaults.recipient_email
    recipient_ssn = defaults.recipient_ssn
    policy_query = defaults.policy_query
    retry_request = defaults.retry_request
    manager_approved = defaults.manager_approved
    bypass_approval = defaults.bypass_approval
    retry_bypass_approval = defaults.retry_bypass_approval
    retriever_partitions = defaults.retriever_partitions
    retriever_limit = defaults.retriever_limit

    if scenario.level in {"trace", "session", "llm", "tool"}:
        account_id = st.text_input(
            "Account ID",
            value=defaults.account_id,
            key=f"account::{scenario.key}",
        )
    if scenario.level in {"trace", "session", "tool"}:
        transfer_col, recipient_col = st.columns(2)
        amount = transfer_col.number_input(
            "Transfer amount",
            min_value=0.01,
            value=defaults.amount,
            step=25.0,
            key=f"amount::{scenario.key}",
        )
        recipient = recipient_col.text_input(
            "Recipient",
            value=defaults.recipient,
            key=f"recipient::{scenario.key}",
        )

    if scenario.level == "trace":
        email_col, ssn_col = st.columns(2)
        recipient_email = email_col.text_input(
            "Recipient email included in final response",
            value=defaults.recipient_email,
            key="trace-recipient-email",
        )
        recipient_ssn = ssn_col.text_input(
            "Recipient SSN included in final response",
            value=defaults.recipient_ssn,
            key="trace-recipient-ssn",
        )
        policy_query = st.text_input(
            "Child retriever query",
            value=defaults.policy_query,
            key="trace-policy-query",
        )
    elif scenario.level == "session":
        retry_request = st.text_area(
            "Second-turn retry request",
            value=defaults.retry_request,
            height=80,
            key="session-retry-request",
        )
        approval_col, first_col, retry_col = st.columns(3)
        manager_approved = approval_col.checkbox(
            "Manager approved",
            value=defaults.manager_approved,
            key="session-manager-approved",
        )
        bypass_approval = first_col.checkbox(
            "Bypass on first attempt",
            value=defaults.bypass_approval,
            key="session-first-bypass",
        )
        retry_bypass_approval = retry_col.checkbox(
            "Bypass on retry",
            value=defaults.retry_bypass_approval,
            key="session-retry-bypass",
        )
    elif scenario.level == "tool":
        approval_col, bypass_col = st.columns(2)
        manager_approved = approval_col.checkbox(
            "Manager approved",
            value=defaults.manager_approved,
            key="tool-manager-approved",
        )
        bypass_approval = bypass_col.checkbox(
            "Bypass approval",
            value=defaults.bypass_approval,
            key="tool-bypass-approval",
        )
    elif scenario.level == "retriever":
        partition_col, limit_col = st.columns(2)
        selected_partitions = partition_col.multiselect(
            "Policy partitions",
            options=["wire", "rewards"],
            default=list(defaults.retriever_partitions),
            key="retriever-partitions",
        )
        retriever_partitions = tuple(selected_partitions)
        max_documents = 3
        retriever_limit = limit_col.slider(
            "Maximum documents",
            min_value=1,
            max_value=max_documents,
            value=min(defaults.retriever_limit, max_documents),
            key="retriever-limit",
        )

    return BankingScenarioInput(
        request=request,
        account_id=account_id,
        amount=float(amount),
        recipient=recipient,
        recipient_email=recipient_email,
        recipient_ssn=recipient_ssn,
        policy_query=policy_query,
        retry_request=retry_request,
        manager_approved=manager_approved,
        bypass_approval=bypass_approval,
        retry_bypass_approval=retry_bypass_approval,
        retriever_partitions=retriever_partitions,
        retriever_limit=retriever_limit,
    )


def main() -> None:
    st.set_page_config(page_title="Banking Agent: five evaluation levels", layout="wide")
    st.title("Banking Agent · Agent Control + Orbit runtime")
    st.caption(
        "Executable banking workflows with independent trace, session, LLM, tool, and "
        "retriever scorer scenarios. Functions run against a safe in-memory sandbox."
    )
    selected_label = st.selectbox("Scenario", [scenario.label for scenario in SCENARIOS])
    scenario = next(item for item in SCENARIOS if item.label == selected_label)
    st.info(f"{scenario.description} Target: `{scenario.level}` / `{scenario.step_name}`")
    with st.form(key=f"inputs::{scenario.key}"):
        inputs = _interactive_inputs(scenario)
        submitted = st.form_submit_button(
            f"Run {scenario.level} scenario",
            type="primary",
            use_container_width=True,
        )
    if submitted:
        if not inputs.request.strip():
            st.error("Enter a banking request before running the scenario.")
            return
        if scenario.level == "retriever" and not inputs.retriever_partitions:
            st.error("Select at least one retriever partition.")
            return
        with st.spinner("Running the workflow and evaluating its completed steps..."):
            st.session_state["banking_multilevel_result"] = run_scenario(
                scenario, inputs
            )
    result = st.session_state.get("banking_multilevel_result")
    if result is not None and result.get("scenario") == scenario.key:
        _render_result(result)


if __name__ == "__main__":
    main()
