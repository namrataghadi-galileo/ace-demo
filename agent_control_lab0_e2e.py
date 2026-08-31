#!/usr/bin/env python3
"""
Agent Control end-to-end on Splunk O11y Cloud (lab0), QE reproduction script.

What this proves, in one run:
  1. A @control-decorated tool call is evaluated server-side through the O11y gateway.
  2. A bound control (regex "2FA for >= $10,000") fires and STEERS (blocks) the call.
  3. The control execution shows up as a span inside the same trace in the AO UI,
     tagged "Triggered", with the tool span and the matched control spans under one session.

Background:
  - O11y Cloud supports the splunk-ao SDK only (HYBIM-1000). The plain galileo SDK is not
    supported here. agent_control (enforcement) is a separate SDK and works through the gateway.
  - The agent_control feature flag must be ON for the lab0 cluster (per cluster customer_name
    o11y-lab0, not per org). See the QE note for how to enable/verify it.

Prereqs:
  pip install "agent-control-sdk==8.5.0" "splunk-ao"
  (install as two separate packages; there is no agent-control-sdk[splunk-ao] extra)

  VPN: GlobalProtect US West Full Tunnel + Aviatrix.
  Org membership: you must be provisioned into the AO org (gateway auth is pre-provisioned only).

Set these env vars before running (values are the lab0 test tokens; rotate as needed):
  export SPLUNK_AO_REALM="lab0"
  export SPLUNK_AO_O11Y_TOKEN="<ingest token>"          # OTLP span export
  export SPLUNK_AO_O11Y_API_TOKEN="<api token>"         # CRUD (project/stream lookup)
  export AC_SF_TOKEN="<session/SF token>"               # gateway auth, sent as X-SF-Token
  export AC_PROJECT_ID="f592350e-414d-4fef-9a1a-a359ebbda38a"
  export AC_STREAM_ID="640d0614-0d23-49b3-b33a-589d8908528b"
  export AC_GATEWAY="https://app.lab0.signalfx.com/ao/agent-control"
  export AC_AGENT_NAME="hybim871-flag-verify"
  export AC_AMOUNT="75000"                              # any integer >= 10000 matches the control

Run:
  python agent_control_lab0_e2e.py

Then open the AO UI Tracing tab for the stream, Last 15 minutes, open the newest session.
The trace tree shows the control node with the tool span and two control spans under it;
the control span detail shows Controls 1 / Triggered and output
{"action":"steer","matched":true,"confidence":1}.

The four things that make the control render in the UI trace tree (miss any one and the
session shows Traces 0):
  1. observability_sink_name="registered"  -> control events go to the splunk-ao bridge sink
  2. func.tool_name set before @control()  -> step is a tool, not llm (llm pulls in the
     Luna control, which errors because Luna is unavailable on lab0)
  3. let setup_agent_control_bridge() own the trace context  -> it installs a provider that
     returns the logger's real (root parent id, current parent id). Do NOT override it with a
     manual set_trace_context_provider: the bridge accepts a control event only when the event
     span_id equals the current parent's FULL UUID, so a truncated 16-hex span_id is dropped.
  4. await agent_control.shutdown_observability() before exit  -> flush the background events
  Also: do not use a named start_session; let splunk-ao own the session/trace.
"""

import os
import asyncio
import time


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v


def _setup_env() -> None:
    # splunk-ao telemetry side (O11y mode)
    os.environ.setdefault("SPLUNK_AO_REALM", "lab0")

    problems = []

    # all required env vars at once (not one at a time)
    required = ["SPLUNK_AO_O11Y_TOKEN", "SPLUNK_AO_O11Y_API_TOKEN", "AC_SF_TOKEN"]
    missing = [n for n in required if not os.environ.get(n)]
    if missing:
        problems.append("Missing env vars: " + ", ".join(missing)
                        + ". See docs/04_tokens_and_env.md.")

    # AC_PROJECT_ID / AC_STREAM_ID come from the setup script; guide the user there
    if not os.environ.get("AC_PROJECT_ID") or not os.environ.get("AC_STREAM_ID"):
        problems.append("AC_PROJECT_ID and/or AC_STREAM_ID are not set. Run "
                        "agent_control_lab0_setup.py first and export the IDs it prints "
                        "(see docs/03_setup_project_stream_control.md).")

    if problems:
        raise SystemExit("Preflight failed:\n  - " + "\n  - ".join(problems))

    # Set AGENT_CONTROL_* env BEFORE importing agent_control. The SDK reads AGENT_CONTROL_URL
    # at import time; if it is not set the evaluation client falls back to localhost:8000 and
    # the control check dies with "All connection attempts failed".
    sf = os.environ["AC_SF_TOKEN"]
    gateway = os.environ.get("AC_GATEWAY", "https://app.lab0.signalfx.com/ao/agent-control")
    os.environ.update({
        "AGENT_CONTROL_URL": gateway,
        "AGENT_CONTROL_API_KEY": sf,
        "AGENT_CONTROL_API_KEY_HEADER": "X-SF-Token",
        "AGENT_CONTROL_RUNTIME_TOKEN_HEADER": "X-Agent-Control-Runtime-Token",
        "AGENT_CONTROL_RUNTIME_AUTH_MODE": "jwt",
    })

    # SDKs installed (imported AFTER env is set, so agent_control picks up AGENT_CONTROL_URL)
    try:
        import agent_control  # noqa: F401
    except Exception:
        raise SystemExit('agent-control-sdk is not installed. '
                         'Run: pip install "agent-control-sdk==8.5.0"')
    try:
        import splunk_ao  # noqa: F401
    except Exception:
        raise SystemExit('splunk-ao is not installed. Run: pip install "splunk-ao"')


