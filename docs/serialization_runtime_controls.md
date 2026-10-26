# Serialization and Orbit runtime controls

## Purpose and scope

This example exercises the committed Agent Control serialization work from
`~/code/agent-control` with the Galileo evaluator and Orbit scorer runtime
already built into the DevStack. It submits one deny control for each public
Galileo record type: trace, session, LLM, tool, and retriever. The trace,
session, tool, and retriever cases require the configured deny. The LLM
tool-selection case checks the serialization/deserialization contract; its
deny action and `gte 0.5` threshold remain configured, but scorer execution is
not required for this contract check.

The Streamlit app only reads enabled, rendered controls attached to the target.
Create and attach all five controls manually in the Agent Control UI or API
before running it. The app does not create or update control definitions.

The local Orbit scorer catalog at the Agent Control/Orbit development checkout
lists these exact labels, output types, and compatible node types:

| Record level | Scorer label | Galileo output type | Compatible node type |
|---|---|---|---|
| Trace | `output_pii` | `multilabel` | `trace` |
| Session | `action_completion_luna` | `boolean_probability` | `session` |
| LLM | `tool_selection_quality_luna` | `boolean_probability` | `llm` |
| Tool | `tool_error_rate_luna` | `percentage` | `tool` |
| Retriever | `chunk_relevance_luna` | `retrieved_chunk_list_boolean` | `retriever` |

These assignments come from `~/code/orbit/libs/python/promptgalileo/promptgalileo/schemas/scorer_info.py` in the local Orbit checkout. That catalog describes batch metric types. The runtime scorer implementations return raw values through `/scorers/invoke`; the local source shows category strings for PII, numeric probabilities for boolean-probability scorers, and a boolean list for chunk relevance. Confirm deployed scorer versions during the run. For the LLM contract case, a successful score or explicit unsupported-scorer response is acceptable only when the endpoint accepted the request without a validation/deserialization error.

## Required environment

Set the Galileo key and the Agent Control service/target values. `GALILEO_LOG_STREAM_ID` (or `AGENT_CONTROL_TARGET_ID`) must be the actual target ID, not its display name.

```bash
export GALILEO_API_KEY="<your-api-key>"
export AGENT_CONTROL_URL="https://agent-control-test-evals.gcp-dev.galileo.ai"
export AGENT_CONTROL_AGENT_NAME="galileo-control-span-demo"
export AGENT_CONTROL_TARGET_TYPE="log_stream"
export AGENT_CONTROL_TARGET_ID="<log-stream-id>"
```

The Agent Control server-side Galileo Luna evaluator also needs its normal
DevStack configuration, including the Galileo API secret and scorer invoke
URL. Those server credentials do not belong in this app's environment or
repository. Do not put API keys in files.

## Five manual controls

Use one enabled `galileo.luna` evaluator condition and one `deny` action per
control. In the UI, select the scorer by its real scorer ID and its real scorer
version ID. The example deliberately does not select a default version.

| Name | Step name | Step type | Stage | Selector path | Scorer |
|---|---|---|---|---|---|
| `serialization-deny-trace-output-pii` | `serialization_trace_output_pii` | `trace` | `post` | `*` | `output_pii` |
| `serialization-deny-session-action-completion` | `serialization_session_action_completion` | `session` | `post` | `*` | `action_completion_luna` |
| `serialization-deny-llm-tool-selection` | `serialization_llm_tool_selection` | `llm` | `post` | `*` | `tool_selection_quality_luna` |
| `serialization-deny-tool-error-rate` | `serialization_tool_error_rate` | `tool` | `post` | `*` | `tool_error_rate_luna` |
| `serialization-deny-retriever-chunk-relevance` | `serialization_retriever_chunk_relevance` | `retriever` | `post` | `*` | `chunk_relevance_luna` |

Use these exact anchored step-name regexes:

```text
^serialization_trace_output_pii$
^serialization_session_action_completion$
^serialization_llm_tool_selection$
^serialization_tool_error_rate$
^serialization_retriever_chunk_relevance$
```

Set `scope.step_types` to the literal one-item list shown for each control:

