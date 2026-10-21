# Decision Models banking control example

This self-contained Streamlit example runs a realistic in-memory banking
workflow through an Agent Control `tool`/`pre` control. The control uses the
`galileo.decision_models` evaluator with a pinned Jev decision-model scorer.
Unsafe requests are denied before the transfer function runs; allowed requests
create only an in-memory sandbox transaction.

The application never creates, updates, or attaches controls. Configure the
control in Galileo first, then use this application to exercise it.

## What is in this folder

- `streamlit_app.py`: interactive UI, Agent Control initialization, and Galileo telemetry.
- `cases.py`: deterministic banking scenarios and the in-memory transfer function.
- `controls.json`: exact control fields to reproduce in the Galileo Controls UI.
- `.env.example`: all local configuration variables without credentials.
- `pyproject.toml` and `uv.lock`: isolated Python environment.
- `tests/test_demo.py`: focused behavior and configuration tests.

## Prerequisites

Before running the example, confirm all of the following:

1. Python 3.12 through 3.14 and [uv](https://docs.astral.sh/uv/) are installed.
2. You can access a Galileo DevStack and have a Galileo API key.
3. The DevStack has a project and log stream where the example can write traces.
4. Agent Control is deployed and reachable through the Console gateway.
5. The Agent Control deployment exposes `galileo.decision_models`. Its startup
   log should list that evaluator as available.
6. Agent Control has
   `GALILEO_FEATURE_FLAG_SCORER_INVOKE_RUNTIME=enabled` and can reach the Orbit
   scorer-invoke endpoint.
7. A compatible Jev decision-model scorer and scorer version exist in the same
   Galileo organization.
8. Until the Decision Models evaluator is published, keep a sibling checkout at
   `/Users/<you>/code/agent-control` on the branch containing that evaluator.
   The uv source paths assume `ace-demo` and `agent-control` share a parent
   directory. This local checkout supplies only Python packages; the application
   still calls the Agent Control service in the DevStack.

## 1. Create the Galileo target

In Galileo Console:

1. Open or create a project.
2. Open or create an Agent/Log Stream in that project.
3. Record the project name, project ID, log-stream name, and log-stream ID.
4. Create or locate the Jev decision-model scorer and select the exact version
   you want this control to invoke.
5. Record the scorer ID and scorer-version ID. Do not substitute the display
   label for either UUID.

## 2. Create the control in Galileo UI

Use `controls.json` as the field-by-field source of truth. Its scorer UUIDs are
placeholders and must be replaced with values from your environment.

In **Controls**, create an organization-level control with these values:

| UI field | Value |
|---|---|
| Name | `test-jev-banking-transfer-safety-deny-tool` |
| Enabled | Yes |
| Execution | Server |
| Step type / level | `tool` |
| Stage | `pre` |
| Step-name regex | `^banking_jev_submit_transfer$` |
| Selector path | `input` |
| Evaluator | `galileo.decision_models` |
| Scorer ID | Your Jev decision-model scorer ID |
| Scorer version ID | Your pinned scorer-version ID |
| Payload field | `input` |
| Operator | `gte` |
| Threshold | `0.5` |
| Timeout | `10000` ms |
| Action | `deny` |

The important distinction is that **Jev is the scorer/model type**, while
`galileo.decision_models` is the Agent Control evaluator adapter.

Save the control, then attach or clone-and-attach it to the log stream created
in the previous section. Confirm the attached clone remains enabled and retains
the same evaluator, scorer version, tool/pre scope, selector, and action. The
application queries effective controls for that log-stream binding and fails
its preflight check if these fields do not match.

If your UI offers raw JSON entry, copy the object under `definition` from
`controls.json` after replacing the two scorer placeholders. Otherwise, enter
the same values through the form.

## 3. Configure the local environment

From the repository root:

```bash
cd /Users/<you>/code/ace-demo
cp decision_model_example/.env.example decision_model_example/.env
```

Edit `decision_model_example/.env` and replace every placeholder. In particular:

- `GALILEO_API_KEY` is a real secret and must never be committed.
- `AGENT_CONTROL_URL` normally uses the Console proxy:
  `https://console-<stack>.gcp-dev.galileo.ai/api/agent-control`.
- `AGENT_CONTROL_TARGET_ID` must equal `GALILEO_LOG_STREAM_ID`.
- `DECISION_MODEL_CONTROL_NAME` must match the attached control name.
- `DECISION_MODEL_SCORER_ID` and `DECISION_MODEL_SCORER_VERSION_ID` must match
  the values configured on that control.

The application loads only `decision_model_example/.env`; it does not depend on
a repository-root `.env`. The committed `.gitignore` excludes the real `.env`.

Optional connectivity check:

```bash
set -a
source decision_model_example/.env
set +a
curl -sS -m 5 "$AGENT_CONTROL_URL/health"
```

Expected response:

```json
{"status":"healthy","version":"<deployed-version>"}
```

## 4. Install the isolated environment

```bash
cd /Users/<you>/code/ace-demo

UV_NO_CONFIG=1 uv sync \
  --project decision_model_example \
  --locked \
  --default-index https://pypi.org/simple
```

This creates `decision_model_example/.venv`. The project temporarily resolves
six Agent Control packages from the sibling local checkout so the unreleased
Decision Models evaluator is available. Once a published release contains the
feature, remove `[tool.uv.sources]`, pin that release, and regenerate `uv.lock`.

## 5. Run the application

Either run directly through uv:

```bash
UV_NO_CONFIG=1 uv run \
  --project decision_model_example \
  --locked \
  streamlit run decision_model_example/streamlit_app.py
```

Or activate the isolated virtual environment:

```bash
source decision_model_example/.venv/bin/activate
python -m streamlit run decision_model_example/streamlit_app.py
```

Choose a scenario and select **Run protected transfer**. The suspicious or
unverified examples should be denied before the transfer tool executes if the
decision-model score is at least `0.5`. The normal transfer should be allowed if
its score is below the threshold.

The Streamlit UI shows a concise explanation. Full request, response, evaluator,
and telemetry diagnostics are written to stderr in the terminal. Successful
runs also provide a link to the Galileo trace.

## 6. Run checks

```bash
UV_NO_CONFIG=1 uv run --project decision_model_example \
  python -m pytest -q decision_model_example/tests/test_demo.py

UV_NO_CONFIG=1 uv run --project decision_model_example \
  ruff check decision_model_example
```

## Troubleshooting

- **Evaluator not found:** the Agent Control image does not include
  `galileo.decision_models`, even if the scorer-invoke feature flag is enabled.
- **Internal evaluator error:** inspect Agent Control logs for the concrete
  scorer-invoke error and confirm the scorer/version IDs exist in the same
  organization.
- **Runtime-token exchange 404:** verify `AGENT_CONTROL_URL` points to the
  correct stack's Console proxy and restart Streamlit after changing `.env`.
- **No matching control:** confirm the control is attached to the exact
  `AGENT_CONTROL_TARGET_ID`, enabled, and uses the anchored step-name regex.
- **Preflight scorer mismatch:** make the two `DECISION_MODEL_SCORER_*` values in
  `.env` match the attached control exactly.
- **No control span:** confirm Galileo Agent Control telemetry is enabled and
  that application traces and controls target the same log-stream ID.
