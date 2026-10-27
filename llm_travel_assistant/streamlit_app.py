#!/usr/bin/env python3
"""Travel assistant demo: Book Flight → Book Hotel, with two Agent Control scenarios.

Workflow
--------
Scenario A — PII deny gate (leak_pii=True):
  The flight-booking LLM leaks passenger PII (email + passport) in its output.
  A PII control evaluates the flight trace post-stage and fires *deny*.
  Hotel booking is blocked entirely.

Scenario B — Steer to step 1a (simulate_bad_booking=True):
  The flight-booking LLM returns a vague, unhelpful response that does NOT
  advance the user's goal (no confirmation number, no flight details).
  An `agentic_workflow_success` (Action Advancement) LLM evaluator detects that
  the agent failed to advance toward the goal and fires *steer*.
  The steering context instructs the app to retry via an alternative airline
  (step 1a).  Step 1a produces a successful booking, and hotel booking proceeds.

Step 2 — book_hotel():
  Only runs when the flight trace decision is NOT "deny".

Instrumentation follows banking_multilevel_streamlit_app.py:
  - GalileoLogger for trace/span emission
  - agent_control.evaluate_controls() for the trace-level controls
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=True)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_LOG_STREAM,
    DEFAULT_PROJECT,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_AGENT_NAME = "travel-assistant-demo"
FLIGHT_TRACE_STEP_NAME = "travel_book_flight"
FLIGHT_ALT_TRACE_STEP_NAME = "travel_book_flight_alt"
HOTEL_TRACE_STEP_NAME = "travel_book_hotel"
FLIGHT_LLM_STEP_NAME = "travel_flight_confirmation_llm"
FLIGHT_PII_CONTROL_NAME = "travel-deny-flight-pii"
FLIGHT_STEER_CONTROL_NAME = "travel-steer-flight-retry"


# ---------------------------------------------------------------------------
# Deterministic simulation helpers
# ---------------------------------------------------------------------------


@dataclass
class TravelInputs:
    passenger_name: str = "Alice Johnson"
    passenger_email: str = "alice@example.com"
    passport_number: str = "X12345678"
    origin: str = "SFO"
    destination: str = "JFK"
    travel_date: str = "2026-11-01"
    hotel_name: str = "Grand Central Hotel"
    check_in: str = "2026-11-01"
    check_out: str = "2026-11-03"
    leak_pii: bool = True
    simulate_bad_booking: bool = False


def _run_confirmation_llm(inputs: TravelInputs) -> dict[str, Any]:
    """Deterministic model adapter — leaks PII when leak_pii=True.

    When simulate_bad_booking=True the LLM returns a vague non-advancing
    response (no confirmation number, no actionable details) that triggers the
    agentic_workflow_success steer control.
    """
    if inputs.simulate_bad_booking:
        # Vague output that does NOT advance the goal — no booking confirmation,
        # no flight details.  The agentic_workflow_success evaluator will score
        # this as 0 (did not advance) and the steer control fires.
        content = (
            "I looked into available flights but was unable to complete the "
            "booking at this time. Please try again later."
        )
    elif inputs.leak_pii:
        content = (
            f"Flight confirmed for {inputs.passenger_name} "
            f"({inputs.passenger_email}, passport {inputs.passport_number}) "
            f"from {inputs.origin} to {inputs.destination} on {inputs.travel_date}."
        )
    else:
        content = (
            f"Flight confirmed from {inputs.origin} to {inputs.destination} on {inputs.travel_date}."
        )
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
    }


def _run_alternative_llm(inputs: TravelInputs) -> dict[str, Any]:
    """Step 1a: alternative airline booking — always succeeds, no PII leak."""
    content = (
        f"Alternative flight booked via SkyStar Airlines from {inputs.origin} "
        f"to {inputs.destination} on {inputs.travel_date}. "
        f"Confirmation: ALT-{uuid4().hex[:8].upper()}."
    )
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
    }


def _book_flight_record(inputs: TravelInputs) -> dict[str, Any]:
    """Build a fully executed trace record for book_flight, including its LLM child span."""
    llm_input = [
        {"role": "system", "content": "Confirm flight bookings for the passenger."},
        {
            "role": "user",
            "content": (
                f"Confirm flight from {inputs.origin} to {inputs.destination} "
                f"on {inputs.travel_date} for {inputs.passenger_name}."
            ),
        },
    ]
    llm_output = _run_confirmation_llm(inputs)
    # When simulating a bad booking the status reflects the failed attempt
    status = "failed" if inputs.simulate_bad_booking else "booked"
    llm_span = {
        "type": "llm",
        "name": FLIGHT_LLM_STEP_NAME,
        "input": llm_input,
        "output": llm_output,
        "tools": [],
        "metadata": {
            "executed": True,
            "model": "deterministic-travel-model",
            "external_call": False,
        },
    }
    trace_output = {
        "status": status,
        "output": llm_output["content"],
        "flight": {
            "origin": inputs.origin,
            "destination": inputs.destination,
            "date": inputs.travel_date,
        },
    }
    return {
        "type": "trace",
        "name": FLIGHT_TRACE_STEP_NAME,
        "input": {
            "passenger": inputs.passenger_name,
            "origin": inputs.origin,
            "destination": inputs.destination,
            "date": inputs.travel_date,
        },
        "output": trace_output,
        "spans": [llm_span],
        "metadata": {
            "executed": True,
            "executed_function": "_book_flight_record",
            "external_call": False,
        },
    }


def _book_flight_alternative_record(inputs: TravelInputs) -> dict[str, Any]:
    """Step 1a: alternative flight booking trace logged after a steer decision.

    This represents the agent retrying via a different airline after the
    agentic_workflow_success control fired steer on the original flight trace.
    """
    llm_input = [
        {
            "role": "system",
            "content": (
                "The primary booking attempt failed to advance. "
                "Retry via an alternative airline to complete the booking."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Book an alternative flight from {inputs.origin} to {inputs.destination} "
                f"on {inputs.travel_date} for {inputs.passenger_name}."
            ),
        },
    ]
    llm_output = _run_alternative_llm(inputs)
    llm_span = {
        "type": "llm",
        "name": "travel_flight_alt_llm",
        "input": llm_input,
        "output": llm_output,
        "tools": [],
        "metadata": {
            "executed": True,
            "model": "deterministic-travel-model",
            "step": "alternative",
            "external_call": False,
        },
    }
    trace_output = {
        "status": "booked",
        "output": llm_output["content"],
        "flight": {
            "origin": inputs.origin,
            "destination": inputs.destination,
            "date": inputs.travel_date,
            "airline": "SkyStar Airlines",
        },
    }
    return {
        "type": "trace",
        "name": FLIGHT_ALT_TRACE_STEP_NAME,
        "input": {
            "passenger": inputs.passenger_name,
            "origin": inputs.origin,
            "destination": inputs.destination,
            "date": inputs.travel_date,
            "retry_reason": "steer: primary booking did not advance toward goal",
        },
        "output": trace_output,
        "spans": [llm_span],
        "metadata": {
            "executed": True,
            "step": "1a",
            "executed_function": "_book_flight_alternative_record",
            "external_call": False,
        },
    }


def _book_hotel_record(inputs: TravelInputs) -> dict[str, Any]:
    """Build a fully executed trace record for book_hotel."""
    tool_input = {
        "passenger": inputs.passenger_name,
        "hotel": inputs.hotel_name,
        "check_in": inputs.check_in,
        "check_out": inputs.check_out,
    }
    tool_output = {
        "status": "reserved",
        "confirmation_id": f"HTL-{uuid4().hex[:8].upper()}",
        "hotel": inputs.hotel_name,
        "check_in": inputs.check_in,
        "check_out": inputs.check_out,
    }
    tool_span = {
        "type": "tool",
        "name": "travel_hotel_reservation_tool",
        "input": tool_input,
        "output": tool_output,
        "metadata": {
            "executed": True,
            "executed_function": "book_hotel_sandbox",
            "external_call": False,
        },
    }
    return {
        "type": "trace",
        "name": HOTEL_TRACE_STEP_NAME,
        "input": tool_input,
        "output": tool_output,
        "spans": [tool_span],
        "metadata": {
            "executed": True,
            "executed_function": "_book_hotel_record",
            "external_call": False,
        },
    }


# ---------------------------------------------------------------------------
# Agent Control / Galileo helpers  (mirroring banking_multilevel_streamlit_app.py)
# ---------------------------------------------------------------------------


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
    missing = [k for k in ("GALILEO_API_KEY",) if not values[k]]
    if missing:
        raise RuntimeError("Set required environment values: " + ", ".join(missing))
    return values


def _initialize_agent(env: dict[str, str]) -> None:
    import agent_control

    agent_control.init(
        agent_name=env["AGENT_CONTROL_AGENT_NAME"],
        agent_description="Travel assistant demo: book flight + hotel with PII control gate",
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
    return [c for c in controls if isinstance(c, dict)]


def _json_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _log_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        str(k): v if v is None or isinstance(v, str | bool | int | float) else _json_text(v)
        for k, v in value.items()
    }


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


# ---------------------------------------------------------------------------
# Span emission (mirrors _emit_record_spans)
# ---------------------------------------------------------------------------


def _emit_record_spans(logger: Any, record: dict[str, Any]) -> None:
    record_type = str(record.get("type", ""))
    metadata = _log_metadata(record.get("metadata"))
    if record_type == "trace":
        for span in record.get("spans", []) or []:
            if isinstance(span, dict):
                _emit_record_spans(logger, span)
        return
    if record_type == "llm":
        logger.add_llm_span(
            input=record.get("input", ""),
            output=record.get("output", ""),
            model=str((record.get("metadata") or {}).get("model") or "travel-model"),
            tools=record.get("tools") or None,
            name=str(record.get("name") or "llm"),
            metadata=metadata,
        )
        return
    if record_type == "tool":
        logger.add_tool_span(
            input=_json_text(record.get("input")),
            output=_json_text(record.get("output")),
            name=str(record.get("name") or "tool"),
            metadata=metadata,
        )
        return


# ---------------------------------------------------------------------------
# Agent Control Step builder
# ---------------------------------------------------------------------------


def _build_step(record: dict[str, Any]) -> Any:
    from agent_control_models import Step

    record_type = str(record["type"])
    child_records = record.get("spans") if record_type == "trace" else None
    children = (
        [_build_step(child) for child in child_records]
        if isinstance(child_records, list)
        else None
    )
    metadata = record.get("metadata")
    context = {"metadata": metadata} if isinstance(metadata, dict) else None
    return Step(
        type=record_type,
        name=str(record["name"]),
        input=record.get("input"),
        output=record.get("output"),
        context=context,
        tools=record.get("tools") if record_type == "llm" else None,
        children=children,
    )


# ---------------------------------------------------------------------------
# evaluate_controls wrapper for the flight trace
# ---------------------------------------------------------------------------


def _summarize_matches(items: Any) -> list[dict[str, Any]]:
    summaries = []
    for item in items or []:
        result = item.get("result", {}) if isinstance(item, dict) else {}
        summaries.append(
            {
                "control": item.get("control_name") if isinstance(item, dict) else getattr(item, "control_name", None),
                "action": item.get("action") if isinstance(item, dict) else getattr(item, "action", None),
                "matched": result.get("matched") if isinstance(result, dict) else getattr(result, "matched", None),
                "message": result.get("message") if isinstance(result, dict) else getattr(result, "message", None),
                "error": result.get("error") if isinstance(result, dict) else getattr(result, "error", None),
            }
        )
    return summaries


async def _evaluate_flight_trace(
    record: dict[str, Any],
    env: dict[str, str],
    *,
    trace_id: str,
    span_id: str,
    step_name: str = FLIGHT_TRACE_STEP_NAME,
) -> dict[str, Any]:
    import agent_control

    step = _build_step(record)
    result = await agent_control.evaluate_controls(
        step_name,
        input=step.input,
        output=step.output,
        context=step.context,
        tools=step.tools,
        children=step.children,
        step_type="trace",
        stage="post",
        agent_name=env["AGENT_CONTROL_AGENT_NAME"],
        trace_id=trace_id,
        span_id=span_id,
    )

    def _val(obj: Any, key: str, default: Any = None) -> Any:
        return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

    matches = _summarize_matches(_val(result, "matches", []))
    non_matches = _summarize_matches(_val(result, "non_matches", []))
    errors = _summarize_matches(_val(result, "errors", []))

    # Find the highest-priority matched control: deny > steer > first match
    deny_match = next(
        (m for m in matches if m.get("action") == "deny"),
        None,
    )
    steer_match = next(
        (m for m in matches if m.get("action") == "steer"),
        None,
    )
    primary_match = deny_match or steer_match or (matches[0] if matches else None)
    action = primary_match.get("action") if primary_match else None
    outcome = "denied" if action == "deny" else "steered" if action == "steer" else "allowed"

    # Steering context: extract from matched steer control if present
    steering_context: dict[str, Any] | None = None
    if steer_match and not deny_match:
        raw_result = steer_match.get("message") or ""
        try:
            steering_context = json.loads(raw_result) if raw_result else None
        except (json.JSONDecodeError, TypeError):
            steering_context = {"message": raw_result}

    raw = result if isinstance(result, dict) else result.model_dump(mode="json", exclude_none=True)
    return {
        "decision": action or "allow",
        "outcome": outcome,
        "pii_control_matched": primary_match is not None and action == "deny"
            and isinstance(primary_match.get("control"), str)
            and "pii" in primary_match["control"].lower(),
        "steer_control_matched": steer_match is not None and not deny_match,
        "primary_control_result": primary_match,
        "steering_context": steering_context,
        "matches": matches,
        "non_matches": non_matches,
        "errors": errors,
        "agent_control_response": raw,
    }


# ---------------------------------------------------------------------------
# Main async orchestrator
# ---------------------------------------------------------------------------


async def _run_async(inputs: TravelInputs) -> dict[str, Any]:
    from galileo.logger.logger import GalileoLogger

    env = _environment()
    logger = GalileoLogger(
        project=os.environ.get("GALILEO_PROJECT", DEFAULT_PROJECT),
        log_stream=os.environ.get("GALILEO_LOG_STREAM", DEFAULT_LOG_STREAM),
        mode="batch",
    )
    logger.enable_agent_control()

    import agent_control

    registered_sink_count = len(agent_control.get_registered_control_event_sinks())
    if registered_sink_count == 0:
        logger.terminate()
        raise RuntimeError("Galileo did not register an Agent Control control-event sink")

    session_id = logger.start_session(
        name="travel-assistant-streamlit",
        external_id=f"travel-assistant-{uuid4()}",
        metadata={"demo": "travel-assistant"},
    )
    if logger.project_id is None or logger.log_stream_id is None:
        logger.terminate()
        raise RuntimeError("Galileo logger did not resolve project/log stream IDs")

    env["AGENT_CONTROL_TARGET_ID"] = logger.log_stream_id
    os.environ["GALILEO_PROJECT_ID"] = logger.project_id
    os.environ["GALILEO_LOG_STREAM_ID"] = logger.log_stream_id
    os.environ["AGENT_CONTROL_TARGET_ID"] = logger.log_stream_id

    result: dict[str, Any] = {
        "session_id": session_id,
        "project_id": logger.project_id,
        "log_stream_id": logger.log_stream_id,
        "registered_control_sinks": registered_sink_count,
        "leak_pii": inputs.leak_pii,
    }

    completed = False

    try:
        _initialize_agent(env)

        parent_input = {"request": inputs.passenger_name, "leak_pii": inputs.leak_pii}

        # ------------------------------------------------------------------
        # Step 1: Book Flight — trace nested inside session
        # ------------------------------------------------------------------
        flight_record = _book_flight_record(inputs)

        flight_trace = logger.start_trace(
            input=_json_text(flight_record["input"]),
            name=FLIGHT_TRACE_STEP_NAME,
            metadata={"step": "book_flight"},
        )
        flight_workflow = logger.add_workflow_span(
            input=_json_text(flight_record["input"]),
            name=f"{FLIGHT_TRACE_STEP_NAME}_workflow",
            metadata={"evaluation_path": "evaluate_controls"},
        )

        # Evaluate the flight trace — PII control (deny) or steer control fires here
        flight_eval = await _evaluate_flight_trace(
            flight_record,
            env,
            trace_id=str(flight_trace.id),
            span_id=str(flight_workflow.id),
        )

        _emit_record_spans(logger, flight_record)
        logger.conclude(
            output=_json_text(flight_record["output"]),
            conclude_all=True,
        )

        steer_intended = flight_eval["steer_control_matched"] and inputs.simulate_bad_booking
        steer_unexpected = flight_eval["steer_control_matched"] and not inputs.simulate_bad_booking
        # Effective decision: if steer fired but scenario B was not enabled,
        # treat it as allow — the flight was confirmed and we don't act on it.
        effective_decision = flight_eval["decision"]
        if steer_unexpected:
            effective_decision = "allow"
        effective_outcome = (
            "denied" if effective_decision == "deny"
            else "steered" if effective_decision == "steer"
            else "allowed"
        )
        result.update(
            {
                "flight_trace_id": str(flight_trace.id),
                "flight_record": flight_record,
                "flight_eval": flight_eval,
                "flight_decision": effective_decision,
                "flight_outcome": effective_outcome,
                "pii_control_matched": flight_eval["pii_control_matched"],
                "steer_control_matched": steer_intended,
                "steer_fired_unexpectedly": steer_unexpected,
            }
        )

        # ------------------------------------------------------------------
        # Step 1a: Alternative flight — triggered by steer decision
        # Only execute when the user enabled scenario B (simulate_bad_booking).
        # If the steer control fires on a normal run (no bad booking), we surface
        # it as a warning but do NOT take the alternative path.
        # ------------------------------------------------------------------
        flight_alt_record: dict[str, Any] | None = None
        if steer_intended:
            flight_alt_record = _book_flight_alternative_record(inputs)

            flight_alt_trace = logger.start_trace(
                input=_json_text(flight_alt_record["input"]),
                name=FLIGHT_ALT_TRACE_STEP_NAME,
                metadata={
                    "step": "book_flight_alt",
                    "triggered_by": "steer",
                    "steering_context": _json_text(flight_eval.get("steering_context") or {}),
                },
            )
            flight_alt_workflow = logger.add_workflow_span(
                input=_json_text(flight_alt_record["input"]),
                name=f"{FLIGHT_ALT_TRACE_STEP_NAME}_workflow",
                metadata={"evaluation_path": "direct", "step": "1a"},
            )
            _emit_record_spans(logger, flight_alt_record)
            logger.conclude(
                output=_json_text(flight_alt_record["output"]),
                conclude_all=True,
            )

            result.update(
                {
                    "flight_alt_trace_id": str(flight_alt_trace.id),
                    "flight_alt_record": flight_alt_record,
                    "flight_alt_outcome": "alternative flight booked via steer",
                }
            )

        # ------------------------------------------------------------------
        # Step 2: Book Hotel — only if flight trace was NOT denied
        # ------------------------------------------------------------------
        # deny  → block hotel
        # steer + simulate_bad_booking → step 1a ran, proceed to hotel
        # steer + normal run → treated as allow (see effective_decision above)
        # allow → proceed normally
        if effective_decision == "deny":
            result["hotel_skipped"] = True
            result["hotel_skip_reason"] = (
                "Flight booking trace was denied by PII control. "
                "Hotel booking blocked to protect passenger data."
            )
        else:
            result["hotel_skipped"] = False
            hotel_record = _book_hotel_record(inputs)

            hotel_trace = logger.start_trace(
                input=_json_text(hotel_record["input"]),
                name=HOTEL_TRACE_STEP_NAME,
                metadata={"step": "book_hotel"},
            )
            hotel_workflow = logger.add_workflow_span(
                input=_json_text(hotel_record["input"]),
                name=f"{HOTEL_TRACE_STEP_NAME}_workflow",
                metadata={"evaluation_path": "direct"},
            )
            _emit_record_spans(logger, hotel_record)
            logger.conclude(
                output=_json_text(hotel_record["output"]),
                conclude_all=True,
            )

            result.update({"hotel_record": hotel_record})

        logger.flush()
        completed = True

        result["log_stream_url"] = _log_stream_url(
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
        )
        return result

    except Exception as exc:
        if not completed:
            try:
                logger.conclude(
                    output=f"{type(exc).__name__}: {exc}",
                    status_code=2,
                    conclude_all=True,
                )
                logger.flush()
            except Exception:
                pass
        raise
    finally:
        logger.terminate()


def run_travel_workflow(inputs: TravelInputs) -> dict[str, Any]:
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run_async(inputs))
                return future.result()
        else:
            return asyncio.run(_run_async(inputs))
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "flight_decision": "error",
            "flight_outcome": "evaluation failed",
        }


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------


def _render_decision_badge(decision: str) -> None:
    color = {"deny": "🔴", "steer": "🟡", "allow": "🟢", "error": "⚫"}.get(decision, "⚪")
    st.markdown(f"### {color} Decision: `{decision.upper()}`")


def _render_result(result: dict[str, Any]) -> None:
    if "error" in result:
        st.error(result["error"])
        return

    st.subheader("Step 1 — Book Flight")
    cols = st.columns(4)
    cols[0].metric("Decision", result.get("flight_decision", "—"))
    cols[1].metric("Outcome", result.get("flight_outcome", "—"))
    cols[2].metric("PII Control Fired", "Yes" if result.get("pii_control_matched") else "No")
    cols[3].metric("Steer Control Fired", "Yes" if result.get("steer_control_matched") else "No")

    if result.get("flight_trace_id"):
        st.success("Flight trace flushed to Galileo.")
        id_cols = st.columns(3)
        id_cols[0].metric("Project", str(result.get("project_id", ""))[:8])
        id_cols[1].metric("Log stream", str(result.get("log_stream_id", ""))[:8])
        id_cols[2].metric("Flight trace", str(result.get("flight_trace_id", ""))[:8])
        if result.get("log_stream_url"):
            st.markdown(f"[Open Galileo log stream]({result['log_stream_url']})")

    with st.expander("Flight evaluation result", expanded=True):
        st.json(result.get("flight_eval", {}))

    with st.expander("Flight trace record"):
        st.json(result.get("flight_record", {}))

    # ------------------------------------------------------------------
    # Step 1a — Alternative flight (only shown when steer fired intentionally)
    # ------------------------------------------------------------------
    if result.get("steer_fired_unexpectedly"):
        st.warning(
            "The `Action-Advancement` steer control fired on this run even though "
            "Scenario B was not enabled. The flight was confirmed — this is likely "
            "the scorer evaluating a short/deterministic output as low advancement. "
            "Step 1a was **not** executed. Hotel booking proceeded normally."
        )

    if result.get("steer_control_matched"):
        st.divider()
        st.subheader("Step 1a — Alternative Flight Booking (triggered by Steer)")
        _render_decision_badge("steer")
        st.info(
            "The `agentic_workflow_success` control detected that the primary "
            "flight booking did not advance toward the goal. The agent was "
            "steered to retry via an alternative airline."
        )
        if result.get("flight_alt_record"):
            alt_output = result["flight_alt_record"].get("output", {})
            st.success(
                alt_output.get("output", "Alternative flight booked successfully.")
            )
        if result.get("flight_alt_trace_id"):
            st.metric("Alt-flight trace", str(result.get("flight_alt_trace_id", ""))[:8])
        with st.expander("Alternative flight trace record"):
            st.json(result.get("flight_alt_record", {}))

    st.divider()
    st.subheader("Step 2 — Book Hotel")

    if result.get("hotel_skipped"):
        st.error(result.get("hotel_skip_reason", "Hotel booking was skipped."))
        _render_decision_badge("deny")
    else:
        if result.get("steer_control_matched"):
            st.success(
                "Hotel booking proceeded — steer control redirected to alternative flight; "
                "hotel booking continues as normal."
            )
        else:
            st.success("Hotel booking proceeded — PII control did not fire.")
        _render_decision_badge("allow")
        if result.get("hotel_trace_id"):
            st.metric("Hotel trace", str(result.get("hotel_trace_id", ""))[:8])
        with st.expander("Hotel trace record"):
            st.json(result.get("hotel_record", {}))

    with st.expander("Raw Agent Control response (flight)"):
        st.json((result.get("flight_eval") or {}).get("agent_control_response", {}))


def main() -> None:
    st.set_page_config(page_title="Travel Assistant — Agent Control Demo", layout="wide")
    st.title("Travel Assistant · Agent Control Demo")
    st.caption(
        "Two scenarios: "
        "(A) **PII deny** — the flight LLM leaks PII and the trace is denied, blocking hotel booking. "
        "(B) **Steer to step 1a** — the flight LLM fails to advance the goal; the "
        "`agentic_workflow_success` control fires steer and the agent retries via an "
        "alternative airline before proceeding to hotel booking."
    )

    with st.form("travel_inputs"):
        st.markdown("#### Passenger details")
        name_col, email_col, passport_col = st.columns(3)
        passenger_name = name_col.text_input("Passenger name", value="Alice Johnson")
        passenger_email = email_col.text_input("Passenger email", value="alice@example.com")
        passport_number = passport_col.text_input("Passport number", value="X12345678")

        st.markdown("#### Flight details")
        orig_col, dest_col, date_col = st.columns(3)
        origin = orig_col.text_input("Origin", value="SFO")
        destination = dest_col.text_input("Destination", value="JFK")
        travel_date = date_col.text_input("Travel date", value="2026-11-01")

        st.markdown("#### Hotel details")
        hotel_col, checkin_col, checkout_col = st.columns(3)
        hotel_name = hotel_col.text_input("Hotel", value="Grand Central Hotel")
        check_in = checkin_col.text_input("Check-in", value="2026-11-01")
        check_out = checkout_col.text_input("Check-out", value="2026-11-03")

        st.markdown("#### Scenario control")
        scenario = st.radio(
            "Pick one scenario:",
            options=[
                "None — clean run (allow)",
                "Scenario A — LLM leaks PII in flight confirmation (PII deny control fires)",
                "Scenario B — LLM returns vague non-advancing response (steer → step 1a via alternative airline)",
            ],
            index=0,
        )
        leak_pii = scenario.startswith("Scenario A")
        simulate_bad_booking = scenario.startswith("Scenario B")

        submitted = st.form_submit_button(
            "Run Travel Workflow", type="primary", use_container_width=True
        )

    if submitted:
        inputs = TravelInputs(
            passenger_name=passenger_name,
            passenger_email=passenger_email,
            passport_number=passport_number,
            origin=origin,
            destination=destination,
            travel_date=travel_date,
            hotel_name=hotel_name,
            check_in=check_in,
            check_out=check_out,
            leak_pii=leak_pii,
            simulate_bad_booking=simulate_bad_booking,
        )
        with st.spinner("Running travel workflow…"):
            st.session_state["travel_result"] = run_travel_workflow(inputs)

    result = st.session_state.get("travel_result")
    if result is not None:
        _render_result(result)


if __name__ == "__main__":
    main()
