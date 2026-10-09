#!/usr/bin/env python3
"""Compare agent_control.record_step() against this repo's hand-rolled Step pattern.

Agent Control PR #287 (https://github.com/agentcontrol/agent-control/pull/287)
added `agent_control.record_step()` / `StepRecorder` as an incremental way to
build a trace/session `Step` tree. Before that, the only way to populate
`Step.children` was the pattern already used by `banking_multilevel_cases.py`
and `banking_multilevel_streamlit_app.py` in this repo:

  1. Every leaf function returns a `StepExecution(value=..., record=...)` -
     the real return value plus a hand-built dict shaped like a `Step`.
  2. A parent function collects those `.record` dicts into `"spans": [...]`
     (trace children) or `"traces": [...]` (session children).
  3. `build_agent_control_step()` recursively converts that dict tree into
     real `Step` objects.
  4. `evaluate_controls()` is called with the built `Step`'s fields unpacked
     back out again (input=, output=, children=, ...).

This script builds the *same* three scenarios both ways and asserts the
resulting `Step` trees are byte-for-byte equivalent - including `context`,
the one field that has to be populated by hand on both sides - so a reader
can see directly, not just read a claim, that the new API produces the same
result with less code and no intermediate dict:

  - "trace":   one trace with retriever + tool + llm children
               (mirrors banking_multilevel_cases.trace_record)
  - "session": one session nesting two traces, each with llm + tool children
               (mirrors banking_multilevel_cases.session_record)
  - "empty":   a trace that ran with zero children, built once with the
               default omitted-children behavior and once opting into
               `container_types` to get `children=[]` instead - the one
               piece of PR #287 behavior that has no old-way equivalent
               short of remembering to add an empty list by hand

Build and compare everything, no network access or credentials required:

    python step_recorder_vs_manual_demo.py

Build only one way, or only one scenario:

    python step_recorder_vs_manual_demo.py --mode new --scenario session
    python step_recorder_vs_manual_demo.py --mode old --scenario empty

Also evaluate both built Steps against a live Agent Control + Galileo
devstack (see AGENTS.md for the full variable list). GALILEO_API_KEY is
read from the environment only and is never printed:

    GALILEO_API_KEY="<your-api-key>" \\
    AGENT_CONTROL_URL="https://agent-control-test-evals.gcp-dev.galileo.ai" \\
    python step_recorder_vs_manual_demo.py --evaluate

The new-way path needs agent-control-sdk>=8.11.0 (the version record_step()
first shipped in).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from copy import deepcopy
from typing import Any

from agent_control_models import Step

from banking_multilevel_cases import (
    SCENARIO_BY_KEY,
    TOOL_DEFINITIONS,
    BankingSandbox,
    BankingScenarioInput,
    default_scenario_input,
    run_banking_model,
    scenario_record,
    search_policy_documents,
)
from banking_multilevel_streamlit_app import build_agent_control_step
from common import (
    DEFAULT_AGENT_CONTROL_URL,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

SYSTEM_PROMPT = "Help with banking requests without exposing private data."
CACHE_HIT_STEP_NAME = "banking_multilevel_trace_cache_hit"
DEMO_AGENT_NAME = os.environ.get("AGENT_CONTROL_AGENT_NAME", "step-recorder-vs-manual-demo")


# --------------------------------------------------------------------------
# Old way: reuse this repo's existing StepExecution + dict-record pattern.
# --------------------------------------------------------------------------


def old_way_trace() -> Step:
    return build_agent_control_step(scenario_record("trace"))


def old_way_session() -> Step:
    return build_agent_control_step(scenario_record("session"))


def old_way_empty_trace(*, explicit_empty_children: bool) -> Step:
    record: dict[str, Any] = {
        "type": "trace",
        "name": CACHE_HIT_STEP_NAME,
        "input": {"request": "What is my account balance?"},
        "output": {"status": "answered_from_cache", "message": "Your balance is $1,875.25."},
    }
    if explicit_empty_children:
        # Easy to forget: nothing enforces that an intentionally childless
        # trace gets an empty list instead of just omitting the key.
        record["spans"] = []
    return build_agent_control_step(record)


# --------------------------------------------------------------------------
# New way: agent_control.record_step() / StepRecorder, built from the same
# underlying sandbox/model calls so the two paths produce the same values.
# --------------------------------------------------------------------------


def new_way_trace(inputs: BankingScenarioInput | None = None) -> Step:
    import agent_control

    scenario = SCENARIO_BY_KEY["trace"]
    inputs = inputs or default_scenario_input(scenario)
    sandbox = BankingSandbox()

    with agent_control.record_step(
        "trace",
        scenario.step_name,
        input={"request": inputs.request, "customer_id": "cust-demo-001"},
    ) as trace:
        # .child() with an explicit output: step_context= could carry this
        # leaf's metadata, but .call()'s auto-derived `input` would be a
        # bound-args dict ({"query": ..., "partitions": ..., "limit": ...}),
        # not the bare query string the old way's record uses as `input`.
        with trace.child(
            "retriever",
            "banking_multilevel_trace_policy_lookup",
            input=inputs.policy_query,
            context={
                "metadata": {
                    "executed": True,
                    "executed_function": "search_policy_documents",
                    "partitions": ["wire"],
                    "external_call": False,
                }
            },
        ) as retrieval:
            retrieval.output = search_policy_documents(
                inputs.policy_query, partitions=("wire",), limit=1
            )

        # .call(): runs the real function and records it in one step. Its
        # auto-derived `input` (the bound call arguments) already matches the
        # old way's tool_input dict here, and step_context= (not context=, to
        # avoid colliding with a same-named kwarg on the wrapped function -
        # not an issue for this particular function, but a general hazard)
        # attaches the same per-leaf metadata the old way sets by hand.
        account = trace.call(
            sandbox.lookup_account,
            account_id=inputs.account_id,
            step_type="tool",
            step_name="banking_multilevel_trace_account_lookup",
            step_context={
                "metadata": {
                    "executed": True,
                    "executed_function": "BankingSandbox.lookup_account",
                    "external_call": False,
                }
            },
        )

        with trace.child(
            "llm",
            "banking_multilevel_trace_plan_llm",
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": inputs.request},
            ],
            tools=deepcopy(TOOL_DEFINITIONS),
            context={
                "metadata": {
                    "executed": True,
                    "executed_function": "run_banking_model",
                    "model": "deterministic-local-banking-model",
                    "behavior": "sensitive_transfer_plan",
                    "external_call": False,
                }
            },
        ) as model:
            model.output = run_banking_model(
                inputs.request,
                behavior="sensitive_transfer_plan",
                context={
                    "policies": retrieval.output,
                    "account": account,
                    "recipient": {
                        "name": inputs.recipient,
                        "email": inputs.recipient_email,
                        "ssn": inputs.recipient_ssn,
                    },
                    "amount": inputs.amount,
                },
            )

        trace.output = {"status": "planned", "message": model.output["content"]}
        # Set after the children exist, same as the old way: trace_record()
        # only knows each child's executed_function once it has built them.
        trace.context = {
            "metadata": {
                "executed": True,
                "executed_function": "trace_record",
                "child_functions": [
                    "search_policy_documents",
                    "BankingSandbox.lookup_account",
                    "run_banking_model",
                ],
                "scenario": scenario.key,
                "external_call": False,
            }
        }

    return trace.build()


def _new_way_session_turn(
    session: Any,
    sandbox: BankingSandbox,
    *,
    trace_name: str,
    request: str,
    behavior: str,
    llm_name: str,
    tool_name: str,
    account_id: str,
    amount: float,
    recipient: str,
    manager_approved: bool,
    bypass_approval: bool,
) -> dict[str, Any]:
    with session.child(
        "trace",
        trace_name,
        input={"request": request},
        context={
            "metadata": {
                "executed": True,
                "executed_function": "_execute_session_turn",
                "external_call": False,
            }
        },
    ) as trace:
        with trace.child(
            "llm",
            llm_name,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request},
            ],
            tools=deepcopy(TOOL_DEFINITIONS),
            context={
                "metadata": {
                    "executed": True,
                    "executed_function": "run_banking_model",
                    "model": "deterministic-local-banking-model",
                    "behavior": behavior,
                    "external_call": False,
                }
            },
        ) as model:
            model.output = run_banking_model(
                request,
                behavior=behavior,
                context={
                    "account_id": account_id,
                    "amount": amount,
                    "manager_approved": manager_approved,
                },
            )

        # Unlike the trace scenario's account lookup, this leaf can't move to
        # .call() even with step_context=: sandbox_transaction_count is only
        # known after submit_transfer() runs (it reads len(sandbox.transactions)
        # post-call, same as the old way's execute_transfer()), but .call()'s
        # step_context is evaluated eagerly, before the call happens. .child()
        # stays necessary here because its context can be set after output.
        with trace.child(
            "tool",
            tool_name,
            input={
                "account_id": account_id,
                "amount": amount,
                "recipient": recipient,
                "manager_approved": manager_approved,
                "bypass_approval": bypass_approval,
            },
        ) as transfer_step:
            transfer_step.output = sandbox.submit_transfer(
                account_id=account_id,
                amount=amount,
                recipient=recipient,
                manager_approved=manager_approved,
                bypass_approval=bypass_approval,
            )
            transfer_step.context = {
                "metadata": {
                    "executed": True,
                    "executed_function": "BankingSandbox.submit_transfer",
                    "sandbox_transaction_count": len(sandbox.transactions),
                    "external_call": False,
                }
            }
        transfer = transfer_step.output
        trace.output = deepcopy(transfer)
    return transfer


def new_way_session(inputs: BankingScenarioInput | None = None) -> Step:
    import agent_control

    scenario = SCENARIO_BY_KEY["session"]
    inputs = inputs or default_scenario_input(scenario)
    sandbox = BankingSandbox()
    first_request = inputs.request
    retry_request = inputs.retry_request

    with agent_control.record_step(
        "session",
        scenario.step_name,
        input={
            "messages": [
                {"role": "user", "content": first_request},
                {"role": "user", "content": retry_request},
            ]
        },
    ) as session:
        first_output = _new_way_session_turn(
            session,
            sandbox,
            trace_name="banking_multilevel_session_first_attempt",
            request=first_request,
            behavior="approval_attempt",
            llm_name="banking_multilevel_session_first_llm",
            tool_name="banking_multilevel_session_first_tool",
            account_id=inputs.account_id,
            amount=inputs.amount,
            recipient=inputs.recipient,
            manager_approved=inputs.manager_approved,
            bypass_approval=inputs.bypass_approval,
        )
        second_output = _new_way_session_turn(
            session,
            sandbox,
            trace_name="banking_multilevel_session_retry_attempt",
            request=retry_request,
            behavior="approval_bypass_retry" if inputs.retry_bypass_approval else "approval_attempt",
            llm_name="banking_multilevel_session_retry_llm",
            tool_name="banking_multilevel_session_retry_tool",
            account_id=inputs.account_id,
            amount=inputs.amount,
            recipient=inputs.recipient,
            manager_approved=inputs.manager_approved,
            bypass_approval=inputs.retry_bypass_approval,
        )
        completed = [
            output for output in (first_output, second_output) if output["status"] == "completed"
        ]
        session.output = {
            "status": "completed" if completed else "blocked",
            "summary": (
                "The assistant repeated the approval bypass and reported a completed transfer."
                if completed and inputs.retry_bypass_approval
                else "The assistant completed an approved transfer."
                if completed
                else "Both transfer attempts remained blocked by the approval requirement."
            ),
            "attempts": 2,
            "completed_transactions": len(completed),
        }
        # Set after both turns, same as the old way: session_record() only
        # knows the final transaction count once both turns have run.
        session.context = {
            "metadata": {
                "executed": True,
                "executed_function": "session_record",
                "sandbox_transaction_count": len(sandbox.transactions),
                "scenario": scenario.key,
                "external_call": False,
            }
        }

    return session.build()


def new_way_empty_trace(*, explicit_empty_children: bool) -> Step:
    import agent_control

    container_types = {"trace"} if explicit_empty_children else frozenset()
    with agent_control.record_step(
        "trace",
        CACHE_HIT_STEP_NAME,
        input={"request": "What is my account balance?"},
        container_types=container_types,
    ) as trace:
        trace.output = {"status": "answered_from_cache", "message": "Your balance is $1,875.25."}
    return trace.build()


# --------------------------------------------------------------------------
# Equivalence check and ergonomics display.
# --------------------------------------------------------------------------


def comparable(step: Step) -> dict[str, Any]:
    return step.model_dump(mode="json")


def print_equivalence(label: str, old_step: Step, new_step: Step) -> bool:
    old_payload = comparable(old_step)
    new_payload = comparable(new_step)
    equal = old_payload == new_payload
    print(f"\n[{label}] old way vs new way: {'EQUIVALENT' if equal else 'MISMATCH'}")
    if not equal:
        print("--- old way ---")
        print(json.dumps(old_payload, indent=2, sort_keys=True))
        print("--- new way ---")
        print(json.dumps(new_payload, indent=2, sort_keys=True))
    return equal


def print_source_contrast(old_fn: Any, new_fn: Any) -> None:
    old_source = inspect.getsource(old_fn)
    new_source = inspect.getsource(new_fn)
    print(f"\n--- old way: {old_fn.__module__}.{old_fn.__name__} ({len(old_source.splitlines())} lines) ---")
    print(old_source.rstrip())
    print(
        "    (plus the leaf helpers it calls - execute_policy_search / "
        "execute_account_lookup / execute_banking_llm - and the recursive "
        "converter build_agent_control_step(), none of which the new way needs)"
    )
    print(f"\n--- new way: {new_fn.__module__}.{new_fn.__name__} ({len(new_source.splitlines())} lines) ---")
    print(new_source.rstrip())


# --------------------------------------------------------------------------
# Optional: evaluate both built Steps against a live devstack.
# --------------------------------------------------------------------------


async def evaluate_both(label: str, old_step: Step, new_step: Step, *, stage: str) -> None:
    import agent_control

    agent_control.init(
        agent_name=DEMO_AGENT_NAME,
        server_url=os.environ.get("AGENT_CONTROL_URL", DEFAULT_AGENT_CONTROL_URL),
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
    )

    # Old way: the Step was already built, so it must be unpacked back into
    # evaluate_controls()'s field-based arguments.
    old_result = await agent_control.evaluate_controls(
        old_step.name,
        input=old_step.input,
        output=old_step.output,
        context=old_step.context,
        tools=old_step.tools,
        children=old_step.children,
        step_type=old_step.type,
        stage=stage,
        agent_name=DEMO_AGENT_NAME,
    )
    # New way: evaluate the recorder's own built Step directly.
    new_result = await agent_control.evaluate_step(new_step, agent_name=DEMO_AGENT_NAME, stage=stage)

    for way, result in (("old", old_result), ("new", new_result)):
        matches = len(getattr(result, "matches", None) or [])
        non_matches = len(getattr(result, "non_matches", None) or [])
        errors = len(getattr(result, "errors", None) or [])
        print(
            f"[{label}:{way}] evaluate(stage={stage}) -> "
            f"matches={matches} non_matches={non_matches} errors={errors}"
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["trace", "session", "empty", "all"], default="all")
    parser.add_argument("--mode", choices=["old", "new", "both"], default="both")
    parser.add_argument("--stage", choices=["pre", "post"], default="post")
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Also send the built Step(s) to a live Agent Control + Galileo devstack. "
        "Requires GALILEO_API_KEY / AGENT_CONTROL_URL (see AGENTS.md).",
    )
    args = parser.parse_args()

    scenarios = ["trace", "session", "empty"] if args.scenario == "all" else [args.scenario]

    builders: dict[str, tuple[Any, Any]] = {
        "trace": (old_way_trace, new_way_trace),
        "session": (old_way_session, new_way_session),
        "empty": (
            lambda: old_way_empty_trace(explicit_empty_children=True),
            lambda: new_way_empty_trace(explicit_empty_children=True),
        ),
    }

    for scenario in scenarios:
        old_builder, new_builder = builders[scenario]
        old_step = old_builder() if args.mode in ("old", "both") else None
        new_step = new_builder() if args.mode in ("new", "both") else None

        if scenario == "empty" and args.mode in ("old", "both"):
            # Also show the default (no explicit container_types) case, where
            # both ways agree that children is simply omitted.
            default_old = old_way_empty_trace(explicit_empty_children=False)
            default_new = new_way_empty_trace(explicit_empty_children=False)
            print_equivalence("empty (default, children omitted)", default_old, default_new)

        for label, step in (("old", old_step), ("new", new_step)):
            if step is not None:
                print(f"\n[{scenario}:{label}] {json.dumps(comparable(step), indent=2, sort_keys=True)}")

        if old_step is not None and new_step is not None:
            print_equivalence(scenario, old_step, new_step)
            if args.evaluate:
                asyncio.run(evaluate_both(scenario, old_step, new_step, stage=args.stage))

    if args.scenario in ("all", "trace") and args.mode == "both":
        from banking_multilevel_cases import trace_record

        print_source_contrast(trace_record, new_way_trace)


if __name__ == "__main__":
    main()
