# Diagrams

Four views of the same system: what is deployed, why the auth works, how the two SDKs relate,
and the exact run you perform.

## 1. What is deployed on lab0 (the setup)

```
  lab0 cluster   (customer_name = o11y-lab0)

   o11y-shared ns                     o11y-ao ns
   +---------------------+     +--------------------------------+
   | O11y api-gateway    |     | agent-control pod  :8000       |
   | app.lab0.signalfx   |---->|  image v0.2.74 = server 8.5.0  |
   | authenticates       |     |  Option A wired:               |
   | X-SF-Token          |     |  RUNTIME_TOKEN_HEADER =        |
   +---------------------+     |   X-Agent-Control-Runtime-Token|
            |                  +--------------------------------+
            |                  +--------------------------------+
            +----------------->| api (galileo) deploy  2/2      |
                               |  serves /ao/api/configuration  |
                               |  feature_flags.agent_control   |
                               |   = true  (env override/#1870) |
                               +--------------------------------+

   OTLP ingest: ingest.lab0.observability.splunkcloud.com
   AO UI:       cui-ui-token-2.lab0.observability.splunkcloud.com

  Three preflight gates before any test counts:
    PF1 image v0.2.74       (health -> version 8.5.0)
    PF2 Option A wired      (RUNTIME_TOKEN_HEADER env + runtime secret on pod)
    PF3 gateway host + cred (X-SF-Token) + one org for all three tokens, with membership
        (known-good default: realm lab0, org HHLQ5TxAIAA; confirm membership is current)
```

## 2. The no-collision hot path (why Option A)

```
  Client / SDK
    X-SF-Token: <external credential>
    X-Agent-Control-Runtime-Token: <raw runtime JWT>
        |  POST /ao/agent-control/api/v1/evaluation
        v
  +-----------------------------------------------------------+
  |  O11y api-gateway                                         |
  |   1. authenticates X-SF-Token                            |
  |   2. OVERWRITES Authorization with its OWN identity JWT   |  <- the collision risk
  |   3. strips /ao/agent-control from the path              |
  |   4. passes X-Agent-Control-Runtime-Token through UNTOUCHED| <- Option A dodges it
  +-----------------------------------------------------------+
        |  /api/v1/evaluation
        |  Authorization: <gateway identity JWT>             (gateway-owned)
        |  X-Agent-Control-Runtime-Token: <raw runtime JWT>  (survives)
        v
  +-----------------------------------------------------------+
  |  agent-control pod  (server 8.5.0)                        |
  |   verifies runtime token from the DEDICATED header        |
  |   ignores Authorization for runtime auth       => 200     |
  +-----------------------------------------------------------+

  Without the dedicated header the runtime token would sit on Authorization, the gateway
  would overwrite it at step 2, and the eval would 401. That negative case is the proof.
```

## 3. The two SDKs and where each test group hits

```
  Your agent app (Python)  installs TWO packages

    agent_control SDK  (enforcement: @control, exchange, evaluate, steer)
        |   Group F  = agent_control SDK e2e through gateway
        |   Group G  = AO/telemetry SDK injects runtime token   (blocked, HYBIM-947)
        v
    +--------------+        +-----------------------+
    | O11y gateway |------->| agent-control pod     |  A routing, B exchange,
    +--------------+        |  (evaluate / steer)   |  C eval, D authz, E boundary
        |                   +-----------------------+
        |  splunk-ao SDK (telemetry only: OTLP spans)
        v
    +--------------+        +-----------------------+
    | OTLP ingest  |------->|  AO UI trace tree      |  control fires + renders
    +--------------+        |  control span=Triggered|  (regex steer control)
                            +-----------------------+

  Enforcement lives in agent_control only. splunk-ao is telemetry only.
  @control will NOT be added to splunk-ao (confirmed 2026-08-25). Install both, use together.
```

## 4. The run flow you execute

```
  Step 0  pip install "agent-control-sdk==8.5.0" "splunk-ao"      (two packages)
  Step 1  set env: SF token, ingest token, api token, realm=lab0  (all three tokens: same org)
  Step 2  python agent_control_lab0_setup.py
             |- splunk-ao: create Project -> create agent stream
             |- agent-control REST: PUT /controls (regex steer)
             |                      POST /controls/<id>/clone-and-bind -> stream
             prints: export AC_PROJECT_ID=...  AC_STREAM_ID=...
  Step 3  python agent_control_lab0_e2e.py
             wire_transfer(amount=75000)
                |- @control intercepts (pre, tool step)
                |- exchange -> runtime token
                |- evaluation: regex [1-9][0-9]{4,} matches -> action=steer
                |- tool body never runs (ControlSteerError)
                |- splunk-ao bridge -> span shares trace id -> OTLP 202
             expect: STEER FIRED + INGESTED trace_id=...
  Step 4  AO UI Tracing (Last 15 min) -> newest session
             session
               |- control node
                  |- <control>-clone   [Triggered]
                  |- execute_tool wire_transfer

  Both scripts run preflight first: a missing token/env fails early with a clear message and a
  docs pointer, instead of breaking mid-run. If bind or exchange 404s, see docs/08 (org match).
```
