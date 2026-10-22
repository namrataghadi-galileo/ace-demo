# Agent Control Galileo E2E

Standalone demo app for exercising real Agent Control `ControlExecutionEvent`s through the Galileo Python bridge and into Galileo as `ControlSpan`s, while the Agent Control server itself runs remotely.

For a step-by-step Forward Deploy Engineer runbook, including manual Console setup, devstack routes, commands, and troubleshooting, see [`FDE_RUNBOOK.md`](FDE_RUNBOOK.md).

For the deterministic banking workflow that independently exercises trace,
session, LLM, tool, and retriever records through Agent Control and Orbit
runtime scorers, see
[`docs/11_banking_multilevel_runtime_e2e.md`](docs/11_banking_multilevel_runtime_e2e.md).
It uses a separate Streamlit app and a separate manual-control catalog, leaving
the existing banking controls unchanged.

For the preset-SLM direct-runtime and LLM/tool record-compatibility tests for
Orbit SAO-16056 and Agent Control, see
[`docs/10_slm_runtime_e2e.md`](docs/10_slm_runtime_e2e.md). It uses the
separate [`slm_runtime_streamlit_app.py`](slm_runtime_streamlit_app.py) so the
existing banking demo remains unchanged.

## What This Demo Proves

- Agent Control is installed and running as a remote service in the Galileo cluster.
- `GalileoLogger` auto-registers the Agent Control sink/provider when the Agent Control SDK is importable.
- Real `@control()` decorator calls emit `ControlExecutionEvent`s for server-side controls.
- Evaluation uses the runtime JWT flow bound to the target Galileo log stream.
- The remote `galileo.luna` evaluator invokes Galileo's `/scorers/invoke` API directly.
- The active Galileo workflow provides trace/span context to Agent Control through the current `GalileoLogger` bridge.
- Those events flow through Agent Control's registered sink path into the Galileo bridge.
- The bridge converts them into Galileo `ControlSpan`s during the same agent run.
- The stored control spans can be queried back through the trends dashboard and custom metrics APIs.

## Local Python Environment

The demo does not require local `agent-control`, `orbit`, or `galileo-python` checkouts. Agent Control runs as a remote service, Orbit is already part of the stack, and this package installs `galileo[openai]` plus the Agent Control SDK/evaluators from PyPI.

```bash
cd ~/code/agent-control-galileo-e2e
python3.12 -m venv .venv
export DEMO_PYTHON="$PWD/.venv/bin/python"
"$DEMO_PYTHON" -m pip install --upgrade pip
"$DEMO_PYTHON" -m pip install .
```

## Pre-requisites:

Recommended env vars:

```bash
export GALILEO_CONSOLE_URL='https://console-test-evals.gcp-dev.galileo.ai'
export GALILEO_API_URL='https://api-test-evals.gcp-dev.galileo.ai'
export GALILEO_API_KEY='<your-api-key>'
export GALILEO_PROJECT='test-evals-project'
export GALILEO_LOG_STREAM='test-evals-logstream'
export AGENT_CONTROL_URL='https://agent-control-test-evals.gcp-dev.galileo.ai'
export AGENT_CONTROL_AGENT_NAME='galileo-control-span-demo'
export AGENT_CONTROL_TARGET_TYPE='log_stream'
export AGENT_CONTROL_RUNTIME_AUTH_MODE='jwt'
```

For Agent Control Enterprise, use the Galileo API key with the `Galileo-API-Key` header. If `AGENT_CONTROL_API_KEY` is also set, keep it equal to `GALILEO_API_KEY`; stale OSS keys can cause `401 Unauthorized` during Agent Control init.

## 1. Banking Transfer Controls

The demo expects the composite controls to be created in Console and bound to the Galileo log stream. Use [`controls_for_ui.json`](controls_for_ui.json) as the raw field reference when creating them manually in the UI. Pass `--setup-controls` only as a less-preferred fallback when UI setup is blocked.

- `demo-steer-large-transfer-2fa`: uses `AND` plus nested `NOT` to steer transfers of `$10,000` or more that have not completed 2FA, returning retry flags that set `verified_2fa=true`.
- `demo-deny-risky-transfer-composite`: uses `OR` to deny transfers when either the destination is sanctioned or the fraud score is at least `0.8`.

The optional `demo-observe-luna-transfer-request` control denies prompt injection
with Galileo Luna. Create it through Console so the scorer selector stores the
actual scorer ID. Scripted setup includes it only when
`GALILEO_LUNA_SCORER_ID` is set to that ID; a scorer label is not a valid
substitute in Agent Control 8.x.

For the response-side Luna compatibility scenario, also set
`GALILEO_OUTPUT_TOXICITY_LUNA_SCORER_ID` to the `output_toxicity_luna` scorer ID.
Running `setup_controls.py` then creates
`demo-deny-luna-toxic-transfer-response`, a post-LLM control that evaluates the
generated response. Set `AGENT_CONTROL_EXPECTED_VERSION` only when you want the
Streamlit app to reject a DevStack running a different Agent Control version.

