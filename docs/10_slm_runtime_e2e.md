# Preset SLM direct-runtime and record-compatibility E2E

This runbook tests the Orbit `scorer_invoke_runtime` path from a Galileo
scorer-invoke request and from Agent Control runtime evaluation. It covers the
nine LLM-shaped preset SLMs, the LLM-shaped `tool_selection_quality_luna`
scorer, and the tool-shaped `tool_error_rate_luna` scorer.

The test deliberately stops when the runtime feature flag is not enabled. A
successful response from `/scorers/invoke` alone is not enough to prove which
internal route ran; use the feature flag, request shape, and Runners logs
together.

## 1. Deploy the two changes

Use a devstack whose Runners deployment contains the Orbit change from
`feature/SAO-16056-expand-scorers-invoke-api` (the change is also available in
Orbit PR #1720 after merge) and whose Agent Control service contains the
corresponding branch. Do not infer that the right code is deployed from a
successful pod rollout; record the image tags or commit SHAs first.

The original direct-runtime suite expects these preset names exactly:

```text
input_pii
input_sexist_luna
input_tone
input_toxicity_luna
output_pii
output_sexist_luna
output_tone
output_toxicity_luna
prompt_injection_luna
```

The compatibility suite additionally exercises this mapping:

| Scorer | Record type | Required shape |
| --- | --- | --- |
| `tool_selection_quality_luna` | `llm` | LLM record with `tools` and `tool_calls` |
| `tool_error_rate_luna` | `tool` | Tool input/output |

These are explicit E2E cases, not a production capability registry. The
`runtime_record_types` metadata design is intentionally deferred pending the
Metrics team discussion. Do not substitute customer-created scorer IDs.

Before running the app, verify the Console configuration endpoint reports the
runtime flag as enabled:

```bash
curl -sS "$GALILEO_CONSOLE_URL/api/config" | jq '.. | objects | with_entries(select(.key == "scorer_invoke_runtime"))'
```

If this prints nothing, inspect the complete JSON for the feature-flag object.
The Streamlit app accepts common shapes such as `featureFlags` or
`feature_flags`, but it requires the value to be `true`, `enabled`, or `on`.

## 2. Configure the test

Use the preset scorer UUID shown by Console for each of the nine names plus the
two compatibility scorers. The
IDs are installation-specific and must not be committed. Start with the
example file:

```bash
cd /Users/namratag/code/ace-demo
cp slm_runtime_scorers.example.json slm_runtime_scorers.json

export GALILEO_API_KEY="<your-api-key>"
export GALILEO_CONSOLE_URL="https://console-<stack>.gcp-dev.galileo.ai"
export GALILEO_API_URL="https://api-<stack>.gcp-dev.galileo.ai"
export GALILEO_PROJECT="slm-runtime-e2e-project"
export GALILEO_LOG_STREAM="slm-runtime-e2e-logstream"
export AGENT_CONTROL_URL="https://agent-control-<stack>.gcp-dev.galileo.ai"
export AGENT_CONTROL_AGENT_NAME="galileo-slm-runtime-e2e"
export AGENT_CONTROL_TARGET_TYPE="log_stream"
export GALILEO_SLM_SCORER_IDS_FILE="$PWD/slm_runtime_scorers.json"
```

Replace every placeholder in `slm_runtime_scorers.json` with the corresponding
preset UUID, including `tool_selection_quality_luna` and
`tool_error_rate_luna`. Never use the existing fine-tuned Luna ID for these
cases.

Resolve the target log-stream ID before binding controls. The existing demo can
print or resolve it, or it can be copied from Console. Then set:

```bash
export AGENT_CONTROL_TARGET_ID="<log-stream-uuid>"
```

## 3. Create the Agent Control controls

```bash
"$PWD/.venv/bin/python" slm_runtime_controls.py \
  --agent-name "$AGENT_CONTROL_AGENT_NAME" \
  --server-url "$AGENT_CONTROL_URL" \
  --target-type "$AGENT_CONTROL_TARGET_TYPE" \
  --target-id "$AGENT_CONTROL_TARGET_ID" \
  --scorer-ids-file "$GALILEO_SLM_SCORER_IDS_FILE"
```

This creates or updates nine original LLM `deny` controls, one LLM
tool-selection `deny` control, one compatible tool `deny` control, and one
deliberately incompatible tool `deny` control, in addition to the existing
blocking and regex examples. The existing regex examples retain their original
actions: deny, steer, and deny. The newly added SLM controls use:

- `evaluator.name = galileo.luna`
- the new `scorer_id` and `scorer_label` fields
- the correct `input`/`output` selector and `pre`/`post` stage
- `record.type = llm` for the original suite and tool-selection scorer
- `record.type = tool` for `tool_error_rate_luna`
- `operator = any`, with `deny` actions so each individual probe demonstrates
  enforcement for a matching scorer result

Because these controls deny, run one selected scorer at a time when validating
the decorated path. The intentionally harmful samples are expected to raise a
`ControlViolationError`; the Streamlit app catches and reports that result.

## 4. Run the new Streamlit app

```bash
"$PWD/.venv/bin/python" -m streamlit run slm_runtime_streamlit_app.py
```

Use **Run selected scorer** to exercise one scorer at a time. Every original
LLM scorer has a distinct decorated function name and matching step-name regex,
so the other SLM controls do not run for that case. Use **Run all 9 scorer
suite** for the original LLM cases, or **Run record compatibility suite** for
the new cases. The compatibility suite verifies:

1. Console `scorer_invoke_runtime` is enabled.
2. Agent Control `/health` is reachable.
3. The installed Agent Control packages match `8.7.0` by default.
4. `tool_selection_quality_luna` succeeds with an LLM record containing tool
   definitions and calls.
5. `tool_error_rate_luna` succeeds with `record.type = tool`.
6. `input_pii` rejects a deliberately mismatched tool record rather than
   silently returning a score.
7. Agent Control runs decorated tool steps; the positive and incompatible cases
   are expected to be denied when their controls match or the evaluator rejects
   the record, with Galileo span readback checking telemetry.

`tool_error_rate_luna` also runs through a decorated tool function. The
`tool_selection_quality_luna` direct API probe includes `tools` and
`tool_calls`; the current Agent Control decorator payload does not expose
available tools, so the single-scorer UI intentionally does not claim an
Agent Control decorator result for that scorer.

For a package branch with another compatible Agent Control version, set the
expected version before starting Streamlit:

```bash
export AGENT_CONTROL_EXPECTED_VERSION="<branch-version>"
```

The app displays the direct response, score, status, trace ID, local control
span count, and persisted control span count. It reports missing scorer labels
instead of treating a partial result as success.

## 5. Direct Runners API smoke test

Before running the full app, one scorer can be tested directly. Use an exact
allowlisted preset name and its matching UUID:

```bash
export SCORER_ID="<input-pii-preset-uuid>"
curl -sS -X POST "$GALILEO_API_URL/scorers/invoke" \
  -H "Content-Type: application/json" \
  -H "Galileo-API-Key: $GALILEO_API_KEY" \
  --data-binary @- <<EOF | jq .
{
  "scorer_id": "$SCORER_ID",
  "scorer_label": "input_pii",
  "inputs": {
    "query": "Please update account 4412 using SSN 123-45-6789 and alice@example.com.",
    "response": "The transfer policy requires verification before completion."
  },
  "record": {
    "type": "llm",
    "name": "slm_runtime_curl_probe",
    "input": "Please update account 4412 using SSN 123-45-6789 and alice@example.com.",
    "output": "The transfer policy requires verification before completion.",
    "context": {
      "slm_runtime_probe_id": "curl-canary",
      "expected_path": "runtime"
    }
  },
  "config": {"request_timeout_seconds": 25}
}
EOF
```

The expected response has `status: success`, a non-null score, and
`scorer_label: input_pii`. This verifies request/response compatibility, but
the feature flag plus runner logs are still needed to prove the selected
execution path.

## 6. Confirm the selected route

The public response intentionally does not expose a route marker. Use all of
the following evidence:

1. The app preflight shows `scorer_invoke_runtime = true`.
2. Every request uses a preset SLM UUID and the record type shown in the
   compatibility table.
3. The Runners pod logs for the test window show the runtime scorer path. For
   example:

```bash
kubectl get pods -n "$NS" | rg -i 'runner'
kubectl logs -n "$NS" <runners-pod> --since=15m \
  | rg -i 'scorer_invoke|runtime_scorer|protect|slm_runtime'
```

For an unambiguous test, use a dedicated test stack or an otherwise quiet
window and capture the Runners image digest plus logs before and after the
Streamlit run. If the negative mismatch is accepted as a successful score,
treat the deployment as incompatible and investigate it.

## 7. Expected result

The passing suite has:

- nine original direct API rows plus two compatible-record rows with
  `status = success`
- the negative `input_pii` tool-record case is rejected
- no entries in `direct_missing`
- at least one persisted `control` record for the Agent Control trace
- the positive Agent Control tool case has `record_type = tool`
- no fallback indication in the Runners logs

For local static checks:

```bash
python3 -m compileall -q slm_runtime_controls.py slm_runtime_streamlit_app.py
python3 -m pytest -q tests/test_slm_runtime_demo.py
```

The repository test environment may need `pytest` installed separately; the
application itself only requires the dependencies in `pyproject.toml`.
