# Scope and open items

## Version notes

- Agent Control server 8.5.0 (image tag v0.2.74) contains the configurable runtime-token
  header. rc0 must be bumped to v0.2.74+ before these tests mean anything there.
- O11y Cloud supports splunk-ao (Python) only. The plain galileo SDK is not supported
  (a `/ao/api` 404 from the galileo SDK is expected, not a bug).
- The gateway strips the `/ao/agent-control` and `/ao/api` path prefixes; clients send the
  full path and the gateway forwards the rest to the service.

---

The control examples in this doc (a Luna prompt-injection control and a regex 2FA-steer
control) are for verifying the flow. They are not a statement of what ships at GA. A couple
of things are still being confirmed with product:

- Supported evaluators shrink at GA. The Controls UI dropdown currently lists roughly 10 to
  20 SLM evaluators, but GA will support only a smaller SLM-based set (around five, to be
  confirmed). Luna / SLM is not supported for the alpha release (target Sept 4). Tracked in
  HYBIM-1006 (clean up the dropdown to the GA-supported set).
- Regex evaluator support is not confirmed. Whether regex-based evaluators are supported for
  Agent Control at GA is an open question. If they are, user-supplied regex must be validated
  (compile and reject invalid patterns before accepting). Tracked in HYBIM-1008. So the regex
  control used here may or may not be in the GA scope.

Enforcement vs telemetry, for clarity:

- agent_control SDK alone gives enforcement. @control runs evaluation and fires a steer
  through the gateway without splunk-ao installed, and agent-control-sdk does not depend on
  splunk-ao.
- splunk-ao is what makes the control execution show up as a span in the O11y UI trace. So
  agent_control alone gives enforcement, and agent_control plus splunk-ao gives enforcement
  plus the control appearing in the UI trace (what this script sets up).
