# Banking Jev pre-tool control demo

This interactive Streamlit app exercises an existing `galileo.decision_models`
control backed by a Jev decision-model scorer in the `test-jev` DevStack. It
does **not** create or update controls.

The protected workflow checks the account balance, validates the recipient,
and calls one `@control(step_type="tool")` transfer function. The complete bound
argument map becomes the tool `Step.input` evaluated at the `pre` stage. A deny
therefore prevents the sandbox transfer function from running.

The app starts a Galileo trace directly. It intentionally does not create a
session because session creation is unrelated to this tool-level control and
must not prevent the protected function from reaching Agent Control.

## Existing backend configuration

- Control: `test-jev-banking-transfer-safety-deny-tool` (`id=1`)
- Evaluator: `galileo.decision_models` (Jev scorer)
- Scorer: `3b50e50d-8fef-4062-91c7-df7004b549d7`
- Scorer version: `febe6355-6e2a-4a1d-a78e-43a27a92ddd6`
- Scope: `tool` / `pre`
- Selector: `input`
- Action: `deny`
- Binding: `log_stream` / `c21b8514-cc74-4fa0-abda-fec5ddbabecc`

The app reads and validates this binding before execution. It sends no control
creation or mutation requests.

## Local dependencies

The isolated `banking-jev` project uses the local
`/Users/namratag/code/agent-control` checkout so the unreleased Decision Models
evaluator is available. Once a published Agent Control release contains it,
remove the six local source overrides in `banking-jev/pyproject.toml`, pin that
release, and regenerate the lockfile.

## Run

```bash
cd /Users/namratag/code/ace-demo

export GALILEO_API_KEY="<your-api-key>"

UV_NO_CONFIG=1 uv sync \
  --project banking-jev \
  --default-index https://pypi.org/simple

UV_NO_CONFIG=1 uv run \
  --project banking-jev \
  --default-index https://pypi.org/simple \
  streamlit run banking_jev_streamlit_app.py
```

The application fixes the non-secret URLs, agent name, project ID, and log-stream
binding to `test-jev`. It deliberately ignores generic `GALILEO_*` and
`AGENT_CONTROL_*` target variables left over from other demos; only the API key
is inherited from the shell or `.env`.

The four presets cover a low-value verified transfer, a large transfer without
approval, a destination that failed sanctions screening, and missing
verification. Every field remains editable.

Full evaluator diagnostics are printed to stderr. The UI shows the request,
exact tool invocation, allow/deny decision, whether the transfer executed,
scorer metadata propagated by Agent Control, and a Galileo trace link.

## DevStack assumptions

- The control and log-stream binding above remain enabled.
- The Agent Control deployment has
  `GALILEO_FEATURE_FLAG_SCORER_INVOKE_RUNTIME=enabled`.
- The DevStack Agent Control image contains `galileo.decision_models` and can reach the
  scorer-invoke runtime.
- The API key is supplied through the environment; it is never stored here.

## Checks

```bash
UV_NO_CONFIG=1 uv run --project banking-jev python -m pytest -q \
  tests/test_banking_jev_demo.py

UV_NO_CONFIG=1 uv run --project banking-jev ruff check \
  banking_jev_cases.py banking_jev_streamlit_app.py \
  tests/test_banking_jev_demo.py
```