Create the controls at the log stream level in Console. The app uses Agent Control's `@control()` decorator, so control matching sees the decorated function names as step names:

- LLM function: `draft_transfer_plan`
- Tool function: `process_wire_transfer`

Do the step-name filtering in the control UI, for example with regexes that match those function names. The demo does not pass `steps`, `llm_step_name`, or `tool_step_name` through `agent_control.init(...)`.

`run_demo.py` registers the demo agent with the resolved log stream target, then verifies the effective target controls through Agent Control before evaluating. It does not create or update controls unless `--setup-controls` is passed explicitly.

## 2. Run The End-To-End Demo

```bash
cd /Users/namrataghadi/code/agent-control-galileo-e2e
"$DEMO_PYTHON" run_demo.py \
  --verify-api \
  --query-trends
```

This uses `batch` mode by default and now performs the full flow:

- registers the demo agent against the resolved Galileo log stream target
- verifies the existing log-stream-bound controls with `GET /api/v1/agents/{agent}/controls`
- verifies Agent Control runtime JWT exchange for the Galileo log stream target
- verifies Galileo `/scorers/invoke` without sending `project_id` before the Agent Control Luna evaluator path uses it
- emits real control execution events through the Galileo logger bridge
- verifies the stored trace and control spans through the public APIs
- creates or fetches the trends dashboard for the target log stream
- runs multiple `/projects/{project_id}/metrics/custom_search` chart queries with different control-focused filters

Default demo inputs are chosen to trigger 2FA steering and then complete the transfer:

- prompt: `Wire $15,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-014.`
- parsed transfer: amount `$15,000`, recipient `Horizon Robotics`, destination `United Kingdom`
- first tool pre-check steers because 2FA is missing
- the demo applies the steering context, retries with `verified_2fa=true`, and then executes the transfer

This default path evaluates the `AND` and nested `NOT` nodes. To exercise the
`OR` control, run with a sanctioned destination or high fraud score:

```bash
"$DEMO_PYTHON" run_demo.py \
  --skip-luna-control \
  --destination-country Iran \
  --verify-api

"$DEMO_PYTHON" run_demo.py \
  --skip-luna-control \
  --fraud-score 0.95 \
  --verify-api
```

The runner prints:

- the control IDs attached to the demo agent
- whether the Galileo bridge auto-registered
- how many Agent Control sinks are registered
- the Agent Control server URL, runtime auth mode, and target binding
- runtime JWT exchange metadata, without printing the token
- `/scorers/invoke` response metadata, including `project_id=not-sent`
- the evaluation results returned by Agent Control
- the Galileo project, log stream, session, and trace IDs
- the Galileo trace ID
- the control spans attached during the run
- devstack verification results for:
  - `GET /current_user`
  - `GET /ingest/healthz`
  - `GET /projects/{project_id}/traces/{trace_id}`
  - `GET /projects/{project_id}/sessions/{session_id}`
  - `POST /projects/{project_id}/spans/partial_search`
  - `GET /projects/{project_id}/log_streams/{log_stream_id}/trends`
  - `POST /projects/{project_id}/metrics/custom_search` for several control-oriented chart slices

### OTLP-only variant

`run_demo_otel.py` leaves the Galileo Logger demo unchanged and sends both sides of
the trace over OTLP/HTTP:

- application workflow, LLM, and tool spans use the standard OTLP/HTTP exporter
- Agent Control execution events use the built-in `otel` sink
- both exporters target the exact `AGENT_CONTROL_OTEL_ENDPOINT` value
- Agent Control receives the application trace ID and workflow span ID, so
  control spans are workflow children just like the Galileo Logger bridge
- the Agent Control exporter supplies the Galileo API key, project, and log-stream
  routing headers without printing them

Install the OTEL extras, load the local environment, and run the full readback check:

```bash
cd /Users/namratag/code/ace-demo
./.venv/bin/python -m pip install -e .
set -a
source .env
set +a

./.venv/bin/python run_demo_otel.py \
  --verify-api \
  --query-trends \
  --verify-delay-seconds 10
```

The `agents` namespace currently has no public HTTPRoute for its
`agent-control` service. Until one is added, start a port-forward in a second
terminal:

```bash
kubectl port-forward -n agents svc/agent-control 18000:8000
```

Then set the override before Python starts because the decorators read the URL
at import time:

```bash
set -a
source .env
set +a
AGENT_CONTROL_URL=http://127.0.0.1:18000 \
  ./.venv/bin/python run_demo_otel.py \
    --verify-api \
    --query-trends \
    --verify-delay-seconds 10
```

The required OTLP settings are:

