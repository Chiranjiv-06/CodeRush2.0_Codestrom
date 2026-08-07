"""Ephemeral sandbox execution.

Backends
--------
docker      Real isolation: per-job container, ``--network none``, read-only
            root, memory/pids caps, non-root user, auto-removed on exit.
subprocess  Portable fallback for machines without Docker. Enforces a private
            working directory, wall-clock timeout, output caps and a scrubbed
            environment. Used automatically when the Docker daemon is absent.

Contract for service code (all runtimes):
    stdin + ``input.json``   -> the job payload
    stdout                   -> JSON result (or ``output.json``)
    ``artifacts/``           -> files captured, hashed and stored
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from ..integrity import build_manifest, sha256_hex
from ..metering import Usage

log = logging.getLogger("m2x.sandbox")

RUNTIME_FILES = {"python": "main.py", "bash": "main.sh", "node": "main.js"}
DOCKER_IMAGES = {
    "python": lambda: settings.sandbox_image_python,
    "node": lambda: settings.sandbox_image_node,
    "bash": lambda: settings.sandbox_image_python,
}


@dataclass
class ExecutionResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    result: dict | None
    usage: Usage
    artifacts: dict[str, bytes] = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    backend: str = "subprocess"
    workspace: str = ""
    timed_out: bool = False
    error: str = ""
    # False for failures a retry cannot fix — a rejected input, an exhausted
    # quota, an unconfigured provider. Sandbox runs are always retryable.
    retryable: bool = True


def _docker_available() -> bool:
    if settings.sandbox_backend == "subprocess":
        return False
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        proc = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, timeout=8)
        return proc.returncode == 0
    except Exception:
        return False


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} bytes]"


def _parse_result(stdout: str, workspace: Path) -> dict | None:
    out_file = workspace / "output.json"
    if out_file.exists():
        try:
            return json.loads(out_file.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            pass
    stripped = stdout.strip()
    if not stripped:
        return None
    # last JSON object printed wins (lets services log freely before the result)
    for chunk in reversed(stripped.splitlines()):
        chunk = chunk.strip()
        if chunk.startswith(("{", "[")):
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _collect_artifacts(workspace: Path, cap: int) -> dict[str, bytes]:
    art_dir = workspace / "artifacts"
    files: dict[str, bytes] = {}
    if not art_dir.is_dir():
        return files
    budget = cap
    for path in sorted(art_dir.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if len(data) > budget:
            data = data[:budget]
        budget -= len(data)
        files[str(path.relative_to(art_dir)).replace("\\", "/")] = data
        if budget <= 0:
            break
    return files


def _scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment — no host secrets leak into service code."""
    keep = ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"HOME": ".", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                "NODE_OPTIONS": "--max-old-space-size=256"})
    env.update(extra or {})
    return env


