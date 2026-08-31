#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: I001 -- load the selected dotenv file before app imports

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from dotenv import load_dotenv

load_dotenv(os.environ.get("SPLUNK_AO_ENV_FILE", ".env.splunk-ao"), override=True)

from common_splunk_ao import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_AGENT_NAME,
    DEFAULT_API_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_LOG_STREAM,
    DEFAULT_PROJECT,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

from agent_control import ControlSteerError, ControlViolationError, control

from setup_controls_splunk_ao import control_specs, ensure_demo_controls

LUNA_CONTROL_NAME = os.environ.get(
    "AGENT_CONTROL_LUNA_CONTROL_NAME", "demo-observe-luna-transfer-request"
)
DEFAULT_BANKING_PROMPT = (
    "Wire $15,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-014."
)


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_dev_cluster(args: argparse.Namespace) -> bool:
    urls = (
        getattr(args, "api_base_url", ""),
        getattr(args, "console_url", ""),
        getattr(args, "server_url", ""),
    )
    return any(".dev.galileo.ai" in str(url).rstrip("/") for url in urls)


def _should_skip_luna_control(args: argparse.Namespace) -> bool:
    if getattr(args, "expect_luna_deny", False):
        return False
    explicit = getattr(args, "skip_luna_control", None)
    if explicit is not None:
        return bool(explicit)
    env_value = _env_flag("AGENT_CONTROL_SKIP_LUNA_CONTROL")
    if env_value is not None:
        return env_value
    return _is_dev_cluster(args)


def _agent_control_tool(func: Any) -> Any:
    """Mark a function as a tool before @control() infers its step type."""
    func.tool_name = func.__name__
    return func


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a standalone demo agent and let real Agent Control execution events flow into Splunk AO."
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_BANKING_PROMPT,
        help="Natural-language banking transfer request passed to the demo LLM planning step.",
    )
    parser.add_argument(
        "--expect-luna-deny",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Require the Luna prompt-injection control to deny the LLM pre-check.",
    )
    parser.add_argument(
        "--amount", type=float, default=None, help="Override parsed transfer amount."
    )
    parser.add_argument(
        "--destination-country",
        default=None,
        help="Override parsed transfer destination country.",
    )
    parser.add_argument(
        "--recipient-name",
        default=None,
        help="Override parsed transfer recipient name.",
    )
    parser.add_argument(
        "--fraud-score", type=float, default=None, help="Override computed fraud score."
    )
    parser.add_argument(
        "--max-steer-attempts",
        type=int,
        default=3,
        help="Maximum steering retries for the transfer.",
    )
    parser.add_argument(
        "--project", default=DEFAULT_PROJECT, help="Splunk AO project name."
    )
    parser.add_argument(
        "--log-stream", default=DEFAULT_LOG_STREAM, help="Splunk AO Agent Stream name."
    )
    parser.add_argument(
        "--mode",
        choices=("batch", "distributed"),
        default="batch",
        help="Splunk AO logger mode.",
    )
    parser.add_argument(
        "--agent-name", default=DEFAULT_AGENT_NAME, help="Agent Control agent name."
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_AGENT_CONTROL_URL,
        help="Agent Control server base URL.",
    )
    parser.add_argument(
        "--target-type",
        default=os.environ.get("AGENT_CONTROL_TARGET_TYPE", "log_stream"),
        help="Preferred Agent Control binding target type. The demo auto-detects Console binding aliases.",
    )
    parser.add_argument(
        "--session-name",
        default="agent-control-splunk-ao-e2e",
        help="Splunk AO session name.",
    )
    parser.add_argument(
        "--console-url",
        default=DEFAULT_CONSOLE_URL,
        help="Splunk AO console URL used by the SDK for auth and configuration.",
    )
    parser.add_argument(
        "--api-base-url",
        default=DEFAULT_API_URL,
        help="Splunk AO API base URL used for devstack verification calls.",
    )
    parser.add_argument(
        "--verify-api",
        action="store_true",
        help="Fetch the stored trace from Splunk AO after ingestion and print a short summary.",
    )
    parser.add_argument(
        "--verify-delay-seconds",
        type=float,
        default=10.0,
        help="Delay before API verification to allow ingest/processing to settle.",
    )
    parser.add_argument(
        "--skip-control-setup",
        action="store_true",
        help="Deprecated no-op. Controls are expected to already be bound to the Splunk AO log stream.",
    )
    parser.add_argument(
        "--setup-controls",
        action="store_true",
        help="Create/update demo controls and bind them to the Splunk AO log stream. Off by default.",
    )
    parser.add_argument(
        "--runtime-auth-mode",
        choices=("jwt", "auto", "api_key", "none"),
        default=os.environ.get("AGENT_CONTROL_RUNTIME_AUTH_MODE", "jwt"),
        help="Agent Control runtime auth mode used by evaluation requests.",
    )
    parser.add_argument(
        "--skip-runtime-token-check",
        action="store_true",
        help="Skip the explicit runtime-token exchange preflight.",
    )
    parser.add_argument(
        "--skip-scorer-invoke-check",
        action="store_true",
        help="Skip the direct Splunk AO /scorers/invoke preflight.",
    )
    parser.add_argument(
        "--skip-luna-control",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Skip the decorated Luna LLM control path. Defaults on for dev.galileo.ai, "
            "where SLM metrics are not available."
        ),
    )
    parser.add_argument(
        "--query-trends",
        action="store_true",
        help="Call the trends dashboard and custom-metrics APIs after verifying ingestion.",
    )
    parser.add_argument(
        "--trends-lookback-hours",
        type=float,
        default=2.0,
        help="How far back to query when generating control trend charts.",
    )
    parser.add_argument(
        "--trends-interval-minutes",
        type=int,
        default=5,
        help="Bucket size, in minutes, for trend chart queries.",
    )
    parser.add_argument(
        "--trends-retries",
        type=int,
        default=6,
        help="How many times to retry trend queries while waiting for fresh spans to appear.",
    )
    parser.add_argument(
        "--trends-retry-delay-seconds",
        type=float,
        default=5.0,
        help="Delay between trend-query retries.",
    )
    return parser.parse_args()


