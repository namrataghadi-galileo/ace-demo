#!/usr/bin/env python3
"""Run the banking demo with application and control spans exported over OTLP/HTTP.

This is intentionally separate from run_demo.py. The original demo continues to
exercise SplunkAOLogger and its registered Agent Control bridge; this variant uses
the standard OTLP/HTTP exporter for application spans and Agent Control's built-in
``otel`` control-event sink.
"""

from __future__ import annotations

# ruff: noqa: I001 -- load the selected dotenv file before app imports

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import UUID

from dotenv import load_dotenv

base_dotenv_path = Path(".env")
if base_dotenv_path.exists():
    load_dotenv(base_dotenv_path, override=True)

dotenv_path = Path(os.environ.get("SPLUNK_AO_ENV_FILE", ".env.otel-lab0"))
if not dotenv_path.exists():
    dotenv_path = Path(".env.splunk-ao")
os.environ["SPLUNK_AO_ENV_FILE"] = str(dotenv_path)
load_dotenv(dotenv_path, override=True)


def _configure_lab0_aliases() -> None:
    """Map the shared Lab0 secrets to the SDK-specific environment names."""
    if os.environ.get("SPLUNK_AO_REALM") != "lab0":
        return

    ingest_token = os.environ.get("LAB0_INGEST_TOKEN")
    sf_token = os.environ.get("LAB0_SF_TOKEN")
    if ingest_token:
        os.environ.setdefault("SPLUNK_AO_O11Y_TOKEN", ingest_token)
        os.environ.setdefault("SPLUNK_AO_O11Y_API_TOKEN", ingest_token)
    if sf_token:
        os.environ["AGENT_CONTROL_API_KEY"] = sf_token
        os.environ["AGENT_CONTROL_API_KEY_HEADER"] = "X-SF-Token"

    os.environ.setdefault(
        "AGENT_CONTROL_URL",
        "https://app.lab0.signalfx.com/ao/agent-control",
    )
    os.environ.setdefault(
        "AGENT_CONTROL_RUNTIME_TOKEN_HEADER",
        "X-Agent-Control-Runtime-Token",
    )


_configure_lab0_aliases()

from common_splunk_ao import (
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

from agent_control import ControlSteerError, ControlViolationError

import run_demo_splunk_ao as demo
from setup_controls_splunk_ao import control_specs, ensure_demo_controls


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _is_o11y() -> bool:
    return bool(os.environ.get("SPLUNK_AO_REALM"))


def _otel_endpoint(args: Any) -> str:
    configured = os.environ.get("AGENT_CONTROL_OTEL_ENDPOINT")
    if configured:
        return configured.rstrip("/")
    if _is_o11y():
        realm = _required_env("SPLUNK_AO_REALM")
        return f"https://ingest.{realm}.observability.splunkcloud.com/v2/trace/otlp"
    api_url = args.api_base_url or _required_env("SPLUNK_AO_API_URL")
    return urljoin(api_url.rstrip("/") + "/", "otel/v1/traces").rstrip("/")


def _otel_headers(
    args: Any, *, project_id: str, log_stream_id: str
) -> dict[str, str]:
    configured = os.environ.get("AGENT_CONTROL_OTEL_HEADERS")
    if configured:
        try:
            value = json.loads(configured)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "AGENT_CONTROL_OTEL_HEADERS must be a JSON object."
            ) from exc
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise RuntimeError(
                "AGENT_CONTROL_OTEL_HEADERS must contain only string keys and values."
            )
        return value

    if _is_o11y():
        return {
            "X-SF-Token": _required_env("SPLUNK_AO_O11Y_TOKEN"),
            "projectid": project_id,
            "logstreamid": log_stream_id,
        }

    return {
        "Splunk-AO-API-Key": _required_env("SPLUNK_AO_API_KEY"),
        "project": args.project,
        "logstream": args.log_stream,
    }


def _validate_otel_configuration(
    args: Any, endpoint: str, headers: dict[str, str]
) -> None:
    required_headers = (
        {"X-SF-Token", "projectid", "logstreamid"}
        if _is_o11y()
        else {"Splunk-AO-API-Key", "project", "logstream"}
    )
    missing = sorted(required_headers.difference(headers))
    if missing:
        raise RuntimeError(f"OTLP routing headers are missing: {', '.join(missing)}")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "AGENT_CONTROL_OTEL_ENDPOINT must be an absolute HTTP(S) URL."
        )


