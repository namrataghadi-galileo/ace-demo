# Turn on the feature flag (agent_control)

The AO UI hides Agent Control until the `agent_control` feature flag is ON for the cluster.

```
   feature-flags.json (orbit repo)
     defaults:      { ... no agent_control ... }
     o11y-lab0:     { ... }      <- lab0 reads THIS block (per cluster customer_name)
        |
        |  resolved by the api service as:  defaults | customer_override | env-var
        |  (env-var wins)
        v
   GET /ao/api/configuration   ->   feature_flags.agent_control : true/false
```

The flag is per cluster (customer_name `o11y-lab0`), not per org. Turning it on enables it for
the whole lab0 cluster.

Temporary enable (env override on the api deployment):

```
   Step A: kubectl config use-context lab0
   Step B: kubectl -n o11y-ao set env deployment/api \
             GALILEO_FEATURE_FLAG_AGENT_CONTROL=enabled
   Step C: wait ~60s (flag cache TTL) for the api pods to roll
   Step D: verify
           curl -s -H "X-SF-Token: <sf-token>" \
             https://app.lab0.signalfx.com/ao/api/configuration
           -> feature_flags.agent_control : true
   Rollback: kubectl -n o11y-ao set env deployment/api GALILEO_FEATURE_FLAG_AGENT_CONTROL-
```

Permanent (preferred): orbit PR #1870 adds `"agent_control": "enabled"` to the `o11y-lab0`
block in `configs/feature-flags/feature-flags.json`. Once merged and synced, drop the env
override.

```
   diff (configs/feature-flags/feature-flags.json)
     "o11y-lab0": {
       ...
       "o11y_cloud_integration": "enabled",
   +   "agent_control": "enabled"
     },
```

---