def _parse_transfer_request(args: argparse.Namespace) -> dict[str, Any]:
    import re

    prompt = args.prompt
    amount = args.amount
    if amount is None:
        amount_match = re.search(
            r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k)?", prompt, re.IGNORECASE
        )
        amount = 15000.0
        if amount_match:
            amount = float(amount_match.group(1).replace(",", ""))
            if amount_match.group(2):
                amount *= 1000

    destination_country = args.destination_country
    if destination_country is None:
        country_match = re.search(
            r"\bin\s+(?:the\s+)?([A-Za-z ]+?)(?:\s+for|\s+from|\.|$)", prompt
        )
        if country_match is None:
            country_match = re.search(
                r"\bto\s+(?:the\s+)?([A-Za-z ]+?)(?:\s+for|\s+from|\.|$)", prompt
            )
        destination_country = (
            country_match.group(1).strip() if country_match else "United States"
        )

    recipient_name = args.recipient_name
    if recipient_name is None:
        recipient_match = re.search(
            r"\b(?:to|for)\s+([A-Z][A-Za-z0-9 &.-]+?)(?:\s+in|\s+for|\.|$)", prompt
        )
        recipient_name = (
            recipient_match.group(1).strip() if recipient_match else "Unknown Recipient"
        )

    return {
        "amount": float(amount),
        "destination_country": destination_country,
        "recipient_name": recipient_name,
        "verified_2fa": False,
        "manager_approved": False,
        "justification": None,
    }


def _compute_fraud_score(transfer: dict[str, Any], override: float | None) -> float:
    if override is not None:
        return override
    destination = str(transfer.get("destination_country", "")).lower()
    if any(
        country in destination
        for country in ("north korea", "iran", "syria", "cuba", "crimea")
    ):
        return 0.95
    if float(transfer.get("amount", 0)) >= 50000:
        return 0.35
    return 0.1


def _draft_transfer_plan_impl(prompt: str, transfer: dict[str, Any]) -> str:
    return (
        "I parsed a wire-transfer request and will send it through compliance controls before execution. "
        f"Transfer: ${transfer['amount']:,.2f} to {transfer['recipient_name']} "
        f"({transfer['destination_country']})."
    )


@control()
async def draft_transfer_plan(prompt: str, transfer: dict[str, Any]) -> str:
    return _draft_transfer_plan_impl(prompt, transfer)


@control()
@_agent_control_tool
async def process_wire_transfer(
    *,
    amount: float,
    destination_country: str,
    recipient_name: str,
    verified_2fa: bool = False,
    manager_approved: bool = False,
    justification: str | None = None,
    fraud_score: float | None = None,
) -> dict[str, Any]:
    return {
        "status": "completed",
        "transaction_id": f"TXN-{abs(hash((recipient_name, amount))) % 100000:05d}",
        "amount": amount,
        "destination_country": destination_country,
        "recipient_name": recipient_name,
        "verified_2fa": verified_2fa,
        "manager_approved": manager_approved,
        "fraud_score": fraud_score,
        "justification": justification,
    }


def _render_final_answer(
    draft: str,
    transfer_result: dict[str, Any],
    steering_history: list[str],
) -> str:
    steering_summary = (
        "\nControls applied:\n" + "\n".join(f"- {item}" for item in steering_history)
        if steering_history
        else "\nControls applied: none"
    )
    return (
        f"{draft}\n\n"
        f"Transfer status: {transfer_result['status']}\n"
        f"Transaction ID: {transfer_result['transaction_id']}\n"
        f"Amount: ${transfer_result['amount']:,.2f}\n"
        f"Recipient: {transfer_result['recipient_name']}\n"
        f"Destination: {transfer_result['destination_country']}"
        f"{steering_summary}"
    )


def _steering_context_message(match: Any) -> str:
    steering_context = getattr(match, "steering_context", None)
    if isinstance(steering_context, str) and steering_context:
        return steering_context
    message = getattr(steering_context, "message", None)
    if isinstance(message, str) and message:
        return message
    result_message = getattr(getattr(match, "result", None), "message", None)
    return result_message if isinstance(result_message, str) else "{}"


def _apply_steering_context(transfer: dict[str, Any], match: Any) -> dict[str, Any]:
    message = _steering_context_message(match)
    try:
        steering_data = json.loads(message)
    except json.JSONDecodeError:
        steering_data = _parse_partial_steering_context(message)

    retry_flags = steering_data.get("retry_flags")
    if not isinstance(retry_flags, dict):
        retry_flags = {}

    updated = transfer | retry_flags
    reason = steering_data.get("reason") or "Additional verification required."
    required_actions = steering_data.get("required_actions") or []
    print(
        "Steering applied: "
        f"control={getattr(match, 'control_name', 'unknown-control')} "
        f"required_actions={required_actions} reason={reason}"
    )
    print(f"  retry_flags={json.dumps(retry_flags, sort_keys=True)}")
    return updated


