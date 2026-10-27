#!/usr/bin/env python3
"""Set up Agent Control controls for the travel assistant demo.

Controls created
----------------
travel-deny-flight-pii
    Scope  : trace / post / step_name = travel_book_flight
    Action : deny
    Evaluator: galileo.llm (output PII)
    Scenario: The flight booking LLM leaks passenger email/passport in its
              output.  If PII is detected the trace is denied and hotel
              booking is blocked.

travel-steer-flight-retry
    Scope  : trace / post / step_name = travel_book_flight
    Action : steer
    Evaluator: galileo.llm (agentic_workflow_success / Action Advancement)
    Scenario: The flight booking LLM returns a vague response that does not
              advance the user's goal (no confirmation, no flight details).
              The evaluator scores the trace as 0 (did not advance).
              The control fires steer and the app retries via step 1a —
              an alternative airline booking (travel_book_flight_alt).

Prerequisites
-------------
Set the following env vars before running:

  GALILEO_API_KEY=<your-key>
  AGENT_CONTROL_URL=<server>          # e.g. https://console.staging.galileo.ai/api/agent-control
  AGENT_CONTROL_AGENT_NAME=travel-assistant-demo   # optional, default used if absent

  # For the PII deny control (LLM-as-a-judge):
  GALILEO_OUTPUT_PII_LLM_SCORER_ID=<scorer-id>
  GALILEO_OUTPUT_PII_LLM_SCORER_VERSION_ID=<scorer-version-id>

  # For the steer control (agentic_workflow_success):
  GALILEO_AGENTIC_WORKFLOW_SUCCESS_SCORER_ID=<scorer-id>
  GALILEO_AGENTIC_WORKFLOW_SUCCESS_SCORER_VERSION_ID=<scorer-version-id>
"""
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

from common import (
    DEFAULT_AGENT_CONTROL_URL,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

TRAVEL_AGENT_NAME = os.environ.get("AGENT_CONTROL_AGENT_NAME", "travel-assistant-demo")
FLIGHT_TRACE_STEP_NAME = "travel_book_flight"

# ---------------------------------------------------------------------------
# Evaluator config builders
# ---------------------------------------------------------------------------

STEER_TIMEOUT_MS = 30_000


def output_pii_llm_config() -> dict[str, Any] | None:
    """Config for galileo.llm output-PII scorer (used by the deny control)."""
    scorer_id = os.environ.get("GALILEO_OUTPUT_PII_LLM_SCORER_ID", "").strip()
    scorer_version_id = os.environ.get("GALILEO_OUTPUT_PII_LLM_SCORER_VERSION_ID", "").strip()
    if not scorer_id or not scorer_version_id:
        return None
    return {
        "scorer_id": scorer_id,
        "scorer_version_id": scorer_version_id,
        "scorer_label": "output_pii_llm",
        "operator": "contains",
        "threshold": "email",
        "timeout_ms": STEER_TIMEOUT_MS,
    }


def agentic_workflow_success_config() -> dict[str, Any] | None:
    """Config for galileo.llm agentic_workflow_success (Action Advancement) scorer.

    This scorer evaluates whether the agent advanced toward the user goal in a
    single trace step.  It returns a percentage score (0.0 = did not advance,
    1.0 = fully advanced).  We fire steer when score < 0.5 (gte/0.5 negated via
    lt operator, or simply use `lt` with threshold 0.5).
    """
    scorer_id = os.environ.get("GALILEO_AGENTIC_WORKFLOW_SUCCESS_SCORER_ID", "").strip()
    scorer_version_id = os.environ.get(
        "GALILEO_AGENTIC_WORKFLOW_SUCCESS_SCORER_VERSION_ID", ""
    ).strip()
    if not scorer_id or not scorer_version_id:
        return None
    return {
        "scorer_id": scorer_id,
        "scorer_version_id": scorer_version_id,
        "scorer_label": "agentic_workflow_success",
        # Fire when score < 0.5 — agent did NOT advance toward the goal
        "operator": "lt",
        "threshold": 0.5,
        "timeout_ms": STEER_TIMEOUT_MS,
    }


# ---------------------------------------------------------------------------
# Control specs
# ---------------------------------------------------------------------------


def travel_control_specs() -> list[tuple[str, dict[str, Any]]]:
    specs: list[tuple[str, dict[str, Any]]] = []

    pii_config = output_pii_llm_config()
    if pii_config is not None:
        specs.append(
            (
                "travel-deny-flight-pii",
                {
                    "description": (
                        "Deny the flight booking trace when the LLM leaks PII "
                        "(email or passport) in its output."
                    ),
                    "enabled": True,
                    "execution": "server",
                    "scope": {
                        "step_types": ["trace"],
                        "stages": ["post"],
                        "step_name_regex": f"^{FLIGHT_TRACE_STEP_NAME}$",
                    },
                    "condition": {
                        "selector": {"path": "output"},
                        "evaluator": {
                            "name": "galileo.llm",
                            "config": pii_config,
                        },
                    },
                    "action": {"decision": "deny"},
                    "tags": ["demo", "travel", "galileo", "llm", "deny", "pii", "post-trace"],
                },
            )
        )

    steer_config = agentic_workflow_success_config()
    if steer_config is not None:
        specs.append(
            (
                "travel-steer-flight-retry",
                {
                    "description": (
                        "Steer the flight booking trace when the agent does not advance "
                        "toward the booking goal (agentic_workflow_success < 0.5). "
                        "The steering context instructs the agent to retry via an "
                        "alternative airline (step 1a)."
                    ),
                    "enabled": True,
                    "execution": "server",
                    "scope": {
                        "step_types": ["trace"],
                        "stages": ["post"],
                        "step_name_regex": f"^{FLIGHT_TRACE_STEP_NAME}$",
                    },
                    "condition": {
                        "selector": {"path": "output"},
                        "evaluator": {
                            "name": "galileo.llm",
                            "config": steer_config,
                        },
                    },
                    "action": {
                        "decision": "steer",
                        "steering_context": {
                            "message": (
                                '{"required_actions":["retry_flight_booking"],'
                                '"retry_via":"alternative_airline",'
                                '"reason":"Primary flight booking did not advance toward the '
                                'user goal. Retry via SkyStar Airlines as step 1a."}'
                            )
                        },
                    },
                    "tags": [
                        "demo",
                        "travel",
                        "galileo",
                        "llm",
                        "steer",
                        "agentic-workflow-success",
                        "post-trace",
                    ],
                },
            )
        )

    return specs


# ---------------------------------------------------------------------------
# CRUD helpers (copied from setup_controls.py)
# ---------------------------------------------------------------------------


async def _ensure_agent(client: Any, agent_name: str) -> None:
    response = await client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent_name,
                "agent_description": "Travel assistant demo for Agent Control — PII deny + steer to step 1a",
            },
            "steps": [],
        },
    )
    response.raise_for_status()


