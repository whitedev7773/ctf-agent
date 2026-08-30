from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.model_specs import base_model_spec, effort_from_spec, quota_fallback_spec
from backend.solver_base import solver_agent_name
from backend.tracing import SolverTracer
from backend.url_utils import same_origin


class ModelSpecTests(unittest.TestCase):
    def test_effort_variants_have_distinct_agent_names(self) -> None:
        medium = solver_agent_name("chal", "claude-sdk/claude-opus-4-6/medium")
        maximum = solver_agent_name("chal", "claude-sdk/claude-opus-4-6/max")
        self.assertNotEqual(medium, maximum)

    def test_base_spec_removes_effort_for_fallback_lookup(self) -> None:
        spec = "claude-sdk/claude-opus-4-6/max"
        self.assertEqual(base_model_spec(spec), "claude-sdk/claude-opus-4-6")
        self.assertEqual(effort_from_spec(spec), "max")
        self.assertEqual(
            quota_fallback_spec(spec),
            "bedrock/us.anthropic.claude-opus-4-6-v1",
        )

    def test_codex_56_effort_and_api_fallback(self) -> None:
        spec = "codex/gpt-5.6-sol/xhigh"
        self.assertEqual(effort_from_spec(spec), "xhigh")
        self.assertEqual(quota_fallback_spec(spec), "openai/gpt-5.6-sol")

    def test_known_openai_efforts_are_parsed(self) -> None:
        for effort in ("none", "low", "medium", "high", "xhigh", "max", "ultra"):
            with self.subTest(effort=effort):
                self.assertEqual(effort_from_spec(f"codex/gpt-5.6-sol/{effort}"), effort)


class UrlOriginTests(unittest.TestCase):
    def test_same_origin_accepts_default_port(self) -> None:
        self.assertTrue(same_origin("https://ctf.example/file", "https://ctf.example:443"))

    def test_same_origin_rejects_external_host_and_port(self) -> None:
        self.assertFalse(same_origin("https://files.example/file", "https://ctf.example"))
        self.assertFalse(same_origin("https://ctf.example:8443/file", "https://ctf.example"))


class TracingTests(unittest.TestCase):
    def test_trace_paths_are_unique_and_windows_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = SolverTracer("rev: con?", "claude-sdk/model/max", log_dir=tmp)
            second = SolverTracer("rev: con?", "claude-sdk/model/max", log_dir=tmp)
            try:
                self.assertNotEqual(first.path, second.path)
                self.assertEqual(Path(first.path).parent, Path(tmp))
                self.assertNotRegex(Path(first.path).name, r'[<>:"/\\|?*]')
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
