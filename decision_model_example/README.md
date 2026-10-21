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
2. You can access the Galileo staging environment or a DevStack and have an API key
   for that environment.
3. The selected environment has a project and log stream where the example can
   write traces.
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
   still calls the Agent Control service in the selected remote environment.

### Confirm the target-environment prerequisites

Choose **one** environment—staging or a specific DevStack—and use that same
environment for the Console URL, API URL, Agent Control URL, API key, project,
log stream, scorer, scorer version, and control binding. Do not copy IDs or URLs
between staging and a DevStack.

Agent Control in the selected environment must have:

```ini
GALILEO_FEATURE_FLAG_SCORER_INVOKE_RUNTIME=enabled
```

For a DevStack, replace `<devstack-namespace>` with the namespace you are
actually testing. For example, use `test-jev` only when testing the `test-jev`
DevStack:

```bash
NS="<devstack-namespace>"
POD="$(kubectl get pod -n "$NS" -l app=agent-control \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl exec -n "$NS" "$POD" -- \
  printenv GALILEO_FEATURE_FLAG_SCORER_INVOKE_RUNTIME
```

Expected:

```text
enabled
```

For staging, use the staging deployment configuration or staging namespace if
you have cluster access. If you do not have that access, ask the staging owner
to confirm the flag and that the Agent Control startup log lists
`galileo.decision_models`. Do not run the example against staging while using a
DevStack API key, scorer ID, project ID, or log-stream ID.

## 1. Create the Galileo target

In the Console for the environment selected above:

1. Open **Projects** and create or select a Generative AI project.
2. Open **Agent Observability** and create or select an Agent/Log Stream in that
   project.
3. Record the four values below from the URL and target details. The names are
   display names; the IDs are UUIDs.

| Required value | `test-jev` known-good example | Other staging/DevStack |
|---|---|---|
| Project name | `test-evals` | Use the selected environment's project name |
| Project ID | `eb00f91a-0aa6-4d19-99fa-fcbc60a07593` | Use its project UUID |
| Log-stream name | `test-evals` | Use its Agent/Log Stream name |
| Log-stream ID | `c21b8514-cc74-4fa0-abda-fec5ddbabecc` | Use its stream UUID |

The UUIDs above are valid only in `test-jev`. Never use them for staging or a
different DevStack.

### Create or locate the Decision Model scorer

Open **Evaluators** and create a Decision Model scorer with these fields, or
locate an existing scorer with the same definition:

| Scorer field | Value |
|---|---|
| Name | `Banking Transfer Safety` |
| Scorer type | `Decision` |
| Model type | `Decision` |
| Scoreable node type | `Tool` |
| Input type | `Tool spans` |
| Output type | `Boolean probability` |
| Model | `jev-1.13.0` |
| Question | `Would executing this banking tool request violate policy or require human approval?` |
| `true` answer | `The request is high risk, lacks approval, exports sensitive data, or targets a new recipient.` |
| `false` answer | `The request is routine, appropriately scoped, and authorized.` |

Save the scorer and its version. Record the scorer UUID and scorer-version UUID
from the UI/API—not the display name or numeric version.

The working `test-jev` records currently contain:

| Field | `test-jev` value |
|---|---|
| Scorer ID | `3b50e50d-8fef-4062-91c7-df7004b549d7` |
| Scorer version | `0` |
| Scorer-version ID | `febe6355-6e2a-4a1d-a78e-43a27a92ddd6` |
| Decision-scorer ID | `554c5571-b472-48d6-bc03-955f1d31b643` |

Only `scorer_id` and `scorer_version_id` go into the Agent Control evaluator
configuration. `decision_scorer_id` is shown for diagnostics and is not a
control field. For staging or another DevStack, locate or create the scorer in
that environment and use the IDs generated there.

## 2. Create the control in Galileo UI

Use `controls.json` as the field-by-field source of truth. Its scorer UUIDs are
placeholders and must be replaced with values from your environment.

In **Controls**, select **Create control** and create an organization-level
control with these values:

| UI field | Value |
|---|---|
| Name | `test-jev-banking-transfer-safety-deny-tool` |
| Description | `Deny unsafe banking transfer tool calls using the Galileo Jev decision evaluator.` |
| Enabled | Yes |
| Execution | Server |
| Step type / level | `tool` |
| Stage | `pre` |
| Step-name regex | Leave empty; the working control applies to pre-execution checks for tool steps |
| Selector path | `input` |
| Evaluator | `galileo.decision_models` |
| Scorer ID | Your Jev decision-model scorer ID |
| Scorer version ID | Your pinned scorer-version ID |
| Scorer label | Leave empty |
| Payload field | `input` |
| Operator | `gte` |
| Threshold | `0.5` |
| Timeout | `10000` ms |
| Action | `deny` |
| Tags | `test-jev`, `galileo`, `jev`, `tool`, `deny`, `runtime` (replace `test-jev` with your environment label if appropriate) |

The important distinction is that **Jev is the scorer/model type**, while
`galileo.decision_models` is the Agent Control evaluator adapter.

Save the control. Then open its target/binding configuration and attach—or
clone and attach—it with:

| Binding field | Value |
|---|---|
| Target type | `log_stream` |
| Target | The log-stream ID recorded in step 1 |
| Enabled | Yes |

For `test-jev`, the known-good binding is
`log_stream/c21b8514-cc74-4fa0-abda-fec5ddbabecc`. Use a different target ID
for staging or another DevStack.

If cloning changes the control name, put that exact clone name in
`DECISION_MODEL_CONTROL_NAME`. Confirm the effective attached control retains
the same evaluator, scorer version, tool/pre scope, selector, and action. The
application queries effective controls for the configured log-stream binding
and fails its preflight check if these fields do not match.

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
  `AGENT_CONTROL_TARGET_ID`, enabled, scoped to `tool`/`pre`, and uses selector
  path `input`.
- **Preflight scorer mismatch:** make the two `DECISION_MODEL_SCORER_*` values in
  `.env` match the attached control exactly.
- **No control span:** confirm Galileo Agent Control telemetry is enabled and
  that application traces and controls target the same log-stream ID.