def _parse_partial_steering_context(message: str) -> dict[str, Any]:
    steering_data: dict[str, Any] = {"retry_flags": {}, "reason": message}

    retry_flags_match = re.search(r'"retry_flags"\s*:\s*(\{[^{}]*\})', message)
    if retry_flags_match is not None:
        try:
            retry_flags = json.loads(retry_flags_match.group(1))
        except json.JSONDecodeError:
            retry_flags = {}
        if isinstance(retry_flags, dict):
            steering_data["retry_flags"] = retry_flags

    required_actions_match = re.search(
        r'"required_actions"\s*:\s*(\[[^\[\]]*\])', message
    )
    if required_actions_match is not None:
        try:
            required_actions = json.loads(required_actions_match.group(1))
        except json.JSONDecodeError:
            required_actions = []
        if isinstance(required_actions, list):
            steering_data["required_actions"] = required_actions

    reason_match = re.search(r'"reason"\s*:\s*"([^"]+)"', message)
    if reason_match is not None:
        steering_data["reason"] = reason_match.group(1)

    return steering_data


def _describe_steering(match: Any) -> str:
    message = _steering_context_message(match)
    try:
        steering_data = json.loads(message)
    except json.JSONDecodeError:
        steering_data = _parse_partial_steering_context(message)
    reason = steering_data.get("reason") or "Additional verification required."
    return f"{getattr(match, 'control_name', 'unknown-control')}: {reason}"


def _control_exception_message(stage: str, exc: ControlViolationError) -> str:
    return f"Execution blocked at {stage} by {exc.control_name}: {exc.message}"


def _control_exception_rows(
    exc: ControlViolationError | ControlSteerError, action: str
) -> list[dict[str, Any]]:
    return [
        {
            "control": exc.control_name,
            "action": action,
            "matched": True,
            "confidence": None,
            "message": exc.message,
        }
    ]


def _collect_control_spans(workflow: Any) -> list[Any]:
    return [
        span
        for span in getattr(workflow, "spans", [])
        if getattr(span, "type", None) == "control"
    ]


def _count_control_spans_in_payload(node: dict[str, Any]) -> int:
    count = 1 if node.get("type") == "control" else 0
    for child in node.get("spans", []) or []:
        count += _count_control_spans_in_payload(child)
    return count


def _resolve_api_base_url(args: argparse.Namespace) -> str:
    if args.api_base_url:
        return args.api_base_url.rstrip("/")
    if args.console_url:
        return args.console_url.replace("console", "api", 1).rstrip("/")
    raise RuntimeError(
        "Missing Splunk AO API base URL. Set SPLUNK_AO_API_URL or pass --api-base-url."
    )


def _configure_splunk_ao_environment(args: argparse.Namespace) -> None:
    if args.console_url:
        os.environ["SPLUNK_AO_CONSOLE_URL"] = args.console_url
    if args.api_base_url:
        os.environ["SPLUNK_AO_API_URL"] = args.api_base_url
    os.environ["AGENT_CONTROL_URL"] = args.server_url
    os.environ["AGENT_CONTROL_RUNTIME_AUTH_MODE"] = args.runtime_auth_mode
    resolve_agent_control_api_key()
    resolve_agent_control_api_key_header()


def _iso8601_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _text_filter(
    name: str, value: str, *, case_sensitive: bool = True
) -> dict[str, Any]:
    return {
        "filter": {
            "name": name,
            "operator": "eq",
            "value": value,
            "case_sensitive": case_sensitive,
            "type": "text",
        }
    }


def _and_filter(*filters: dict[str, Any]) -> dict[str, Any]:
    return {"and_": [item for item in filters if item]}


def _build_trend_query_definitions(
    *,
    log_stream_id: str,
    agent_name: str,
    start_time: datetime,
    end_time: datetime,
    interval_minutes: int,
) -> list[dict[str, Any]]:
    base_filter = _and_filter(
        _text_filter("type", "control"),
        _text_filter("agent_name", agent_name),
    )
    return [
        {
            "label": "control-counts-by-name-and-stage",
            "body": {
                "log_stream_id": log_stream_id,
                "start_time": _iso8601_utc(start_time),
                "end_time": _iso8601_utc(end_time),
                "interval_minutes": interval_minutes,
                "group_by": "control.check_stage",
                "metric_details": [
                    {
                        "id": "control_name_counts",
                        "metric_name": "control.name",
                        "aggregation": "Count",
                    }
                ],
                "filter_tree": base_filter,
            },
        },
        {
            "label": "tool-pre-control-match-status",
            "body": {
                "log_stream_id": log_stream_id,
                "start_time": _iso8601_utc(start_time),
                "end_time": _iso8601_utc(end_time),
                "interval_minutes": interval_minutes,
                "group_by": "control.output.matched",
                "metric_details": [
                    {
                        "id": "tool_control_names",
                        "metric_name": "control.name",
                        "aggregation": "Count",
                    }
                ],
                "filter_tree": _and_filter(
                    _text_filter("type", "control"),
                    _text_filter("agent_name", agent_name),
                    _text_filter("applies_to", "tool_call"),
                    _text_filter("check_stage", "pre"),
                ),
            },
        },
        {
            "label": "matched-actions-for-transfer-controls",
            "body": {
                "log_stream_id": log_stream_id,
                "start_time": _iso8601_utc(start_time),
                "end_time": _iso8601_utc(end_time),
                "interval_minutes": interval_minutes,
                "group_by": "control.output.action",
                "metric_details": [
                    {
                        "id": "matched_outcomes",
                        "metric_name": "control.output.matched",
                        "aggregation": "Count",
                    }
                ],
                "filter_tree": _and_filter(
                    _text_filter("type", "control"),
                    _text_filter("agent_name", agent_name),
                    _text_filter("applies_to", "tool_call"),
                ),
            },
        },
        {
            "label": "selector-paths-for-llm-controls",
            "body": {
                "log_stream_id": log_stream_id,
                "start_time": _iso8601_utc(start_time),
                "end_time": _iso8601_utc(end_time),
                "interval_minutes": interval_minutes,
                "group_by": "control.evaluator_name",
                "metric_details": [
                    {
                        "id": "selector_paths",
                        "metric_name": "control.selector_path",
                        "aggregation": "Count",
                    }
                ],
                "filter_tree": _and_filter(
                    _text_filter("type", "control"),
                    _text_filter("agent_name", agent_name),
                    _text_filter("applies_to", "llm_call"),
                ),
            },
        },
    ]


