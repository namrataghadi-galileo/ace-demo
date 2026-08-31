# Troubleshooting

The errors below are the ones a first-time runner actually hits. The server's error text is
often misleading (a `404 Invalid API Key` usually means "wrong org," not "bad key"), so match
on the symptom, not the title.

## Setup or e2e fails at bind / exchange with 404 "Invalid API Key" / "Resource not found in the requested namespace"

Symptom: the control is created fine (you get a `control_id`), but `clone-and-bind`,
`PUT /control-bindings`, or `runtime-token-exchange` returns:

```
{"status":404,"error_code":"AUTH_INVALID_KEY","reason":"NotFound",
 "hint":"Verify the resource exists in the requested namespace."}
```

Cause: your three tokens are not all in the same org. The control is created under the org of
`AC_SF_TOKEN`, but the project and agent stream are created under the org of
`SPLUNK_AO_O11Y_API_TOKEN`. If those two orgs differ, the SF-token org cannot see the stream,
so binding and token-exchange 404 even though every individual call is authenticated.

Fix: get all three tokens (SF, ingest, API) from the SAME org in the AO UI, and make sure that
org has agent-control membership / is provisioned. If you have access to more than one org,
pick the one that already shows the Agent Observability nav with Controls enabled.

Known-good default: realm `lab0`, org `HHLQ5TxAIAA`. This is the org the end-to-end flow was
verified in (2026-08-25). Membership can change, so confirm it is still current rather than
assuming it. Another org (`G_yXEGYAIAI`) was tried and did not work because it lacked
membership; gateway auth is pre-provisioned only.

Confirm your token's org matches your stream, in two calls:

```
   # 1. SF token can reach the management API (auth works at all):
   curl -sk -H "X-SF-Token: <sf-token>" \
     https://app.lab0.signalfx.com/ao/agent-control/api/v1/controls
   # -> 200 with a controls list

   # 2. SF token can see a stream you just created (org matches):
   curl -sk -X POST -H "X-SF-Token: <sf-token>" -H "Content-Type: application/json" \
     --data '{"target_type":"log_stream","target_id":"<stream-id>"}' \
     https://app.lab0.signalfx.com/ao/agent-control/api/v1/auth/runtime-token-exchange
   # -> 200 with a runtime_token
```

If call 1 is 200 but call 2 is 404, your SF-token org and your stream's org do not match. Fix
the tokens, do not touch the stream.

## Control check fails with "All connection attempts failed" (localhost:8000)

Cause: the agent_control SDK read `AGENT_CONTROL_URL` at import time and it was not set, so the
evaluation client defaulted to `localhost:8000`.

Fix: set `AC_GATEWAY` (or `AGENT_CONTROL_URL`) before running. The e2e script sets the
`AGENT_CONTROL_*` env before it imports agent_control, so this only bites if you import the SDK
yourself earlier, or set the URL after import.

## Gateway returns 401 on health or any call

Cause: the SF token is expired or invalid. Session/SF tokens are short-lived.

Fix: get a fresh SF token from the AO UI (Settings, Access Tokens) and re-export `AC_SF_TOKEN`.
Confirm with: `curl -sk -H "X-SF-Token: <token>" https://app.lab0.signalfx.com/ao/agent-control/health`
should return 200 `{status:healthy, version:8.5.0}`.

## Gateway returns 403 AUTH_INSUFFICIENT_PRIVILEGES

Cause: the SF token authenticates but lacks the capability for that call (for example a
read-only token doing `PUT /controls`).

Fix: use a token with management privileges for setup (it does control create/bind). A
read-only token is fine for GET /controls but cannot create or bind.

## The UI shows "No scorers are currently available" when creating a control

Cause: you picked the `luna` evaluator. Luna / SLM is not provisioned on lab0 and is not
supported for the alpha release, so the scorer dropdown comes back empty and the control will
not save.

Fix: this is expected on lab0, not a setup problem. The setup script uses a `regex` evaluator
instead, which does not need a scorer. For a UI-created control, choose an evaluator that does
not require an SLM backend. Tracked in HYBIM-1006 (GA evaluator set) and HYBIM-1008 (regex
support at GA).

## Feature flag agent_control is off (Controls not visible in the AO UI)

Cause: the flag is resolved per cluster customer_name (`o11y-lab0`), and neither the o11y-lab0
block nor defaults set it, so it defaults to disabled.

Fix: see docs/02. Enforcement still runs with the flag off, but controls will not appear in the
UI until it is on.

## The session shows "Traces 0" (control fired but nothing in the UI trace)

Cause: one of the four wiring details in docs/06 is missing (wrong sink, llm step instead of
tool, missing trace-context provider, or no shutdown_observability flush).

Fix: work through docs/06. The e2e script already does all four; this only bites if you adapt
the script.

## Cannot reach the gateway at all (timeout / connection refused)

Cause: not on the right VPN.

Fix: connect GlobalProtect US West Full Tunnel plus Aviatrix, then retry the health check.
