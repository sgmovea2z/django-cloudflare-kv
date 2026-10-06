from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[2]
WORKER_ROOT = Path(__file__).resolve().parent / "worker"


class WorkerProcess(TypedDict):
    """An active local Worker and its captured diagnostics."""

    process: subprocess.Popen[str]
    port: int
    persist_dir: Path
    stdout: Path
    stderr: Path


class HTTPResponse(Protocol):
    status: int

    def read(self) -> bytes: ...

    def close(self) -> None: ...


class NativeResult(TypedDict):
    put: bool
    get: str | None


class DiagnosticResult(TypedDict):
    value: str | None


class ResolvedProbe(TypedDict):
    outcome: Literal["resolved"]
    value: str | list[str] | None


class FailedProbe(TypedDict):
    outcome: Literal["error"]
    type: str
    message: str


type ProbeResult = ResolvedProbe | FailedProbe


class BaselineProbe(TypedDict):
    outcome: Literal["resolved"]
    value: str | None
    type: str
    shape: str


class WsgiResult(TypedDict):
    value: str | None
    coroutine_probe: ProbeResult
    promise_probe: ProbeResult
    single_awaitable_baseline: BaselineProbe
    composition_probe: dict[str, dict[str, str | list[str] | None]]


class MissingBindingResult(TypedDict):
    type: str
    message: str
    cause_type: str | None
    cause_message: str | None


class VersionsResult(TypedDict):
    python: str
    pyodide: str
    workerd: str


class NativeSuiteValues(TypedDict):
    bytes: str
    none: str | None
    structured: dict[str, object]
    add: bool
    touch: bool
    delete: bool
    get_many: dict[str, object]
    get_or_set: str
    has_key: bool
    incr: int
    decr: int
    set_many: list[str]
    delete_many: None
    incr_version: int
    decr_version: int
    clear: bool
    sibling: str | None


class NativeSuiteResult(TypedDict):
    methods: list[str]
    result: NativeSuiteValues


class TTLResult(TypedDict):
    logical: str
    physical_present: bool
    envelope: str | None


class TTLValueEnvelope(TypedDict):
    format: int
    expires_at: float | None
    payload: str


class ASGICacheResult(TypedDict):
    value: str


class F3CorruptResult(TypedDict):
    payload: str
    physical_key: str
    error_type: str | None
    is_backend_error: bool
    error_message: str
    cause_type: str | None
    cause_message: str | None
    returned_default: bool


class F3ClearResult(TypedDict):
    clear: bool
    cleared_physical_key: str
    sibling_physical_key: str
    cleared_envelope_before: str | None
    written_sibling_envelope: str | None


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast("tuple[str, int]", listener.getsockname())[1]


def _terminate_worker_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        _ = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        _ = process.wait(timeout=10)
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _cleanup_worker_persistence(persist_dir: Path) -> None:
    shutil.rmtree(persist_dir, ignore_errors=True)
    wrangler_tmp = WORKER_ROOT / ".wrangler" / "tmp"
    for dev_dir in wrangler_tmp.glob("dev-*"):
        shutil.rmtree(dev_dir, ignore_errors=True)


def request(port: int, path: str) -> tuple[int, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        response = cast("HTTPResponse", urllib.request.urlopen(request, timeout=10))
        try:
            return response.status, response.read().decode("utf-8")
        finally:
            response.close()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def decode_native_result(body: str) -> NativeResult:
    return cast("NativeResult", json.loads(body))


def decode_diagnostic_result(body: str) -> DiagnosticResult:
    return cast("DiagnosticResult", json.loads(body))


def decode_wsgi_result(body: str) -> WsgiResult:
    return cast("WsgiResult", json.loads(body))


def decode_missing_binding_result(body: str) -> MissingBindingResult:
    return cast("MissingBindingResult", json.loads(body))


def decode_versions_result(body: str) -> VersionsResult:
    return cast("VersionsResult", json.loads(body))


def decode_native_suite_result(body: str) -> NativeSuiteResult:
    return cast("NativeSuiteResult", json.loads(body))


def decode_ttl_result(body: str) -> TTLResult:
    return cast("TTLResult", json.loads(body))


def decode_ttl_envelope(value: str) -> TTLValueEnvelope:
    return cast("TTLValueEnvelope", json.loads(value))


def decode_asgi_cache_result(body: str) -> ASGICacheResult:
    return cast("ASGICacheResult", json.loads(body))


def decode_f3_corrupt_result(body: str) -> F3CorruptResult:
    return cast("F3CorruptResult", json.loads(body))


def decode_f3_clear_result(body: str) -> F3ClearResult:
    return cast("F3ClearResult", json.loads(body))


@pytest.fixture
def worker_process(tmp_path: Path) -> Iterator[WorkerProcess]:
    """Start an isolated pywrangler local Worker and retain its stderr."""
    port = _unused_port()
    persist_dir = tmp_path / "kv-persist"
    stdout = tmp_path / "worker.stdout.txt"
    stderr = tmp_path / "worker.stderr.txt"
    with stdout.open("w") as stdout_file, stderr.open("w") as stderr_file:
        process = subprocess.Popen(
            [
                "uv",
                "run",
                "--project",
                str(ROOT),
                "pywrangler",
                "dev",
                "--local",
                "--ip",
                "127.0.0.1",
                "--port",
                str(port),
                "--persist-to",
                str(persist_dir),
                "--live-reload",
                "false",
            ],
            cwd=WORKER_ROOT,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
        )
    details: WorkerProcess = {
        "process": process,
        "port": port,
        "persist_dir": persist_dir,
        "stdout": stdout,
        "stderr": stderr,
    }
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _terminate_worker_group(process)
            message = (
                f"pywrangler exited early; stdout: {stdout.read_text()}; "
                f"stderr: {stderr.read_text()}"
            )
            _cleanup_worker_persistence(persist_dir)
            pytest.fail(message)
        try:
            status, body = request(port, "/health")
            if status == 200 and body == "ready":
                break
        except (OSError, TimeoutError):
            pass
        time.sleep(0.25)
    else:
        _terminate_worker_group(process)
        _cleanup_worker_persistence(persist_dir)
        pytest.fail(f"Worker readiness timed out; stderr: {stderr.read_text()}")

    try:
        yield details
    finally:
        _terminate_worker_group(process)
        _cleanup_worker_persistence(persist_dir)
