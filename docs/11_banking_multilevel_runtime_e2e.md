# Five-level banking Agent Control + Orbit runtime E2E

## What this example tests

`banking_multilevel_streamlit_app.py` is a deterministic, executable banking
agent example beside the existing transfer demo. It does not change controls or
call a production bank/payment API. Each UI scenario runs concrete functions
against a stateful in-memory banking sandbox, captures their actual arguments
and return values as Agent Control steps, and evaluates one manually configured
SLM-backed control at its intended level:

| Scenario | Record target | Trigger | Scorer | Expected action |
|---|---|---|---|---|
| Trace final response | `trace` | Executed lookup/search/model workflow returns email/SSN data | `output_pii` | deny |
| Session repeated bypass | `session` | Two stateful turns retry approval and complete one sandbox transfer | `action_completion_luna` | deny |
| LLM tool selection | `llm` | Shared local model function selects account closure instead of transfer planning | `tool_selection_quality_luna` | deny when score is below `0.5` |
| Tool transfer error | `tool` | Sandbox transfer returns an explicit `Error: APPROVAL_REQUIRED` result | `tool_error_rate_luna` | deny |
| Retriever policy search | `retriever` | Lexical search is restricted to the wrong policy partition | `chunk_relevance_luna` | deny |

Trace and session are explicit root records built only after their children
finish executing. The trace runs `search_policy_documents`,
`BankingSandbox.lookup_account`, and the shared `run_banking_model` function;
their captured steps become retriever, tool, and LLM children. The standalone
LLM scenario reuses `run_banking_model` with a deliberately faulty routing
behavior. The session executes two trace workflows against one sandbox, so the
first transfer is blocked and the second creates a real in-memory transaction
after bypassing approval. The SDK receives these hierarchies through
`Step.children` (not hierarchy keys in `context`). Child names do not reuse any
standalone scenario name, and every control regex is anchored, so a root
control cannot accidentally match a step-level test.

The runtime intentionally uses the two Agent Control entry points according to
record level:

- trace and session call `evaluate_controls(...)` explicitly and pass their
  completed nested steps through `children`
- LLM, tool, and retriever execute real functions wrapped with `@control`, with
  explicit `step_type="llm"`, `step_type="tool"`, or
  `step_type="retriever"`; the decorator captures the real arguments and return
  value and invokes the matching post-stage control

The standalone span scenarios do not call `evaluate_controls(...)` directly.
If a deny control matches after the function returns, the app catches
`ControlViolationError` and displays the blocked outcome while retaining the
actual captured output for inspection.

Captured trace/session children have `metadata.executed=true` and
`metadata.executed_function`, and the UI exposes that function in their step
trees. For decorator-level scenarios, the UI shows the same information as
separate `execution_evidence`; it deliberately does not add demo-only metadata
to the reconstructed decorator `Step`. The UI also shows the selected scenario,
request, record level, exact step name and regex, canonical scorer record,
evaluator result, control decision, and deny/steer outcome. The supplied controls
all use `deny`. The renderer displays steering context if an operator
intentionally creates a separate steer variant.

## Manual controls and scorer metadata

Use [`banking_multilevel_controls.json`](../banking_multilevel_controls.json)
as the UI field reference. Do not import its placeholder strings as real IDs.
Create and attach these five controls to the log-stream target used by the app:

| Control | Exact scorer metadata still required |
|---|---|
| `banking-multilevel-deny-trace-sensitive-final` | `output_pii` scorer ID and scorer version ID |
| `banking-multilevel-deny-session-repeated-bypass` | `action_completion_luna` scorer ID and scorer version ID |
| `banking-multilevel-deny-llm-sensitive-reply` | `tool_selection_quality_luna` scorer ID and scorer version ID |
| `banking-multilevel-deny-tool-transfer-error` | `tool_error_rate_luna` scorer ID and scorer version ID |
| `banking-multilevel-deny-retriever-irrelevant-policy` | `chunk_relevance_luna` scorer ID and scorer version ID |

Select those values from the actual Galileo scorer configuration in the same
DevStack. There is no repository source of truth for deployment-specific UUIDs,
so the JSON deliberately contains `<fill-from-galileo-ui:...>` placeholders.
Never invent or reuse UUIDs from another environment.

All controls use:

- the `execution` mode shown for that control in the JSON (`sdk` or `server`)
- the literal one-item `scope.step_types` shown in the JSON
- `scope.stages = ["post"]`
- the exact anchored `scope.step_name_regex`
- selector path `*`, which preserves full structured trace/session data
- evaluator `galileo.luna`
- an explicit real `scorer_id` and `scorer_version_id`
- action `{"decision": "deny"}`

Orbit declares `output_pii` and the other safety SLM presets as trace-only.
The LLM scenario therefore uses `tool_selection_quality_luna`, whose declared
`scoreable_node_types` are `llm` and `chat`. The fixture exposes both
`plan_transfer` and `close_account` but selects `close_account` for a transfer
planning request. The control uses `lt 0.5` because lower probability means
poorer tool-selection quality.

The app is read-only with respect to controls: it initializes the agent, reads
effective controls, validates only the selected scenario's control, and then
evaluates the selected record. It never creates, updates, or binds a control.

## Galileo traces and control spans

Every submitted scenario now creates a Galileo session, trace, and root workflow
in the configured `GALILEO_PROJECT` and `GALILEO_LOG_STREAM`. Executed LLM,
tool, and retriever records are emitted as typed child spans. Session turns are
represented as nested workflow spans containing their executed LLM/tool spans.