```json
["trace"]
["session"]
["llm"]
["tool"]
["retriever"]
```

`*` selects the complete serialized Agent Control step. This is required for
trace and session: those records need their `spans`/`traces` collections from
context, so a scalar selector such as `output` cannot construct the canonical
Galileo record. The trace case includes only its related LLM transfer decision
and the resulting tool call; their names are distinct from the standalone LLM
and tool cases, so those controls remain isolated. The session case exercises
nested records. The retriever case sends its query as a string, matching
Galileo's `RetrieverSpanRecord.input` contract; the SDK decorator's custom-step
argument mapping (`{"query": ...}`) would change what Orbit receives as the
query.

## Luna evaluator settings

The evaluator configuration must contain the IDs from the actual Galileo
evaluator configuration. Never use a default scorer version or copy a scorer
ID/version from another environment.

```json
{
  "scorer_id": "<scorer-id>",
  "scorer_version_id": "<scorer-version-id>",
  "scorer_label": "<exact scorer label>",
  "operator": "<operator>",
  "threshold": "<value>"
}
```

The `galileo.luna` evaluator compares Orbit's raw runtime result locally. Use
these output-type-aware comparisons so each fixture can match:

| Scorer | Operator | Value | Comparison meaning |
|---|---|---|---|
| `output_pii` | `contains` | `"email"` (scalar string) | Deny when the raw runtime category list includes email PII. The fixture also includes an SSN (mapped to `ssn`). |
| `action_completion_luna` | `gte` | `0.5` | Deny when the session completion probability is at least 0.5. |
| `tool_selection_quality_luna` | `gte` | `0.5` | Deny when the tool selection quality probability is at least 0.5. |
| `tool_error_rate_luna` | `gte` | `0.5` | Deny when the tool error score is at least 0.5. |
| `chunk_relevance_luna` | `contains` | `false` (JSON boolean) | Deny when any retrieved chunk is classified as irrelevant. |

The evaluator's comparison operators are `any`, `eq`, `ne`, `contains`, `gt`,
`gte`, `lt`, and `lte`. Numeric operators require a numeric threshold;
`contains` checks a list member or a mapping/string value. Confirm the raw
runtime encoding in the DevStack if it differs from the local runtime scorer
implementation; do not infer runtime shapes from batch metric types alone.
For `output_pii`, the scorer returns a list such as `["email", "ssn"]`. The
`contains` threshold must be the scalar string `"email"`; do not enter
`["email"]`, which asks the evaluator to find that whole list as one item
inside the category list and will not match.
For `chunk_relevance_luna`, the scorer returns booleans such as `[true, false]`.
For this devstack-compatible negative-relevance case, the threshold must be
the JSON boolean `false`, not the string `"false"` or a list containing that
string.

For `tool_error_rate_luna`, the fixture returns the error text in the tool
record's `output`. It simulates a tool that returned an error result; it does
not raise a Python exception. The scorer reads the tool output and returns a
failure probability, which the `gte 0.5` control threshold turns into a deny.
The tool's `input` is its argument object, such as `{"code": "..."}`. The app
evaluates this case directly so it serializes as a canonical `ToolSpanRecord`,
without wrapping the arguments in a `tool_input` mapping.

## LLM tool-selection execution path

The LLM case sends a canonical LLM record with its available `tools` schema and
the assistant's selected tool call in `output`. The UI displays the exact
scorer-invoke `record` and the expected legacy `inputs.query`/`inputs.response`
values. Those legacy values are structured and derived from the canonical record so
tool-call normalization cannot make the two representations conflict. A
successful score passes the contract check. An explicit unsupported-scorer
response also passes if the endpoint returned a successful HTTP status; an
HTTP validation error, deserialization error, or generic evaluator failure
does not pass. The case still reports whether the configured deny action
actually fired separately from its contract result.

This demo does not replace the requested metric or add Orbit-specific routing.
When the environment executes the canonical LLM record, it must retain
`tools`; the existing LLM scorer reads query, response, and tools from that
record.

## Create and attach controls manually

For each table row in the Agent Control UI:

