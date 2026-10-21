"""Interactive banking demo for an existing Galileo Jev pre-tool control."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import streamlit as st
from dotenv import load_dotenv

from decision_model_example.cases import (
    CONTROL_NAME,
    EVALUATOR_NAME,
    SCENARIOS,
    STEP_NAME,
    BankingSandbox,
    JevScenario,
    TransferRequest,
    request_for_scenario,
    run_banking_workflow,
)

DEFAULT_AGENT_NAME = "banking-jev-streamlit"
EXAMPLE_DIR = Path(__file__).resolve().parent

load_dotenv(EXAMPLE_DIR / ".env", override=True)


@dataclass(frozen=True)
class RuntimeEnvironment:
    api_key: str
    console_url: str
    api_url: str
    agent_control_url: str
    project_id: str
    project_name: str
    log_stream_id: str
    log_stream_name: str
    agent_name: str
    target_type: str
    target_id: str
    control_name: str
    scorer_id: str
    scorer_version_id: str


class ControlEventCapture:
    """Capture the same finalized events sent to Galileo for concise UI details."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def is_active(self) -> bool:
        return True

    def write_events(self, events):
        from agent_control_telemetry.sinks import SinkResult

        self.events.extend(events)
        return SinkResult(accepted=len(events))


def normalize_api_root(value: str) -> str:
    """Return the Galileo service root, tolerating a legacy trailing `/api`."""
    root = value.strip().rstrip("/")
    if root.endswith("/api"):
        root = root.removesuffix("/api")
    return root


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Set {name} in {EXAMPLE_DIR / '.env'}; copy .env.example to get started"
        )
    return value


def resolve_agent_control_api_key() -> str:
    """Use the Galileo API key as the Agent Control Enterprise credential."""
    api_key = os.environ.get("GALILEO_API_KEY") or os.environ.get("AGENT_CONTROL_API_KEY")
    if not api_key:
        raise RuntimeError("Set GALILEO_API_KEY before running the example")
    os.environ["AGENT_CONTROL_API_KEY"] = api_key
    return api_key


def resolve_agent_control_api_key_header() -> str:
    """Resolve the API-key header expected by the Agent Control gateway."""
    header = os.environ.get("AGENT_CONTROL_API_KEY_HEADER", "Galileo-API-Key")
    os.environ["AGENT_CONTROL_API_KEY_HEADER"] = header
    return header


def runtime_environment() -> RuntimeEnvironment:
    """Resolve the credential, Galileo target, and Decision Models scorer."""
    api_key = resolve_agent_control_api_key()
    console_url = _required_environment("GALILEO_CONSOLE_URL").rstrip("/")
    log_stream_id = _required_environment("GALILEO_LOG_STREAM_ID")
    target_type = os.environ.get("AGENT_CONTROL_TARGET_TYPE", "log_stream")
    target_id = os.environ.get("AGENT_CONTROL_TARGET_ID", log_stream_id)
    if target_type == "log_stream" and target_id != log_stream_id:
        raise RuntimeError(
            "AGENT_CONTROL_TARGET_ID must equal GALILEO_LOG_STREAM_ID so controls and "
            "telemetry use the same log stream"
        )
    env = RuntimeEnvironment(
        api_key=api_key,
        console_url=console_url,
        api_url=normalize_api_root(_required_environment("GALILEO_API_URL")),
        agent_control_url=os.environ.get(
            "AGENT_CONTROL_URL", f"{console_url}/api/agent-control"
        ).rstrip("/"),
        project_id=_required_environment("GALILEO_PROJECT_ID"),
        project_name=_required_environment("GALILEO_PROJECT"),
        log_stream_id=log_stream_id,
        log_stream_name=_required_environment("GALILEO_LOG_STREAM"),
        agent_name=os.environ.get("AGENT_CONTROL_AGENT_NAME", DEFAULT_AGENT_NAME),
        target_type=target_type,
        target_id=target_id,
        control_name=os.environ.get("DECISION_MODEL_CONTROL_NAME", CONTROL_NAME),
        scorer_id=_required_environment("DECISION_MODEL_SCORER_ID"),
        scorer_version_id=_required_environment("DECISION_MODEL_SCORER_VERSION_ID"),
    )
    return env


