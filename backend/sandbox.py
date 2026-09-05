"""Docker sandbox for CTF challenge solving — native async via aiodocker."""

from __future__ import annotations

import asyncio
import io
import logging
import shlex
import tarfile
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiodocker

logger = logging.getLogger(__name__)

CONTAINER_LABEL = "ctf-agent"

# Concurrency control
_start_semaphore: asyncio.Semaphore | None = None
_active_count: int = 0
_count_lock = asyncio.Lock()

_WARN_THRESHOLDS = {100, 200, 500}


def configure_semaphore(max_concurrent: int = 50) -> None:
    """Set the max concurrent container starts. Call once at startup."""
    global _start_semaphore
    _start_semaphore = asyncio.Semaphore(max_concurrent)


async def _track_start() -> None:
    global _active_count
    async with _count_lock:
        _active_count += 1
        if _active_count in _WARN_THRESHOLDS:
            logger.warning("Active containers: %d", _active_count)


async def _track_stop() -> None:
    global _active_count
    async with _count_lock:
        _active_count = max(0, _active_count - 1)


async def cleanup_orphan_containers() -> None:
    """Kill any leftover ctf-agent containers from a previous run."""
    try:
        docker = aiodocker.Docker()
        try:
            containers = await docker.containers.list(
                all=True,
                filters={"label": [CONTAINER_LABEL]},
            )
            for c in containers:
                try:
                    await c.delete(force=True)
                except Exception:
                    pass
            if containers:
                logger.info("Cleaned up %d orphan container(s)", len(containers))
        finally:
            await docker.close()
    except Exception as e:
        logger.warning("Orphan cleanup failed: %s", e)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class DockerSandbox:
    """Isolated Docker container for a single solver agent."""

    image: str
    challenge_dir: str
    memory_limit: str = "16g"
    cpu_limit: float = 2.0
    max_exec_timeout_s: int = 600
    workspace_dir: str = ""
    shared_workspace_dir: str = ""
    experience_dir: str = ""
    keep_workspace: bool = False
    resource_sample_interval_s: float = 2.0
    _container: Any = field(default=None, repr=False)
    _docker: Any = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _resource_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _resource_history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=60),
        repr=False,
    )
    _resource_snapshot: dict[str, Any] = field(default_factory=dict, repr=False)
    _started_monotonic: float = field(default=0.0, repr=False)

    @property
    def container_id(self) -> str:
        """The Docker container ID, available after start()."""
        if not self._container:
            raise RuntimeError("Sandbox not started")
        return self._container.id

    def _parse_memory_limit(self) -> int:
        s = self.memory_limit.strip().lower()
        try:
            if s.endswith("g"):
                return int(s[:-1]) * 1024 * 1024 * 1024
            if s.endswith("m"):
                return int(s[:-1]) * 1024 * 1024
            return int(s)
        except (ValueError, IndexError):
            logger.warning("Invalid memory_limit %r, defaulting to 4GB", self.memory_limit)
            return 4 * 1024 * 1024 * 1024

    @staticmethod
    def _sum_block_io(entries: list[dict[str, Any]], operation: str) -> int:
        return sum(
            int(entry.get("value", 0) or 0)
            for entry in entries
            if str(entry.get("op", "")).casefold() == operation
        )

    def _parse_resource_stats(self, raw: dict[str, Any]) -> dict[str, Any]:
        cpu_stats = raw.get("cpu_stats") or {}
        previous_cpu = raw.get("precpu_stats") or {}
        cpu_usage = cpu_stats.get("cpu_usage") or {}
        previous_usage = previous_cpu.get("cpu_usage") or {}
        cpu_delta = int(cpu_usage.get("total_usage", 0) or 0) - int(
            previous_usage.get("total_usage", 0) or 0
        )
        system_delta = int(cpu_stats.get("system_cpu_usage", 0) or 0) - int(
            previous_cpu.get("system_cpu_usage", 0) or 0
        )
        online_cpus = int(cpu_stats.get("online_cpus", 0) or 0)
        if not online_cpus:
            online_cpus = len(cpu_usage.get("percpu_usage") or []) or 1
        cpu_percent = (
            max(0.0, cpu_delta / system_delta * online_cpus * 100.0)
            if cpu_delta > 0 and system_delta > 0
            else 0.0
        )

        memory_stats = raw.get("memory_stats") or {}
        memory_detail = memory_stats.get("stats") or {}
        memory_total = int(memory_stats.get("usage", 0) or 0)
        memory_cache = int(
            memory_detail.get("inactive_file", memory_detail.get("cache", 0)) or 0
        )
        memory_used = max(0, memory_total - memory_cache)
        memory_limit = int(memory_stats.get("limit", 0) or self._parse_memory_limit())

        networks = raw.get("networks") or {}
        network_rx = sum(int(item.get("rx_bytes", 0) or 0) for item in networks.values())
        network_tx = sum(int(item.get("tx_bytes", 0) or 0) for item in networks.values())

        block_entries = (
            (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
        )
        pids = int((raw.get("pids_stats") or {}).get("current", 0) or 0)
        sampled_at = datetime.now(UTC).isoformat()
        uptime_seconds = max(0.0, time.monotonic() - self._started_monotonic)
        previous_peak_cpu = float(self._resource_snapshot.get("peak_cpu_percent", 0) or 0)
        previous_peak_memory = int(self._resource_snapshot.get("peak_memory_bytes", 0) or 0)
        return {
            "available": True,
            "stale": False,
            "status": "running",
            "container_id": self.container_id[:12],
            "sampled_at": sampled_at,
            "uptime_seconds": round(uptime_seconds, 1),
            "cpu_percent": round(cpu_percent, 2),
            "cpu_limit": self.cpu_limit,
            "peak_cpu_percent": round(max(previous_peak_cpu, cpu_percent), 2),
            "memory_bytes": memory_used,
            "memory_limit_bytes": memory_limit,
            "memory_percent": round(memory_used / memory_limit * 100.0, 2)
            if memory_limit
            else 0.0,
            "peak_memory_bytes": max(previous_peak_memory, memory_used),
            "pids": pids,
            "network_rx_bytes": network_rx,
            "network_tx_bytes": network_tx,
            "block_read_bytes": self._sum_block_io(block_entries, "read"),
            "block_write_bytes": self._sum_block_io(block_entries, "write"),
            "error": "",
        }

    async def _sample_resources_once(self) -> None:
        if not self._container:
            return
        payload = await asyncio.wait_for(
            self._container.stats(stream=False, timeout=5),
            timeout=6,
        )
        raw = payload[-1] if payload else {}
        if not isinstance(raw, dict):
            raise TypeError("Docker stats payload is not an object")
        snapshot = self._parse_resource_stats(raw)
        self._resource_history.append(
            {
                "sampled_at": snapshot["sampled_at"],
                "cpu_percent": snapshot["cpu_percent"],
                "memory_bytes": snapshot["memory_bytes"],
            }
        )
        snapshot["history"] = list(self._resource_history)
        self._resource_snapshot = snapshot

    async def _monitor_resources(self) -> None:
        interval = max(0.5, float(self.resource_sample_interval_s))
        while self._container:
            try:
                await self._sample_resources_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                previous = dict(self._resource_snapshot)
                previous.update(
                    {
                        "available": bool(previous),
                        "stale": True,
                        "status": "unavailable",
                        "container_id": self._container.id[:12] if self._container else "",
                        "sampled_at": datetime.now(UTC).isoformat(),
                        "error": str(exc)[:240],
                        "history": list(self._resource_history),
                    }
                )
                self._resource_snapshot = previous
            await asyncio.sleep(interval)

    def resource_snapshot(self) -> dict[str, Any]:
        """Return the latest non-blocking Docker resource sample."""
        snapshot = dict(self._resource_snapshot)
        snapshot["history"] = list(self._resource_history)
        return snapshot

    async def start(self) -> None:
        sem = _start_semaphore or asyncio.Semaphore(50)
        async with sem:
            self._docker = aiodocker.Docker()

            if self.workspace_dir:
                workspace = Path(self.workspace_dir).expanduser().resolve()
                workspace.mkdir(parents=True, exist_ok=True)
                self.workspace_dir = str(workspace)
            else:
                self.workspace_dir = tempfile.mkdtemp(prefix="ctf-workspace-")

            challenge_root = Path(self.challenge_dir).resolve()
            distfiles = str(challenge_root / "distfiles")
            meta_yml = str(challenge_root / "metadata.yml")

            binds: list[str] = [f"{self.workspace_dir}:/challenge/workspace:rw"]
            if self.shared_workspace_dir:
                shared_workspace = Path(self.shared_workspace_dir).expanduser().resolve()
                shared_workspace.mkdir(parents=True, exist_ok=True)
                self.shared_workspace_dir = str(shared_workspace)
                binds.append(f"{self.shared_workspace_dir}:/challenge/shared:rw")
            if self.experience_dir:
                experience = Path(self.experience_dir).expanduser().resolve()
                experience.mkdir(parents=True, exist_ok=True)
                self.experience_dir = str(experience)
                binds.append(f"{self.experience_dir}:/challenge/experience:ro")
            if Path(distfiles).exists():
                binds.append(f"{distfiles}:/challenge/distfiles:ro")
            if Path(meta_yml).exists():
                binds.append(f"{meta_yml}:/challenge/metadata.yml:ro")

            config = {
                "Image": self.image,
                "Cmd": ["sleep", "infinity"],
                "WorkingDir": "/challenge",
                "Tty": False,
                "Labels": {CONTAINER_LABEL: "true"},
                "HostConfig": {
                    "Binds": binds,
                    "ExtraHosts": ["host.docker.internal:host-gateway"],
                    "CapAdd": ["SYS_ADMIN", "SYS_PTRACE"],
                    "SecurityOpt": ["seccomp=unconfined"],
                    "Devices": [{"PathOnHost": "/dev/loop-control", "PathInContainer": "/dev/loop-control", "CgroupPermissions": "rwm"}],
                    "Memory": self._parse_memory_limit(),
                    "NanoCpus": int(max(0.1, self.cpu_limit) * 1e9),
                },
            }

            self._container = await self._docker.containers.create(config)
            await self._container.start()
            await _track_start()
            self._started_monotonic = time.monotonic()

            info = await self._container.show()
            short_id = info["Id"][:12]
            logger.info("Sandbox started: %s", short_id)
            self._resource_snapshot = {
                "available": False,
                "stale": False,
                "status": "starting",
                "container_id": short_id,
                "sampled_at": datetime.now(UTC).isoformat(),
                "cpu_limit": self.cpu_limit,
                "memory_limit_bytes": self._parse_memory_limit(),
                "history": [],
                "error": "",
            }
            self._resource_task = asyncio.create_task(
                self._monitor_resources(),
                name=f"docker-stats-{short_id}",
            )

    async def exec(self, command: str, timeout_s: int = 300) -> ExecResult:
        if not self._container:
            raise RuntimeError("Sandbox not started")

        timeout_s = max(1, min(int(timeout_s), self.max_exec_timeout_s))

        async with self._lock:
            try:
                return await self._exec_inner(command, timeout_s)
            except aiodocker.exceptions.DockerError as e:
                # Container was deleted (e.g., sibling solver found the flag)
                return ExecResult(exit_code=-1, stdout="", stderr=f"Container gone: {e}")

    async def _exec_inner(self, command: str, timeout_s: int) -> ExecResult:
        # Wrap command with `timeout` so the container kills the process on expiry.
        # --signal=KILL ensures hard kill; --kill-after=5 is a safety net.
        wrapped = f"timeout --signal=KILL --kill-after=5 {timeout_s} bash -c {shlex.quote(command)}"
        exec_instance = await self._container.exec(
            cmd=["bash", "-c", wrapped],
            stdout=True,
            stderr=True,
            tty=False,
        )

        stream = exec_instance.start(detach=False)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _collect() -> None:
            while True:
                msg = await stream.read_out()
                if msg is None:
                    break
                if msg.stream == 1:
                    stdout_chunks.append(msg.data)
                else:
                    stderr_chunks.append(msg.data)

        try:
            # Give extra margin beyond the container-side timeout
            await asyncio.wait_for(_collect(), timeout=timeout_s + 30)
        except TimeoutError:
            try:
                await stream.close()
            except Exception:
                pass
            return ExecResult(
                exit_code=-1,
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr="Command timed out",
            )

        inspect = await exec_instance.inspect()
        exit_code = inspect.get("ExitCode", 0)

        return ExecResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> str | bytes:
        """Read a file from the container. Returns str for text, bytes for binary."""
        if not self._container:
            raise RuntimeError("Sandbox not started")

        try:
            tar = await asyncio.wait_for(
                self._container.get_archive(path),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out reading {path}") from e

        # aiodocker 0.26.0 returns tarfile.TarFile directly
        with tar:
            for member in tar:
                if member.isfile():
                    f = tar.extractfile(member)
                    if f:
                        data = f.read()
                        try:
                            return data.decode("utf-8")
                        except UnicodeDecodeError:
                            return data
        raise FileNotFoundError(f"No file found at {path}")

    async def read_file_bytes(self, path: str) -> bytes:
        """Read a file from the container as raw bytes."""
        result = await self.read_file(path)
        if isinstance(result, str):
            return result.encode("utf-8")
        return result

    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write a file into the container via tar archive."""
        if not self._container:
            raise RuntimeError("Sandbox not started")

        if isinstance(content, str):
            content = content.encode("utf-8")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=Path(path).name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buf.seek(0)

        try:
            await asyncio.wait_for(
                self._container.put_archive(str(Path(path).parent), buf.getvalue()),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out writing {path}") from e

    async def copy_from(self, container_path: str, host_path: str) -> None:
        """Copy a file from the container to the host."""
        data = await self.read_file_bytes(container_path)
        Path(host_path).parent.mkdir(parents=True, exist_ok=True)
        Path(host_path).write_bytes(data)

    async def stop(self) -> None:
        if self._resource_task:
            self._resource_task.cancel()
            await asyncio.gather(self._resource_task, return_exceptions=True)
            self._resource_task = None

        if self._container:
            self._resource_snapshot.update(
                {
                    "stale": True,
                    "status": "stopped",
                    "sampled_at": datetime.now(UTC).isoformat(),
                    "history": list(self._resource_history),
                }
            )
            try:
                await self._container.delete(force=True)
            except Exception:
                pass
            self._container = None
            await _track_stop()

        if self._docker:
            try:
                await self._docker.close()
            except Exception:
                pass
            self._docker = None

        if self.workspace_dir and not self.keep_workspace:
            import shutil
            try:
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            except Exception:
                pass
        if not self.keep_workspace:
            self.workspace_dir = ""
        logger.info("Sandbox stopped")
