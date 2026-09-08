# CTF Agent benchmark harness

Use a fixed challenge corpus and compare named runtime variants with identical repetitions.

```yaml
version: 1
output: results.jsonl
overwrite: false
repetitions: 3
timeout_seconds: 14400

cases:
  - path: ../corpus/example
    expected_flag: TEAM{known_test_flag}

variants:
  baseline:
    models: [codex/gpt-5.6-sol/high]
    no_submit: true
    env: {}
  experiment:
    models: [codex/gpt-5.6-sol/high]
    no_submit: true
    env:
      DYNAMIC_DELEGATION_ENABLED: "true"
```

Run it with:

```text
ctf-benchmark benchmarks/corpus.yml
ctf-benchmark --summarize benchmarks/results.jsonl
```

For offline runs, `expected_flag` is hashed before being passed to the solver process; the raw flag is not written to the JSONL result. For live CTFd evaluation, omit `expected_flag` and set `no_submit: false`.

The summary reports solve@1, solve@3, median time-to-flag, fresh tokens and tool calls per solve, duplicate experiment rate, false candidate rate, clean reproduction rate, delegate utility, peer-context tokens, and hypothesis refutations.
