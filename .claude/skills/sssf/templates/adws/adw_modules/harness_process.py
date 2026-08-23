"""Strict JSONL process execution shared by command harness adapters."""

from __future__ import annotations

import codecs
import json
import os
import queue
import signal
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from _thread import LockType
from threading import Lock, Thread
from typing import TextIO

from pydantic import BaseModel

from .harness_types import (HarnessConfigError, HarnessProtocolError,
                            HarnessProcessError, HarnessUnavailableError,
                            ProcessCallback)
from .diagnostics import sanitize_diagnostic, sanitize_text
from .utils import operator_env

JsonEventCallback = Callable[[dict[str, object]], None]
GROUP_STOP_TIMEOUT_SECONDS = 1.0
GROUP_POLL_SECONDS = 0.02
STDOUT_READER_JOIN_SECONDS = 0.25
STDOUT_POLL_SECONDS = 0.02


class ProcessSpec(BaseModel):
    command: list[str]
    cwd: str
    raw_output_path: str
    stderr_output_path: str
    stdin_text: str | None = None


class ProcessOutcome(BaseModel):
    returncode: int
    stderr_tail: str = ""


@dataclass
class StdoutReader:
    """Own one background stdout reader; e.g. ``reader.thread.join()``."""

    stream: TextIO
    descriptor: int
    encoding: str
    errors: str
    lines: queue.Queue[str | None]
    descriptor_lock: LockType
    thread: Thread = field(init=False)
    descriptor_closed: bool = False


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
    """Stream JSONL while preserving output; e.g. ``run_jsonl_process(spec, callback)``."""
    _prepare_output_paths(spec)
    temporary_stderr = _temporary_stderr_path()
    try:
        process = _start_process(spec, temporary_stderr)
    except BaseException:
        temporary_stderr.unlink(missing_ok=True)
        raise
    return _run_started_process(process, spec, on_json, on_spawn, on_exit, temporary_stderr)


def _run_started_process(process: subprocess.Popen[str], spec: ProcessSpec,
                         on_json: JsonEventCallback, on_spawn: ProcessCallback | None,
                         on_exit: ProcessCallback | None, temporary_stderr: Path) -> ProcessOutcome:
    """Stream one started process, then finish its lifecycle before reporting exit.

    Example: ``_run_started_process(process, spec, callback, None, None, stderr)``.
    """
    returncode: int | None = None
    stderr_text = ""
    try:
        returncode = _run_process_body(process, spec, on_json, on_spawn)
    finally:
        returncode, stderr_text = _close_process(process, returncode, spec,
                                                  temporary_stderr, on_exit)
    return ProcessOutcome(returncode=returncode,
                          stderr_tail=stderr_text[-800:])


def _run_process_body(process: subprocess.Popen[str], spec: ProcessSpec,
                      on_json: JsonEventCallback, on_spawn: ProcessCallback | None) -> int | None:
    """Notify, write input, and stream one harness process.

    Example: ``_run_process_body(process, spec, callback, None)``.
    """
    _notify_spawn(process, on_spawn)
    _write_stdin(process, spec.stdin_text)
    return _stream_stdout(process, spec, on_json)


def _close_process(process: subprocess.Popen[str], known_returncode: int | None, spec: ProcessSpec,
                   temporary_stderr: Path, on_exit: ProcessCallback | None) -> tuple[int, str]:
    """Finish the process group, preserve stderr, then issue the exit callback.

    Example: ``_close_process(process, None, spec, stderr, callback)`` returns diagnostics.
    """
    try:
        returncode = known_returncode if known_returncode is not None else _finish_process(process)
    finally:
        try:
            stderr_text = _persist_sanitized_stderr(temporary_stderr, spec.stderr_output_path)
        finally:
            _notify_exit(process, on_exit)
    return returncode, stderr_text


def _prepare_output_paths(spec: ProcessSpec) -> None:
    raw_parent = Path(spec.raw_output_path).parent
    stderr_parent = Path(spec.stderr_output_path).parent
    raw_parent.mkdir(parents=True, exist_ok=True)
    stderr_parent.mkdir(parents=True, exist_ok=True)
    _secure_diagnostic_path(Path(spec.raw_output_path))
    _secure_diagnostic_path(Path(spec.stderr_output_path))


