from __future__ import annotations

import json

import pytest

from slm_runtime_controls import (
    ALL_RUNTIME_SCORERS,
    LLM_TOOL_RUNTIME_SCORERS,
    SLM_RUNTIME_SCORERS,
    TOOL_RUNTIME_SCORERS,
    control_specs,
    incompatible_tool_control_specs,
    llm_tool_slm_control_specs,
    load_scorer_ids,
    tool_slm_control_specs,
)
from slm_runtime_streamlit_app import _feature_enabled, _incompatibility_result, _probe_payload


def _ids() -> dict[str, str]:
    return {spec.name: f"00000000-0000-4000-8000-{index:012d}" for index, spec in enumerate(SLM_RUNTIME_SCORERS, 1)}


def _all_ids() -> dict[str, str]:
    return {spec.name: f"00000000-0000-4000-8000-{index:012d}" for index, spec in enumerate(ALL_RUNTIME_SCORERS, 1)}


def test_exact_direct_runtime_scorer_set() -> None:
    assert [spec.name for spec in SLM_RUNTIME_SCORERS] == [
        "input_pii",
        "input_sexist_luna",
        "input_tone",
        "input_toxicity_luna",
        "prompt_injection_luna",
        "output_pii",
        "output_sexist_luna",
        "output_tone",
        "output_toxicity_luna",
    ]


def test_runtime_scorer_record_type_mapping_is_explicit() -> None:
    assert len({spec.step_name for spec in SLM_RUNTIME_SCORERS}) == len(SLM_RUNTIME_SCORERS)
    assert [(spec.name, spec.record_type) for spec in LLM_TOOL_RUNTIME_SCORERS] == [
        ("tool_selection_quality_luna", "llm")
    ]
    assert [(spec.name, spec.record_type) for spec in TOOL_RUNTIME_SCORERS] == [
        ("tool_error_rate_luna", "tool")
    ]
    assert [spec.name for spec in ALL_RUNTIME_SCORERS][-2:] == [
        "tool_selection_quality_luna",
        "tool_error_rate_luna",
    ]


def test_control_specs_use_new_runtime_schema_without_legacy_keys() -> None:
    specs = control_specs(_ids())
    assert len(specs) == 9
    for name, data in specs:
        config = data["condition"]["evaluator"]["config"]
        assert data["action"] == {"decision": "deny"}
        assert data["condition"]["evaluator"]["name"] == "galileo.luna"
        assert config["scorer_label"] == name.removeprefix("slm-runtime-deny-")
        assert "metric" not in config
        assert "payload_field" not in config
        assert "on_error" not in config


def test_probe_payload_is_an_llm_record_and_has_runtime_canary() -> None:
    spec = SLM_RUNTIME_SCORERS[0]
    payload = _probe_payload(spec, probe_id="canary-123")
    assert payload["record"]["type"] == "llm"
    assert payload["record"]["context"]["slm_runtime_probe_id"] == "canary-123"
    assert payload["config"]["request_timeout_seconds"] < 30


def test_probe_payload_for_tool_scorer_is_a_tool_record() -> None:
    payload = _probe_payload(TOOL_RUNTIME_SCORERS[0], probe_id="tool-canary")
    assert payload["record"]["type"] == "tool"
    assert payload["record"]["name"] == "slm_runtime_probe_tool"
    assert isinstance(payload["record"]["input"], dict)
    assert isinstance(payload["record"]["output"], dict)


def test_tool_selection_scorer_keeps_llm_record_and_tool_context() -> None:
    payload = _probe_payload(LLM_TOOL_RUNTIME_SCORERS[0], probe_id="llm-tools-canary")
    assert payload["record"]["type"] == "llm"
    assert payload["inputs"]["tools"] == payload["record"]["tools"]
    assert payload["inputs"]["tool_calls"] == payload["record"]["tool_calls"]


def test_tool_controls_target_tool_record_and_compatible_scorer() -> None:
    specs = tool_slm_control_specs(_all_ids())
    assert len(specs) == 1
    name, data = specs[0]
    assert name == "slm-runtime-deny-tool-error-rate"
    assert data["scope"]["step_types"] == ["tool"]
    assert data["condition"]["selector"] == {"path": "output"}
    assert data["action"] == {"decision": "deny"}
    assert data["condition"]["evaluator"]["config"]["scorer_label"] == "tool_error_rate_luna"


def test_tool_selection_control_targets_an_llm_record() -> None:
    specs = llm_tool_slm_control_specs(_all_ids())
    assert specs[0][1]["scope"]["step_types"] == ["llm"]
    assert specs[0][1]["scope"]["step_name_regex"] == "^slm_runtime_probe_llm_tools$"
    assert specs[0][1]["action"] == {"decision": "deny"}
    assert specs[0][1]["condition"]["evaluator"]["config"]["scorer_label"] == "tool_selection_quality_luna"


def test_incompatible_tool_control_is_explicitly_negative() -> None:
    specs = incompatible_tool_control_specs(_all_ids())
    assert specs[0][1]["scope"]["step_types"] == ["tool"]
    assert specs[0][1]["action"] == {"decision": "deny"}
    assert specs[0][1]["condition"]["evaluator"]["config"]["scorer_label"] == "input_pii"
    assert _incompatibility_result({"status": "error", "error_message": "record type mismatch"})
    assert not _incompatibility_result({"status": "success", "score": 0.1})


def test_feature_flag_parser_accepts_console_config_shape() -> None:
    payload = {"featureFlags": {"feature_flags": {"scorer_invoke_runtime": "enabled"}}}
    assert _feature_enabled(payload, "scorer_invoke_runtime") is True
    assert _feature_enabled({"featureFlags": {"scorer_invoke_runtime": "disabled"}}, "scorer_invoke_runtime") is False
    assert _feature_enabled({}, "scorer_invoke_runtime") is None


def test_ids_can_be_loaded_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GALILEO_SLM_SCORER_IDS_JSON", json.dumps(_ids()))
    assert load_scorer_ids() == _ids()


def test_all_ids_can_be_loaded_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _all_ids()
    monkeypatch.setenv("GALILEO_SLM_SCORER_IDS_JSON", json.dumps(expected))
    assert load_scorer_ids() == expected
