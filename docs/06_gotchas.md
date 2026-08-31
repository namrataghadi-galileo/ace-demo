# The four wiring gotchas

Miss any one and the session shows Traces 0.

```
   1. observability_sink_name="registered"
      -> control events go to the splunk-ao bridge sink (add_control_span),
         not agent_control's own event store

   2. func.tool_name = "wire_transfer"  (set before @control())
      -> the step is a tool, not llm.  An llm step pulls in the Luna control,
         which errors when the Luna SLM backend is unavailable on lab0

   3. let setup_agent_control_bridge(logger) own the trace context
      -> the bridge installs its own trace-context provider that returns the logger's real
         (root parent id, current parent id). Do NOT override it with a manual
         set_trace_context_provider.
      -> why: the bridge accepts a control event only when the event's span_id equals the
         current parent's FULL UUID (splunk_ao bridge _matches_active_context runs both through
         uuid.UUID). A truncated 16-hex span_id normalizes to None, never matches, and the
         event is dropped (accepted=0, dropped=1). The full UUID matches and is accepted.
      -> tear down with logger.disable_agent_control() then
         agent_control.clear_trace_context_provider() after shutdown_observability().

   4. await agent_control.shutdown_observability()  before exit
      -> flushes the background event batcher; otherwise events drop on shutdown

   plus: do NOT use a named start_session; let splunk-ao own the session/trace
```

---
