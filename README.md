# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Codex default) |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  5.6 Sol        | |  5.6 Sol        | |                |
     |  5.6 Terra      | |  5.6 Terra      | |     ...        |
     |  5.6 Luna       | |  5.6 Luna       | |                |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

## Quick Start

처음 설치하거나 실제 대회 운영 절차가 필요하면 [한국어 Codex 사용자 매뉴얼](docs/CODEX_USER_MANUAL.ko.md)을 먼저 확인하세요.

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Configure optional fallback credentials
cp .env.example .env

# Start the coordinator and dashboard
uv run ctf-solve \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v
```

Open the local operations dashboard at [http://127.0.0.1:9400](http://127.0.0.1:9400). Enter a CTFd URL and token there, or stay in standalone mode and register one local challenge with its description, connection details, and attachments. The dashboard also shows live solver status, traces, cost, and swarm controls.

CTFd can still be configured ahead of time with `CTFD_URL`/`CTFD_TOKEN` or the `--ctfd-url`/`--ctfd-token` CLI options. It is no longer required.

Use another port when needed:

```bash
uv run ctf-solve --dashboard-port 9500 ...
```

## Coordinator Backends

```bash
# Codex coordinator (default, GPT-5.6 Terra via JSON-RPC)
uv run ctf-solve --coordinator codex ...

# Claude SDK coordinator (optional)
uv run ctf-solve --coordinator claude ...
```

## Solver Models

Default model lineup, updated against OpenAI's model catalog on 2026-08-29
(configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| GPT-5.6 Sol | Codex | Quality-first solver (`xhigh`) |
| GPT-5.6 Terra | Codex | Balanced solver (`high`) |
| GPT-5.6 Luna | Codex | Fast, cost-sensitive solver (`medium`) |

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary** | radare2, GDB, objdump, binwalk, strings, readelf |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| **Web** | curl, nmap, Python requests, flask |
| **Misc** | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
# Optional; these can instead be entered at runtime in the dashboard
CTFD_URL=
CTFD_TOKEN=
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

All settings can also be passed as environment variables or CLI flags.

## Requirements

- Python 3.14+
- Docker
- Authenticated `codex` CLI (default solver/coordinator)
- `OPENAI_API_KEY` (optional, for Codex quota fallback)
- `claude` CLI (optional Claude coordinator/solver)

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
