# Agent Control on Splunk O11y Cloud (lab0): QE test guide

Read these in order. The numbers in the filenames are the reading order.

- 00_README.md (this file): overview and quick start.
- 01_architecture.md: how O11y, the gateway, and the two SDKs fit together.
- 02_feature_flag.md: turn on the agent_control feature flag and verify it.
- 03_setup_project_stream_control.md: create the project, agent stream, and a bound control.
- 04_tokens_and_env.md: the three tokens and the env vars to set.
- 05_run_e2e.md: run the end-to-end test and read the result in the UI.
- 06_gotchas.md: the four wiring details that make the control show up in the UI trace.
- 07_scope_and_open_items.md: GA scope, version notes, and open items.
- 08_troubleshooting.md: the errors a first-time runner hits, and what they actually mean.
- 09_diagrams.md: four diagrams (deployment, auth hot path, the two SDKs, the run flow).

Scripts (repo root):
- agent_control_lab0_setup.py: run once to create the project, stream, and control.
- agent_control_lab0_e2e.py: run to fire a control and see it in the AO UI trace.

## Quick start

1. Feature flag on (02).
2. pip install "agent-control-sdk==8.5.0" "splunk-ao" (04).
3. Get the three tokens, all from the SAME org with agent-control membership, and set env (04).
4. python agent_control_lab0_setup.py, then export the AC_PROJECT_ID and AC_STREAM_ID it prints (03).
5. python agent_control_lab0_e2e.py. Expect STEER FIRED and INGESTED trace_id=... (05).
6. Open the AO UI Tracing tab, Last 15 minutes, newest session. The trace tree shows the
   control fired and tagged Triggered (05).

If setup or the run fails with a 404 "Invalid API Key" at bind or exchange, your tokens are
in different orgs. See 08_troubleshooting.md.

## One thing to know up front

Agent Control enforcement (the @control decorator, evaluation, steer/deny) lives in the
agent_control SDK. splunk-ao is for telemetry only. To run the test you install both and use
them together. See 01 for why.
