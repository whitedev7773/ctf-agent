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
        duplicate = root / "codex-gpt-5.6-terra-high" / "evidence"
        duplicate.mkdir(parents=True)
        (duplicate / "exploit-success-copy.png").write_bytes(
            (evidence / "exploit-success.png").read_bytes()
        )

        status = finalize_writeup(
            self.settings,
            "web warmup",
            "web",
            "TEAM{verified}",
        )
        content, loaded, _ = read_writeup(self.settings, "web warmup")

        self.assertTrue(status["documented"])
        self.assertEqual(len(status["screenshots"]), 1)
        self.assertTrue(status["screenshots"][0]["sha256"])
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

    def test_ai_writeup_rejects_omitted_solver_code_and_unexplained_screenshots(self) -> None:
        root = Path(challenge_workspace_path(self.settings, "thin report"))
        lead = root / "_shared" / "lead"
        output = root / "_shared" / "writeup"
        evidence = output / "evidence"
        lead.mkdir(parents=True)
        evidence.mkdir(parents=True)
        (lead / "solve.py").write_text("print('verified')\n", encoding="utf-8")
        (evidence / "one.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 24)
        (evidence / "two.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 24)
        begin_writeup_generation(self.settings, "thin report", "codex/gpt-5.6-sol/high")
        (output / "WRITEUP.md").write_text(
            "# 풀이 인증\n\n"
            "## 요약\n인증 검사가 누락된 요청을 이용해 관리자 기능에 접근한 문제입니다.\n\n"
            "## 취약점 또는 핵심 원리\n서버가 사용자의 권한을 확인하지 않고 관리자 요청을 처리합니다.\n\n"
            "## 풀이 과정\n핵심 exploit 코드는 solve.py에 구현했으므로 생략합니다.\n\n"
            "## 재현 방법\n보존된 스크립트를 실행합니다.\n\n"
            "## 검증\n반환된 결과에서 정답 문자열을 확인했습니다.\n\n"
            "## 주요 스크린샷\n참고용 이미지 두 장입니다.\n\n"
            "![one](evidence/one.png)\n\n![two](evidence/two.png)\n",
            encoding="utf-8",
        )

        status = finalize_writeup(
            self.settings,
            "thin report",
            "web",
            "TEAM{verified}",
            prefer_canonical=True,
            require_screenshots=True,
        )

        combined = " ".join(status["issues"])
        self.assertFalse(status["documented"])
        self.assertIn("핵심 exploit/solver 로직 전체", combined)
        self.assertIn("생략할 수 없습니다", combined)
        self.assertIn("핵심 원리 또는 취약점 증거", combined)

    def test_ai_writeup_manifest_keeps_only_two_explained_decisive_screenshots(self) -> None:
        root = Path(challenge_workspace_path(self.settings, "reviewable report"))
        lead = root / "_shared" / "lead"
        output = root / "_shared" / "writeup"
        evidence = output / "evidence"
        lead.mkdir(parents=True)
        evidence.mkdir(parents=True)
        (lead / "solve.py").write_text("print('verified')\n", encoding="utf-8")
        for index, name in enumerate(("root-cause.png", "success.png", "noise.png"), 1):
            (evidence / name).write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]) * 24)
        begin_writeup_generation(self.settings, "reviewable report", "codex/gpt-5.6-sol/high")
        (output / "WRITEUP.md").write_text(
            "# 풀이 인증\n\n"
            "## 요약\n인증 누락을 재현하고 관리자 결과와 최종 Flag를 확인한 풀이입니다.\n\n"
            "## 취약점 또는 핵심 원리\n요청 헤더를 신뢰해 권한 검사가 우회되는 것이 핵심 원인입니다.\n\n"
            "## 풀이 과정\n아래 코드는 요청 구성부터 응답 검증까지 사용한 전체 핵심 로직입니다.\n\n"
            "```python\nimport requests\n\ndef exploit(base):\n"
            "    session = requests.Session()\n"
            "    response = session.get(base + '/admin', headers={'X-Role': 'admin'})\n"
            "    response.raise_for_status()\n"
            "    body = response.text\n"
            "    assert 'TEAM{' in body\n"
            "    return body\n\nprint(exploit('http://challenge'))\n```\n\n"
            "## 재현 방법\n```sh\npython3 solve.py\n```\n\n"
            "## 검증\n동일한 요청을 다시 보내 같은 Flag 결과가 반환되는지 확인했습니다.\n\n"
            "## 주요 스크린샷\n\n"
            "### 핵심 취약점 분석 근거\n권한 검사 없이 관리자 응답이 반환되는 화면입니다.\n\n"
            "![핵심 취약점](evidence/root-cause.png)\n\n"
            "### 해결 성공 및 Flag 결과\n실제 공격 요청으로 최종 정답을 얻은 화면입니다.\n\n"
            "![Flag 성공](evidence/success.png)\n",
            encoding="utf-8",
        )

        status = finalize_writeup(
            self.settings,
            "reviewable report",
            "web",
            "TEAM{verified}",
            prefer_canonical=True,
            require_screenshots=True,
        )

        self.assertTrue(status["documented"])
        self.assertEqual(
            {item["path"] for item in status["screenshots"]},
            {"_shared/writeup/evidence/root-cause.png", "_shared/writeup/evidence/success.png"},
        )

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