class SandboxRunner:
    def __init__(self) -> None:
        self._docker: bool | None = None

    @property
    def backend(self) -> str:
        if self._docker is None:
            self._docker = _docker_available()
        return "docker" if self._docker else "subprocess"

    def refresh_backend(self) -> str:
        self._docker = None
        return self.backend

    # ------------------------------------------------------------------ #
    def prepare_workspace(self, job_id: str, code: str, runtime: str, payload: dict) -> Path:
        root = settings.workspace_dir / job_id
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        (root / "artifacts").mkdir(parents=True, exist_ok=True)
        (root / RUNTIME_FILES.get(runtime, "main.py")).write_text(code, encoding="utf-8")
        (root / "input.json").write_text(json.dumps(payload, default=str), encoding="utf-8")
        return root

    def run(
        self, *, job_id: str, runtime: str, code: str, payload: dict,
        timeout_seconds: int | None = None, memory_mb: int | None = None,
        network: bool = False,
    ) -> ExecutionResult:
        timeout = min(timeout_seconds or settings.sandbox_timeout_seconds,
                      settings.sandbox_timeout_seconds * 10)
        memory = memory_mb or settings.sandbox_max_memory_mb
        workspace = self.prepare_workspace(job_id, code, runtime, payload)
        started = time.perf_counter()
        if self.backend == "docker":
            proc_out = self._run_docker(workspace, runtime, payload, timeout, memory, network)
        else:
            proc_out = self._run_subprocess(workspace, runtime, payload, timeout)
        wall_ms = int((time.perf_counter() - started) * 1000)

        stdout, stderr, code_exit, timed_out, cpu_ms = proc_out
        cap = settings.sandbox_max_output_bytes
        stdout, stderr = _truncate(stdout, cap), _truncate(stderr, cap // 4)
        result = _parse_result(stdout, workspace)
        artifacts = _collect_artifacts(workspace, cap)
        egress = len(stdout.encode()) + sum(len(v) for v in artifacts.values())

        usage = Usage(
            cpu_ms=cpu_ms if cpu_ms else wall_ms,
            wall_ms=wall_ms,
            peak_memory_mb=float(memory),
            egress_bytes=egress,
            invocations=1,
            exit_code=code_exit,
        )
        manifest = build_manifest({**artifacts, "stdout.txt": stdout.encode()})
        return ExecutionResult(
            ok=(code_exit == 0 and not timed_out),
            exit_code=code_exit,
            stdout=stdout,
            stderr=stderr,
            result=result,
            usage=usage,
            artifacts=artifacts,
            manifest=manifest,
            backend=self.backend,
            workspace=str(workspace),
            timed_out=timed_out,
            error="timeout" if timed_out else ("" if code_exit == 0 else f"exit code {code_exit}"),
        )

    # ------------------------------------------------------------------ #
    def _run_subprocess(self, workspace: Path, runtime: str, payload: dict, timeout: int):
        cmd = self._local_command(runtime, workspace)
        if cmd is None:
            return ("", f"runtime '{runtime}' is not available on this host", 127, False, 0)
        cpu_before = _child_cpu_ms()
        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace,
                input=json.dumps(payload, default=str),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=_scrubbed_env(),
            )
            cpu_ms = max(_child_cpu_ms() - cpu_before, 0)
            return (proc.stdout or "", proc.stderr or "", proc.returncode, False, cpu_ms)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = (exc.stderr or "") + f"\nkilled after {timeout}s"
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            return (out, err, 124, True, timeout * 1000)
        except FileNotFoundError as exc:
            return ("", f"runtime binary missing: {exc}", 127, False, 0)

    def _local_command(self, runtime: str, workspace: Path) -> list[str] | None:
        if runtime == "python":
            return [sys.executable, "-I", "-B", str(workspace / "main.py")]
        if runtime == "node":
            node = shutil.which("node")
            return [node, str(workspace / "main.js")] if node else None
        if runtime == "bash":
            bash = shutil.which("bash")
            if bash:
                return [bash, str(workspace / "main.sh")]
            return None
        return None

    def _run_docker(self, workspace: Path, runtime: str, payload: dict, timeout: int,
                    memory_mb: int, network: bool):  # pragma: no cover - needs daemon
        image = DOCKER_IMAGES.get(runtime, DOCKER_IMAGES["python"])()
        inner = {
            "python": "python /work/main.py",
            "node": "node /work/main.js",
            "bash": "sh /work/main.sh",
        }[runtime]
        cmd = [
            "docker", "run", "--rm", "-i",
            "--network", "bridge" if network else settings.sandbox_network,
            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
            "--cpus", "1", "--pids-limit", "128",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "-v", f"{workspace}:/work:rw",
            "-w", "/work",
            "-e", "PYTHONUNBUFFERED=1", "-e", "HOME=/tmp",
            "--label", "m2x.worker=1",
            image, "sh", "-c", inner,
        ]
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, input=json.dumps(payload, default=str), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout + 20,
            )
            cpu_ms = int((time.perf_counter() - started) * 1000)
            return (proc.stdout or "", proc.stderr or "", proc.returncode, False, cpu_ms)
        except subprocess.TimeoutExpired:
            return ("", f"container killed after {timeout}s", 124, True, timeout * 1000)


def _child_cpu_ms() -> int:
    """Cumulative CPU consumed by child processes, where the OS exposes it."""
    try:
        import resource  # POSIX only

        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        return int((ru.ru_utime + ru.ru_stime) * 1000)
    except Exception:
        return 0


def cleanup_workspace(job_id: str) -> bool:
    path = settings.workspace_dir / job_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        return True
    return False


def reap_orphan_containers() -> int:  # pragma: no cover - needs daemon
    """Kill labelled containers that outlived their job."""
    if not shutil.which("docker"):
        return 0
    try:
        out = subprocess.run(
            ["docker", "ps", "-q", "--filter", "label=m2x.worker=1"],
            capture_output=True, text=True, timeout=10,
        )
        ids = [i for i in out.stdout.split() if i]
        for cid in ids:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=15)
        return len(ids)
    except Exception:
        return 0


def sweep_stale_workspaces(max_age_seconds: int) -> int:
    removed = 0
    now = time.time()
    for path in settings.workspace_dir.glob("*"):
        try:
            if path.is_dir() and now - path.stat().st_mtime > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


runner = SandboxRunner()
