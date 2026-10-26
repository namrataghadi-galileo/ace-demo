"""Environment defaults and credentials for the standalone Luna example."""

from __future__ import annotations

import os

DEFAULT_CONSOLE_URL = os.environ.get(
    "GALILEO_CONSOLE_URL", "https://console-test-evals.gcp-dev.galileo.ai"
)
DEFAULT_AGENT_CONTROL_URL = os.environ.get(
    "AGENT_CONTROL_URL", f"{DEFAULT_CONSOLE_URL.rstrip('/')}/api/agent-control"
)
DEFAULT_PROJECT = os.environ.get("GALILEO_PROJECT", "agent-control-banking-demo-project")
DEFAULT_LOG_STREAM = os.environ.get(
    "GALILEO_LOG_STREAM", "agent-control-banking-demo-logstream"
)


def resolve_agent_control_api_key() -> str | None:
    """Use the Galileo API key as the Agent Control Enterprise credential."""
    api_key = os.environ.get("GALILEO_API_KEY") or os.environ.get("AGENT_CONTROL_API_KEY")
    if api_key:
        os.environ["AGENT_CONTROL_API_KEY"] = api_key
    return api_key


def resolve_agent_control_api_key_header() -> str:
    """Resolve the API-key header expected by the Agent Control gateway."""
    header = os.environ.get("AGENT_CONTROL_API_KEY_HEADER", "Galileo-API-Key")
    os.environ["AGENT_CONTROL_API_KEY_HEADER"] = header
    return header