def _configure_process_environment(env: RuntimeEnvironment) -> None:
    values = {
        "GALILEO_API_KEY": env.api_key,
        "GALILEO_CONSOLE_URL": env.console_url,
        "GALILEO_API_URL": env.api_url,
        "GALILEO_PROJECT_ID": env.project_id,
        "GALILEO_PROJECT": env.project_name,
        "GALILEO_LOG_STREAM_ID": env.log_stream_id,
        "GALILEO_LOG_STREAM": env.log_stream_name,
        "AGENT_CONTROL_URL": env.agent_control_url,
        "AGENT_CONTROL_AGENT_NAME": env.agent_name,
        "AGENT_CONTROL_TARGET_TYPE": env.target_type,
        "AGENT_CONTROL_TARGET_ID": env.target_id,
        "DECISION_MODEL_CONTROL_NAME": env.control_name,
        "DECISION_MODEL_SCORER_ID": env.scorer_id,
        "DECISION_MODEL_SCORER_VERSION_ID": env.scorer_version_id,
    }
    os.environ.update(values)


def initialize_agent_control(env: RuntimeEnvironment) -> None:
    import agent_control

    agent_control.init(
        agent_name=env.agent_name,
        agent_description="Interactive Jev-protected banking transfer demo",
        server_url=env.agent_control_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        target_type=env.target_type,
        target_id=env.target_id,
        observability_enabled=True,
        observability_sink_name="registered",
    )


def _nested_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nested_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_dicts(item)


def verify_bound_control(
    controls: list[dict[str, Any]],
    *,
    control_name: str,
    scorer_id: str,
    scorer_version_id: str,
) -> dict[str, Any]:
    """Verify, but never create or modify, the pre-existing backend control."""
    candidates = [item for item in controls if item.get("name") == control_name]
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one bound control named {control_name!r}; found {len(candidates)}"
        )
    control = candidates[0]
    definition = control.get("data") or control.get("control") or control.get("definition") or {}
    nodes = list(_nested_dicts(definition))
    scope = next((node for node in nodes if "step_types" in node and "stages" in node), {})
    action = next((node for node in nodes if "decision" in node), {})
    evaluator = next((node for node in nodes if node.get("name") == EVALUATOR_NAME), None)
    config = evaluator.get("config", {}) if isinstance(evaluator, dict) else {}
    selector = next((node for node in nodes if node.get("path") == "input"), None)
    failures: list[str] = []
    if scope.get("step_types") != ["tool"]:
        failures.append("scope.step_types must be ['tool']")
    if scope.get("stages") != ["pre"]:
        failures.append("scope.stages must be ['pre']")
    if action.get("decision") != "deny":
        failures.append("action.decision must be 'deny'")
    if evaluator is None:
        failures.append(f"evaluator must be {EVALUATOR_NAME}")
    if selector is None:
        failures.append("condition selector must target input")
    if config.get("scorer_id") != scorer_id:
        failures.append("scorer_id does not match the Jev scorer")
    if config.get("scorer_version_id") != scorer_version_id:
        failures.append("scorer_version_id does not match the pinned Jev version")
    if failures:
        raise RuntimeError("Existing Jev control preflight failed: " + "; ".join(failures))
    return {
        "id": control.get("id"),
        "name": control.get("name", control_name),
        "evaluator": EVALUATOR_NAME,
        "scorer_id": config.get("scorer_id"),
        "scorer_version_id": config.get("scorer_version_id"),
        "selector": "input",
        "stage": "pre",
        "step_type": "tool",
        "action": "deny",
    }


