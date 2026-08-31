# Set up project, agent stream, and control

## Easiest: run the setup script

Run this once (after the feature flag is on and env is set, see 02 and 04):

```
python agent_control_lab0_setup.py
```

It creates the project and agent stream with the splunk-ao SDK, then creates a steer control
and binds it to the stream via the Agent Control REST API. The control name is unique per run,
so you can run it again without a name conflict. At the end it prints the IDs to export:

```
export AC_PROJECT_ID=...
export AC_STREAM_ID=...
```

Export those, then go to 05 to run the e2e test.

Notes:
- Project and agent stream are created with the splunk-ao SDK (Project.create,
  project.create_agent_stream). splunk-ao has no control-management API, so the control is
  created and bound through the Agent Control REST API with the X-SF-Token.
- Controls bind to an agent stream ID, not a name, which is why the stream is created first.

## Manual alternative (AO UI)

If you prefer to set it up by hand, do this once per test org:

```
   Step 1: Open the AO UI
     https://<console-host>/#/agent-obs
     (lab0 example host: cui-ui-token-2.lab0.observability.splunkcloud.com)

   Step 2: Create / open a Project
     UI: Agent Observability -> Projects -> create or open
     Example: hybim871-ace-demo
       project id: f592350e-414d-4fef-9a1a-a359ebbda38a

   Step 3: Create / open an Agent Stream under that project
     UI: open the project -> Agent Streams -> create or open
     Example: hybim871-e2e
       stream id: 640d0614-0d23-49b3-b33a-589d8908528b

   Step 4: Open the Controls tab for that stream
     UI: project -> agent stream -> Controls tab
     URL shape:
       .../#/agent-obs/project/<project-id>/agent-streams/<stream-id>?view=controls

   Step 5: Add a control (or clone-and-bind an existing one)
     A steer control used here:
       name:      2fa-steer
       step type: tool
       stage:     pre
       evaluator: regex,  pattern [1-9][0-9]{4,}   (matches any integer >= 10000)
       action:    steer   (block and steer, e.g. "2FA required")
     The control must be BOUND to the stream (used_by count > 0) or evaluation matches nothing.
```

You can also create/bind controls via the API (used during this test):

```
   # create a control
   PUT /ao/agent-control/api/v1/controls        (X-SF-Token)
       body: { "name": "...", "data": { execution, scope{step_types,stages},
               condition{selector,evaluator}, action{decision} } }
       -> 200 { "control_id": N }
       (note: create is PUT; POST /controls returns 405 by design)

   # clone an existing control and bind it to a stream
   POST /ao/agent-control/api/v1/controls/<id>/clone-and-bind    (X-SF-Token)
        body: { "target_binding": { "target_type": "log_stream",
                                    "target_id": "<stream-id>" } }
        -> 200 { "id": N, "cloned_from_control_id": <id>, "binding_id": M }
```