def _start_process(spec: ProcessSpec, temporary_stderr: Path) -> subprocess.Popen[str]:
    stderr_file = temporary_stderr.open("ab")
    try:
        process = subprocess.Popen(
            spec.command, stdin=subprocess.PIPE if spec.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=stderr_file, text=True, bufsize=1,
            cwd=spec.cwd, env=operator_env(), start_new_session=os.name == "posix",
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
                   on_json: JsonEventCallback) -> int | None:
    """Consume stdout without letting an inherited pipe hide leader exit.

    Example: ``_stream_stdout(process, spec, callback)`` returns a known exit.
    """
    if process.stdout is None:
        return None
    reader = _start_stdout_reader(process.stdout)
    with Path(spec.raw_output_path).open("a") as raw_output:
        try:
            return _monitor_stdout(process, reader, raw_output, on_json)
        finally:
            _release_stdout_reader(reader)


def _start_stdout_reader(stream: TextIO) -> StdoutReader:
    reader = StdoutReader(stream=stream, descriptor=os.dup(stream.fileno()),
                          encoding=stream.encoding or "utf-8", errors=stream.errors or "strict",
                          lines=queue.Queue(), descriptor_lock=Lock())
    reader.thread = Thread(target=_copy_stdout_lines, args=(reader,), daemon=True)
    reader.thread.start()
    return reader


def _copy_stdout_lines(reader: StdoutReader) -> None:
    decoder = codecs.getincrementaldecoder(reader.encoding)(errors=reader.errors)
    pending = ""
    try:
        while chunk := os.read(reader.descriptor, 8192):
            pending = _queue_stdout_chunk(decoder.decode(chunk), pending, reader.lines)
        trailing = pending + decoder.decode(b"", final=True)
        if trailing:
            reader.lines.put(trailing)
    except (OSError, UnicodeError):
        pass
    finally:
        _close_reader_descriptor(reader)
        reader.lines.put(None)


def _queue_stdout_chunk(chunk: str, pending: str, lines: queue.Queue[str | None]) -> str:
    pending += chunk
    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        lines.put(line + "\n")
    return pending


def _monitor_stdout(process: subprocess.Popen[str], reader: StdoutReader,
                    raw_output: TextIO, on_json: JsonEventCallback) -> int | None:
    line_number = 0
    while True:
        returncode = process.poll()
        if returncode is not None:
            return _finish_leader_stream(process, reader, raw_output, on_json, line_number)
        has_line, line = _next_stdout_line(reader, STDOUT_POLL_SECONDS)
        if not has_line:
            continue
        if line is None:
            return None
        line_number = _consume_stdout_line(line, line_number, raw_output, on_json)


def _finish_leader_stream(process: subprocess.Popen[str], reader: StdoutReader,
                          raw_output: TextIO, on_json: JsonEventCallback,
                          line_number: int) -> int:
    returncode = _finish_process(process)
    _join_stdout_reader(reader)
    _drain_stdout_lines(reader, raw_output, on_json, line_number)
    return returncode


def _next_stdout_line(reader: StdoutReader, timeout: float) -> tuple[bool, str | None]:
    try:
        return True, reader.lines.get(timeout=timeout)
    except queue.Empty:
        return False, None


def _consume_stdout_line(line: str, line_number: int, raw_output: TextIO,
                         on_json: JsonEventCallback) -> int:
    next_number = line_number + 1
    _consume_line(line, next_number, raw_output, on_json)
    raw_output.flush()
    return next_number


def _join_stdout_reader(reader: StdoutReader) -> None:
    reader.thread.join(STDOUT_READER_JOIN_SECONDS)
    if not reader.thread.is_alive():
        return
    _close_reader_descriptor(reader)
    reader.thread.join(STDOUT_READER_JOIN_SECONDS)
    if reader.thread.is_alive():
        raise HarnessProcessError("stdout_reader", reader.thread.name,
                                  "a stdout pipe that closes after group teardown")


def _drain_stdout_lines(reader: StdoutReader, raw_output: TextIO,
                        on_json: JsonEventCallback, line_number: int) -> None:
    while True:
        has_line, line = _next_stdout_line(reader, 0)
        if not has_line or line is None:
            return
        line_number = _consume_stdout_line(line, line_number, raw_output, on_json)


def _release_stdout_reader(reader: StdoutReader) -> None:
    _close_reader_descriptor(reader)
    reader.thread.join(STDOUT_READER_JOIN_SECONDS)
    _close_stdout_stream(reader.stream)


def _close_reader_descriptor(reader: StdoutReader) -> None:
    with reader.descriptor_lock:
        if reader.descriptor_closed:
            return
        reader.descriptor_closed = True
        try:
            os.close(reader.descriptor)
        except OSError:
            return