async def main() -> None:
    _setup_env()

    sf = os.environ["AC_SF_TOKEN"]
    gateway = os.environ["AGENT_CONTROL_URL"]
    project_id = _require("AC_PROJECT_ID")
    stream_id = _require("AC_STREAM_ID")
    agent_name = os.environ.get("AC_AGENT_NAME", "hybim871-flag-verify")
    amount = int(os.environ.get("AC_AMOUNT", "75000"))
    to_acct = os.environ.get("AC_TO", "acct-777")

    import agent_control
    from agent_control import control, ControlSteerError
    from splunk_ao.decorator import splunk_ao_context
    from splunk_ao import setup_agent_control_bridge

    # The tool. Mark it as a tool so @control treats the step as a tool step (not llm).
    async def wire_transfer(amount: int, to: str) -> str:
        return f"transferred {amount} to {to}"

    wire_transfer.tool_name = "wire_transfer"  # gotcha #2
    wire_transfer = control()(wire_transfer)

    # splunk-ao logger by IDs (skips the CRUD name lookup)
    logger = splunk_ao_context.get_logger_instance(project_id=project_id, agent_stream_id=stream_id)

    # gotcha #1: route control events to the splunk-ao bridge sink
    setup_agent_control_bridge(logger)

    agent_control.init(
        agent_name=agent_name,
        server_url=gateway,
        api_key=sf,
        api_key_header="X-SF-Token",
        runtime_token_header="X-Agent-Control-Runtime-Token",
        target_type="log_stream",
        target_id=stream_id,
        observability_enabled=True,
        observability_sink_name="registered",  # gotcha #1
        runtime_auth_mode="jwt",
    )

    # Let splunk-ao own the session/trace. Do NOT use a named start_session.
    payload = f'{{"amount":{amount},"to":"{to_acct}"}}'
    logger.start_trace(input=payload, name="wire_transfer")
    parent = logger.current_parent()
    tid = str(getattr(parent, "id", ""))

    # gotcha #3: the bridge stitches the control span into the splunk-ao trace on its own.
    # setup_agent_control_bridge() already installed a trace-context provider that returns the
    # logger's real (root parent id, current parent id). Do NOT override it with a manual
    # set_trace_context_provider: the bridge only accepts a control event when the event's
    # span_id equals the current parent's full UUID (see splunk_ao bridge _matches_active_context,
    # which runs both through uuid.UUID). A truncated 16-hex span_id normalizes to None, never
    # matches, and the control event is dropped (accepted=0, dropped=1). Let the bridge own it.

    steered = False
    try:
        await wire_transfer(amount=amount, to=to_acct)
        print(f"NOT blocked (amount {amount} did not match the control)")
    except ControlSteerError as e:
        steered = True
        print(f"STEER FIRED: {str(e)[:160]}")

    # Emit the tool span so the trace shows the tool step.
    logger.add_tool_span(
        input=payload,
        output='{"blocked":true,"reason":"2fa steer"}' if steered else '{"ok":true}',
        name="wire_transfer",
    )

    # gotcha #4: flush background control events before exit
    await agent_control.shutdown_observability()
    logger.conclude(output="steered" if steered else "ok")
    logger.flush()
    # Tear down the bridge, then clear the provider it installed.
    logger.disable_agent_control()
    agent_control.clear_trace_context_provider()
    time.sleep(3)  # give the async span exporter a moment
    print(f"INGESTED trace_id={tid} steered={steered}")
    print("Open the AO UI Tracing tab (Last 15 minutes) and inspect the newest session.")


if __name__ == "__main__":
    asyncio.run(main())