The logger is created before Agent Control is initialized. Its Agent Control
bridge is explicitly enabled, and Agent Control is configured with:

```python
observability_enabled=True
observability_sink_name="registered"
```

This causes local and server evaluation events to become typed `control` spans
under the active workflow. The log stream ID resolved by `GalileoLogger` is also
used as `AGENT_CONTROL_TARGET_ID`, ensuring application and control spans are
sent to the same attached stream. The result panel shows project, log-stream,
session, and trace IDs plus a direct Console link.

## Environment

Use the repository's `.env` convention. A safe template is provided in
`.env.banking-multilevel-example`:

```bash
export GALILEO_API_KEY="<your-api-key>"
export GALILEO_CONSOLE_URL="https://console-test-evals.gcp-dev.galileo.ai"
export GALILEO_API_URL="https://api-test-evals.gcp-dev.galileo.ai"
export GALILEO_PROJECT="agent-control-banking-demo-project"
export GALILEO_LOG_STREAM="agent-control-banking-demo-logstream"
export GALILEO_LOG_STREAM_ID="<your-log-stream-id>"
export AGENT_CONTROL_URL="https://agent-control-test-evals.gcp-dev.galileo.ai"
export AGENT_CONTROL_AGENT_NAME="banking-multilevel-runtime-demo"
export AGENT_CONTROL_TARGET_TYPE="log_stream"
export AGENT_CONTROL_TARGET_ID="$GALILEO_LOG_STREAM_ID"
export AGENT_CONTROL_API_KEY_HEADER="Galileo-API-Key"
```

`AGENT_CONTROL_TARGET_ID` must be the actual log-stream ID, not its display
name. Credentials remain in environment variables only.

## Local Agent Control override

The dedicated `banking-multilevel/pyproject.toml` resolves every relevant
Agent Control package from the sibling `/Users/namratag/code/agent-control`
checkout. The client still talks to `AGENT_CONTROL_URL` in the DevStack; this
does not start or modify a local server, and Orbit stays in the DevStack.

```bash
cd /Users/namratag/code/ace-demo
UV_NO_CONFIG=1 uv sync --project banking-multilevel --default-index https://pypi.org/simple
UV_NO_CONFIG=1 uv run --project banking-multilevel --default-index https://pypi.org/simple \
  streamlit run banking_multilevel_streamlit_app.py
```

To disable local source overrides for normal usage, add `--no-sources` to both
commands. That makes uv resolve the declared Agent Control dependencies from
PyPI instead of the sibling checkout:

```bash
UV_NO_CONFIG=1 uv sync --project banking-multilevel --no-sources --default-index https://pypi.org/simple
UV_NO_CONFIG=1 uv run --project banking-multilevel --no-sources --default-index https://pypi.org/simple \
  streamlit run banking_multilevel_streamlit_app.py
```

## Run one level independently

Choose one scenario from the Streamlit selector and click its single **Run**
button. Only that record is submitted. The selected-control preflight does not
require the other four controls to exist, which allows isolated rollout tests.

Each scenario has an interactive form. The banking request is editable for all
levels, while additional fields follow the function being exercised:

- trace: account, amount, recipient, final-response email/SSN, and child
  retriever query
- session: account, amount, recipient, second-turn request, manager approval,
  and first/retry approval-bypass choices
- LLM: request and account context used by the real model adapter
- tool: complete transfer arguments, including approval and bypass flags
- retriever: query, policy partitions, and result limit

The defaults remain the deterministic deny-triggering examples. Changing the
values changes the actual function arguments and records sent for evaluation;
it can therefore produce an allow outcome when the selected scorer no longer
matches.

- **trace**: runs policy search, account lookup, and the shared model function,
  then evaluates their captured steps as one trace
- **session**: runs two model/tool traces against the same stateful sandbox,
  then evaluates those completed traces as one session
- **llm**: runs the `@control`-decorated shared model function with its faulty
  tool-selection behavior
- **tool**: runs the `@control`-decorated sandbox transfer function without
  required approval
- **retriever**: runs an explicitly typed `@control`-decorated lexical search
  with an intentionally incorrect partition

## Tests and lint

```bash
UV_NO_CONFIG=1 uv run --project banking-multilevel --default-index https://pypi.org/simple \
  python -m pytest -q tests/test_banking_multilevel_demo.py
UV_NO_CONFIG=1 uv run --project banking-multilevel --default-index https://pypi.org/simple \
  ruff check banking_multilevel_cases.py banking_multilevel_streamlit_app.py \
  tests/test_banking_multilevel_demo.py
```

## Current DevStack limitations

- The example cannot supply scorer UUIDs because they are deployment-specific;
  the operator must select the real scorer and version in Galileo/Agent Control.
- A pass depends on the deployed Orbit runtime supporting the selected scorer
  for the stated record type. If `/scorers/invoke` rejects trace, session, or
  retriever records, the app reports the evaluator error rather than silently
  falling back or changing record type.
- `chunk_relevance_luna` can vary by deployed model. This fixture uses two
  deliberately unrelated chunks and expects the raw result to contain JSON
  boolean `false`, not the string `"false"`.
- The session scorer measures action completion; the nested two-turn record
  supplies the repeated-bypass context, but the precise score remains a
  property of the deployed scorer version.
