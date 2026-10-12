#!/usr/bin/env python3
"""Compare agent_control.record_step() against this repo's hand-rolled Step pattern.

Agent Control PR #287 (https://github.com/agentcontrol/agent-control/pull/287)
added `agent_control.record_step()` / `StepRecorder` as an incremental way to
build a trace/session `Step` tree. Before that, the only way to populate
`Step.children` was the hand-built record pattern retained in
`banking_multilevel_cases.py` for comparison:

  1. Every leaf function returns a `StepExecution(value=..., record=...)` -
     the real return value plus a hand-built dict shaped like a `Step`.
  2. A parent function collects those `.record` dicts into `"spans": [...]`
     (trace children) or `"traces": [...]` (session children).
  3. `build_agent_control_step()` recursively converts that dict tree into
     real `Step` objects.
  4. `evaluate_controls()` is called with the built `Step`'s fields unpacked
     back out again (input=, output=, children=, ...).

The reusable StepRecorder workflows live in `banking_multilevel_cases.py`;
the Streamlit app imports them directly. This separate script builds the
*same* three scenarios both ways and asserts the
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
from typing import Any

from agent_control_models import Step

from banking_multilevel_cases import (
    recorder_session_step,
    recorder_trace_step,
    scenario_record,
)
from common import (
    DEFAULT_AGENT_CONTROL_URL,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

CACHE_HIT_STEP_NAME = "banking_multilevel_trace_cache_hit"
DEMO_AGENT_NAME = os.environ.get(
    "AGENT_CONTROL_AGENT_NAME", "step-recorder-vs-manual-demo"
)


def build_agent_control_step(record: dict[str, Any]) -> Step:
    """Translate the legacy dictionary tree without importing the Streamlit app."""
    record_type = str(record["type"])
    input_value = (
        record.get("input", record.get("query"))
        if record_type == "retriever"
        else record.get("input")
    )
    child_records = (
        record.get("traces") if record_type == "session" else record.get("spans")
    )
    children = (
        [build_agent_control_step(child) for child in child_records]
        if isinstance(child_records, list)
        else None
    )
    metadata = record.get("metadata")
    return Step(
        type=record_type,
        name=str(record["name"]),
        input=input_value,
        output=record.get("output"),
        context={"metadata": metadata} if isinstance(metadata, dict) else None,
        tools=record.get("tools") if record_type == "llm" else None,
        children=children,
    )


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
        "output": {
            "status": "answered_from_cache",
            "message": "Your balance is $1,875.25.",
        },
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


new_way_trace = recorder_trace_step
new_way_session = recorder_session_step


def new_way_empty_trace(*, explicit_empty_children: bool) -> Step:
    import agent_control

    container_types = {"trace"} if explicit_empty_children else frozenset()
    with agent_control.record_step(
        "trace",
        CACHE_HIT_STEP_NAME,
        input={"request": "What is my account balance?"},
        container_types=container_types,
    ) as trace:
        trace.output = {
            "status": "answered_from_cache",
            "message": "Your balance is $1,875.25.",
        }
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
    print(
        f"\n--- old way: {old_fn.__module__}.{old_fn.__name__} ({len(old_source.splitlines())} lines) ---"
    )
    print(old_source.rstrip())
    print(
        "    (plus the leaf helpers it calls - execute_policy_search / "
        "execute_account_lookup / execute_banking_llm - and the recursive "
        "converter build_agent_control_step(), none of which the new way needs)"
    )
    print(
        f"\n--- new way: {new_fn.__module__}.{new_fn.__name__} ({len(new_source.splitlines())} lines) ---"
    )
    print(new_source.rstrip())


# --------------------------------------------------------------------------
# Optional: evaluate both built Steps against a live devstack.
# --------------------------------------------------------------------------


async def evaluate_both(
    label: str, old_step: Step, new_step: Step, *, stage: str
) -> None:
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
    new_result = await agent_control.evaluate_step(
        new_step, agent_name=DEMO_AGENT_NAME, stage=stage
    )

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
    parser.add_argument(
        "--scenario", choices=["trace", "session", "empty", "all"], default="all"
    )
    parser.add_argument("--mode", choices=["old", "new", "both"], default="both")
    parser.add_argument("--stage", choices=["pre", "post"], default="post")
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Also send the built Step(s) to a live Agent Control + Galileo devstack. "
        "Requires GALILEO_API_KEY / AGENT_CONTROL_URL (see AGENTS.md).",
    )
    args = parser.parse_args()

    scenarios = (
        ["trace", "session", "empty"] if args.scenario == "all" else [args.scenario]
    )

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
            print_equivalence(
                "empty (default, children omitted)", default_old, default_new
            )

        for label, step in (("old", old_step), ("new", new_step)):
            if step is not None:
                print(
                    f"\n[{scenario}:{label}] {json.dumps(comparable(step), indent=2, sort_keys=True)}"
                )

        if old_step is not None and new_step is not None:
            print_equivalence(scenario, old_step, new_step)
            if args.evaluate:
                asyncio.run(
                    evaluate_both(scenario, old_step, new_step, stage=args.stage)
                )

    if args.scenario in ("all", "trace") and args.mode == "both":
        from banking_multilevel_cases import trace_record

        print_source_contrast(trace_record, new_way_trace)


if __name__ == "__main__":
    main()
