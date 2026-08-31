# Run the end-to-end test

```
   python agent_control_lab0_e2e.py

   1. app calls the @control-decorated tool  wire_transfer(amount=75000)
        |
   2. @control intercepts BEFORE running the tool, and asks the server:
        POST /ao/agent-control/api/v1/auth/runtime-token-exchange   -> 200 (runtime JWT)
        POST /ao/agent-control/api/v1/evaluation                    -> 200
        |
   3. server evaluates the bound control:
        regex [1-9][0-9]{4,} matches "amount": 75000   -> matched=true, action=steer
        |
   4. @control blocks execution:
        raises ControlSteerError  ("Pattern found", 2FA steer)
        the tool body never runs
        |
   5. the decision is recorded as a telemetry span:
        splunk-ao bridge -> add_control_span -> shares the trace id ->
        POST /ao/agent-control/api/v1/observability/events  -> 202
        span ingested via OTLP
        |
   6. AO UI Tracing tab (Last 15 minutes), open the newest session:

        session
          └─ control node
             ├─ 2fa-steer-clone (control span)   [Triggered]
             ├─ 2fa-steer-clone (control span)   [Triggered]
             └─ execute_tool wire_transfer (tool span)

        control span detail: Controls 1 / Triggered
        output: { "action": "steer", "matched": true, "confidence": 1 }
```

The control span can appear twice: once for the pre-execution evaluation and once when the
steer decision is recorded. Both are tagged Triggered and belong to the same control. One
matched control, shown at two points in its lifecycle, is expected.

Positive vs negative check:

```
   AC_AMOUNT=75000   ->  matches [1-9][0-9]{4,}  ->  STEER fires, tool blocked
   AC_AMOUNT=5       ->  no match                ->  tool runs normally, is_safe=true
```

---