def _resolve_splunk_ao_ids(args: Any) -> tuple[str, str]:
    configured_project_id = os.environ.get("SPLUNK_AO_PROJECT_ID")
    configured_stream_id = os.environ.get("SPLUNK_AO_AGENT_STREAM_ID")
    if configured_project_id and configured_stream_id:
        return configured_project_id, configured_stream_id
    if configured_project_id or configured_stream_id:
        raise RuntimeError(
            "SPLUNK_AO_PROJECT_ID and SPLUNK_AO_AGENT_STREAM_ID must be set together."
        )

    from splunk_ao.agent_streams import get_agent_stream

    agent_stream = get_agent_stream(name=args.log_stream, project_name=args.project)
    if agent_stream is None or not agent_stream.id or not agent_stream.project_id:
        raise RuntimeError(
            f"Could not resolve Splunk AO Agent Stream {args.project}/{args.log_stream}. "
            "Create it first or correct SPLUNK_AO_PROJECT and SPLUNK_AO_AGENT_STREAM."
        )
    return str(agent_stream.project_id), str(agent_stream.id)


def _set_common_span_attributes(span: Any, *, operation: str, input_value: Any) -> None:
    span.set_attribute("gen_ai.system", "splunk-ao-otel")
    span.set_attribute("gen_ai.operation.name", operation)
    span.set_attribute(
        "gen_ai.input.messages",
        json.dumps([{"role": "user", "content": input_value}], default=str),
    )


def _trace_uuid(span: Any) -> str:
    return str(UUID(int=span.get_span_context().trace_id))


def _backend_trace_uuid(source_trace_id: str) -> str:
    """Return the UUIDv4 form used by Lab0 for an arbitrary OTEL trace ID."""
    raw = bytearray(UUID(source_trace_id).bytes)
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _otel_trace_context_for_span(span: Any) -> dict[str, str] | None:
    """Expose one application span as Agent Control's stable parent."""
    from opentelemetry.trace import INVALID_SPAN_ID, INVALID_TRACE_ID

    context = span.get_span_context()
    if context.trace_id == INVALID_TRACE_ID or context.span_id == INVALID_SPAN_ID:
        return None
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


def _count_span_name(node: dict[str, Any], name: str) -> int:
    count = 1 if node.get("name") == name else 0
    for child in node.get("spans", []) or []:
        count += _count_span_name(child, name)
    return count


async def _verify_otel_trace(
    args: Any,
    *,
    project_id: str,
    log_stream_id: str,
    trace_id: str,
) -> None:
    if _is_o11y():
        await _verify_o11y_otel_trace(
            project_id=project_id,
            log_stream_id=log_stream_id,
            source_trace_id=trace_id,
        )
        return

    try:
        await demo._verify_trace(
            args,
            project_id=project_id,
            log_stream_id=log_stream_id,
            session_id=None,
            trace_id=trace_id,
            control_span_ids=[],
        )
        return
    except RuntimeError as exc:
        if "did not return any ControlSpan records" not in str(exc):
            raise

    import httpx

    api_key = _required_env("SPLUNK_AO_API_KEY")
    api_base_url = demo._resolve_api_base_url(args)
    async with httpx.AsyncClient(timeout=60.0) as client:
        bearer_headers = await demo._login_with_api_key(client, api_base_url, api_key)
        trace_payload = await demo._fetch_json(
            client,
            f"{api_base_url}/projects/{project_id}/traces/{trace_id}",
            headers=bearer_headers,
        )

    raw_control_events = _count_span_name(
        trace_payload, "agent_control.control_execution"
    )
    if raw_control_events:
        raise RuntimeError(
            f"OTLP delivery succeeded, but Splunk AO ingest classified {raw_control_events} "
            "'agent_control.control_execution' spans as workflow spans instead of control spans. "
            "Deploy an ingest-service image containing Orbit commit 9ccc212c (Agent Control "
            "native OTEL support), then rerun this verification."
        )
    raise RuntimeError(
        "The OTEL trace was stored, but it contained no Agent Control execution spans."
    )


