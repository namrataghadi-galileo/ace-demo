#!/usr/bin/env python3
"""Agent Control native-OTLP end-to-end test for Splunk O11y Cloud.

Unlike ``agent_control_lab0_e2e.py``, this test does not register the
``splunk-ao`` Agent Control bridge. Agent Control converts its evaluation event
to an ``agent_control.control_execution`` OpenTelemetry span, the Splunk AO span
processor exports it to the realm OTLP endpoint, and API readback verifies the
ingestion-normalized control fields used by the UI.

Prerequisites:

  pip install "agent-control-sdk==8.5.0" "splunk-ao>=0.3.0"

Required environment variables:

  SPLUNK_AO_REALM              Realm such as ``lab0`` or ``rc0`` (default: lab0)
  SPLUNK_AO_O11Y_TOKEN         OTLP ingest token
  AC_SF_TOKEN                  Agent Control gateway token
  AC_PROJECT_ID                Existing Splunk AO project UUID
  AC_STREAM_ID                 Existing bound agent-stream UUID

Optional environment variables:

  SPLUNK_AO_ENV_FILE           Dotenv configuration file to load after ``.env``
  SPLUNK_AO_O11Y_API_TOKEN     API readback token; falls back to AC_SF_TOKEN
  AC_GATEWAY                   Defaults to the selected realm's gateway
  AC_AGENT_NAME                Defaults to ``hybim-otel-e2e``
  AC_AMOUNT                    Defaults to 75000
  AC_TO                        Defaults to ``acct-777``
  AC_VERIFY_ATTEMPTS           Defaults to 24
  AC_VERIFY_DELAY_SECONDS      Defaults to 5
  AGENT_CONTROL_OTEL_ENDPOINT  Overrides the realm-derived OTLP endpoint

Where to get the tokens (realm is rc0 in these examples; swap in your realm):

  SPLUNK_AO_O11Y_TOKEN
      The realm INGEST token (referred to elsewhere as <REALM>_INGEST_TOKEN).
      Create it in the O11y Cloud UI under Settings > Access Tokens; select the
      token there. This authorizes the OTLP export to
      ``https://ingest.<realm>.observability.splunkcloud.com/v2/trace/otlp``.
  AC_SF_TOKEN
      Your user API access token (the sf-token; referred to elsewhere as
      <REALM>_SF_TOKEN). Grab it from your profile page (the avatar menu):
      ``https://app.<realm>.observability.splunkcloud.com/#/userprofile`` and
      click "Show User API Access Token". It is the same value that appears as
      the ``sf-token`` / ``X-SF-Token`` header on requests in the browser dev
      tools Network tab. The token's org must be provisioned for Agent Control,
      or the gateway (``/ao/agent-control``) returns 401/403.
  SPLUNK_AO_O11Y_API_TOKEN
      Token for the spans/search API readback. In practice the same INGEST
      token as SPLUNK_AO_O11Y_TOKEN was used here. Falls back to AC_SF_TOKEN
      when unset.

Use ``agent_control_lab0_setup.py`` first to create and bind the regex steering
control expected by this test.

Note: getting the normalized control span to appear
====================================================

For the ingestion-normalized ``control`` span to appear in the UI (populated
Input Text, Triggered / Matched, evaluator fields), the SDK must actually emit
a control-execution event. That only happens when the agent's control cache is
populated at ``init`` time. Two conditions are required (verified on rc0 with
agent-control-sdk 8.5.0 and splunk-ao 0.3.0):

1. Declare the guarded tool as a registration step so ``initAgent`` returns the
   target-bound control and the SDK caches it:

       agent_control.init(
           ...,
           steps=[{"type": "tool", "name": "wire_transfer"}],
       )

   Without a matching step, ``initAgent`` returns zero controls, the control
   cache stays empty, and evaluation takes the server-only path that emits no
   observability event. The control still steers, but no control span is
   produced.

2. Do NOT pass ``otel_tracer_provider`` to ``agent_control.init(...)``. It is
   not a recognized ``init`` parameter, so it is stored in ``agent_metadata``
   and ``initAgent`` registration then fails with "Object of type
   TracerProvider is not JSON serializable". The failure is logged and
   swallowed, leaving the control cache empty (same empty-span symptom as #1).
   The OTEL sink builds its own TracerProvider from
   ``observability_sink_config``, so an app-side provider is not needed for the
   control span; span nesting under the tool span is handled by
   ``set_trace_context_provider``.

Also expect indexing lag: the ``control`` span can take a couple of minutes
longer to become queryable than the tool and workflow spans in the same trace,
so raise ``AC_VERIFY_ATTEMPTS`` / ``AC_VERIFY_DELAY_SECONDS`` if readback times
out.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from uuid import UUID


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _load_environment_file() -> None:
    """Load shared secrets first, then the selected realm configuration."""
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise SystemExit(
            'python-dotenv is required. Run: pip install "python-dotenv>=1.0"'
        ) from exc

    base_file = Path(".env")
    if base_file.exists():
        load_dotenv(base_file, override=False)

    selected_file = os.environ.get("SPLUNK_AO_ENV_FILE")
    if selected_file:
        path = Path(selected_file)
        if not path.exists():
            raise SystemExit(f"SPLUNK_AO_ENV_FILE does not exist: {path}")
        load_dotenv(path, override=True)


def _apply_standard_name_aliases() -> None:
    aliases = {
        "AC_SF_TOKEN": "AGENT_CONTROL_API_KEY",
        "AC_PROJECT_ID": "SPLUNK_AO_PROJECT_ID",
        "AC_STREAM_ID": "SPLUNK_AO_AGENT_STREAM_ID",
        "AC_GATEWAY": "AGENT_CONTROL_URL",
        "AC_AGENT_NAME": "AGENT_CONTROL_AGENT_NAME",
    }
    for destination, source in aliases.items():
        if not os.environ.get(destination) and os.environ.get(source):
            os.environ[destination] = os.environ[source]


def _configure_environment() -> tuple[str, str, str]:
    _load_environment_file()
    _apply_standard_name_aliases()
    realm = os.environ.setdefault("SPLUNK_AO_REALM", "lab0")
    required = [
        "SPLUNK_AO_O11Y_TOKEN",
        "AC_SF_TOKEN",
        "AC_PROJECT_ID",
        "AC_STREAM_ID",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    gateway = os.environ.get(
        "AC_GATEWAY", f"https://app.{realm}.signalfx.com/ao/agent-control"
    )
    otlp_endpoint = os.environ.get(
        "AGENT_CONTROL_OTEL_ENDPOINT",
        f"https://ingest.{realm}.observability.splunkcloud.com/v2/trace/otlp",
    )
    os.environ.update(
        {
            "AGENT_CONTROL_URL": gateway,
            "AGENT_CONTROL_API_KEY": os.environ["AC_SF_TOKEN"],
            "AGENT_CONTROL_API_KEY_HEADER": "X-SF-Token",
            "AGENT_CONTROL_RUNTIME_TOKEN_HEADER": "X-Agent-Control-Runtime-Token",
            "AGENT_CONTROL_RUNTIME_AUTH_MODE": "jwt",
            "AGENT_CONTROL_OBSERVABILITY_SINK_NAME": "otel",
            "AGENT_CONTROL_OTEL_ENABLED": "true",
            "AGENT_CONTROL_OTEL_ENDPOINT": otlp_endpoint,
        }
    )
    return realm, gateway, otlp_endpoint


def _otel_context(span: Any) -> dict[str, str]:
    context = span.get_span_context()
    if not context.is_valid:
        raise RuntimeError("OpenTelemetry did not create a valid trace context.")
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


def _backend_trace_uuid(source_trace_id: str) -> str:
    """Return the UUIDv4 form used by O11y ingestion for an OTEL trace ID."""
    raw = bytearray.fromhex(source_trace_id)
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _assert_normalized_control(
    records: list[dict[str, Any]], *, amount: int, to_acct: str
) -> dict[str, Any]:
    controls = [record for record in records if record.get("type") == "control"]
    if not controls:
        raw_events = [
            record
            for record in records
            if record.get("name") == "agent_control.control_execution"
        ]
        suffix = (
            f" Found {len(raw_events)} unnormalized Agent Control OTEL span(s)."
            if raw_events
            else ""
        )
        raise RuntimeError("Readback returned no normalized control span." + suffix)

    control = next(
        (record for record in controls if _json_object(record.get("output")).get("matched") is True),
        controls[0],
    )
    failures: list[str] = []

    control_input = _json_object(control.get("input"))
    if control_input.get("amount") != amount or control_input.get("to") != to_acct:
        failures.append(f"input was not preserved: {control.get('input')!r}")

    output = _json_object(control.get("output"))
    if output.get("action") != "steer":
        failures.append(f"output.action={output.get('action')!r}, expected 'steer'")
    if output.get("matched") is not True:
        failures.append(f"output.matched={output.get('matched')!r}, expected true")
    if output.get("confidence") != 1 and output.get("confidence") != 1.0:
        failures.append(
            f"output.confidence={output.get('confidence')!r}, expected 1"
        )

    if control.get("evaluator_name") != "regex":
        failures.append(
            f"evaluator_name={control.get('evaluator_name')!r}, expected 'regex'"
        )
    if control.get("selector_path") != "input":
        failures.append(
            f"selector_path={control.get('selector_path')!r}, expected 'input'"
        )

    metadata = _json_object(control.get("user_metadata"))
    if not metadata.get("condition_trace"):
        failures.append("user_metadata.condition_trace is missing")
    if metadata.get("primary_evaluator") != "regex":
        failures.append(
            "user_metadata.primary_evaluator is missing or is not 'regex'"
        )
    if metadata.get("primary_selector_path") != "input":
        failures.append(
            "user_metadata.primary_selector_path is missing or is not 'input'"
        )

    if failures:
        raise RuntimeError(
            "Control span reached readback but failed normalization checks:\n  - "
            + "\n  - ".join(failures)
        )
    return control


async def _readback(
    *,
    realm: str,
    project_id: str,
    stream_id: str,
    source_trace_id: str,
    amount: int,
    to_acct: str,
) -> dict[str, Any]:
    import httpx

    api_origin = os.environ.get(
        "SPLUNK_AO_O11Y_API_ENDPOINT",
        f"https://api.{realm}.observability.splunkcloud.com",
    ).rstrip("/")
    api_url = urljoin(api_origin + "/", f"ao/api/projects/{project_id}/spans/partial_search")
    backend_trace_id = _backend_trace_uuid(source_trace_id)
    body = {
        "log_stream_id": stream_id,
        "limit": 100,
        "filter_tree": {
            "filter": {
                "name": "trace_id",
                "operator": "eq",
                "value": backend_trace_id,
                "type": "text",
            }
        },
        "select_columns": {
            "column_ids": [
                "id",
                "trace_id",
                "parent_id",
                "type",
                "name",
                "input",
                "output",
                "control_id",
                "agent_name",
                "check_stage",
                "applies_to",
                "evaluator_name",
                "selector_path",
                "user_metadata",
            ],
            "include_all_metrics": False,
            "include_all_feedback": False,
        },
    }
    token_candidates: list[tuple[str, str]] = []
    for label, token in (
        ("api_token", os.environ.get("SPLUNK_AO_O11Y_API_TOKEN")),
        ("sf_token", os.environ.get("AC_SF_TOKEN")),
    ):
        if token and token not in {candidate for _, candidate in token_candidates}:
            token_candidates.append((label, token))
    attempts = int(os.environ.get("AC_VERIFY_ATTEMPTS", "24"))
    delay_seconds = float(os.environ.get("AC_VERIFY_DELAY_SECONDS", "5"))
    last_records: list[dict[str, Any]] = []
    selected_auth: tuple[str, str] | None = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(1, attempts + 1):
            if selected_auth is None:
                response = None
                for label, token in token_candidates:
                    candidate_response = await client.post(
                        api_url,
                        headers={"X-SF-Token": token},
                        json=body,
                    )
                    if candidate_response.status_code in {401, 403}:
                        continue
                    selected_auth = (label, token)
                    response = candidate_response
                    break
                if response is None:
                    raise RuntimeError(
                        "API readback rejected both SPLUNK_AO_O11Y_API_TOKEN and "
                        "AC_SF_TOKEN. Provide a token with partial_search access."
                    )
            else:
                response = await client.post(
                    api_url,
                    headers={"X-SF-Token": selected_auth[1]},
                    json=body,
                )
            response.raise_for_status()
            last_records = response.json().get("records", [])
            if any(record.get("type") == "control" for record in last_records):
                control = _assert_normalized_control(
                    last_records, amount=amount, to_acct=to_acct
                )
                print("READBACK PASSED")
                print(f"  source_otel_trace_id={source_trace_id}")
                print(f"  backend_trace_id={backend_trace_id}")
                print(f"  readback_auth={selected_auth[0]}")
                print(f"  control_span_id={control.get('id')}")
                print(f"  control_name={control.get('name')}")
                print(f"  input={control.get('input')}")
                print(f"  output={control.get('output')}")
                print(f"  evaluator_name={control.get('evaluator_name')}")
                print(f"  selector_path={control.get('selector_path')}")
                print("  condition_trace=present")
                return control
            if attempt < attempts:
                await asyncio.sleep(delay_seconds)

    names = sorted(str(record.get("name")) for record in last_records)
    raise RuntimeError(
        f"No normalized control span appeared after {attempts} attempts. "
        f"backend_trace_id={backend_trace_id}, returned_names={names}"
    )


async def _verify_gateway_auth(gateway: str, sf_token: str) -> None:
    """Fail before evaluation when the O11y gateway rejects the SF token."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                gateway.rstrip("/") + "/health",
                headers={"X-SF-Token": sf_token},
            )
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"Cannot reach the Agent Control gateway at {gateway}: {type(exc).__name__}. "
            "Check VPN connectivity and AC_GATEWAY."
        ) from exc

    if response.status_code != 200:
        try:
            request_id = response.json().get("requestId", "unavailable")
        except (json.JSONDecodeError, AttributeError):
            request_id = "unavailable"
        raise SystemExit(
            f"Agent Control gateway authentication failed with HTTP {response.status_code}. "
            "Refresh RC0_SF_TOKEN. If a fresh token still fails, verify that the token's "
            "organization is provisioned for Agent Control. "
            f"Gateway request ID: {request_id}"
        )