def _payload_has_non_zero_metrics(payload: dict[str, Any]) -> bool:
    aggregate_metrics = payload.get("aggregate_metrics", {})
    for value in aggregate_metrics.values():
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, int | float) and value > 0:
            return True
    return False


def _summarize_bucketed_metrics(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for group_name, buckets in payload.get("bucketed_metrics", {}).items():
        latest_non_zero: dict[str, Any] | None = None
        for bucket in reversed(buckets):
            metric_values = {
                key: value
                for key, value in bucket.items()
                if key not in {"start_bucket_time", "end_bucket_time"}
                and value not in (0, 0.0, {}, [], None, "")
            }
            if metric_values:
                latest_non_zero = metric_values
                break
        if latest_non_zero is None:
            continue
        lines.append(f"    {group_name}: {json.dumps(latest_non_zero, sort_keys=True)}")
    return lines[:6]


def _control_data(control: dict[str, Any]) -> dict[str, Any]:
    data = control.get("data")
    if isinstance(data, dict):
        return data
    nested = control.get("control")
    if isinstance(nested, dict):
        return nested
    return {}


def _iter_condition_evaluators(
    condition: Any,
) -> list[tuple[str | None, dict[str, Any]]]:
    if not isinstance(condition, dict):
        return []

    evaluator = condition.get("evaluator")
    if isinstance(evaluator, dict):
        config = evaluator.get("config")
        return [(evaluator.get("name"), config if isinstance(config, dict) else {})]

    evaluators: list[tuple[str | None, dict[str, Any]]] = []
    for key in ("and", "or"):
        children = condition.get(key)
        if isinstance(children, list):
            for child in children:
                evaluators.extend(_iter_condition_evaluators(child))

    if "not" in condition:
        evaluators.extend(_iter_condition_evaluators(condition.get("not")))

    return evaluators


def _condition_evaluators(
    control_data: dict[str, Any],
) -> list[tuple[str | None, dict[str, Any]]]:
    return _iter_condition_evaluators(control_data.get("condition"))


def _find_evaluator_config(
    control_data: dict[str, Any],
    evaluator_name: str,
) -> dict[str, Any] | None:
    for name, config in _condition_evaluators(control_data):
        if name == evaluator_name:
            return config
    return None


def _values_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(actual, list) and {
            str(value).lower() for value in expected
        } <= {str(value).lower() for value in actual}
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            _values_match(value, actual.get(key)) for key, value in expected.items()
        )
    return expected == actual


def _control_matches_spec(
    control: dict[str, Any], expected_spec: dict[str, Any]
) -> bool:
    data = _control_data(control)
    expected_scope = expected_spec.get("scope", {})
    actual_scope = data.get("scope", {})
    if not _values_match(expected_scope, actual_scope):
        return False

    expected_action = expected_spec.get("action", {})
    actual_action = data.get("action", {})

    expected_condition = expected_spec.get("condition", {})
    actual_condition = data.get("condition", {})
    expected_selector = expected_condition.get("selector", {})
    actual_selector = actual_condition.get("selector", {})
    if expected_selector.get("path") != actual_selector.get("path"):
        return False

    expected_evaluator = expected_condition.get("evaluator", {})
    expected_evaluator_name = expected_evaluator.get("name")
    actual_evaluator_config = _find_evaluator_config(data, expected_evaluator_name)
    if actual_evaluator_config is None:
        return False

    if expected_evaluator_name == "galileo.luna":
        return (
            actual_action.get("decision") in {"observe", "deny"}
            and "scorer_label" in actual_evaluator_config
        )

    if expected_action.get("decision") != actual_action.get("decision"):
        return False

    expected_config = expected_evaluator.get("config", {})
    return _values_match(expected_config, actual_evaluator_config)


def _find_bound_control(
    controls: list[dict[str, Any]],
    expected_name: str,
    expected_spec: dict[str, Any],
) -> dict[str, Any] | None:
    for control_item in controls:
        if control_item.get("name") == expected_name:
            return control_item

    clone_prefix = f"{expected_name}-clone-"
    for control_item in controls:
        name = control_item.get("name")
        if isinstance(name, str) and name.startswith(clone_prefix):
            return control_item

    for control_item in controls:
        if _control_matches_spec(control_item, expected_spec):
            return control_item

    return None


def _control_summary(control: dict[str, Any]) -> str:
    data = _control_data(control)
    scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    evaluators = [name for name, _ in _condition_evaluators(data)]
    return (
        f"name={control.get('name')!r} action={action.get('decision')!r} "
        f"step_types={scope.get('step_types')} step_names={scope.get('step_names')} "
        f"step_name_regex={scope.get('step_name_regex')!r} stages={scope.get('stages')} "
        f"evaluators={evaluators}"
    )


