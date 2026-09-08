"""Run a minimal real-container smoke test for persistent PTY sessions."""

from __future__ import annotations

import asyncio
import tempfile

from backend.sandbox import DockerSandbox


async def main() -> None:
    challenge_dir = tempfile.mkdtemp(prefix="ctf-pty-smoke-")
    sandbox = DockerSandbox(
        image="ctf-sandbox",
        challenge_dir=challenge_dir,
        max_sessions=1,
        keep_workspace=False,
    )
    await sandbox.start()
    try:
        session_id = await sandbox.session_open("python3 -i")
        await sandbox.session_send(session_id, "print('PTY_' + 'RESULT_42')\n")
        chunks: list[str] = []
        for _ in range(5):
            chunks.append(await sandbox.session_read(session_id, wait_seconds=1))
            if "PTY_RESULT_42" in chunks[-1]:
                break
        output = "\n".join(chunks)
        print(output)
        if "PTY_RESULT_42" not in output:
            raise RuntimeError("PTY output did not contain the expected marker")
        await sandbox.session_close(session_id)
    finally:
        await sandbox.stop()


if __name__ == "__main__":
    asyncio.run(main())