1. Create a control with the exact name and regex from the table.
2. Set `execution` to `sdk`, the literal `scope.step_types` list, and `scope.stages` to `["post"]`.
3. Configure the condition selector as `{"path": "*"}` and select the `galileo.luna` evaluator.
4. Enter that scorer's real `scorer_id`, real `scorer_version_id`, exact `scorer_label`, operator, and threshold from the previous section.
5. Set the action to exactly:

   ```json
   {"action": {"decision": "deny"}}
   ```

6. Enable the control and attach it to the existing Agent Control agent or the target log stream. The app reads controls for `AGENT_CONTROL_AGENT_NAME`, `AGENT_CONTROL_TARGET_TYPE`, and `AGENT_CONTROL_TARGET_ID`; attach the controls so that they are visible to this effective target.
7. Confirm all five controls are enabled and rendered for that agent/target before starting the app.

Do not add observe or steer controls to this suite.

## Use the local Agent Control checkout

The dedicated TOML environment points at editable packages from
`~/code/agent-control`; the repository's normal `pyproject.toml` and its
PyPI-pinned Agent Control dependencies remain unchanged.

```bash
cd ~/code/ace-demo
UV_NO_CONFIG=1 uv sync --project serialization-runtime --default-index https://pypi.org/simple
UV_NO_CONFIG=1 uv run --project serialization-runtime --default-index https://pypi.org/simple streamlit run serialization_runtime_streamlit_app.py
```

The local checkout must be the committed Agent Control change under test. The
Orbit runtime is supplied by the constructed DevStack; this example does not
call `/scorers/invoke` directly.

## Expected results

Click exactly one of **Run trace serialization case**, **Run session
serialization case**, **Run LLM serialization case**, **Run tool serialization
case**, or **Run retriever serialization case** per run. The trace record shows
its own input/output plus the LLM and tool spans that implement that transfer;
it does not include the standalone LLM/tool test fixtures or retriever data.
The session case carries its own nested traces and spans. A case passes only if
its matching named deny control is returned. A successful Luna scorer response
by itself is not a pass.

The retriever case uses Agent Control's direct evaluation API so the
`scorer_invoke` record has the query string as `input`. The decorator path for
custom step types serializes bound arguments as an object, which Orbit would
interpret as the query text and can produce false relevance results.
Its two chunks are unrelated to the query, so this case expects a boolean list
containing `false` and a deny from the `contains false` control. This checks
the list's boolean serialization and deny path without relying on positive
classification, which Orbit's source marks unreliable on the vLLM v2 wizard
used in this devstack.

The fixtures include structured trace input/output, a session with two nested
traces and session-specific child spans, multiple LLM input messages and
tool-call data, nested tool JSON with an error result, and at least two
retriever documents with scalar metadata. IDs and timestamps are synthetic.
The UI displays only the selected case result, its scorer-invoke `record`, and
for LLM the expected compatibility `inputs` fields.

The LLM fixture retains its message list, assistant `tool_calls` object,
available tools, and metadata. The structured record is primary; the legacy
query and response fields are included for backward compatibility.

## Troubleshooting

- **HTTP 422**: Check the literal step type, complete `*` selector, child
  spans/traces, nested JSON types, and the exact scorer output-compatible
  comparison. Keep retriever document metadata scalar.
- **Missing scorer version**: Reopen the selected Galileo evaluator version
  and copy its actual version ID into `scorer_version_id`. The structured
  runtime path requires an explicit version; no default is substituted.
- **Wrong step type**: Confirm `scope.step_types` exactly matches the row and
  that the local editable Agent Control SDK is active (`uv run --project
  serialization-runtime ...`).
- **No deny result**: Check the control is enabled, rendered, attached to the
  configured agent/log-stream target, and uses the exact name regex and
  `post` stage. Confirm scorer ID/version/label and comparison semantics. A
  non-deny scorer response is a failed case.
- **HTTP 500 or scorer error**: Check Agent Control and Orbit service health
  and server-side Galileo evaluator credentials/invoke URL. The app reports
  the error as a failure and does not invoke Orbit itself.