def _scope_applies_to_step(
    scope: dict[str, Any], *, step_type: str, step_name: str, stage: str
) -> str | None:
    step_types = scope.get("step_types")
    if isinstance(step_types, list) and step_type not in step_types:
        return f"step_types={step_types!r} does not include {step_type!r}"

    stages = scope.get("stages")
    if isinstance(stages, list) and stage not in stages:
        return f"stages={stages!r} does not include {stage!r}"

    step_names = scope.get("step_names")
    if isinstance(step_names, list) and step_name not in step_names:
        return f"step_names={step_names!r} does not include {step_name!r}"

    step_name_regex = scope.get("step_name_regex")
    if isinstance(step_name_regex, str) and step_name_regex:
        try:
            if re.search(step_name_regex, step_name) is None:
                return (
                    f"step_name_regex={step_name_regex!r} does not match {step_name!r}"
                )
        except re.error as exc:
            return f"step_name_regex={step_name_regex!r} is invalid: {exc}"

    return None


async def _verify_bound_controls(
    args: argparse.Namespace,
    *,
    target_type: str,
    target_id: str,
) -> dict[str, Any] | None:
    api_key = resolve_agent_control_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing AGENT_CONTROL_API_KEY or SPLUNK_AO_API_KEY in the environment."
        )

    params = {
        "rendered_state": "rendered",
        "enabled_state": "enabled",
        "target_type": target_type,
        "target_id": target_id,
    }
    headers = {resolve_agent_control_api_key_header(): api_key}
    async with httpx.AsyncClient(
        base_url=args.server_url.rstrip("/"), headers=headers, timeout=60.0
    ) as client:
        response = await client.get(
            f"/api/v1/agents/{args.agent_name}/controls", params=params
        )
        response.raise_for_status()
        controls = response.json().get("controls", [])

    if not isinstance(controls, list):
        raise TypeError("Agent Control returned an invalid controls payload.")

    valid_controls = [control for control in controls if isinstance(control, dict)]
    matched_controls: dict[str, dict[str, Any]] = {}
    for expected_name, expected_spec in control_specs():
        matched = _find_bound_control(valid_controls, expected_name, expected_spec)
        if matched is not None:
            matched_controls[expected_name] = matched

    required_names = [name for name, _ in control_specs()]
    missing = [name for name in required_names if name not in matched_controls]

    luna_control = matched_controls.get(LUNA_CONTROL_NAME)
    if luna_control is None:
        for name, control in matched_controls.items():
            if (
                _find_evaluator_config(_control_data(control), "galileo.luna")
                is not None
            ):
                luna_control = control
                break
    if luna_control is None:
        if args.expect_luna_deny:
            raise RuntimeError(
                "The demo agent target did not return an enabled Splunk AO Luna deny control."
            )
        luna_config = None
    else:
        luna_data = _control_data(luna_control)
        luna_action = luna_data.get("action")
        luna_decision = (
            luna_action.get("decision") if isinstance(luna_action, dict) else None
        )
        if args.expect_luna_deny and luna_decision != "deny":
            raise RuntimeError(
                f"{LUNA_CONTROL_NAME} must be configured with action.decision='deny'; "
                f"found {luna_decision!r}."
            )
        if luna_decision not in {"observe", "deny"}:
            print(
                f"Warning: {LUNA_CONTROL_NAME} should use action.decision='observe' or 'deny'; "
                f"found {luna_decision!r}."
            )

        luna_config = _find_evaluator_config(luna_data, "galileo.luna")
        if luna_config is None:
            if args.expect_luna_deny:
                raise RuntimeError(
                    "Luna control is present but does not include evaluator 'galileo.luna'."
                )
            print(
                "Warning: Luna control is present but does not include evaluator 'galileo.luna'."
            )
        elif "scorer_label" not in luna_config or "metric" in luna_config:
            message = (
                "Luna control config is not using the new scorer_label schema: "
                f"{sorted(luna_config.keys())}"
            )
            if args.expect_luna_deny:
                raise RuntimeError(message)
            print(f"Warning: {message}")
        if luna_config is not None:
            stale_luna_keys = {"on_error", "payload_field"} & luna_config.keys()
            if stale_luna_keys:
                print(
                    "Luna control config includes legacy keys that are ignored by the current "
                    f"scorer invoke path: {sorted(stale_luna_keys)}"
                )

    print()
    print("Agent Control target controls verification:")
    print(f"  target={target_type}:{target_id}")
    print(f"  effective_controls={len(controls)}")
    if missing:
        print("  expected demo controls currently disabled or not returned:")
        for name in missing:
            print(f"    - {name}")
    for name in required_names:
        control = matched_controls.get(name)
        if control is None:
            continue
        data = _control_data(control)
        scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
        evaluators = [evaluator for evaluator, _ in _condition_evaluators(data)]
        actual_name = control.get("name")
        name_note = "" if actual_name == name else f" actual_name={actual_name!r}"
        print(f"  {name}: id={control.get('id')}{name_note} evaluators={evaluators}")
        print(f"    {_control_summary(control)}")

        if name == LUNA_CONTROL_NAME:
            applies_error = _scope_applies_to_step(
                scope,
                step_type="llm",
                step_name=draft_transfer_plan.__name__,
                stage="pre",
            )
        else:
            applies_error = _scope_applies_to_step(
                scope,
                step_type="tool",
                step_name=process_wire_transfer.__name__,
                stage="pre",
            )
        if applies_error is not None:
            print(f"    Warning: control scope will not apply: {applies_error}")
    if not valid_controls:
        print("  controls returned for this log stream: none")
    else:
        print("  controls returned for this log stream:")
        for control in valid_controls[:20]:
            print(f"    - {_control_summary(control)}")

    return luna_config


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.request(method=method, url=url, headers=headers, json=body)
    response.raise_for_status()
    return response.json()