async def main() -> None:
    realm, gateway, otlp_endpoint = _configure_environment()
    project_id = _require("AC_PROJECT_ID")
    stream_id = _require("AC_STREAM_ID")
    sf_token = _require("AC_SF_TOKEN")
    agent_name = os.environ.get("AC_AGENT_NAME", "hybim-otel-e2e")
    amount = int(os.environ.get("AC_AMOUNT", "75000"))
    to_acct = os.environ.get("AC_TO", "acct-777")

    await _verify_gateway_auth(gateway, sf_token)

    import agent_control
    from agent_control import ControlSteerError, control
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from splunk_ao import otel

    provider = TracerProvider(resource=Resource.create({"service.name": agent_name}))
    processor = otel.add_splunk_ao_span_processor(
        provider,
        project_id=project_id,
        agent_stream_id=stream_id,
    )
    tracer = provider.get_tracer("agent-control-native-otel-e2e")

    agent_control.init(
        agent_name=agent_name,
        agent_description="Agent Control native OTLP ingestion normalization E2E",
        server_url=gateway,
        api_key=sf_token,
        api_key_header="X-SF-Token",
        runtime_token_header="X-Agent-Control-Runtime-Token",
        target_type="log_stream",
        target_id=stream_id,
        observability_enabled=True,
        observability_sink_name="otel",
        observability_sink_config={
            "enabled": True,
            "endpoint": otlp_endpoint,
            "headers": {
                "X-SF-Token": _require("SPLUNK_AO_O11Y_TOKEN"),
                "projectid": project_id,
                "logstreamid": stream_id,
            },
            "service_name": agent_name,
        },
        otel_tracer_provider=provider,
        runtime_auth_mode="jwt",
    )

    async def wire_transfer(amount: int, to: str) -> str:
        return f"transferred {amount} to {to}"

    wire_transfer.tool_name = "wire_transfer"
    guarded_wire_transfer = control()(wire_transfer)

    source_trace_id = ""
    steered = False
    try:
        with tracer.start_as_current_span("agent-control-native-otel-e2e") as root_span:
            root_span.set_attribute("gen_ai.operation.name", "workflow")
            root_span.set_attribute("gen_ai.system", "splunk-ao-otel")
            root_span.set_attribute(
                "gen_ai.input.messages",
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": {"amount": amount, "to": to_acct},
                        }
                    ]
                ),
            )
            source_trace_id = _otel_context(root_span)["trace_id"]

            with tracer.start_as_current_span("wire_transfer") as tool_span:
                tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
                tool_span.set_attribute("gen_ai.tool.name", "wire_transfer")
                tool_span.set_attribute(
                    "gen_ai.tool.call.arguments",
                    json.dumps({"amount": amount, "to": to_acct}),
                )
                tool_context = _otel_context(tool_span)
                agent_control.set_trace_context_provider(lambda: tool_context)
                try:
                    await guarded_wire_transfer(amount=amount, to=to_acct)
                except ControlSteerError as exc:
                    steered = True
                    tool_span.set_attribute(
                        "gen_ai.tool.call.result",
                        json.dumps({"blocked": True, "reason": "2fa steer"}),
                    )
                    print(f"STEER FIRED: {str(exc)[:160]}")
                finally:
                    agent_control.clear_trace_context_provider()

            root_span.set_attribute(
                "gen_ai.output.messages",
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "content": "steered" if steered else "not steered",
                        }
                    ]
                ),
            )
    finally:
        agent_control.clear_trace_context_provider()
        await agent_control.shutdown_observability()
        if not provider.force_flush():
            raise RuntimeError("OTLP exporter flush timed out.")
        export_health = processor.export_health
        provider.shutdown()

    if not steered:
        raise RuntimeError(
            f"Expected the bound regex control to steer amount={amount}, but it did not."
        )
    if not export_health.healthy:
        raise RuntimeError(
            "OTLP export was not healthy: "
            f"consecutive_failures={export_health.consecutive_failures}"
        )

    print("OTLP EXPORT PASSED")
    print(f"  realm={realm}")
    print(f"  endpoint={otlp_endpoint}")
    print(f"  source_otel_trace_id={source_trace_id}")
    await _readback(
        realm=realm,
        project_id=project_id,
        stream_id=stream_id,
        source_trace_id=source_trace_id,
        amount=amount,
        to_acct=to_acct,
    )


if __name__ == "__main__":
    asyncio.run(main())
