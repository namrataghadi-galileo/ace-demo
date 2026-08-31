#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(os.environ.get("SPLUNK_AO_ENV_FILE", ".env.splunk-ao"), override=True)

from common_splunk_ao import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_AGENT_NAME,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

LUNA_SCORER_LABEL = "prompt_injection_luna"
LUNA_OPERATOR = "gte"
LUNA_THRESHOLD = 0.8
LUNA_TIMEOUT_MS = 30000


def luna_config() -> dict[str, Any]:
    return {
        "scorer_label": LUNA_SCORER_LABEL,
        "operator": LUNA_OPERATOR,
        "timeout_ms": LUNA_TIMEOUT_MS,
        "threshold": LUNA_THRESHOLD,
    }


def control_specs() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "demo-observe-luna-transfer-request",
            {
                "description": "Block prompt-injection attempts in the banking transfer request with the Luna evaluator.",
                "enabled": True,
                "execution": "server",
                "scope": {"step_types": ["llm"], "stages": ["pre"]},
                "condition": {
                    "selector": {"path": "input"},
                    "evaluator": {
                        "name": "galileo.luna",
                        "config": luna_config(),
                    },
                },
                "action": {"decision": "deny"},
                "tags": ["demo", "galileo", "luna", "banking", "deny", "pre-llm"],
            },
        ),
        (
            "demo-steer-large-transfer-2fa",
            {
                "description": "Require 2FA verification for wire transfers of $10,000 or more.",
                "enabled": True,
                "execution": "server",
                "scope": {"step_types": ["tool"], "stages": ["pre"]},
                "condition": {
                    "selector": {"path": "input"},
                    "evaluator": {
                        "name": "regex",
                        "config": {
                            "pattern": r"['\"]amount['\"]\s*:\s*[1-9]\d{4,}(?:\.0+)?"
                            r"[\s\S]*['\"]verified_2fa['\"]\s*:\s*(?:false|False)",
                        },
                    },
                },
                "action": {
                    "decision": "steer",
                    "steering_context": {
                        "message": (
                            '{"required_actions":["request_2fa","verify_2fa"],'
                            '"retry_flags":{"verified_2fa":true},'
                            '"reason":"Transfers >= $10,000 require identity verification via 2FA."}'
                        )
                    },
                },
                "tags": ["demo", "galileo", "banking", "steer", "2fa", "pre-tool"],
            },
        ),
    ]


CONTROL_SPECS = control_specs()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create demo Agent Control controls for Splunk AO control span ingestion."
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
        default=os.environ.get("AGENT_CONTROL_TARGET_TYPE"),
        help="Optional target type to bind controls to, for example 'log_stream'.",
    )
    parser.add_argument(
        "--target-id",
        default=os.environ.get("AGENT_CONTROL_TARGET_ID"),
        help="Optional target ID to bind controls to, for example a Splunk AO agent_stream_id.",
    )
    return parser.parse_args()


async def _ensure_agent(client: Any, agent_name: str) -> None:
    response = await client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent_name,
                "agent_description": "Standalone banking transfer demo for Agent Control + Splunk AO control spans",
            },
            "steps": [],
        },
    )
    response.raise_for_status()


async def _list_exact_control(
    client: httpx.AsyncClient, name: str
) -> dict[str, Any] | None:
    response = await client.get("/api/v1/controls", params={"name": name, "limit": 20})
    response.raise_for_status()
    controls_payload = response.json().get("controls", [])
    for control in controls_payload:
        if control.get("name") == name:
            return control
    return None


async def _ensure_control(
    client: httpx.AsyncClient, name: str, data: dict[str, Any]
) -> int:
    try:
        response = await client.put(
            "/api/v1/controls", json={"name": name, "data": data}
        )
        response.raise_for_status()
        result = response.json()
        return int(result["control_id"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise

    existing = await _list_exact_control(client, name)
    if existing is None:
        raise RuntimeError(f"Control '{name}' already exists but could not be listed.")

    control_id = int(existing["id"])
    response = await client.put(
        f"/api/v1/controls/{control_id}/data", json={"data": data}
    )
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


async def ensure_demo_controls(
    *,
    agent_name: str = DEFAULT_AGENT_NAME,
    server_url: str = DEFAULT_AGENT_CONTROL_URL,
    target_type: str | None = None,
    target_id: str | None = None,
) -> list[int]:
    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")

    api_key = resolve_agent_control_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing AGENT_CONTROL_API_KEY or SPLUNK_AO_API_KEY in the environment."
        )

    headers = {resolve_agent_control_api_key_header(): api_key}
    async with httpx.AsyncClient(
        base_url=server_url.rstrip("/"), headers=headers, timeout=60.0
    ) as client:
        health = await client.get("/health")
        health.raise_for_status()
        await _ensure_agent(client, agent_name)

        control_ids: list[int] = []
        for control_name, control_data in control_specs():
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


async def main() -> None:
    args = parse_args()
    control_ids = await ensure_demo_controls(
        agent_name=args.agent_name,
        server_url=args.server_url,
        target_type=args.target_type,
        target_id=args.target_id,
    )

    for control_name, control_id in zip(
        (name for name, _ in control_specs()), control_ids, strict=True
    ):
        print(f"Prepared control: {control_name} ({control_id})")

    print()
    print(f"Agent '{args.agent_name}' is ready with {len(control_ids)} controls.")
    print("Next step: run ./run_demo.py")


if __name__ == "__main__":
    asyncio.run(main())