async def read_bound_control(env: RuntimeEnvironment) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=env.agent_control_url.rstrip("/"),
        headers={resolve_agent_control_api_key_header(): env.api_key},
        timeout=30,
    ) as client:
        response = await client.get(
            f"/api/v1/agents/{env.agent_name}/controls",
            params={
                "rendered_state": "rendered",
                "enabled_state": "enabled",
                "target_type": env.target_type,
                "target_id": env.target_id,
            },
        )
        response.raise_for_status()
    body = response.json()
    controls = body.get("controls") if isinstance(body, dict) else None
    if not isinstance(controls, list):
        raise TypeError("Agent Control returned an invalid controls response")
    return verify_bound_control(
        [item for item in controls if isinstance(item, dict)],
        control_name=env.control_name,
        scorer_id=env.scorer_id,
        scorer_version_id=env.scorer_version_id,
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _count_control_spans(value: Any) -> int:
    count = int(getattr(value, "type", None) == "control")
    for child in getattr(value, "spans", []) or []:
        count += _count_control_spans(child)
    return count


def _captured_control_event(
    capture: ControlEventCapture, control_name: str
) -> dict[str, Any] | None:
    for event in reversed(capture.events):
        if getattr(event, "control_name", None) != control_name:
            continue
        if hasattr(event, "model_dump"):
            return event.model_dump(mode="json", exclude_none=True)
    return None


def _trace_url(env: RuntimeEnvironment, trace_id: str) -> str | None:
    hostname = urlparse(env.console_url).hostname or ""
    if not hostname.startswith("console-"):
        return None
    organization = hostname.removeprefix("console-").split(".gcp-dev.", 1)[0]
    return (
        f"{env.console_url.rstrip('/')}/{organization}/project/{env.project_id}"
        f"/agent-streams/{env.log_stream_id}?traceId={trace_id}"
    )


async def run_demo(request: TransferRequest, scenario: JevScenario) -> dict[str, Any]:
    """Run the real decorated transfer tool and emit application/control telemetry."""
    import agent_control
    from galileo.logger.logger import GalileoLogger

    env = runtime_environment()
    _configure_process_environment(env)
    logger = GalileoLogger(
        project_id=env.project_id,
        log_stream_id=env.log_stream_id,
        mode="batch",
    )
    logger.enable_agent_control()
    capture = ControlEventCapture()
    agent_control.register_control_event_sink(capture)
    registered_sinks = len(agent_control.get_registered_control_event_sinks())
    if registered_sinks < 2:
        agent_control.unregister_control_event_sink(capture)
        logger.terminate()
        raise RuntimeError("Galileo and diagnostic Agent Control event sinks were not registered")

    try:
        initialize_agent_control(env)
        control_config = await read_bound_control(env)
        sandbox = BankingSandbox()
        trace = logger.start_trace(
            input=request.user_request,
            name="banking_jev_transfer_request",
            metadata={"scenario": scenario.key, "evaluator": EVALUATOR_NAME},
        )
        workflow = logger.add_workflow_span(
            input=request.user_request,
            name="banking_jev_transfer_workflow",
            metadata={"control_stage": "pre", "control_step_type": "tool"},
        )
    except Exception:
        agent_control.unregister_control_event_sink(capture)
        logger.terminate()
        raise

    @agent_control.control(step_name=STEP_NAME, step_type="tool")
    async def submit_transfer(
        user_request: str,
        source_account_id: str,
        amount: float,
        currency: str,
        recipient_name: str,
        destination_account_id: str,
        destination_country: str,
        purpose: str,
        recipient_verified: bool,
        identity_verified: bool,
        manager_approved: bool,
        sanctions_screened: bool,
        sanctions_check_passed: bool,
    ) -> dict[str, Any]:
        return sandbox.submit_transfer(
            user_request=user_request,
            source_account_id=source_account_id,
            amount=amount,
            currency=currency,
            recipient_name=recipient_name,
            destination_account_id=destination_account_id,
            destination_country=destination_country,
            purpose=purpose,
            recipient_verified=recipient_verified,
            identity_verified=identity_verified,
            manager_approved=manager_approved,
            sanctions_screened=sanctions_screened,
            sanctions_check_passed=sanctions_check_passed,
        )

    try:
        result = await run_banking_workflow(
            request,
            submit_transfer,
            sandbox=sandbox,
            control_name=env.control_name,
        )
        control_event = _captured_control_event(capture, env.control_name)
        if control_event is not None:
            result["control_event"] = control_event
            control = result.get("control")
            event_metadata = control_event.get("metadata")
            if isinstance(control, dict) and isinstance(event_metadata, dict):
                control["metadata"] = event_metadata
        logger.add_tool_span(
            input=_json({"account_id": request.source_account_id}),
            output=_json(result["balance_check"]),
            name="lookup_balance",
        )
        logger.add_tool_span(
            input=_json(
                {
                    "recipient": request.recipient_name,
                    "destination_country": request.destination_country,
                }
            ),
            output=_json(result["recipient_validation"]),
            name="validate_recipient",
        )
        logger.add_tool_span(
            input=_json(result["tool_input"]),
            output=_json(
                result["tool_output"]
                if result["tool_output"] is not None
                else {"status": result["status"], "message": "Blocked before execution"}
            ),
            name=STEP_NAME,
            status_code=2 if result["status"] in {"denied", "error"} else 1,
        )
        logger.conclude(
            output=_json(
                {
                    "status": result["status"],
                    "decision": result["decision"],
                    "tool_executed": result["tool_executed"],
                }
            ),
            status_code=2 if result["status"] == "error" else 1,
            conclude_all=True,
        )
        flushed = logger.flush()
        result.update(
            {
                "scenario": scenario.key,
                "scenario_label": scenario.label,
                "control_config": control_config,
                "project_id": env.project_id,
                "log_stream_id": env.log_stream_id,
                "trace_id": str(trace.id),
                "workflow_span_id": str(workflow.id),
                "control_span_count": sum(_count_control_spans(item) for item in flushed),
                "registered_control_sinks": registered_sinks,
                "trace_url": _trace_url(env, str(trace.id)),
                "sandbox_transaction_count": len(sandbox.transactions),
            }
        )
        return result
    finally:
        agent_control.unregister_control_event_sink(capture)
        logger.terminate()


def run_demo_sync(request: TransferRequest, scenario: JevScenario) -> dict[str, Any]:
    try:
        result = asyncio.run(run_demo(request, scenario))
    except Exception as exc:  # noqa: BLE001 - render actionable E2E failures in Streamlit.
        control_name = os.environ.get("DECISION_MODEL_CONTROL_NAME", CONTROL_NAME)
        result = {
            "scenario": scenario.key,
            "scenario_label": scenario.label,
            "status": "error",
            "decision": "error",
            "request": request.user_request,
            "tool_input": request.__dict__,
            "tool_executed": False,
            "tool_output": None,
            "control_config": {
                "id": None,
                "name": control_name,
                "evaluator": EVALUATOR_NAME,
                "scorer_id": os.environ.get("DECISION_MODEL_SCORER_ID"),
                "scorer_version_id": os.environ.get("DECISION_MODEL_SCORER_VERSION_ID"),
            },
            "error": f"{type(exc).__name__}: {exc}",
        }
    print("\n=== banking Jev result ===", file=sys.stderr, flush=True)
    print(json.dumps(result, indent=2, default=str), file=sys.stderr, flush=True)
    print("=== end banking Jev result ===\n", file=sys.stderr, flush=True)
    return result


def _interactive_request(scenario: JevScenario) -> TransferRequest:
    defaults = request_for_scenario(scenario)
    user_request = st.text_area("Banking request", value=defaults.user_request, height=90)
    account_col, amount_col, currency_col = st.columns([2, 1, 1])
    source_account_id = account_col.text_input("Source account", value=defaults.source_account_id)
    amount = amount_col.number_input("Amount", min_value=0.01, value=defaults.amount, step=25.0)
    currency = currency_col.text_input("Currency", value=defaults.currency)
    recipient_col, destination_col, country_col = st.columns([2, 2, 1])
    recipient_name = recipient_col.text_input("Recipient", value=defaults.recipient_name)
    destination_account_id = destination_col.text_input(
        "Destination account", value=defaults.destination_account_id
    )
    destination_country = country_col.text_input("Country", value=defaults.destination_country)
    purpose = st.text_input("Transfer purpose", value=defaults.purpose)
    st.markdown("**Verification supplied to the transfer tool**")
    verify_col, identity_col, manager_col = st.columns(3)
    recipient_verified = verify_col.checkbox(
        "Recipient verified", value=defaults.recipient_verified
    )
    identity_verified = identity_col.checkbox("Identity verified", value=defaults.identity_verified)
    manager_approved = manager_col.checkbox("Manager approved", value=defaults.manager_approved)
    sanctions_col, passed_col = st.columns(2)
    sanctions_screened = sanctions_col.checkbox(
        "Sanctions screening completed", value=defaults.sanctions_screened
    )
    sanctions_check_passed = passed_col.checkbox(
        "Sanctions screening passed", value=defaults.sanctions_check_passed
    )
    return TransferRequest(
        user_request=user_request,
        source_account_id=source_account_id,
        amount=float(amount),
        currency=currency,
        recipient_name=recipient_name,
        destination_account_id=destination_account_id,
        destination_country=destination_country,
        purpose=purpose,
        recipient_verified=recipient_verified,
        identity_verified=identity_verified,
        manager_approved=manager_approved,
        sanctions_screened=sanctions_screened,
        sanctions_check_passed=sanctions_check_passed,
    )


def render_result(result: dict[str, Any]) -> None:
    if result["status"] == "denied":
        st.error(
            "Transfer blocked before execution — the Jev safety control matched the "
            "tool input. No sandbox transaction was created."
        )
    elif result["status"] == "completed":
        st.success(
            "Transfer completed — the Jev pre-tool control evaluated the request and "
            "did not block it."
        )
    else:
        st.error(
            "The workflow could not complete because control evaluation or telemetry "
            f"failed: {result.get('error', 'unknown error')}"
        )

    st.markdown(f"**Request:** {result['request']}")
    input_col, decision_col = st.columns(2)
    with input_col:
        st.markdown("#### Transfer tool invocation")
        st.json(result["tool_input"])
    with decision_col:
        st.markdown("#### Control result")
        st.metric("Decision", str(result["decision"]).upper())
        st.write(f"**Tool executed:** {'Yes' if result['tool_executed'] else 'No'}")
        control = result.get("control") or result.get("control_config") or {}
        st.write(f"**Control:** `{control.get('name', CONTROL_NAME)}`")
        st.write(f"**Evaluator:** `{control.get('evaluator', EVALUATOR_NAME)}`")
        message = control.get("message")
        if message:
            st.info(message)
        metadata = control.get("metadata")
        if isinstance(metadata, dict) and metadata:
            score = metadata.get("score")
            if score is not None:
                st.write(f"**Jev score:** `{score}`")
            elapsed = metadata.get("execution_time_seconds")
            if isinstance(elapsed, int | float):
                st.write(f"**Evaluation time:** `{elapsed:.3f}s`")

    if result.get("tool_output") is not None:
        st.markdown("#### Transfer result")
        st.json(result["tool_output"])
    if result.get("error"):
        st.error(result["error"])
    if result.get("trace_id"):
        st.caption(
            f"Saved trace `{str(result['trace_id'])[:8]}` · Agent Control spans: "
            f"{result.get('control_span_count', 0)}"
        )
        if result.get("trace_url"):
            st.markdown(f"[Open the trace in Galileo]({result['trace_url']})")
    st.caption("Complete evaluator and workflow diagnostics were written to stderr.")


def main() -> None:
    st.set_page_config(page_title="Banking Agent: Jev tool control", layout="wide")
    st.title("Banking Agent · Jev pre-tool safety control")
    st.caption(
        "A realistic in-memory transfer workflow protected by the existing "
        f"`{os.environ.get('DECISION_MODEL_CONTROL_NAME', CONTROL_NAME)}` backend control. "
        "This app never creates or modifies controls."
    )
    selected_label = st.selectbox("Scenario", [item.label for item in SCENARIOS])
    scenario = next(item for item in SCENARIOS if item.label == selected_label)
    st.info(scenario.description)
    with st.form(f"banking-jev::{scenario.key}"):
        request = _interactive_request(scenario)
        submitted = st.form_submit_button(
            "Run protected transfer", type="primary", use_container_width=True
        )
    if submitted:
        if not request.user_request.strip():
            st.error("Enter a banking request.")
        elif not request.source_account_id.strip() or not request.destination_account_id.strip():
            st.error("Source and destination account IDs are required.")
        else:
            with st.spinner(
                "Checking the account, validating the recipient, and evaluating the transfer..."
            ):
                st.session_state["banking_jev_result"] = run_demo_sync(request, scenario)
    result = st.session_state.get("banking_jev_result")
    if result is not None and result.get("scenario") == scenario.key:
        render_result(result)


if __name__ == "__main__":
    main()
