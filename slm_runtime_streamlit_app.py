#!/usr/bin/env python3
"""Streamlit E2E app for Agent Control + direct preset-SLM runtime scoring."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from importlib.metadata import PackageNotFoundError, version
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from common import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_AGENT_NAME,
    DEFAULT_API_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_LOG_STREAM,
    DEFAULT_PROJECT,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)
from slm_runtime_controls import (
    ALL_RUNTIME_SCORERS,
    LLM_BLOCK_CASES,
    LLM_TOOL_RUNTIME_SCORERS,
    SLM_RUNTIME_SCORERS,
    SLMRuntimeScorer,
    TOOL_RUNTIME_SCORERS,
    control_specs,
    load_scorer_ids,
    missing_scorer_ids,
)


AGENT_CONTROL_PACKAGES = (
    "agent-control-sdk",
    "agent-control-evaluators",
    "agent-control-evaluator-galileo",
)


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        agent_name=os.environ.get("AGENT_CONTROL_AGENT_NAME", "galileo-slm-runtime-e2e"),
        server_url=os.environ.get("AGENT_CONTROL_URL", DEFAULT_AGENT_CONTROL_URL),
        project=os.environ.get("GALILEO_PROJECT", DEFAULT_PROJECT),
        log_stream=os.environ.get("GALILEO_LOG_STREAM", DEFAULT_LOG_STREAM),
        console_url=os.environ.get("GALILEO_CONSOLE_URL", DEFAULT_CONSOLE_URL),
        api_base_url=os.environ.get("GALILEO_API_URL", DEFAULT_API_URL),
        target_type=os.environ.get("AGENT_CONTROL_TARGET_TYPE", "log_stream"),
        expected_agent_control_version=os.environ.get("AGENT_CONTROL_EXPECTED_VERSION", "8.7.0"),
        scorer_ids_file=os.environ.get("GALILEO_SLM_SCORER_IDS_FILE", ""),
    )


def _configure_environment(args: SimpleNamespace) -> None:
    os.environ["GALILEO_CONSOLE_URL"] = args.console_url
    os.environ["GALILEO_API_URL"] = args.api_base_url
    os.environ["GALILEO_PROJECT"] = args.project
    os.environ["GALILEO_LOG_STREAM"] = args.log_stream
    os.environ["AGENT_CONTROL_URL"] = args.server_url
    resolve_agent_control_api_key()
    resolve_agent_control_api_key_header()


def _package_versions(expected: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for package in AGENT_CONTROL_PACKAGES:
        try:
            installed = version(package)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"Required package {package} is not installed.") from exc
        if expected and installed != expected:
            raise RuntimeError(f"{package}={installed}; expected {expected}.")
        result[package] = installed
    return result


def _walk_key_values(value: Any, key: str):
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                yield current_value
            yield from _walk_key_values(current_value, key)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_key_values(item, key)


def _feature_enabled(payload: Any, feature_name: str) -> bool | None:
    values = list(_walk_key_values(payload, feature_name))
    if not values:
        return None
    for value in values:
        if value is True or (isinstance(value, str) and value.lower() in {"enabled", "true", "on"}):
            return True
        if value is False or (isinstance(value, str) and value.lower() in {"disabled", "false", "off"}):
            return False
    return None


async def _runtime_preflight(args: SimpleNamespace) -> dict[str, Any]:
    """Verify the console flag and Agent Control service before making scorer calls."""

    async with httpx.AsyncClient(timeout=30.0) as client:
        config_response = await client.get(f"{args.console_url.rstrip('/')}/api/config")
        config_response.raise_for_status()
        config = config_response.json()
        runtime_flag = _feature_enabled(config, "scorer_invoke_runtime")

        health_response = await client.get(f"{args.server_url.rstrip('/')}/health")
        health_response.raise_for_status()
        health = health_response.json()

    if runtime_flag is not True:
        raise RuntimeError(
            "Console feature flag scorer_invoke_runtime is not enabled. "
            f"Observed value: {runtime_flag!r}. This test must stop instead of accepting Protect fallback."
        )
    expected_version = args.expected_agent_control_version.strip()
    remote_version = str(health.get("version", ""))
    if expected_version and remote_version and remote_version != expected_version:
        raise RuntimeError(
            f"Agent Control service is {remote_version}; expected {expected_version}. "
            "Set AGENT_CONTROL_EXPECTED_VERSION only when the deployed branch uses another compatible version."
        )
    return {"runtime_flag": runtime_flag, "agent_control_health": health}


def _probe_payload(spec: SLMRuntimeScorer, *, probe_id: str) -> dict[str, Any]:
    if spec.record_type == "tool":
        tool_input = {
            "account_id": "acct-4412",
            "amount": 2500,
            "destination_country": "United Kingdom",
        }
        tool_output = {
            "status": "error",
            "transaction_id": "txn-runtime-probe",
            "message": spec.sample,
        }
        return {
            "inputs": {"query": "", "response": tool_output},
            "record": {
                "type": "tool",
                "name": spec.step_name,
                "input": tool_input,
                "output": tool_output,
                "context": {"slm_runtime_probe_id": probe_id, "expected_path": "runtime"},
            },
            "config": {"request_timeout_seconds": 25},
        }

    benign = "Please summarize the account transfer policy in one sentence."
    query = spec.sample if spec.side == "input" else benign
    response = spec.sample if spec.side == "output" else "The transfer policy requires verification before completion."
    available_tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": "wire_transfer",
                    "description": "Submit a verified wire transfer.",
                },
            }
        ]
        if spec.name == "tool_selection_quality_luna"
        else None
    )
    tool_calls = (
        [
            {
                "id": "call-runtime-probe",
                "function": {
                    "name": "wire_transfer",
                    "arguments": '{"amount": 2500, "destination_country": "United Kingdom"}',
                },
            }
        ]
        if spec.name == "tool_selection_quality_luna"
        else None
    )
    return {
        "inputs": {
            "query": query,
            "response": response,
            "tools": available_tools,
            "tool_calls": tool_calls,
        },
        "record": {
            "type": "llm",
            "name": spec.step_name,
            "input": query,
            "output": response,
            "tools": available_tools,
            "tool_calls": tool_calls,
            "context": {"slm_runtime_probe_id": probe_id, "expected_path": "runtime"},
        },
        "config": {"request_timeout_seconds": 25},
    }


def _incompatibility_result(payload: dict[str, Any]) -> bool:
    """Return true only when the scorer invocation failed as expected."""

    status = str(payload.get("status", "")).lower()
    error = str(payload.get("error_message", "")).lower()
    return status not in {"success", "ok"} and bool(error)


async def _invoke_public_scorer(
    args: SimpleNamespace,
    spec: SLMRuntimeScorer,
    scorer_id: str,
    *,
    probe_id: str,
) -> dict[str, Any]:
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        raise RuntimeError("GALILEO_API_KEY is required.")
    body = _probe_payload(spec, probe_id=probe_id)
    body.update({"scorer_id": scorer_id, "scorer_label": spec.name})
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            f"{args.api_base_url.rstrip('/')}/scorers/invoke",
            headers={"Galileo-API-Key": api_key},
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    if str(payload.get("status", "")).lower() != "success":
        raise RuntimeError(f"{spec.name} returned {payload}")
    return {
        "scorer": spec.name,
        "side": spec.side,
        "status": payload.get("status"),
        "score": payload.get("score"),
        "scorer_label": payload.get("scorer_label"),
        "execution_time": payload.get("execution_time"),
        "request_record_type": body["record"]["type"],
        "request_probe_id": probe_id,
    }


async def _invoke_incompatible_tool_scorer(
    args: SimpleNamespace,
    scorer_id: str,
    *,
    probe_id: str,
) -> dict[str, Any]:
    """Send input_pii a tool record and require a clear runtime error."""

    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        raise RuntimeError("GALILEO_API_KEY is required.")
    body = _probe_payload(TOOL_RUNTIME_SCORERS[0], probe_id=probe_id)
    body["record"]["name"] = "slm_runtime_incompatible_tool"
    body.update({"scorer_id": scorer_id, "scorer_label": "input_pii"})
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            f"{args.api_base_url.rstrip('/')}/scorers/invoke",
            headers={"Galileo-API-Key": api_key},
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    if not _incompatibility_result(payload):
        raise RuntimeError(f"input_pii unexpectedly accepted a tool record: {payload}")
    return {
        "scorer": "input_pii",
        "expected": "incompatible",
        "status": payload.get("status"),
        "error_message": payload.get("error_message"),
        "request_record_type": body["record"]["type"],
        "request_probe_id": probe_id,
    }


async def _readback_records(args: SimpleNamespace, *, project_id: str, log_stream_id: str, trace_id: str) -> list[dict[str, Any]]:
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        return []
    async with httpx.AsyncClient(timeout=30.0) as client:
        login = await client.post(
            f"{args.api_base_url.rstrip('/')}/login/api_key",
            json={"api_key": api_key},
        )
        login.raise_for_status()
        token = login.json().get("access_token")
        if not token:
            raise RuntimeError("Galileo API login did not return an access token.")
        for _ in range(12):
            response = await client.post(
                f"{args.api_base_url.rstrip('/')}/projects/{project_id}/spans/partial_search",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "log_stream_id": log_stream_id,
                    "filter_tree": {"filter": {"name": "trace_id", "operator": "eq", "value": trace_id, "type": "text"}},
                    "pagination": {"limit": 100},
                    "select_columns": {
                        "column_ids": ["id", "trace_id", "type", "name", "control_id", "input", "output"],
                        "include_all_metrics": False,
                        "include_all_feedback": False,
                    },
                },
            )
            response.raise_for_status()
            records = [record for record in response.json().get("records", []) if isinstance(record, dict)]
            if any(record.get("type") == "control" for record in records):
                return records
            await asyncio.sleep(1.0)
    return []


def _extract_labels(value: Any) -> set[str]:
    labels: set[str] = set()
    if isinstance(value, dict):
        for key, current in value.items():
            if key in {"scorer_label", "scorer_name", "control"} and isinstance(current, str):
                labels.add(current)
            labels.update(_extract_labels(current))
    elif isinstance(value, list):
        for current in value:
            labels.update(_extract_labels(current))
    elif isinstance(value, str):
        for spec in ALL_RUNTIME_SCORERS:
            if spec.name in value:
                labels.add(spec.name)
        try:
            labels.update(_extract_labels(json.loads(value)))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return labels


def _runtime_probe_function(spec: SLMRuntimeScorer):
    import agent_control

    @agent_control.control(step_name=spec.step_name, step_type=spec.record_type)
    async def slm_runtime_probe_llm(input_text: str, output_text: str) -> str:
        return output_text

    return slm_runtime_probe_llm


def _runtime_test_functions() -> SimpleNamespace:
    """Return decorated functions whose names are targeted by the UI controls."""

    import agent_control

    def _agent_control_tool(func: Any) -> Any:
        func.tool_name = func.__name__
        return func

    @agent_control.control()
    async def slm_runtime_block_llm(input_text: str, output_text: str) -> str:
        return output_text

    @agent_control.control(step_type="tool")
    @_agent_control_tool
    async def slm_runtime_probe_tool(tool_input: dict[str, Any]) -> dict[str, Any]:
        return {"status": "success", "tool_input": tool_input}

    @agent_control.control(step_type="tool")
    @_agent_control_tool
    async def slm_runtime_incompatible_tool(tool_input: dict[str, Any]) -> dict[str, Any]:
        return {"status": "success", "tool_input": tool_input}

    @agent_control.control(step_type="tool")
    @_agent_control_tool
    async def slm_runtime_transfer_tool(
        destination_country: str,
        amount: str,
        verified_2fa: bool,
    ) -> dict[str, Any]:
        return {
            "destination_country": destination_country,
            "amount": amount,
            "verified_2fa": verified_2fa,
            "status": "transfer-executed",
        }

    @agent_control.control(step_type="tool")
    @_agent_control_tool
    async def slm_runtime_secret_tool(operation: str) -> dict[str, str]:
        return {"operation": operation, "status": "operation-executed"}

    return SimpleNamespace(
        block_llm=slm_runtime_block_llm,
        probe_tool=slm_runtime_probe_tool,
        incompatible_tool=slm_runtime_incompatible_tool,
        transfer_tool=slm_runtime_transfer_tool,
        secret_tool=slm_runtime_secret_tool,
    )


async def _run_blocking_llm_case(
    args: SimpleNamespace,
    case: Any,
    scorer_ids: dict[str, str],
) -> dict[str, Any]:
    """Run one deny control against a specifically named LLM function."""

    await _runtime_preflight(args)
    _configure_environment(args)
    import agent_control
    from agent_control import ControlViolationError
    from galileo.logger.logger import GalileoLogger

    spec = next(spec for spec in SLM_RUNTIME_SCORERS if spec.name == case.scorer_name)
    benign = "Please summarize the account transfer policy."
    probe_input = case.sample if spec.side == "input" else benign
    probe_output = case.sample if spec.side == "output" else "Verification is required before completion."
    logger = GalileoLogger(project=args.project, log_stream=args.log_stream, mode="batch")
    session_id = logger.start_session(name="slm-runtime-block-llm", external_id=f"slm-block-{uuid4()}")
    if logger.project_id is None or logger.log_stream_id is None:
        raise RuntimeError("Galileo logger did not resolve project/log-stream IDs.")
    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Targeted blocking LLM SLM probe",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=args.target_type,
        target_id=logger.log_stream_id,
    )
    trace = logger.start_trace(input={"input": probe_input}, name="slm_runtime_block_llm_trace")
    workflow = logger.add_workflow_span(input=probe_input, name="slm_runtime_block_llm_workflow")
    blocked = False
    error = None
    try:
        fn = _runtime_test_functions().block_llm
        try:
            output = await fn(probe_input, probe_output)
        except ControlViolationError as exc:
            blocked = True
            output = f"blocked: {exc}"
            error = str(exc)
        logger.add_llm_span(input=probe_input, output=output, model="slm-runtime-block-probe", name=fn.__name__)
        logger.conclude(output=output)
        logger.flush()
        records = await _readback_records(
            args,
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
            trace_id=str(trace.id),
        )
        return {
            "case": case.control_name,
            "scorer": case.scorer_name,
            "expected": "blocked",
            "blocked": blocked,
            "error": error,
            "trace_id": str(trace.id),
            "local_control_spans": sum(getattr(span, "type", None) == "control" for span in getattr(workflow, "spans", [])),
            "persisted_control_spans": sum(record.get("type") == "control" for record in records),
        }
    finally:
        logger.terminate()


async def _run_tool_case(args: SimpleNamespace, case_name: str) -> dict[str, Any]:
    """Run one targeted regex tool control and report deny/steer behavior."""

    await _runtime_preflight(args)
    _configure_environment(args)
    import agent_control
    from agent_control import ControlSteerError, ControlViolationError
    from galileo.logger.logger import GalileoLogger

    logger = GalileoLogger(project=args.project, log_stream=args.log_stream, mode="batch")
    session_id = logger.start_session(name="slm-runtime-tool", external_id=f"slm-tool-{uuid4()}")
    if logger.project_id is None or logger.log_stream_id is None:
        raise RuntimeError("Galileo logger did not resolve project/log-stream IDs.")
    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Targeted regex tool control probe",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=args.target_type,
        target_id=logger.log_stream_id,
    )
    functions = _runtime_test_functions()
    if case_name == "sanctioned destination deny":
        trace_input = {"destination_country": "Iran", "amount": "2500", "verified_2fa": True}
        trace_name = "slm_runtime_tool_sanctioned_trace"
    elif case_name == "large transfer 2FA steer":
        trace_input = {"destination_country": "United Kingdom", "amount": "15000", "verified_2fa": False}
        trace_name = "slm_runtime_tool_2fa_trace"
    else:
        trace_input = {"operation": "export"}
        trace_name = "slm_runtime_tool_secret_trace"
    trace = logger.start_trace(input=trace_input, name=trace_name)
    workflow = logger.add_workflow_span(input=json.dumps(trace_input), name="slm_runtime_tool_workflow")
    blocked = False
    steered = False
    retry_succeeded = False
    error = None
    try:
        if case_name == "secret export deny":
            try:
                output = await functions.secret_tool(operation="export")
            except ControlViolationError as exc:
                blocked = True
                error = str(exc)
                output = {"status": "blocked"}
            logger.add_tool_span(input=json.dumps(trace_input), output=json.dumps(output), name=functions.secret_tool.__name__)
        else:
            tool_input = dict(trace_input)
            try:
                output = await functions.transfer_tool(**tool_input)
            except ControlSteerError as exc:
                steered = True
                error = str(exc)
                tool_input["verified_2fa"] = True
                output = await functions.transfer_tool(**tool_input)
                retry_succeeded = True
            except ControlViolationError as exc:
                blocked = True
                error = str(exc)
                output = {"status": "blocked"}
            logger.add_tool_span(input=json.dumps(tool_input), output=json.dumps(output), name=functions.transfer_tool.__name__)
        logger.conclude(output=output)
        logger.flush()
        records = await _readback_records(
            args,
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
            trace_id=str(trace.id),
        )
        return {
            "case": case_name,
            "blocked": blocked,
            "steered": steered,
            "retry_succeeded": retry_succeeded,
            "error": error,
            "trace_id": str(trace.id),
            "local_control_spans": sum(getattr(span, "type", None) == "control" for span in getattr(workflow, "spans", [])),
            "persisted_control_spans": sum(record.get("type") == "control" for record in records),
        }
    finally:
        logger.terminate()


async def _run_blocking_suite(args: SimpleNamespace, scorer_ids: dict[str, str]) -> list[dict[str, Any]]:
    return [await _run_blocking_llm_case(args, case, scorer_ids) for case in LLM_BLOCK_CASES]


async def _run_tool_suite(args: SimpleNamespace) -> list[dict[str, Any]]:
    return [
        await _run_tool_case(args, "sanctioned destination deny"),
        await _run_tool_case(args, "large transfer 2FA steer"),
        await _run_tool_case(args, "secret export deny"),
    ]


async def _run_agent_control_probe(
    args: SimpleNamespace,
    spec: SLMRuntimeScorer,
    *,
    probe_input: str,
    probe_output: str,
) -> dict[str, Any]:
    _configure_environment(args)
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        raise RuntimeError("GALILEO_API_KEY is required.")

    import agent_control
    from agent_control import ControlViolationError
    from galileo.logger.logger import GalileoLogger

    logger = GalileoLogger(project=args.project, log_stream=args.log_stream, mode="batch")
    session_id = logger.start_session(name="slm-runtime-streamlit", external_id=f"slm-runtime-{uuid4()}")
    if logger.project_id is None or logger.log_stream_id is None:
        raise RuntimeError("Galileo logger did not resolve project/log-stream IDs.")
    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Preset SLM direct-runtime E2E probe",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=args.target_type,
        target_id=logger.log_stream_id,
    )
    trace = logger.start_trace(input={"input": probe_input}, name=f"{spec.step_name}_trace")
    workflow = logger.add_workflow_span(input=probe_input, name=f"{spec.step_name}_workflow")
    try:
        probe = _runtime_probe_function(spec)
        blocked = False
        error = None
        try:
            output = await probe(probe_input, probe_output)
        except ControlViolationError as exc:
            blocked = True
            error = str(exc)
            output = f"blocked: {exc}"
        logger.add_llm_span(input=probe_input, output=output, model="slm-runtime-probe", name=spec.step_name)
        logger.conclude(output=output)
        logger.flush()
        local_control_spans = [span for span in getattr(workflow, "spans", []) if getattr(span, "type", None) == "control"]
        records = await _readback_records(
            args,
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
            trace_id=str(trace.id),
        )
        persisted_controls = [record for record in records if record.get("type") == "control"]
        labels = set().union(*(_extract_labels(record) for record in persisted_controls)) if persisted_controls else set()
        return {
            "session_id": session_id,
            "project_id": logger.project_id,
            "log_stream_id": logger.log_stream_id,
            "trace_id": str(trace.id),
            "scorer": spec.name,
            "step_name": spec.step_name,
            "record_type": spec.record_type,
            "expected_action": "deny",
            "blocked": blocked,
            "error": error,
            "output": output,
            "local_control_spans": len(local_control_spans),
            "persisted_control_spans": len(persisted_controls),
            "persisted_scorer_labels": sorted(labels & {spec.name for spec in SLM_RUNTIME_SCORERS}),
        }
    finally:
        logger.terminate()


async def _run_agent_control_tool_probe(args: SimpleNamespace) -> dict[str, Any]:
    """Run the positive tool-step SLM control through a real Agent Control tool."""

    _configure_environment(args)
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        raise RuntimeError("GALILEO_API_KEY is required.")

    import agent_control
    from agent_control import ControlViolationError
    from galileo.logger.logger import GalileoLogger

    tool_input = {
        "account_id": "acct-4412",
        "amount": 2500,
        "destination_country": "United Kingdom",
    }
    logger = GalileoLogger(project=args.project, log_stream=args.log_stream, mode="batch")
    session_id = logger.start_session(name="slm-runtime-tool-scorer", external_id=f"slm-tool-scorer-{uuid4()}")
    if logger.project_id is None or logger.log_stream_id is None:
        raise RuntimeError("Galileo logger did not resolve project/log-stream IDs.")
    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Preset SLM direct-runtime tool-step probe",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=args.target_type,
        target_id=logger.log_stream_id,
    )
    trace = logger.start_trace(input=tool_input, name="slm_runtime_probe_tool_trace")
    workflow = logger.add_workflow_span(input=json.dumps(tool_input), name="slm_runtime_probe_tool_workflow")
    try:
        probe = _runtime_test_functions().probe_tool
        blocked = False
        error = None
        try:
            output = await probe(tool_input)
        except ControlViolationError as exc:
            blocked = True
            error = str(exc)
            output = {"status": "blocked"}
        logger.add_tool_span(input=json.dumps(tool_input), output=json.dumps(output), name=probe.__name__)
        logger.conclude(output=output)
        logger.flush()
        records = await _readback_records(
            args,
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
            trace_id=str(trace.id),
        )
        persisted_controls = [record for record in records if record.get("type") == "control"]
        labels = set().union(*(_extract_labels(record) for record in persisted_controls)) if persisted_controls else set()
        return {
            "case": "tool_error_rate_luna",
            "expected": "success",
            "record_type": "tool",
            "expected_action": "deny",
            "blocked": blocked,
            "error": error,
            "session_id": session_id,
            "trace_id": str(trace.id),
            "output": output,
            "local_control_spans": sum(getattr(span, "type", None) == "control" for span in getattr(workflow, "spans", [])),
            "persisted_control_spans": len(persisted_controls),
            "persisted_scorer_labels": sorted(labels & {spec.name for spec in TOOL_RUNTIME_SCORERS}),
        }
    finally:
        logger.terminate()


async def _run_agent_control_incompatible_tool_probe(args: SimpleNamespace) -> dict[str, Any]:
    """Prove that an LLM-only SLM does not silently accept a tool record."""

    _configure_environment(args)
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        raise RuntimeError("GALILEO_API_KEY is required.")

    import agent_control
    from galileo.logger.logger import GalileoLogger

    tool_input = {"email": "alice@example.com", "ssn": "123-45-6789"}
    logger = GalileoLogger(project=args.project, log_stream=args.log_stream, mode="batch")
    logger.start_session(name="slm-runtime-incompatible-tool", external_id=f"slm-incompatible-{uuid4()}")
    if logger.project_id is None or logger.log_stream_id is None:
        raise RuntimeError("Galileo logger did not resolve project/log-stream IDs.")
    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Negative SLM record compatibility probe",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=args.target_type,
        target_id=logger.log_stream_id,
    )
    trace = logger.start_trace(input=tool_input, name="slm_runtime_incompatible_tool_trace")
    workflow = logger.add_workflow_span(input=json.dumps(tool_input), name="slm_runtime_incompatible_tool_workflow")
    rejected = False
    error = None
    try:
        probe = _runtime_test_functions().incompatible_tool
        try:
            output = await probe(tool_input)
        except Exception as exc:  # The deny control must reject an incompatible record.
            rejected = True
            error = f"{type(exc).__name__}: {exc}"
            output = {"status": "rejected"}
        agent_control_result = "rejected" if rejected else "unexpectedly_accepted"
        logger.add_tool_span(input=json.dumps(tool_input), output=json.dumps(output), name=probe.__name__)
        logger.conclude(output=output)
        logger.flush()
        records = await _readback_records(
            args,
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
            trace_id=str(trace.id),
        )
        return {
            "case": "input_pii on tool",
            "expected": "rejected",
            "rejected": rejected,
            "record_type": "tool",
            "agent_control_result": agent_control_result,
            "expected_agent_control_behavior": "reject_on_evaluator_error",
            "error": error,
            "trace_id": str(trace.id),
            "local_control_spans": sum(getattr(span, "type", None) == "control" for span in getattr(workflow, "spans", [])),
            "persisted_control_spans": sum(record.get("type") == "control" for record in records),
        }
    finally:
        logger.terminate()


async def _run_compatibility_suite(args: SimpleNamespace, scorer_ids: dict[str, str]) -> dict[str, Any]:
    """Exercise compatible LLM/tool records and one deliberately incompatible tool record."""

    preflight = await _runtime_preflight(args)
    versions = _package_versions(args.expected_agent_control_version)
    direct_rows = [
        await _invoke_public_scorer(args, spec, scorer_ids[spec.name], probe_id=f"compat-{uuid4()}")
        for spec in LLM_TOOL_RUNTIME_SCORERS + TOOL_RUNTIME_SCORERS
    ]
    incompatible_direct = await _invoke_incompatible_tool_scorer(
        args,
        scorer_ids["input_pii"],
        probe_id=f"compat-negative-{uuid4()}",
    )
    agent_control_tool = await _run_agent_control_tool_probe(args)
    agent_control_incompatible = await _run_agent_control_incompatible_tool_probe(args)
    return {
        "preflight": preflight,
        "agent_control_versions": versions,
        "direct_rows": direct_rows,
        "incompatible_direct": incompatible_direct,
        "agent_control_tool": agent_control_tool,
        "agent_control_incompatible": agent_control_incompatible,
    }


async def _run_suite(args: SimpleNamespace, scorer_ids: dict[str, str]) -> dict[str, Any]:
    preflight = await _runtime_preflight(args)
    versions = _package_versions(args.expected_agent_control_version)
    direct_rows = []
    for spec in SLM_RUNTIME_SCORERS:
        direct_rows.append(
            await _invoke_public_scorer(args, spec, scorer_ids[spec.name], probe_id=f"direct-{uuid4()}")
        )
    agent_control_results = []
    for spec in SLM_RUNTIME_SCORERS:
        probe_input = spec.sample if spec.side == "input" else "Please summarize the account transfer policy."
        probe_output = spec.sample if spec.side == "output" else "The transfer policy requires verification before completion."
        agent_control_results.append(
            await _run_agent_control_probe(
                args,
                spec,
                probe_input=probe_input,
                probe_output=probe_output,
            )
        )
    expected_names = {spec.name for spec in SLM_RUNTIME_SCORERS}
    direct_names = {row["scorer"] for row in direct_rows if row.get("status") == "success"}
    persisted_names = set().union(*(set(result["persisted_scorer_labels"]) for result in agent_control_results))
    return {
        "preflight": preflight,
        "agent_control_versions": versions,
        "direct_rows": direct_rows,
        "agent_control": agent_control_results,
        "direct_missing": sorted(expected_names - direct_names),
        "persisted_missing": sorted(expected_names - persisted_names),
        "control_unblocked": [result["scorer"] for result in agent_control_results if not result["blocked"]],
    }


def _mask(value: str) -> str:
    if len(value) < 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def main() -> None:
    st.set_page_config(page_title="Preset SLM Runtime E2E", layout="wide")
    st.title("Preset SLM Runtime E2E")
    st.caption("Agent Control → Galileo scorer invoke → Orbit direct runtime, with explicit LLM/tool record compatibility probes.")
    args = _defaults()
    scorer_ids = load_scorer_ids(args.scorer_ids_file or None)

    with st.sidebar:
        st.header("Configuration")
        args.agent_name = st.text_input("Agent name", args.agent_name)
        args.expected_agent_control_version = st.text_input("Expected Agent Control version", args.expected_agent_control_version)
        st.caption(f"Project: {args.project}")
        st.caption(f"Log stream: {args.log_stream}")
        st.caption(f"API: {args.api_base_url}")
        st.caption(f"Agent Control: {args.server_url}")
        api_key = os.environ.get("GALILEO_API_KEY", "")
        st.caption(f"API key: {_mask(api_key) if api_key else 'unset'}")
        st.divider()
        st.subheader("Direct-runtime scorer IDs")
        st.caption("Use preset scorer UUIDs, not the existing fine-tuned Luna ID.")
        st.dataframe(
            [
                {
                    "scorer": spec.name,
                    "record_type": spec.record_type,
                    "side": spec.side,
                    "id_present": spec.name in scorer_ids,
                }
                for spec in ALL_RUNTIME_SCORERS
            ],
            hide_index=True,
            width="stretch",
        )

    missing = missing_scorer_ids(scorer_ids)
    if missing:
        st.warning("Missing scorer IDs: " + ", ".join(missing))
        st.code("cp slm_runtime_scorers.example.json slm_runtime_scorers.json\nexport GALILEO_SLM_SCORER_IDS_FILE=$PWD/slm_runtime_scorers.json")
    compatibility_missing = missing_scorer_ids(scorer_ids, ALL_RUNTIME_SCORERS)

    selected_name = st.selectbox("Single scorer probe", [spec.name for spec in ALL_RUNTIME_SCORERS])
    selected = next(spec for spec in ALL_RUNTIME_SCORERS if spec.name == selected_name)
    st.info(f"{selected.description} This runs the {selected.side} selector at {selected.stage} stage.")

    run_single, run_suite, run_compatibility, run_blocking, run_tools = st.columns(5)
    single_clicked = run_single.button(
        "Run selected scorer", type="primary", width="stretch", disabled=selected.name in missing
    )
    suite_clicked = run_suite.button("Run all 9 scorer suite", width="stretch", disabled=bool(missing))
    compatibility_clicked = run_compatibility.button(
        "Run record compatibility suite", width="stretch", disabled=bool(compatibility_missing)
    )
    blocking_names = {case.scorer_name for case in LLM_BLOCK_CASES}
    blocking_clicked = run_blocking.button(
        "Run blocking LLM cases", width="stretch", disabled=not blocking_names.isdisjoint(set(missing))
    )
    tools_clicked = run_tools.button("Run tool regex cases", width="stretch")
    if not single_clicked and not suite_clicked and not compatibility_clicked and not blocking_clicked and not tools_clicked:
        return

    args.agent_name = args.agent_name.strip()
    try:
        with st.spinner("Checking runtime flag and executing Agent Control cases..."):
            if suite_clicked:
                result = asyncio.run(_run_suite(args, scorer_ids))
                st.success("Completed direct API and Agent Control runtime probes.")
                st.json({"runtime_flag": result["preflight"]["runtime_flag"], "agent_control_health": result["preflight"]["agent_control_health"]})
                st.subheader("Direct Runners API results")
                st.dataframe(result["direct_rows"], hide_index=True, width="stretch")
                st.subheader("Agent Control / Galileo readback")
                st.json(result["agent_control"])
                if result["direct_missing"] or result["persisted_missing"] or result["control_unblocked"]:
                    st.error(
                        f"Missing direct={result['direct_missing']} persisted={result['persisted_missing']} "
                        f"unblocked={result['control_unblocked']}"
                    )
                else:
                    st.success("All nine direct-runtime calls succeeded and each independent @control probe was denied.")
            elif compatibility_clicked:
                result = asyncio.run(_run_compatibility_suite(args, scorer_ids))
                st.success("Completed compatible LLM/tool probes and the incompatible-tool negative test.")
                st.subheader("Compatible direct-runtime records")
                st.dataframe(result["direct_rows"], hide_index=True, width="stretch")
                st.subheader("Incompatible tool record")
                st.json(result["incompatible_direct"])
                st.subheader("Agent Control tool readback")
                st.json(
                    {
                        "positive": result["agent_control_tool"],
                        "negative": result["agent_control_incompatible"],
                    }
                )
                compatible_success = all(row["status"] == "success" for row in result["direct_rows"])
                negative_success = (
                    result["incompatible_direct"]["expected"] == "incompatible"
                    and result["agent_control_incompatible"]["record_type"] == "tool"
                    and result["agent_control_incompatible"]["agent_control_result"] == "rejected"
                )
                if compatible_success and negative_success:
                    st.success("Compatible scorers returned successfully; Orbit rejected input_pii on a tool record.")
                else:
                    st.error("The record-type compatibility expectations were not met.")
            elif blocking_clicked:
                results = asyncio.run(_run_blocking_suite(args, scorer_ids))
                st.subheader("Blocking LLM cases")
                st.dataframe(results, hide_index=True, width="stretch")
                if all(row["blocked"] for row in results):
                    st.success("All targeted SLM-backed LLM cases were blocked.")
                else:
                    st.error("At least one LLM block case completed unexpectedly.")
            elif tools_clicked:
                results = asyncio.run(_run_tool_suite(args))
                st.subheader("Regex tool cases")
                st.dataframe(results, hide_index=True, width="stretch")
                expected = {
                    "sanctioned destination deny": lambda row: row["blocked"],
                    "large transfer 2FA steer": lambda row: row["steered"] and row["retry_succeeded"],
                    "secret export deny": lambda row: row["blocked"],
                }
                if all(expected[row["case"]](row) for row in results):
                    st.success("All targeted tool regex cases produced the expected deny/steer behavior.")
                else:
                    st.error("At least one tool regex case did not produce its expected action.")
            else:
                preflight = asyncio.run(_runtime_preflight(args))
                row = asyncio.run(_invoke_public_scorer(args, selected, scorer_ids[selected.name], probe_id=f"single-{uuid4()}"))
                if selected.record_type == "tool":
                    ac_result = asyncio.run(_run_agent_control_tool_probe(args))
                    message = f"{selected.name} returned through the direct API and @control tool paths."
                elif selected.name == "tool_selection_quality_luna":
                    ac_result = {
                        "status": "not_run",
                        "reason": "The Agent Control decorator payload does not carry available tools; use the direct API probe for this scorer.",
                    }
                    message = f"{selected.name} returned successfully through the direct API path; Agent Control decorator probe was skipped."
                else:
                    probe_input = selected.sample if selected.side == "input" else "Please summarize the account transfer policy."
                    probe_output = selected.sample if selected.side == "output" else "The transfer policy requires verification before completion."
                    ac_result = asyncio.run(
                        _run_agent_control_probe(
                            args,
                            selected,
                            probe_input=probe_input,
                            probe_output=probe_output,
                        )
                    )
                    message = f"{selected.name} returned through the direct API and @control paths."
                st.success(message)
                st.json({"preflight": preflight, "direct_api": row, "agent_control": ac_result})
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