async def _login_with_api_key(
    client: httpx.AsyncClient, api_base_url: str, api_key: str
) -> dict[str, str]:
    login_payload = await _fetch_json(
        client,
        f"{api_base_url}/login/api_key",
        method="POST",
        body={"api_key": api_key},
    )
    return {"Authorization": f"Bearer {login_payload['access_token']}"}


async def _verify_runtime_token_exchange(
    args: argparse.Namespace,
    *,
    target_type: str,
    target_id: str,
) -> None:
    api_key = resolve_agent_control_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing AGENT_CONTROL_API_KEY or SPLUNK_AO_API_KEY in the environment."
        )

    async with httpx.AsyncClient(
        base_url=args.server_url.rstrip("/"), timeout=60.0
    ) as client:
        response = await client.post(
            "/api/v1/auth/runtime-token-exchange",
            headers={resolve_agent_control_api_key_header(): api_key},
            json={"target_type": target_type, "target_id": target_id},
        )
        response.raise_for_status()
        payload = response.json()

    token = payload.get("token") or payload.get("runtime_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(
            "Runtime token exchange succeeded but did not return a token."
        )

    print()
    print("Agent Control runtime JWT verification:")
    print(f"  server_url={args.server_url.rstrip('/')}")
    print(f"  target={payload.get('target_type')}:{payload.get('target_id')}")
    print(f"  scopes={payload.get('scopes')}")
    print(f"  expires_at={payload.get('expires_at')}")


async def _verify_scorer_invoke(
    args: argparse.Namespace, luna_config: dict[str, Any] | None = None
) -> None:
    api_key = os.environ.get("SPLUNK_AO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing SPLUNK_AO_API_KEY in the environment.")

    api_base_url = _resolve_api_base_url(args)
    scorer_label = luna_config.get("scorer_label") if luna_config else None
    if not isinstance(scorer_label, str) or not scorer_label:
        print()
        print(
            "Splunk AO scorer invoke verification: skipped; no enabled Luna control was returned."
        )
        return

    body: dict[str, Any] = {
        "inputs": {"query": args.prompt},
        "scorer_label": scorer_label,
    }
    if luna_config:
        for key in ("scorer_id", "scorer_version_id"):
            value = luna_config.get(key)
            if isinstance(value, str) and value:
                body[key] = value

    async with httpx.AsyncClient(timeout=60.0) as client:
        payload = await _fetch_json(
            client,
            f"{api_base_url}/scorers/invoke",
            headers={"Splunk-AO-API-Key": api_key},
            method="POST",
            body=body,
        )

    if str(payload.get("status", "")).lower() != "success":
        raise RuntimeError(f"/scorers/invoke returned non-success status: {payload}")

    print()
    print("Splunk AO scorer invoke verification:")
    print(f"  api_base_url={api_base_url}")
    print("  project_id=not-sent")
    print(f"  scorer_id={'sent' if body.get('scorer_id') else 'not-sent'}")
    print(
        f"  scorer_version_id={'sent' if body.get('scorer_version_id') else 'not-sent'}"
    )
    print(f"  scorer_label={payload.get('scorer_label')}")
    print(f"  status={payload.get('status')}")
    print(f"  score_type={type(payload.get('score')).__name__}")
    print(f"  execution_time={payload.get('execution_time')}")


async def _verify_trace(
    args: argparse.Namespace,
    *,
    project_id: str,
    log_stream_id: str,
    session_id: str | None,
    trace_id: str,
    control_span_ids: list[str],
) -> None:
    api_key = os.environ.get("SPLUNK_AO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing SPLUNK_AO_API_KEY in the environment.")

    api_base_url = _resolve_api_base_url(args)

    async with httpx.AsyncClient(timeout=60.0) as client:
        current_user = await _fetch_json(
            client,
            f"{api_base_url}/current_user",
            headers={"Splunk-AO-API-Key": api_key},
        )
        ingest_health = await _fetch_json(client, f"{api_base_url}/ingest/healthz")
        bearer_headers = await _login_with_api_key(client, api_base_url, api_key)
        trace_payload = await _fetch_json(
            client,
            f"{api_base_url}/projects/{project_id}/traces/{trace_id}",
            headers=bearer_headers,
        )

        session_payload = None
        if session_id:
            session_payload = await _fetch_json(
                client,
                f"{api_base_url}/projects/{project_id}/sessions/{session_id}",
                headers=bearer_headers,
            )

        partial_search_payload = await _fetch_json(
            client,
            f"{api_base_url}/projects/{project_id}/spans/partial_search",
            headers=bearer_headers,
            method="POST",
            body={
                "log_stream_id": log_stream_id,
                "filter_tree": {
                    "filter": {
                        "name": "type",
                        "operator": "eq",
                        "value": "control",
                        "type": "text",
                    }
                },
                "pagination": {"limit": 20},
                "select_columns": {
                    "column_ids": [
                        "id",
                        "trace_id",
                        "type",
                        "control_id",
                        "agent_name",
                        "check_stage",
                        "applies_to",
                        "evaluator_name",
                        "selector_path",
                        "output",
                        "user_metadata",
                    ],
                    "include_all_metrics": False,
                    "include_all_feedback": False,
                },
            },
        )

        fetched_control_span = None
        fetched_control_span_lookup_status = "skipped"
        if control_span_ids:
            try:
                fetched_control_span = await _fetch_json(
                    client,
                    f"{api_base_url}/projects/{project_id}/spans/{control_span_ids[0]}",
                    headers=bearer_headers,
                )
                fetched_control_span_lookup_status = "found"
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                fetched_control_span_lookup_status = "not_found"

    control_count = _count_control_spans_in_payload(trace_payload)
    matching_partial_records = [
        record
        for record in partial_search_payload.get("records", [])
        if record.get("trace_id") == trace_id
    ]

    if control_count == 0 or not matching_partial_records:
        raise RuntimeError(
            "Devstack verification did not return any ControlSpan records for this trace."
        )

    print()
    print("Splunk AO API verification:")
    print(f"  api_base_url={api_base_url}")
    print(
        f"  current_user={current_user.get('email') or current_user.get('id') or 'unknown'}"
    )
    print(
        f"  ingest_health={ingest_health.get('status') or ingest_health.get('name') or 'ok'}"
    )
    print(f"  project_id={project_id}")
    print(f"  log_stream_id={log_stream_id}")
    print(f"  session_id={session_id or 'none'}")
    print(f"  trace_id={trace_id}")
    print(f"  control_spans_in_trace_payload={control_count}")
    print(
        f"  control_spans_in_partial_search_for_trace={len(matching_partial_records)}"
    )
    if session_payload is not None:
        print(f"  session_lookup_id={session_payload.get('id', session_id)}")
    print(f"  sample_control_span_lookup={fetched_control_span_lookup_status}")
    if fetched_control_span is not None:
        print(
            "  "
            f"sample_control_span=id={fetched_control_span.get('id')} "
            f"control_id={fetched_control_span.get('control_id')} "
            f"check_stage={fetched_control_span.get('check_stage')}"
        )