```bash
export AGENT_CONTROL_OTEL_ENABLED=true
export AGENT_CONTROL_OTEL_ENDPOINT="$GALILEO_API_URL/otel/v1/traces"
```

`AGENT_CONTROL_OTEL_HEADERS` is optional for this runner. If omitted, it is
constructed in memory from `GALILEO_API_KEY`, `GALILEO_PROJECT`, and
`GALILEO_LOG_STREAM`. The application exporter and Agent Control exporter are
both constructed from the same endpoint and headers so a run cannot silently
split its trace across collectors.

The `agents` stack inspected on June 26, 2026 runs
`ingest-service:v0.33.0`. That image stores the OTLP spans named
`agent_control.control_execution`, but classifies them as `workflow`, so they
do not appear as control spans in the UI. Deploy an ingest-service build that
contains Orbit commit `9ccc212c` (`feat: add agent control native support in
otel`) or a later release containing it. That change recognizes the
`agent_control.*` contract and maps `control_id`, `agent_name`, `check_stage`,
`applies_to`, evaluator fields, and the control result. The runner's
`--verify-api` check reports this condition explicitly rather than treating raw
workflow rows as a successful control-span test.

## 3. Optional Variants

Batch mode:

```bash
"$DEMO_PYTHON" run_demo.py \
  --mode batch
```

Distributed mode:

```bash
"$DEMO_PYTHON" run_demo.py \
  --mode distributed \
  --verify-api \
  --query-trends
```

Custom inputs:

```bash
"$DEMO_PYTHON" run_demo.py \
  --prompt "Wire $75,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-099." \
  --project my-project \
  --log-stream my-log-stream \
  --setup-controls \
  --verify-api \
  --query-trends
```

Force a hard deny:

```bash
"$DEMO_PYTHON" run_demo.py \
  --prompt "Wire $5,000 to Research Bureau in Iran for consulting." \
  --setup-controls
```

## 4. Interactive Streamlit App

### OTLP-only Streamlit variant

The OTEL variant leaves `banking_streamlit_app.py` unchanged and does not
initialize `GalileoLogger`:

```bash
cd /Users/namratag/code/ace-demo
set -a
source .env
set +a

./.venv/bin/python -m streamlit run banking_streamlit_app_otel.py
```

Open `http://localhost:8501`. The result view reports both OTEL control-event
spans found in trace readback and spans normalized to Galileo's typed `control`
record. If the first count is non-zero while the second is zero, the configured
ingest-service does not yet contain Orbit commit `9ccc212c`.

`banking_streamlit_app.py` is an interactive version of the same standalone banking demo. It does not use Strands or hooks. It uses the same `@control()` decorator path as `run_demo.py`, displays each evaluation stage, shows steering retries, and prints the Galileo trace metadata created by the run.

The app uses the same decorated functions as the CLI run, so keep the log-stream-level controls in Console aligned with `draft_transfer_plan` and `process_wire_transfer`.

Streamlit is installed by default when you run `$DEMO_PYTHON -m pip install .`.

Run the app:

```bash
cd /Users/namratag/code/ace-demo
export AGENT_CONTROL_SKIP_LUNA_CONTROL="false"
export GALILEO_LUNA_SCORER_ID="<prompt-injection-scorer-id>"
export GALILEO_OUTPUT_TOXICITY_LUNA_SCORER_ID="<output-toxicity-scorer-id>"

# Create/update and bind the optional Luna controls before opening the app.
"$DEMO_PYTHON" setup_controls.py \
  --target-type "$AGENT_CONTROL_TARGET_TYPE" \
  --target-id "$AGENT_CONTROL_TARGET_ID"
"$DEMO_PYTHON" -m streamlit run banking_streamlit_app.py
```

In the sidebar, enable **Exercise Luna controls**, then use **Run Luna
compatibility suite**. The app uses the deployed Agent Control version unless
`AGENT_CONTROL_EXPECTED_VERSION` is set explicitly. The suite fails closed if
a required scorer ID is absent, an expected decision does not occur, or the
resulting control telemetry cannot be read back.

The app includes repeatable scenarios with automatic expected-result and
control-telemetry checks for:

- Luna prompt-injection allow (query-side success)
- Luna prompt-injection denial
- Luna output-toxicity denial (response-side success and enforcement)
- `AND + NOT` 2FA steering
- `OR` sanctioned-country denial
- `OR` high-fraud-score denial
- no composite match

## Notes

- This demo treats control spans as independent Galileo spans attached under the active workflow for the run.
- The demo uses real Agent Control evaluation events. App code does not call `logger.add_control_span(...)` directly.
- The demo mirrors the earlier devstack retest flow by validating the public ingest health endpoint and fetching the persisted trace/span records back from the public API.
- Batch mode is verified on this stack. In my retest, distributed mode attached the spans locally but the public trace/span read APIs returned no spans at all for that run, which points to a separate streaming-ingest/readback issue.
