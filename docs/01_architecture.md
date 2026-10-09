# How it fits together (component map)

```
   Your agent app (Python)
        |
        |  uses TWO separate SDKs (installed as two packages)
        |
   +----+-----------------------------+
   |                                  |
   v                                  v
 agent_control SDK                splunk-ao SDK (SAO SDK)
 (enforcement)                    (telemetry / logging)
 - @control decorator             - sends spans over OTLP
 - runtime-token exchange         - built on galileo-core
 - evaluate / steer / deny        - the ONLY supported O11y Cloud telemetry SDK
        |                                  |
        |  both go through the O11y gateway |
        +----------------+-----------------+
                         |
                         v
        +-------------------------------------------+
        |  O11y API Gateway  (app.<realm>.signalfx) |
        |  - authenticates X-SF-Token               |
        |  - puts its own identity JWT on           |
        |    Authorization                          |
        |  - strips /ao/agent-control and /ao/api   |
        |  - passes X-Agent-Control-Runtime-Token   |
        |    through untouched                      |
        +-------------------------------------------+
              |                          |
              v                          v
   Agent Control server          Galileo api service
   (o11y-ao namespace)           (evaluates flags, CRUD,
   image v0.2.74 = 8.5.0         serves /ao/api/configuration)
   - reads runtime token from
     X-Agent-Control-Runtime-Token
   - runs the control, returns
     steer / deny / allow
              |
              v
   OTLP ingest (ingest.<realm>.observability.splunkcloud.com)
   spans land here and show in the AO UI Tracing tab
```

Key idea: the gateway owns the `Authorization` header for its identity JWT, so the
Agent Control runtime token rides a separate header, `X-Agent-Control-Runtime-Token`. The two
never collide.

Which SDK where:

```
  O11y Cloud (SaaS, lab0/rc0):  splunk-ao SDK only   (galileo SDK NOT supported)
  OnPrem:                       galileo SDK (to be discontinued) + splunk-ao
  agent_control SDK:            separate, enforcement, works via the gateway in both
```

The agent_control SDK is always local, embedded in your process. What changes
between OnPrem, Galileo cloud, and the O11y embed is only where the Agent Control
server (ACS) lives and which credential you present to the gateway.

For how the SDK and server behave at runtime (evaluation, failure modes, cache
refresh, the span-to-chart rollup, framework integration, multi-tenancy, and
default timeouts), see docs/10_runtime_behavior.md.

---

## Inside the Galileo cluster (OnPrem component map)

This is the OnPrem deployment: the whole Galileo stack runs in one cluster and
the agent's SDK calls in from outside. In the O11y embed the same components run
in the `o11y-ao` namespace and the O11y gateway fronts them (see the top of this
file); the internal wiring below is the same.

Numbers on the arrows are explained under the diagram.

```
   Agent (outside the cluster)
   +-----------+
   |  SDK      |
   +-----+-----+
     |  (1) GalileoAPIKey / X-SF-Token        ^  (2) runtime JWT
     v  POST /auth/runtime-token-exchange     |  (short-lived, target-bound)
   ==|=========================================|==============  Galileo Cluster ==
     v                                         |
   +-------------------------------+   (3)   +-----------------+
   |  ACS (Agent Control Server)   |-------->|  Postgres DB    |
   |  - mints runtime JWT          |  read/  |  controls,      |
   |  - evaluates the control set  |  write  |  bindings,      |
   |  - execution=sdk local /      |         |  agents         |
   |    execution=server here      |         +-----------------+
   +--+--------+-------+-----------+--+  (8) +-----------+ (8) +--------+ (8) +--------+
      |        ^       |           ^  |----->| Runners-  |---->| Redis  |---->| Wizard |
   (4)|     (5)|    (8)|        (7)|  |      |   API     |<----| (pod   |<----| (GPU   |
      v        |       v           |  |      +-----------+ pick+--------+ run +--------+
   +--------+  |    +-------+    +--+--+--+
   |  API   |--+    | Redis |    | Authz  |
   |        |  (6)  | cache+|    |        |
   |  CRUD, | flags | event |    | RBAC:  |
   |  /ao/  | config| queue |    | admin  |
   |  api   |       +-------+    | vs     |
   +---+----+                    | runtime|
       ^                         +---+----+
    (9)|                             ^
       |                          (7)|
   +---+----+   (10)            +----+---+
   | UI     |<------------------|Console |
   |        |                   |  UI    |
   +--------+                   +--------+
```

Critical arrows:

```
   (1) SDK -> ACS   auth with the Galileo API key (X-SF-Token in the O11y embed),
                    the ONLY credential the agent app holds. On first init ACS
                    validates it against the API service (which calls Authz), then
                    mints a JWT. In the O11y embed the token must carry API scope
                    for the gateway; a separate INGEST token handles span export
                    (docs/04_tokens_and_env.md).

   (2) ACS -> SDK   returns a runtime JWT (scope runtime.use, target-bound, short
                    lived). The SDK caches it and sends it back on
                    X-Agent-Control-Runtime-Token for later calls, never on
                    Authorization. ACS refreshes it on the configured interval.

   (3) ACS -> Postgres    control definitions, bindings, and agent registrations.
                    initAgent looks up the controls bound to the target
                    (log/agent stream) here; the 60s refresh re-reads them.
                    Postgres is Galileo's shared instance; agent-control keeps its
                    own database inside it.

   (4) API -> ACS / (5) ACS -> API   control CRUD and config. Creating or editing
                    a control goes through the API (or, on init, ACS validates the
                    API key via the API service). ACS consumes the definitions.

   (6) UI/Console -> API   admins manage controls and view results through the
                    API. Console UI is the OnPrem console; the O11y embed uses the
                    AO UI.

   (7) API -> Authz   authorization. Authz enforces who may do what: admins
                    mutate controls, runtime principals only fetch and evaluate
                    their assigned controls. (Per-agent / per-role API keys are a
                    requested feature, not yet built; verify before relying on the
                    admin-vs-runtime split.)

   (8) Luna path (execution=server): ACS -> Runners-API -> Redis -> Wizard.
                    ACS has a Luna client; it calls runners-api, which fetches the
                    scorer metadata from Postgres, checks Redis for an available
                    Wizard pod, forwards the request to that Wizard pod (scorer
                    runs on GPU), then applies the control's threshold/operator and
                    returns a match/no-match. ACS never sees the raw metric value;
                    the numeric comparison happens in runners-api. Only ACS/API
                    reach runners-api and Redis (O11y egress config). Redis is also
                    the AO cache (~5 min Controls-chart TTL) and the agent-control
                    control-event queue (RedisEventIngestor).

   (9) API <-> UI    the UI reads spans, controls, and chart data from the API.

   (10) Console UI -> UI   entry point to the AO/Agent Control views.
```

Component roles, one line each:

```
   ACS          the enforcement engine (agent control server): mints runtime
                tokens, runs the engine, aggregates control results into a final
                verdict. What the SDK talks to.
   API          app API: control CRUD, feature flags, /ao/api/configuration, span
                readback, the Controls-chart rollup query, and API-key validation.
   Authz        RBAC and tenant isolation (namespace/stream).
   Postgres     shared Galileo instance; agent-control's own DB holds controls,
                bindings, agents; scorer metadata lives in a separate DB.
   Runners-API  schedules/runs scorers; applies the Luna threshold and returns a
                decision (not the raw score) to ACS.
   Wizard       runs Luna/SLM scorers on GPU. Redis tracks available Wizard pods.
   Redis        Wizard pod selection + AO cache (~5 min Controls-chart TTL) +
                control-event queue.
   UI/Console   admin and viewing surface.
```

---