async def _verify_o11y_otel_trace(
    *, project_id: str, log_stream_id: str, source_trace_id: str
) -> None:
    """Verify recent Lab0 spans without assuming OTLP and AO trace IDs are equal."""
    import httpx

    realm = _required_env("SPLUNK_AO_REALM")
    api_origin = os.environ.get(
        "SPLUNK_AO_O11Y_API_ENDPOINT",
        f"https://api.{realm}.observability.splunkcloud.com",
    ).rstrip("/")
    api_base = f"{api_origin}/ao/api"
    headers = {"X-SF-Token": _required_env("SPLUNK_AO_O11Y_TOKEN")}
    expected_backend_trace_id = _backend_trace_uuid(source_trace_id)
    request_body = {
        "log_stream_id": log_stream_id,
        "limit": 100,
        "filter_tree": {
            "filter": {
                "name": "trace_id",
                "operator": "eq",
                "value": expected_backend_trace_id,
                "type": "text",
            }
        },
        "select_columns": {
            "column_ids": [
                "id",
                "trace_id",
                "type",
                "name",
                "created_at",
                "control_id",
                "check_stage",
                "applies_to",
                "output",
            ],
            "include_all_metrics": False,
            "include_all_feedback": False,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(12):
            response = await client.post(
                f"{api_base}/projects/{project_id}/spans/partial_search",
                headers=headers,
                json=request_body,
            )
            response.raise_for_status()
            records = response.json().get("records", [])
            control_records = [
                record
                for record in records
                if record.get("type") == "control"
                or record.get("name") == "agent_control.control_execution"
            ]
            application_records = [
                record
                for record in records
                if record.get("name")
                in {
                    "agent-control-splunk-ao-otel-e2e",
                    "draft_transfer_plan",
                    "process_wire_transfer",
                }
            ]
            control_trace_ids = {
                str(record.get("trace_id"))
                for record in control_records
                if record.get("trace_id")
            }
            application_trace_ids = {
                str(record.get("trace_id"))
                for record in application_records
                if record.get("trace_id")
            }
            backend_trace_ids = sorted(control_trace_ids & application_trace_ids)
            if backend_trace_ids:
                matching_control_records = [
                    record
                    for record in control_records
                    if str(record.get("trace_id")) in backend_trace_ids
                ]
                matching_application_records = [
                    record
                    for record in application_records
                    if str(record.get("trace_id")) in backend_trace_ids
                ]
                print("Lab0 OTLP readback verification:")
                print(f"  source_otel_trace_id={source_trace_id}")
                print(f"  expected_backend_trace_id={expected_backend_trace_id}")
                print(f"  backend_trace_ids={backend_trace_ids}")
                print(f"  application_spans={len(matching_application_records)}")
                print(f"  control_spans={len(matching_control_records)}")
                return
            if attempt < 11:
                await asyncio.sleep(5.0)

    raise RuntimeError(
        "Lab0 OTLP readback did not return both application and Agent Control spans "
        "for the configured project and agent stream."
    )


async def _run(args: Any) -> None:
    if _is_o11y():
        os.environ["AGENT_CONTROL_URL"] = args.server_url
        os.environ["AGENT_CONTROL_RUNTIME_AUTH_MODE"] = args.runtime_auth_mode
        resolve_agent_control_api_key()
        resolve_agent_control_api_key_header()
    else:
        demo._configure_splunk_ao_environment(args)
    os.environ["AGENT_CONTROL_OBSERVABILITY_SINK_NAME"] = "otel"
    os.environ["AGENT_CONTROL_OTEL_ENABLED"] = "true"

    import agent_control
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from splunk_ao import otel

    project_id, log_stream_id = await asyncio.to_thread(_resolve_splunk_ao_ids, args)
    endpoint = _otel_endpoint(args)
    headers = _otel_headers(
        args, project_id=project_id, log_stream_id=log_stream_id
    )
    _validate_otel_configuration(args, endpoint, headers)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": args.agent_name,
            }
        )
    )
    processor = otel.add_splunk_ao_span_processor(
        provider,
        project_id=project_id,
        agent_stream_id=log_stream_id,
    )
    trace.set_tracer_provider(provider)
    tracer = provider.get_tracer("agent-control-splunk-ao-otel-demo")

    target_type = args.target_type
    target_id = log_stream_id
    if args.setup_controls:
        control_ids = await ensure_demo_controls(
            agent_name=args.agent_name,
            server_url=args.server_url,
            target_type=target_type,
            target_id=target_id,
        )
        print(f"Prepared and bound {len(control_ids)} controls:")
        for control_name, control_id in zip(
            (name for name, _ in control_specs()), control_ids, strict=True
        ):
            print(f"  {control_name}: {control_id}")

    if not args.skip_runtime_token_check and args.runtime_auth_mode == "jwt":
        await demo._verify_runtime_token_exchange(
            args, target_type=target_type, target_id=target_id
        )

    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Banking transfer demo with OTLP application and control spans",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        runtime_token_header="X-Agent-Control-Runtime-Token",
        runtime_auth_mode=args.runtime_auth_mode,
        observability_enabled=True,
        observability_sink_name="otel",
        observability_sink_config={
            "enabled": True,
            "endpoint": endpoint,
            "headers": headers,
            "service_name": args.agent_name,
        },
        target_type=target_type,
        target_id=target_id,
    )

    luna_config = await demo._verify_bound_controls(
        args, target_type=target_type, target_id=target_id
    )
    skip_luna_control = demo._should_skip_luna_control(args)
    if not args.skip_scorer_invoke_check and not skip_luna_control:
        await demo._verify_scorer_invoke(args, luna_config)

    print()
    print("Telemetry path: OTLP/HTTP only (Splunk AOLogger is not initialized)")
    print(f"OTLP endpoint: {endpoint}")
    print("Agent Control sink: otel")
    print(f"Agent Control target: {target_type}:{target_id}")
    print(f"Splunk AO project_id: {project_id}")
    print(f"Splunk AO log_stream_id: {log_stream_id}")

    transfer = demo._parse_transfer_request(args)
    transfer["fraud_score"] = demo._compute_fraud_score(transfer, args.fraud_score)
    trace_input = {"prompt": args.prompt, "transfer": transfer}
    final_answer = ""
    root_span = None

    try:
        with tracer.start_as_current_span(
            "agent-control-splunk-ao-otel-e2e"
        ) as root_span:
            _set_common_span_attributes(
                root_span, operation="workflow", input_value=trace_input
            )
            root_span.set_attribute("galileo.demo.transport", "otlp_http")
            workflow_trace_context = _otel_trace_context_for_span(root_span)
            agent_control.set_trace_context_provider(lambda: workflow_trace_context)
            blocked_output: str | None = None

            if skip_luna_control:
                draft_response = demo._draft_transfer_plan_impl(args.prompt, transfer)
                print("llm/pre: skipped Luna control execution")
            else:
                with tracer.start_as_current_span(
                    demo.draft_transfer_plan.__name__
                ) as llm_span:
                    _set_common_span_attributes(
                        llm_span, operation="chat", input_value=args.prompt
                    )
                    llm_span.set_attribute("gen_ai.request.model", "demo-rule-based")
                    try:
                        draft_response = await demo.draft_transfer_plan(
                            args.prompt, transfer
                        )
                    except ControlViolationError as exc:
                        blocked_output = demo._control_exception_message("llm/pre", exc)
                    else:
                        llm_span.set_attribute(
                            "gen_ai.output.messages",
                            json.dumps(
                                [{"role": "assistant", "content": draft_response}]
                            ),
                        )

            if blocked_output is None:
                steering_history: list[str] = []
                tool_input = transfer
                tool_output: dict[str, Any] = {}
                for attempt in range(1, args.max_steer_attempts + 1):
                    with tracer.start_as_current_span(
                        demo.process_wire_transfer.__name__
                    ) as tool_span:
                        _set_common_span_attributes(
                            tool_span, operation="execute_tool", input_value=tool_input
                        )
                        tool_span.set_attribute(
                            "gen_ai.tool.name", demo.process_wire_transfer.__name__
                        )
                        tool_span.set_attribute(
                            "gen_ai.tool.call.arguments",
                            json.dumps(tool_input, sort_keys=True),
                        )
                        try:
                            tool_output = await demo.process_wire_transfer(**tool_input)
                        except ControlSteerError as exc:
                            steering_history.append(demo._describe_steering(exc))
                            tool_input = demo._apply_steering_context(tool_input, exc)
                            tool_span.set_attribute("agent_control.retry", True)
                            continue
                        except ControlViolationError as exc:
                            blocked_output = demo._control_exception_message(
                                f"tool attempt {attempt}", exc
                            )
                            break
                        tool_span.set_attribute(
                            "gen_ai.tool.call.result",
                            json.dumps(tool_output, sort_keys=True),
                        )
                        tool_span.set_attribute(
                            "gen_ai.output.messages",
                            json.dumps(
                                [{"role": "tool", "content": tool_output}], default=str
                            ),
                        )
                        break
                else:
                    blocked_output = (
                        "Execution blocked at tool/pre: steering did not converge within "
                        f"{args.max_steer_attempts} attempts."
                    )

            if blocked_output is None:
                final_answer = demo._render_final_answer(
                    draft_response, tool_output, steering_history
                )
            else:
                final_answer = blocked_output
                print(f"Hard block enforced: {blocked_output}")

            root_span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{"role": "assistant", "content": final_answer}]),
            )

        trace_id = _trace_uuid(root_span)
    finally:
        agent_control.clear_trace_context_provider()
        await agent_control.shutdown_observability()
        if not processor.force_flush():
            raise RuntimeError("OTLP application span flush timed out.")
        export_health = processor.export_health
        print(
            "OTLP application export health: "
            f"healthy={export_health.healthy} "
            f"consecutive_failures={export_health.consecutive_failures}"
        )
        provider.shutdown()

    print()
    print(f"OTEL trace_id: {trace_id}")
    print("OTEL export flushed: application spans and Agent Control control spans")

    if args.verify_api:
        await asyncio.sleep(args.verify_delay_seconds)
        await _verify_otel_trace(
            args,
            project_id=project_id,
            log_stream_id=log_stream_id,
            trace_id=trace_id,
        )
    if args.query_trends:
        if not args.verify_api:
            await asyncio.sleep(args.verify_delay_seconds)
        await demo._query_trends(
            args, project_id=project_id, log_stream_id=log_stream_id
        )


def main() -> None:
    args = demo.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
