"""Resource telemetry, writeup, and persistent experience tests."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.artifacts import challenge_shared_path, challenge_workspace_path
from backend.experience import experience_summary, promote_challenge_experience
from backend.sandbox import DockerSandbox
from backend.writeups import (
    begin_writeup_generation,
    finalize_writeup,
    interrupted_writeup_status,
    read_writeup,
)


class DockerResourceTests(unittest.TestCase):
    def test_docker_stats_are_normalized_for_dashboard(self) -> None:
        sandbox = DockerSandbox("ctf-sandbox", "challenge", memory_limit="1g", cpu_limit=2.0)
        sandbox._started_monotonic = time.monotonic() - 12
        sandbox._container = SimpleNamespace(id="1234567890abcdef")
        snapshot = sandbox._parse_resource_stats(
            {
                "cpu_stats": {
                    "cpu_usage": {"total_usage": 1_100, "percpu_usage": [1, 2]},
                    "system_cpu_usage": 10_000,
                    "online_cpus": 2,
                },
                "precpu_stats": {
                    "cpu_usage": {"total_usage": 1_000},
                    "system_cpu_usage": 9_000,
                },
                "memory_stats": {
                    "usage": 500,
                    "limit": 1_000,
                    "stats": {"inactive_file": 100},
                },
                "pids_stats": {"current": 7},
                "networks": {
                    "eth0": {"rx_bytes": 30, "tx_bytes": 40},
                    "eth1": {"rx_bytes": 2, "tx_bytes": 3},
                },
                "blkio_stats": {
                    "io_service_bytes_recursive": [
                        {"op": "read", "value": 9},
                        {"op": "write", "value": 11},
                    ]
                },
            }
        )

        self.assertEqual(snapshot["container_id"], "1234567890ab")
        self.assertEqual(snapshot["cpu_percent"], 20.0)
        self.assertEqual(snapshot["memory_bytes"], 400)
        self.assertEqual(snapshot["memory_percent"], 40.0)
        self.assertEqual(snapshot["pids"], 7)
        self.assertEqual(snapshot["network_rx_bytes"], 32)
        self.assertEqual(snapshot["block_write_bytes"], 11)


class KnowledgeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = SimpleNamespace(
            workspace_root=str(root / "workspace"),
            experience_root=str(root / "experience"),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_verified_experience_is_persistent_and_flag_redacted(self) -> None:
        shared = Path(challenge_shared_path(self.settings, "heap warmup"))
        source = shared / "lead" / "EXPERIENCE.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            "# Reusable notes\n\nUse a two-sample allocator check before assuming a fixed stride.\n"
            "The observed result was TEAM{secret-value}.\n",
            encoding="utf-8",
        )

        result = promote_challenge_experience(
            self.settings,
            "heap warmup",
            "pwn",
            "TEAM{secret-value}",
        )

        self.assertTrue(result["promoted"])
        stored = Path(self.settings.experience_root) / result["path"]
        self.assertTrue(stored.is_file())
        self.assertIn("[REDACTED_FLAG]", stored.read_text(encoding="utf-8"))
        self.assertNotIn("TEAM{secret-value}", stored.read_text(encoding="utf-8"))
        self.assertEqual(experience_summary(self.settings)["record_count"], 1)

    def test_writeup_collects_reproducer_and_real_screenshot(self) -> None:
        root = Path(challenge_workspace_path(self.settings, "web warmup"))
        lead = root / "_shared" / "lead"
        evidence = lead / "evidence"
        evidence.mkdir(parents=True)
        (lead / "SOLUTION.md").write_text(
            "# 풀이\n\n## 증거\n조작한 요청을 전송한 뒤 응답이 관리자 권한 결과로 변경되는 것을 확인했습니다.\n\n"
            "## 재현\n```sh\npython3 solve.py\n```\n",
            encoding="utf-8",
        )
        (lead / "solve.py").write_text("print('verified')\n", encoding="utf-8")
        (evidence / "exploit-success.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 24)

        status = finalize_writeup(
            self.settings,
            "web warmup",
            "web",
            "TEAM{verified}",
        )
        content, loaded, _ = read_writeup(self.settings, "web warmup")

        self.assertTrue(status["documented"])
        self.assertEqual(len(status["screenshots"]), 1)
        self.assertTrue(status["reproducers"])
        self.assertIn("## 주요 스크린샷", content)
        self.assertEqual(loaded["status"], "complete")

    def test_writeup_requires_korean_narrative(self) -> None:
        root = Path(challenge_workspace_path(self.settings, "english report"))
        lead = root / "_shared" / "lead"
        lead.mkdir(parents=True)
        (lead / "SOLUTION.md").write_text(
            "# Solution\n\n## Evidence\n"
            "The crafted request bypassed authentication and returned the administrator response. "
            "The same result was reproduced twice against a fresh process.\n\n"
            "## Reproduction\n```sh\npython3 solve.py\n```\n",
            encoding="utf-8",
        )
        (lead / "solve.py").write_text("print('verified')\n", encoding="utf-8")

        status = finalize_writeup(
            self.settings,
            "english report",
            "web",
            "TEAM{verified}",
        )

        self.assertFalse(status["documented"])
        self.assertEqual(status["status"], "needs_attention")
        self.assertIn("한국어", " ".join(status["issues"]))

    def test_orphaned_generating_writeup_becomes_retryable(self) -> None:
        status = begin_writeup_generation(
            self.settings,
            "interrupted report",
            "codex/gpt-5.6-sol/xhigh",
        )

        self.assertEqual(status["status"], "generating")
        self.assertTrue(status["active"])

        recovered = interrupted_writeup_status(status)

        self.assertEqual(recovered["status"], "needs_attention")
        self.assertFalse(recovered["active"])
        self.assertIn("서버 종료", " ".join(recovered["issues"]))
