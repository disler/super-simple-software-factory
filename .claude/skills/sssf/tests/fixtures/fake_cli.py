#!/usr/bin/env python3
"""One fake CLI, reused for pi/claude/codex tests.

Driven entirely by environment variables, so the same script stands in for
any of the three real CLIs without per-test script generation:

  FAKE_CLI_ARGV_FILE        if set, argv[1:] is dumped there as JSON (for
                             asserting on the exact command an adapter built).
  FAKE_CLI_STDIN_FILE       if set, stdin is dumped there as text.
  FAKE_CLI_LINES_FILE       if set, each line is printed to stdout, flushed
                             one at a time (simulates a streaming JSONL CLI).
  FAKE_CLI_EXIT_CODE        process exit code (default 0).
  FAKE_CLI_STDERR           if set, written to stderr before exiting.
  FAKE_CLI_STDERR_BYTES     if set, that many bytes are written to stderr in
                             ONE blocking write, right after the first stdout
                             line — comfortably past any OS pipe buffer (they
                             are commonly ~64KB). Reproduces a CLI that fills
                             stderr mid-stream: without a concurrent stderr
                             reader, this write blocks, which also stalls the
                             stdout the parent is waiting on — a deadlock.
  FAKE_PI_CATALOG           only consulted when argv[1] == "--list-models":
                             one line "<provider> <model_id> <context>"
                             printed after a header row, mimicking
                             `pi --list-models`.
  FAKE_KIRO_MODELS          only consulted when "--list-models" appears later
                             in argv (`chat --list-models --format json`):
                             printed verbatim as the JSON catalog Kiro CLI
                             returns, `{"models": [...], "default_model": ...}`.
  FAKE_AGY_MODELS           only consulted when argv[1] == "models"
                             (`agy models`): printed verbatim after a status
                             line, as the "<slug>\t<Display Name>" rows that
                             `agy models` emits.
"""
import json
import os
import sys

KIRO_MODELS_DEFAULT = json.dumps({
    "models": [{"model_name": "test-model", "model_id": "test-model",
                "context_window_tokens": 200000}],
    "default_model": "test-model",
})

AGY_MODELS_DEFAULT = "test-model\tTest Model (Medium)"


def main() -> None:
    argv_file = os.environ.get("FAKE_CLI_ARGV_FILE")
    if argv_file:
        with open(argv_file, "w") as f:
            json.dump(sys.argv[1:], f)

    stdin_file = os.environ.get("FAKE_CLI_STDIN_FILE")
    if stdin_file:
        with open(stdin_file, "w") as f:
            f.write(sys.stdin.read())

    if len(sys.argv) > 1 and sys.argv[1] == "--list-models":
        print("PROVIDER  MODEL  CONTEXT")
        print(os.environ.get("FAKE_PI_CATALOG", "testprov test-model 128K"))
        sys.exit(0)

    # Antigravity asks through a bare subcommand: `agy models`.
    if len(sys.argv) > 1 and sys.argv[1] == "models":
        print("Fetching available models...")     # no tab: not a model row
        print(os.environ.get("FAKE_AGY_MODELS", AGY_MODELS_DEFAULT))
        sys.exit(0)

    # Kiro CLI asks for its catalog through a subcommand, so the flag is not
    # argv[1]: `kiro-cli chat --list-models --format json`.
    if "--list-models" in sys.argv:
        print(os.environ.get("FAKE_KIRO_MODELS", KIRO_MODELS_DEFAULT))
        sys.exit(0)

    stderr_bytes = int(os.environ.get("FAKE_CLI_STDERR_BYTES", "0"))
    lines_file = os.environ.get("FAKE_CLI_LINES_FILE")
    if lines_file:
        with open(lines_file) as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
        for index, line in enumerate(lines):
            print(line)
            sys.stdout.flush()
            if index == 0 and stderr_bytes:
                sys.stderr.write("E" * stderr_bytes)
                sys.stderr.flush()

    stderr = os.environ.get("FAKE_CLI_STDERR")
    if stderr:
        print(stderr, file=sys.stderr)

    sys.exit(int(os.environ.get("FAKE_CLI_EXIT_CODE", "0")))


if __name__ == "__main__":
    main()