def _close_stdout_stream(stream: TextIO) -> None:
    if stream.closed:
        return
    try:
        stream.close()
    except OSError:
        return


def _consume_line(line: str, line_number: int, raw_output: TextIO,
                  on_json: JsonEventCallback) -> None:
    content = line.strip()
    if not content:
        return
    try:
        event = json.loads(content)
    except json.JSONDecodeError as error:
        details = _malformed_line_details(line, line_number, error.msg, error.pos)
        _write_protocol_record(raw_output, details)
        raise HarnessProtocolError("stdout", details, "a JSON object per stdout line") from None
    if isinstance(event, dict):
        raw_output.write(json.dumps(sanitize_diagnostic(event), ensure_ascii=False) + "\n")
        on_json(event)
        return
    details = _malformed_line_details(line, line_number, "json_type", None)
    details["json_type"] = type(event).__name__
    _write_protocol_record(raw_output, details)
    raise HarnessProtocolError("stdout", details, "a JSON object per stdout line")


def _malformed_line_details(line: str, line_number: int, category: str,
                            position: int | None) -> dict[str, object]:
    return {"diagnostic": "malformed_stdout", "line_number": line_number,
            "character_length": len(line), "byte_length": len(line.encode("utf-8", "replace")),
            "decode_category": category, "decode_position": position}


def _write_protocol_record(raw_output: TextIO, details: dict[str, object]) -> None:
    raw_output.write(json.dumps(sanitize_diagnostic(details), ensure_ascii=False) + "\n")


def _temporary_stderr_path() -> Path:
    descriptor, name = tempfile.mkstemp(prefix="sssf-harness-", suffix=".stderr", dir="/tmp")
    os.close(descriptor)
    path = Path(name)
    os.chmod(path, 0o600)
    return path


def _persist_sanitized_stderr(temporary_path: Path, output_path: str) -> str:
    try:
        sanitized = sanitize_text(temporary_path.read_text(errors="replace"))
        with Path(output_path).open("a") as output:
            output.write(sanitized)
        return sanitized
    finally:
        temporary_path.unlink(missing_ok=True)


def _secure_diagnostic_path(path: Path) -> None:
    path.touch(exist_ok=True)
    os.chmod(path, 0o600)


def _finish_process(process: subprocess.Popen[str]) -> int:
    if os.name != "posix":
        return _finish_direct_process(process)
    return _finish_posix_group(process)


def _finish_direct_process(process: subprocess.Popen[str]) -> int:
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def _finish_posix_group(process: subprocess.Popen[str]) -> int:
    """Terminate the captured harness group before reaping its leader.

    Keeping the leader unreaped prevents its PID/PGID from being reused while
    group-wide signals and liveness checks are still in progress.
    Example: ``_finish_posix_group(process)`` stops same-session descendants.
    """
    process_group = _captured_process_group(process)
    _signal_process_group(process_group, signal.SIGTERM)
    if _wait_for_group_exit(process_group):
        return process.wait()
    _signal_process_group(process_group, signal.SIGKILL)
    if _wait_for_group_exit(process_group):
        return process.wait()
    raise HarnessProcessError("process_group", process_group,
                              "a process group that exits after SIGKILL")


def _captured_process_group(process: subprocess.Popen[str]) -> int:
    """Return the child-only session group created by ``start_new_session``.

    Example: ``_captured_process_group(process)`` returns the leader PID as PGID.
    """
    process_group = process.pid
    if process_group == os.getpgrp():
        raise HarnessProcessError("process_group", process_group,
                                  "an isolated child process group")
    return process_group


def _signal_process_group(process_group: int, signal_number: int) -> None:
    """Signal a captured child-only group without touching the caller's group.

    Example: ``_signal_process_group(pgid, signal.SIGTERM)`` requests shutdown.
    """
    try:
        os.killpg(process_group, signal_number)
    except (PermissionError, ProcessLookupError):
        return


def _wait_for_group_exit(process_group: int) -> bool:
    """Poll a captured group until it disappears or the short deadline expires.

    Example: ``_wait_for_group_exit(pgid)`` confirms descendant teardown.
    """
    deadline = time.monotonic() + GROUP_STOP_TIMEOUT_SECONDS
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(GROUP_POLL_SECONDS)
    return True


def _process_group_exists(process_group: int) -> bool:
    """Check group liveness without signaling any member.

    Example: ``_process_group_exists(pgid)`` returns false after final teardown.
    """
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _notify_exit(process: subprocess.Popen[str], callback: ProcessCallback | None) -> None:
    if callback:
        callback(process.pid)
        return
    return None
