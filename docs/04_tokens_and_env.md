# Tokens and environment

They are different scopes. Do not mix them.

```
   SF / session token   -> gateway auth (agent-control API, control CRUD, evaluation)
                           sent as header X-SF-Token
   INGEST token         -> splunk-ao OTLP span export (SPLUNK_AO_O11Y_TOKEN)
                           only valid on the ingest endpoint
   API token            -> splunk-ao CRUD, project/stream lookup (SPLUNK_AO_O11Y_API_TOKEN)
```

Get all three tokens from the SAME org, and make sure that org has agent-control membership.
The control is created under the SF-token org while the project and stream are created under
the API-token org; if they differ, bind and token-exchange fail with a misleading
`404 Invalid API Key`. See docs/08_troubleshooting.md.

Known-good defaults (verified on lab0, 2026-08-25). Confirm membership is still current before
relying on these:

```
   realm:  lab0
   org:    HHLQ5TxAIAA        (the org the end-to-end flow was verified in)
   gateway host: app.lab0.signalfx.com
```

---

## Install and configure the app

```
   pip install "agent-control-sdk==8.5.0" "splunk-ao"
   (two separate packages; there is NO agent-control-sdk[splunk-ao] extra)

   export SPLUNK_AO_REALM="lab0"
   export SPLUNK_AO_O11Y_TOKEN="<ingest-token>"
   export SPLUNK_AO_O11Y_API_TOKEN="<api-token>"
   export AC_SF_TOKEN="<sf-token>"
   export AC_PROJECT_ID="<project-id>"
   export AC_STREAM_ID="<stream-id>"
   export AC_GATEWAY="https://app.<realm>.signalfx.com/ao/agent-control"
   export AC_AGENT_NAME="<agent-name>"
   export AC_AMOUNT="75000"     # any integer >= 10000 matches the regex control
```

---
