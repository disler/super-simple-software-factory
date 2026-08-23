"""Strict JSONL process execution shared by command harness adapters."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Collection
from pathlib import Path

from pydantic import BaseModel

from .harness_types import (HarnessConfigError, HarnessProtocolError,
                            HarnessUnavailableError, ProcessCallback)
from .utils import operator_env

JsonEventCallback = Callable[[dict[str, object]], None]


class ProcessSpec(BaseModel):
    command: list[str]
    cwd: str
    raw_output_path: str
    stderr_output_path: str
    stdin_text: str | None = None


class ProcessOutcome(BaseModel):
    returncode: int
    stderr_tail: str = ""


def ensure_executable(executable: str, harness: str) -> None:
    """Reject a configured executable that cannot be resolved on PATH.

    Example: ``ensure_executable("pi", "pi")`` checks the configured command.
    """
    if shutil.which(executable):
        return
    raise HarnessUnavailableError(harness, executable, "an executable available on PATH")


def require_effort(effort: str, supported: Collection[str], harness: str) -> None:
    """Reject an effort level that the selected harness cannot represent.

    Example: ``require_effort("high", {"low", "high"}, "pi")`` validates effort.
    """
    if effort in supported:
        return
    raise HarnessConfigError(harness, effort, f"one of {sorted(supported)!r}")


def run_jsonl_process(
    spec: ProcessSpec,
    on_json: JsonEventCallback,
    on_spawn: ProcessCallback | None = None,
    on_exit: ProcessCallback | None = None,
) -> ProcessOutcome:
    """Stream one strict JSONL process while preserving stdout and stderr files.

    Example: ``run_jsonl_process(spec, consume_event)`` forwards each JSON object.
    """
    _prepare_output_paths(spec)
    process = _start_process(spec)
    try:
        _notify_spawn(process, on_spawn)
        _write_stdin(process, spec.stdin_text)
        _stream_stdout(process, spec, on_json)
        return ProcessOutcome(returncode=process.wait(), stderr_tail=_stderr_tail(spec))
    finally:
        _finish_process(process)
        _notify_exit(process, on_exit)


def _prepare_output_paths(spec: ProcessSpec) -> None:
    raw_parent = Path(spec.raw_output_path).parent
    stderr_parent = Path(spec.stderr_output_path).parent
    raw_parent.mkdir(parents=True, exist_ok=True)
    stderr_parent.mkdir(parents=True, exist_ok=True)


def _start_process(spec: ProcessSpec) -> subprocess.Popen[str]:
    stderr_file = Path(spec.stderr_output_path).open("a")
    try:
        process = subprocess.Popen(
            spec.command, stdin=subprocess.PIPE if spec.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=stderr_file, text=True, bufsize=1,
            cwd=spec.cwd, env=operator_env(),
        )
    except BaseException:
        stderr_file.close()
        raise
    stderr_file.close()
    return process


def _notify_spawn(process: subprocess.Popen[str], callback: ProcessCallback | None) -> None:
    if callback:
        callback(process.pid)
        return
    return None


def _write_stdin(process: subprocess.Popen[str], text: str | None) -> None:
    if text is None:
        return
    if process.stdin is None:
        return
    process.stdin.write(text)
    process.stdin.close()


def _stream_stdout(process: subprocess.Popen[str], spec: ProcessSpec,
                   on_json: JsonEventCallback) -> None:
    if process.stdout is None:
        return
    with Path(spec.raw_output_path).open("a") as raw_output:
        for line in process.stdout:
            raw_output.write(line)
            raw_output.flush()
            _consume_line(line, spec.command[0], on_json)


def _consume_line(line: str, executable: str, on_json: JsonEventCallback) -> None:
    content = line.strip()
    if not content:
        return
    try:
        event = json.loads(content)
    except json.JSONDecodeError as error:
        raise HarnessProtocolError(executable, content, "a JSON object per stdout line") from error
    if isinstance(event, dict):
        on_json(event)
        return
    raise HarnessProtocolError(executable, event, "a JSON object per stdout line")


def _stderr_tail(spec: ProcessSpec) -> str:
    try:
        return Path(spec.stderr_output_path).read_text()[-800:]
    except OSError:
        return ""


def _finish_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _notify_exit(process: subprocess.Popen[str], callback: ProcessCallback | None) -> None:
    if callback:
        callback(process.pid)
        return
    return None
