from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from banking_streamlit_app import (
    AGENT_CONTROL_PACKAGES,
    STREAMLIT_SCENARIOS,
    _scenario_failures,
    _validate_agent_control_package_versions,
)
from banking_streamlit_app_otel import STREAMLIT_SCENARIOS as OTEL_STREAMLIT_SCENARIOS
from banking_streamlit_app_otel_splunk_ao import (
    STREAMLIT_EXAMPLES as OTEL_SPLUNK_STREAMLIT_EXAMPLES,
)
from banking_streamlit_app_splunk_ao import (
    STREAMLIT_EXAMPLES as SPLUNK_STREAMLIT_EXAMPLES,
)
from run_demo import _draft_transfer_plan_impl
from setup_controls import control_specs


class ScenarioValidationTests(unittest.TestCase):
    scenario = {
        "expected_status": "blocked",
        "expected_steers": 0,
        "expected_verified_2fa": False,
        "expected_action": None,
        "expected_min_control_spans": 2,
    }
    base_result = {
        "status": "blocked",
        "steering_history": [],
        "final_transfer": {"verified_2fa": False},
        "answer": "blocked by demo-deny-risky-transfer-composite",
    }

    def test_prompt_injection_scenario_remains_available(self) -> None:
        scenario = STREAMLIT_SCENARIOS["Luna prompt-injection deny"]

        self.assertIn("Ignore previous instructions", scenario["prompt"])
        self.assertEqual(scenario["expected_status"], "blocked")
        self.assertEqual(
            scenario["expected_action"],
            "deny",
        )

    def test_luna_compatibility_suite_covers_query_allow_query_deny_and_output_deny(self) -> None:
        suite = {
            name: scenario
            for name, scenario in STREAMLIT_SCENARIOS.items()
            if scenario.get("luna_compatibility")
        }

        self.assertEqual(
            set(suite),
            {
                "Luna prompt-injection allow",
                "Luna prompt-injection deny",
                "Luna output-toxicity deny",
            },
        )
        self.assertIsNone(suite["Luna prompt-injection allow"]["expected_action"])
        self.assertEqual(
            suite["Luna output-toxicity deny"]["required_env"],
            "GALILEO_OUTPUT_TOXICITY_LUNA_SCORER_ID",
        )

    def test_output_toxicity_scenario_controls_the_generated_response(self) -> None:
        scenario = STREAMLIT_SCENARIOS["Luna output-toxicity deny"]

        self.assertEqual(
            _draft_transfer_plan_impl(
                scenario["prompt"],
                {"amount": 2500, "recipient_name": "Horizon Robotics", "destination_country": "United Kingdom"},
                scenario["draft_response_override"],
            ),
            scenario["draft_response_override"],
        )

    def test_prompt_injection_scenario_is_available_in_every_streamlit_app(self) -> None:
        scenario_name = "Luna prompt-injection deny"
        scenario_maps = (
            STREAMLIT_SCENARIOS,
            OTEL_STREAMLIT_SCENARIOS,
            SPLUNK_STREAMLIT_EXAMPLES,
            OTEL_SPLUNK_STREAMLIT_EXAMPLES,
        )

        for scenario_map in scenario_maps:
            with self.subTest(scenario_map=id(scenario_map)):
                self.assertIn(scenario_name, scenario_map)

    def test_logger_result_fails_without_local_or_persisted_control_spans(self) -> None:
        failures = _scenario_failures(
            self.scenario,
            {
                **self.base_result,
                "control_span_count": 0,
                "persisted_control_span_count": 0,
                "persisted_control_names": [],
            },
        )

        self.assertIn("control spans=0, expected at least 2", failures)
        self.assertIn("persisted control spans=0, expected at least 2", failures)

    def test_logger_result_does_not_require_control_name_in_persisted_records(self) -> None:
        failures = _scenario_failures(
            self.scenario,
            {
                **self.base_result,
                "control_span_count": 2,
                "persisted_control_span_count": 2,
                "persisted_control_names": [],
            },
        )

        self.assertEqual(failures, [])

    def test_otel_result_requires_raw_and_typed_control_spans(self) -> None:
        failures = _scenario_failures(
            self.scenario,
            {
                **self.base_result,
                "raw_control_events": 2,
                "typed_control_spans": 0,
                "persisted_control_names": [],
            },
        )

        self.assertIn("persisted typed control spans=0, expected at least 2", failures)


class LunaConfigurationTests(unittest.TestCase):
    @patch("banking_streamlit_app.version", return_value="8.7.0")
    def test_agent_control_packages_are_pinned_to_expected_version(self, mock_version) -> None:
        versions = _validate_agent_control_package_versions("8.7.0")

        self.assertEqual(versions, {package: "8.7.0" for package in AGENT_CONTROL_PACKAGES})
        self.assertEqual(mock_version.call_count, len(AGENT_CONTROL_PACKAGES))

    @patch("banking_streamlit_app.version", return_value="8.6.0")
    def test_agent_control_package_version_mismatch_fails(self, _mock_version) -> None:
        with self.assertRaisesRegex(RuntimeError, "expected 8.7.0"):
            _validate_agent_control_package_versions("8.7.0")

    def test_luna_control_is_omitted_without_real_scorer_id(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GALILEO_LUNA_SCORER_ID", None)
            names = [name for name, _ in control_specs()]

        self.assertNotIn("demo-observe-luna-transfer-request", names)

    def test_luna_control_uses_environment_scorer_id(self) -> None:
        with patch.dict(os.environ, {"GALILEO_LUNA_SCORER_ID": "actual-scorer-id"}):
            specs = dict(control_specs())

        luna_config = specs["demo-observe-luna-transfer-request"]["condition"]["evaluator"][
            "config"
        ]
        self.assertEqual(luna_config["scorer_id"], "actual-scorer-id")

    def test_output_toxicity_control_is_optional(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GALILEO_OUTPUT_TOXICITY_LUNA_SCORER_ID", None)
            names = [name for name, _ in control_specs()]

        self.assertNotIn("demo-deny-luna-toxic-transfer-response", names)

    def test_output_toxicity_control_uses_response_selector_and_post_stage(self) -> None:
        with patch.dict(
            os.environ,
            {"GALILEO_OUTPUT_TOXICITY_LUNA_SCORER_ID": "output-toxicity-scorer-id"},
        ):
            spec = dict(control_specs())["demo-deny-luna-toxic-transfer-response"]

        self.assertEqual(spec["scope"], {"step_types": ["llm"], "stages": ["post"]})
        self.assertEqual(spec["condition"]["selector"], {"path": "output"})
        self.assertEqual(
            spec["condition"]["evaluator"]["config"]["scorer_id"],
            "output-toxicity-scorer-id",
        )
        self.assertEqual(spec["condition"]["evaluator"]["config"]["threshold"], 0.5)


if __name__ == "__main__":
    unittest.main()
