"""Structured records for the manual Agent Control serialization suite.

This module only defines cases and payloads. It never creates or updates
controls; those are configured manually as documented in
``docs/serialization_runtime_controls.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SerializationCase:
    key: str
    label: str
    scorer_label: str
    record_type: str
    step_name: str
    control_name: str
    selector: str = "*"
    stage: str = "post"
    expected_action: str = "deny"

    @property
    def step_name_regex(self) -> str:
        return f"^{self.step_name}$"


CASES: tuple[SerializationCase, ...] = (
    SerializationCase(
        "trace",
        "Trace output PII",
        "output_pii",
        "trace",
        "serialization_trace_output_pii",
        "serialization-deny-trace-output-pii",
    ),
    SerializationCase(
        "session",
        "Session action completion",
        "action_completion_luna",
        "session",
        "serialization_session_action_completion",
        "serialization-deny-session-action-completion",
    ),
    SerializationCase(
        "llm",
        "LLM tool selection",
        "tool_selection_quality_luna",
        "llm",
        "serialization_llm_tool_selection",
        "serialization-deny-llm-tool-selection",
    ),
    SerializationCase(
        "tool",
        "Tool error rate",
        "tool_error_rate_luna",
        "tool",
        "serialization_tool_error_rate",
        "serialization-deny-tool-error-rate",
    ),
    SerializationCase(
        "retriever",
        "Retriever chunk relevance",
        "chunk_relevance_luna",
        "retriever",
        "serialization_retriever_chunk_relevance",
        "serialization-deny-retriever-chunk-relevance",
    ),
)
CASE_BY_KEY = {case.key: case for case in CASES}


def _llm_span(name: str, *, pii: bool = False) -> dict[str, Any]:
    user_content = "Review transfer 7781 and cite the relevant policy sections."
    assistant_content = (
        "Contact alice@example.test about SSN 123-45-6789."
        if pii
        else "I will verify the transfer before submitting it."
    )
    tool_calls = [
        {
            "id": "call-22222222",
            "type": "function",
            "function": {
                "name": "submit_transfer",
                "arguments": json.dumps(
                    {
                        "amount": 375.5,
                        "metadata": {
                            "source": "serialization-suite",
                            "approved": True,
                        },
                    }
                ),
            },
        }
    ]
    return {
        "type": "llm",
        "name": name,
        "id": "11111111-1111-4111-8111-111111111111",
        "input": [
            {"role": "system", "content": "Follow transfer policy."},
            {"role": "user", "content": user_content},
        ],
        "output": {
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_calls,
        },
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "submit_transfer",
                    "description": "Submit a verified transfer",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "number"},
                            "metadata": {"type": "object"},
                        },
                    },
                },
            }
        ],
        "tool_calls": tool_calls,
        "metadata": {
            "model": "serialization-fixture",
            "attempt": 2,
            "timestamp": "2026-09-27T12:30:00Z",
        },
    }


def trace_payload() -> dict[str, Any]:
    """Return a trace with only the LLM and tool calls in its own flow.

    Child names intentionally differ from the standalone LLM and tool test
    names, so those controls cannot be confused with this trace-level case.
    """
    transfer_tool_call = {
        "id": "trace-call-4444",
        "type": "function",
        "function": {
            "name": "submit_transfer",
            "arguments": json.dumps(
                {"amount": 375.5, "destination_account": "acct-4444"}
            ),
        },
    }
    return {
        "type": "trace",
        "name": CASE_BY_KEY["trace"].step_name,
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "created_at": "2026-09-27T12:30:00Z",
        "input": {
            "messages": [
                {"role": "user", "content": "Transfer $375.50 to account 4444."}
            ],
            "request": {"currency": "USD", "attempt": 1},
        },
        "output": {
            "status": "completed",
            "message": "Contact alice@example.test; SSN 123-45-6789 was reviewed.",
        },
        "spans": [
            {
                "type": "llm",
                "name": "serialization_trace_transfer_llm",
                "input": [
                    {"role": "system", "content": "Verify transfers before submission."},
                    {"role": "user", "content": "Transfer $375.50 to account 4444."},
                ],
                "output": {
                    "role": "assistant",
                    "content": "I will verify and submit the transfer.",
                    "tool_calls": [transfer_tool_call],
                },
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_transfer",
                            "description": "Submit a verified transfer",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "amount": {"type": "number"},
                                    "destination_account": {"type": "string"},
                                },
                            },
                        },
                    }
                ],
            },
            {
                "type": "tool",
                "name": "serialization_trace_submit_transfer",
                "input": {
                    "amount": 375.5,
                    "destination_account": "acct-4444",
                },
                "output": {
                    "status": "completed",
                    "receipt_id": "receipt-5555",
                },
            },
        ],
        "metadata": {"tenant": "demo", "attempt": 1, "synthetic": True},
    }


def session_payload() -> dict[str, Any]:
    first_trace = trace_payload()
    first_trace["id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    first_trace["name"] = "serialization_session_trace_one"
    second_trace = {
        "type": "trace",
        "name": "serialization_session_trace_two",
        "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "input": {"messages": [{"role": "user", "content": "Check transfer status."}]},
        "output": {"status": "verified", "checked_at": "2026-09-27T12:35:00Z"},
        "spans": [
            _llm_span("serialization_session_llm"),
            {
                "type": "tool",
                "name": "serialization_session_tool",
                "input": {
                    "transfer_id": "txn-7777",
                    "options": {"include_events": True},
                },
                "output": {"status": "verified", "events": ["created", "settled"]},
            },
            {
                "type": "retriever",
                "name": "serialization_session_retriever",
                "input": "settlement status",
                "output": [
                    {
                        "content": "The transfer settled.",
                        "metadata": {"source": "ledger.json", "rank": 1, "score": 0.99},
                    },
                    {
                        "content": "Settlement takes one business day.",
                        "metadata": {"source": "faq.md", "rank": 2, "score": 0.73},
                    },
                ],
            },
        ],
    }
    return {
        "type": "session",
        "name": CASE_BY_KEY["session"].step_name,
        "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "created_at": "2026-09-27T12:30:00Z",
        "input": {
            "messages": [
                {"role": "system", "content": "Assist with account transfers."},
                {"role": "user", "content": "Transfer $375.50."},
            ]
        },
        "output": {
            "response": {
                "status": "verified",
                "documents": [{"title": "Transfer receipt", "id": "receipt-5555"}],
            },
            "message": "Transfer verified.",
        },
        "traces": [first_trace, second_trace],
        "metadata": {"channel": "streamlit", "synthetic": True},
    }


def llm_payload() -> dict[str, Any]:
    # Keep the canonical OpenAI-style messages, assistant tool call, and tool
    # definitions intact. These are the fields the scorer's record contract
    # must serialize and Orbit must reconstruct.
    return _llm_span(CASE_BY_KEY["llm"].step_name)


def tool_payload() -> dict[str, Any]:
    return {
        "type": "tool",
        "name": CASE_BY_KEY["tool"].step_name,
        "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        # A tool step's input is its argument object. The output is the
        # simulated tool result that tool_error_rate_luna evaluates.
        "input": {"code": "print(undefined_variable)"},
        "output": "Error: NameError - name 'undefined_variable' is not defined",
        "metadata": {
            "trace_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "redacted_fields": ["code"],
            "timestamp": "2026-09-27T12:31:00Z",
        },
    }


def retriever_payload() -> dict[str, Any]:
    return {
        "type": "retriever",
        "name": CASE_BY_KEY["retriever"].step_name,
        # Use only unrelated content so the case checks a negative relevance
        # result. Orbit documents that positive chunk classifications are not
        # reliable on the vLLM v2 wizard currently used by this devstack.
        "query": "What is the capital of France?",
        "output": [
            {
                "content": "Bananas are a good source of potassium.",
                "metadata": {"source": "nutrition-guide.txt", "rank": 1, "score": 0.96},
            },
            {
                "content": "The Pacific Ocean is the largest ocean on Earth.",
                "metadata": {"source": "ocean-facts.txt", "rank": 2, "score": 0.82},
            },
        ],
        "metadata": {
            "retrieval_id": "77777777-7777-4777-8777-777777777777",
            "created_at": "2026-09-27T12:32:00Z",
        },
    }


PAYLOAD_FACTORIES = {
    "trace": trace_payload,
    "session": session_payload,
    "llm": llm_payload,
    "tool": tool_payload,
    "retriever": retriever_payload,
}


def case_payload(key: str) -> dict[str, Any]:
    try:
        return PAYLOAD_FACTORIES[key]()
    except KeyError as exc:
        raise ValueError(f"Unknown serialization case: {key}") from exc


def validate_case_payload(case: SerializationCase, payload: dict[str, Any]) -> None:
    """Fail fast on accidental loss of the structured test fixture."""
    if payload.get("type") != case.record_type or payload.get("name") != case.step_name:
        raise ValueError(f"Malformed {case.key} fixture: wrong type or step name")
    if case.key == "trace":
        spans = payload.get("spans")
        if not isinstance(spans, list) or {span.get("type") for span in spans} != {
            "llm",
            "tool",
        }:
            raise ValueError("Trace fixture must include only its related LLM and tool spans")
        names = {span.get("name") for span in spans}
        if names & {CASE_BY_KEY["llm"].step_name, CASE_BY_KEY["tool"].step_name}:
            raise ValueError("Trace child spans must not reuse standalone case names")
        llm = next(span for span in spans if span.get("type") == "llm")
        if not llm.get("output", {}).get("tool_calls"):
            raise ValueError("Trace LLM span must show its related tool call")
    elif case.key == "session":
        traces = payload.get("traces")
        if not isinstance(traces, list) or len(traces) != 2:
            raise ValueError("Session fixture must include exactly two nested traces")
    elif case.key == "llm":
        messages = payload.get("input")
        output = payload.get("output")
        if not isinstance(messages, list) or not any(
            isinstance(message, dict) and message.get("role") == "user"
            for message in messages
        ):
            raise ValueError("LLM fixture input must preserve its structured user message")
        if not isinstance(output, dict) or output.get("role") != "assistant":
            raise ValueError("LLM fixture output must be an assistant message object")
        if not output.get("tool_calls") or not payload.get("tools"):
            raise ValueError("LLM fixture must retain tool definitions and assistant tool_calls")
        call = output["tool_calls"][0]
        if not call.get("id") or not call.get("function", {}).get("name"):
            raise ValueError("LLM tool call must include its ID and function name")
        if not call.get("function", {}).get("arguments"):
            raise ValueError("LLM tool call must include function arguments")
    elif case.key == "tool":
        json.dumps(payload, allow_nan=False)
        if not isinstance(payload.get("input"), dict) or not isinstance(payload.get("output"), str):
            raise ValueError(
                "Tool error fixture needs an argument object and textual result"
            )
        if not payload["output"].startswith("Error:"):
            raise ValueError("Tool error fixture must describe its failure")
    elif case.key == "retriever":
        docs = payload.get("output")
        if not isinstance(docs, list) or len(docs) < 2:
            raise ValueError(
                "Retriever fixture must include documents with scalar metadata"
            )
        for document in docs:
            if not isinstance(document.get("content"), str) or not isinstance(
                document.get("metadata"), dict
            ):
                raise TypeError(
                    "Retriever fixture documents require content and scalar metadata"
                )
            if any(
                not isinstance(value, (str, int, float, bool))
                for value in document["metadata"].values()
            ):
                raise ValueError("Retriever metadata values must be scalar")
