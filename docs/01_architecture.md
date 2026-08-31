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

---