async def _list_exact_control(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    response = await client.get("/api/v1/controls", params={"name": name, "limit": 20})
    response.raise_for_status()
    for control in response.json().get("controls", []):
        if control.get("name") == name:
            return control
    return None


async def _ensure_control(client: httpx.AsyncClient, name: str, data: dict[str, Any]) -> int:
    try:
        response = await client.put("/api/v1/controls", json={"name": name, "data": data})
        response.raise_for_status()
        return int(response.json()["control_id"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise

    existing = await _list_exact_control(client, name)
    if existing is None:
        raise RuntimeError(f"Control '{name}' already exists but could not be listed.")

    control_id = int(existing["id"])
    response = await client.put(f"/api/v1/controls/{control_id}/data", json={"data": data})
    response.raise_for_status()
    return control_id


async def _attach_control_to_agent(
    client: httpx.AsyncClient, agent_name: str, control_id: int
) -> None:
    response = await client.post(f"/api/v1/agents/{agent_name}/controls/{control_id}")
    if response.status_code == 409:
        return
    response.raise_for_status()


async def _bind_control_to_target(
    client: httpx.AsyncClient,
    *,
    target_type: str,
    target_id: str,
    control_id: int,
) -> int:
    response = await client.put(
        "/api/v1/control-bindings/by-key",
        json={
            "target_type": target_type,
            "target_id": target_id,
            "control_id": control_id,
            "enabled": True,
        },
    )
    response.raise_for_status()
    return int(response.json()["binding_id"])


# ---------------------------------------------------------------------------
# Main setup function
# ---------------------------------------------------------------------------


async def ensure_travel_controls(
    *,
    agent_name: str = TRAVEL_AGENT_NAME,
    server_url: str = DEFAULT_AGENT_CONTROL_URL,
    target_type: str | None = None,
    target_id: str | None = None,
) -> list[int]:
    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")

    api_key = resolve_agent_control_api_key()
    if not api_key:
        raise RuntimeError("Missing AGENT_CONTROL_API_KEY or GALILEO_API_KEY in the environment.")

    specs = travel_control_specs()
    if not specs:
        print(
            "No scorer IDs configured — set GALILEO_OUTPUT_PII_LLM_SCORER_ID / "
            "GALILEO_OUTPUT_PII_LLM_SCORER_VERSION_ID and/or "
            "GALILEO_AGENTIC_WORKFLOW_SUCCESS_SCORER_ID / "
            "GALILEO_AGENTIC_WORKFLOW_SUCCESS_SCORER_VERSION_ID to enable controls."
        )
        return []

    headers = {resolve_agent_control_api_key_header(): api_key}
    async with httpx.AsyncClient(
        base_url=server_url.rstrip("/"), headers=headers, timeout=60.0
    ) as client:
        health = await client.get("/health")
        health.raise_for_status()
        await _ensure_agent(client, agent_name)

        control_ids: list[int] = []
        for control_name, control_data in specs:
            control_id = await _ensure_control(client, control_name, control_data)
            control_ids.append(control_id)

        for control_id in control_ids:
            await _attach_control_to_agent(client, agent_name, control_id)
            if target_type is not None and target_id is not None:
                await _bind_control_to_target(
                    client,
                    target_type=target_type,
                    target_id=target_id,
                    control_id=control_id,
                )

    return control_ids


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Agent Control controls for the travel assistant demo."
    )
    parser.add_argument(
        "--agent-name", default=TRAVEL_AGENT_NAME, help="Agent Control agent name."
    )
    parser.add_argument(
        "--server-url", default=DEFAULT_AGENT_CONTROL_URL, help="Agent Control server base URL."
    )
    parser.add_argument(
        "--target-type",
        default=os.environ.get("AGENT_CONTROL_TARGET_TYPE"),
        help="Optional target type (e.g. 'log_stream').",
    )
    parser.add_argument(
        "--target-id",
        default=os.environ.get("AGENT_CONTROL_TARGET_ID"),
        help="Optional target ID (e.g. a Galileo log_stream_id).",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    control_ids = await ensure_travel_controls(
        agent_name=args.agent_name,
        server_url=args.server_url,
        target_type=args.target_type,
        target_id=args.target_id,
    )

    specs = travel_control_specs()
    for (control_name, _), control_id in zip(specs, control_ids, strict=True):
        print(f"Prepared control: {control_name} ({control_id})")

    print()
    print(f"Agent '{args.agent_name}' is ready with {len(control_ids)} controls.")
    print("Next step: run streamlit run travel_assistant_multi_trace_streamlit_app.py")


if __name__ == "__main__":
    asyncio.run(main())