async def _query_trends(
    args: argparse.Namespace,
    *,
    project_id: str,
    log_stream_id: str,
) -> None:
    api_key = os.environ.get("SPLUNK_AO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing SPLUNK_AO_API_KEY in the environment.")

    api_base_url = _resolve_api_base_url(args)
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(hours=args.trends_lookback_hours)
    query_definitions = _build_trend_query_definitions(
        log_stream_id=log_stream_id,
        agent_name=args.agent_name,
        start_time=start_time,
        end_time=end_time,
        interval_minutes=args.trends_interval_minutes,
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        bearer_headers = await _login_with_api_key(client, api_base_url, api_key)
        dashboard_payload = await _fetch_json(
            client,
            f"{api_base_url}/projects/{project_id}/log_streams/{log_stream_id}/trends",
            headers=bearer_headers,
        )

        results: list[tuple[str, dict[str, Any]]] = []
        for attempt in range(1, args.trends_retries + 1):
            results = []
            for definition in query_definitions:
                payload = await _fetch_json(
                    client,
                    f"{api_base_url}/projects/{project_id}/metrics/custom_search",
                    headers=bearer_headers,
                    method="POST",
                    body=definition["body"],
                )
                results.append((definition["label"], payload))

            if any(_payload_has_non_zero_metrics(payload) for _, payload in results):
                break

            if attempt < args.trends_retries:
                await asyncio.sleep(args.trends_retry_delay_seconds)

    if not any(_payload_has_non_zero_metrics(payload) for _, payload in results):
        raise RuntimeError(
            "Trend queries completed, but none of the control chart payloads contained data."
        )

    print()
    print("Trends API verification:")
    print(f"  dashboard_id={dashboard_payload.get('id')}")
    print(f"  dashboard_name={dashboard_payload.get('name')}")
    print(f"  dashboard_sections={len(dashboard_payload.get('sections', []))}")
    print(f"  query_window={_iso8601_utc(start_time)} -> {_iso8601_utc(end_time)}")
    print(f"  interval_minutes={args.trends_interval_minutes}")

    for label, payload in results:
        bucketed_metrics = payload.get("bucketed_metrics", {})
        aggregate_metrics = payload.get("aggregate_metrics", {})
        print(f"  {label}:")
        print(f"    aggregate_metrics={json.dumps(aggregate_metrics, sort_keys=True)}")
        print(f"    groups={list(bucketed_metrics.keys())}")
        summary_lines = _summarize_bucketed_metrics(payload)
        if summary_lines:
            for line in summary_lines:
                print(line)
        else:
            print("    no non-zero buckets returned")


async def _run(args: argparse.Namespace) -> None:
    _configure_splunk_ao_environment(args)
    import agent_control
    from splunk_ao.logger import SplunkAOLogger

    logger = SplunkAOLogger(
        project=args.project, agent_stream=args.log_stream, mode=args.mode
    )
    auto_bridge = getattr(logger, "_agent_control_bridge", None)
    session_external_id = f"agent-control-splunk-ao-e2e-{uuid4()}"
    session_id = logger.start_session(
        name=args.session_name,
        external_id=session_external_id,
        metadata={"demo": "agent-control-splunk-ao-e2e"},
    )
    if logger.project_id is None or logger.agent_stream_id is None:
        raise RuntimeError("Splunk AO logger did not resolve project/log stream IDs.")

    target_id = logger.agent_stream_id
    target_type = args.target_type

    if args.setup_controls:
        control_ids = await ensure_demo_controls(
            agent_name=args.agent_name,
            server_url=args.server_url,
            target_type=target_type,
            target_id=target_id,
        )
        print(f"Prepared {len(control_ids)} controls for agent '{args.agent_name}'.")
        for control_name, control_id in zip(
            (name for name, _ in control_specs()), control_ids, strict=True
        ):
            print(f"  {control_name}: {control_id}")
        print(f"Bound controls to {target_type}: {target_id}")
        print()

    if not args.skip_runtime_token_check and args.runtime_auth_mode == "jwt":
        await _verify_runtime_token_exchange(
            args, target_type=target_type, target_id=target_id
        )

    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Standalone banking transfer demo for Agent Control + Splunk AO control spans",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=target_type,
        target_id=target_id,
    )

    luna_config = await _verify_bound_controls(
        args,
        target_type=target_type,
        target_id=target_id,
    )
    skip_luna_control = _should_skip_luna_control(args)
    if skip_luna_control:
        print()
        print("Luna LLM control execution: skipped for this cluster.")
    if not args.skip_scorer_invoke_check and not skip_luna_control:
        await _verify_scorer_invoke(args, luna_config)
    elif not args.skip_scorer_invoke_check and skip_luna_control:
        print(
            "Splunk AO scorer invoke verification: skipped because Luna LLM control execution is skipped."
        )

    print(
        f"Automatic Splunk AO Agent Control bridge: {'enabled' if auto_bridge is not None else 'missing'}"
    )
    print(
        f"Registered Agent Control sinks: {len(agent_control.get_registered_control_event_sinks())}"
    )
    print(f"Agent Control server_url: {args.server_url.rstrip('/')}")
    print(f"Agent Control runtime_auth_mode: {args.runtime_auth_mode}")
    print(f"Agent Control target: {target_type}:{target_id}")
    print(f"Agent Control LLM function: {draft_transfer_plan.__name__}")
    print(f"Agent Control tool function: {process_wire_transfer.__name__}")
    print(f"Splunk AO project_id: {logger.project_id}")
    print(f"Splunk AO log_stream_id: {logger.agent_stream_id}")
    print(f"Splunk AO session_id: {session_id}")

    transfer = _parse_transfer_request(args)
    transfer["fraud_score"] = _compute_fraud_score(transfer, args.fraud_score)

    trace_input = {"prompt": args.prompt, "transfer": transfer}
    trace = logger.start_trace(input=trace_input, name="agent-control-splunk-ao-e2e")
    workflow = logger.add_workflow_span(
        input=json.dumps(trace_input, sort_keys=True),
        name="banking_transfer_workflow",
    )
    blocked_output: str | None = None

    try:
        # The current Splunk AO bridge gets trace/span IDs from the active logger parent.
        # Keep the workflow active while Agent Control emits evaluation events.
        if skip_luna_control:
            draft_response = _draft_transfer_plan_impl(args.prompt, transfer)
            print("llm/pre: skipped Luna control execution")
        else:
            try:
                draft_response = await draft_transfer_plan(args.prompt, transfer)
            except ControlViolationError as exc:
                if args.expect_luna_deny and exc.control_name != LUNA_CONTROL_NAME:
                    raise RuntimeError(
                        f"Expected {LUNA_CONTROL_NAME} to deny during llm/pre, "
                        f"but {exc.control_name} denied instead."
                    ) from exc
                blocked_output = _control_exception_message("llm/pre", exc)
            else:
                if args.expect_luna_deny:
                    raise RuntimeError(
                        f"Expected {LUNA_CONTROL_NAME} to match with action=deny during llm/pre, "
                        "but the decorated LLM function completed."
                    )

        if blocked_output is None:
            logger.add_llm_span(
                input=args.prompt,
                output=draft_response,
                model="demo-rule-based",
                name=draft_transfer_plan.__name__,
                metadata={
                    "amount": transfer["amount"],
                    "destination_country": transfer["destination_country"],
                    "recipient_name": transfer["recipient_name"],
                },
            )

            steering_history: list[str] = []
            tool_input = transfer
            for attempt in range(1, args.max_steer_attempts + 1):
                try:
                    tool_output = await process_wire_transfer(**tool_input)
                except ControlSteerError as exc:
                    steering_history.append(_describe_steering(exc))
                    tool_input = _apply_steering_context(tool_input, exc)
                    continue
                except ControlViolationError as exc:
                    blocked_output = _control_exception_message(
                        f"tool attempt {attempt}", exc
                    )
                    break
                logger.add_tool_span(
                    input=json.dumps(tool_input, sort_keys=True),
                    output=json.dumps(tool_output, sort_keys=True),
                    name=process_wire_transfer.__name__,
                    metadata={
                        "amount": tool_input["amount"],
                        "destination_country": tool_input["destination_country"],
                        "recipient_name": tool_input["recipient_name"],
                        "steering_attempts": len(steering_history),
                    },
                )
                break
            else:
                blocked_output = (
                    "Execution blocked at tool/pre: steering did not converge within "
                    f"{args.max_steer_attempts} attempts."
                )

        if blocked_output is None:
            final_answer = _render_final_answer(
                draft_response, tool_output, steering_history
            )
        else:
            final_answer = blocked_output
            print(f"Hard block enforced: {blocked_output}")

        logger.conclude(output=final_answer)

        trace_id = str(trace.id)
        logger.flush()

        control_spans = _collect_control_spans(workflow)
        control_span_ids = [str(span.id) for span in control_spans]
        print()
        print(f"Splunk AO trace_id: {trace_id}")
        print(f"Workflow child spans: {len(workflow.spans)}")
        print(f"Control spans attached during run: {len(control_spans)}")
        for span in control_spans:
            result = getattr(span, "output", None)
            print(
                "  "
                f"id={span.id} control_id={span.control_id} name={span.name} "
                f"stage={span.check_stage} matched={getattr(result, 'matched', None)}"
            )

        if args.verify_api:
            await asyncio.sleep(args.verify_delay_seconds)
            await _verify_trace(
                args,
                project_id=logger.project_id,
                log_stream_id=logger.agent_stream_id,
                session_id=session_id,
                trace_id=trace_id,
                control_span_ids=control_span_ids,
            )
        if args.query_trends:
            if not args.verify_api:
                await asyncio.sleep(args.verify_delay_seconds)
            await _query_trends(
                args,
                project_id=logger.project_id,
                log_stream_id=logger.agent_stream_id,
            )
    finally:
        logger.terminate()


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
