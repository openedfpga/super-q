"""Persistent `quartus_sh` process you can dispatch TCL into.

Quartus's startup is 6–12 seconds of cold Python and JVM time before the
first TCL command can execute. For tight edit-compile-test loops that's
the dominant cost. A WarmShell keeps `quartus_sh -t warm_shell.tcl`
running and multiplexes requests over its stdin/stdout.

Latency once warm:
  * trivial TCL command:      ~5 ms
  * open project + STA only:  ~2 s
  * incremental compile:     10–40 s (RTL change dependent)

Thread-safety: each shell owns a lock; call sites serialize per shell.
A `ShellPool` multiplies capacity across several warm processes.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from super_q.config import quartus_install
from super_q.quartus import QuartusError, tcl_dir

log = logging.getLogger("superq.warm")


_READY_MARK = "<<<SUPERQ-WARM-READY>>>"
_BEGIN_RE = re.compile(r"^<<<BEGIN (\S+)>>>\s*$")
_END_RE = re.compile(r"^<<<END (\S+) (OK|ERR)>>>\s*$")


class WarmShellError(Exception):
    pass


@dataclass
class ShellResult:
    id: str
    ok: bool
    output: str
    duration_s: float


class WarmShell:
    """One `quartus_sh` instance kept alive across many requests.

    Construct with the project's Quartus directory; it becomes the CWD
    of the subprocess so relative paths in any .qsf behave. Call
    `run_tcl(...)` repeatedly. Call `close()` when done (or use as a
    context manager).
    """

    def __init__(self, *, cwd: Path, startup_timeout_s: float = 60.0,
                 tcl_file: str = "warm_shell.tcl") -> None:
        q = quartus_install()
        if not q.is_installed or q.sh is None:
            raise QuartusError("warm shell requires Quartus; install it first")

        tcl_path = tcl_dir() / tcl_file
        if not tcl_path.exists():
            raise WarmShellError(f"missing warm shell tcl: {tcl_path}")

        env = os.environ.copy()
        # Line-buffered behavior in Quartus is fragile; force unbuffered I/O.
        env["PYTHONUNBUFFERED"] = "1"

        self._cwd = cwd
        self._proc = subprocess.Popen(
            [str(q.sh), "-t", str(tcl_path)],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        self._lock = threading.Lock()
        self._closed = False
        self._pending: dict[str, queue.Queue[ShellResult]] = {}
        self._reader = threading.Thread(
            target=self._read_loop, name="warm-shell-reader", daemon=True
        )
        self._reader.start()

        self._wait_for_ready(startup_timeout_s)

    # ------------------------------------------------------------------ #

    def run_tcl(self, body: str, *, timeout_s: float = 60 * 60) -> ShellResult:
        """Execute a TCL snippet in the warm shell and wait for its result."""
        if self._closed:
            raise WarmShellError("shell is closed")
        if "\n" in body:
            # The protocol is newline-framed on input; collapse into a single
            # `eval { … }` so multi-line bodies round-trip cleanly.
            body = "eval { " + body.replace("\n", "; ") + " }"
        req_id = uuid.uuid4().hex[:10]
        reply: queue.Queue[ShellResult] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[req_id] = reply
            assert self._proc.stdin is not None
            self._proc.stdin.write(f"{req_id}\t{body}\n")
            self._proc.stdin.flush()
        try:
            return reply.get(timeout=timeout_s)
        except queue.Empty as e:
            raise WarmShellError(f"tcl timeout after {timeout_s}s") from e

    def ping(self) -> bool:
        try:
            r = self.run_tcl("__SUPERQ_PING__", timeout_s=5)
            return r.ok
        except WarmShellError:
            return False

    # Convenience wrappers that map to common flows -----------------------

    def open_project(self, name: str) -> ShellResult:
        return self.run_tcl(
            f"if {{[::project_exists {name}]}} {{ project_open {name} -revision {name} -force }}"
        )

    def close_project(self) -> ShellResult:
        return self.run_tcl("project_close")

    def incremental_compile(self, seed: int = 1) -> ShellResult:
        tcl = (
            f"set ::env(SUPER_Q_SEED) {seed}; "
            f"source [file join [file dirname [info script]] incremental_build.tcl]"
        )
        return self.run_tcl(tcl)

    def sta_only(self) -> ShellResult:
        return self.run_tcl("execute_module -tool sta; superq::dump_timing_json [file join [pwd] output_files timing.json]")

    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None:
                try:
                    self.run_tcl("__SUPERQ_QUIT__", timeout_s=5)
                except WarmShellError:
                    pass
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception:
            log.exception("error shutting down warm shell")

    def __enter__(self) -> "WarmShell":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _wait_for_ready(self, timeout_s: float) -> None:
        """Block until the warm shell prints its ready banner."""
        start = time.time()
        while time.time() - start < timeout_s:
            if self._ready.wait(timeout=0.5):
                return
            if self._proc.poll() is not None:
                raise WarmShellError(
                    f"warm shell exited with code {self._proc.returncode} before ready"
                )
        raise WarmShellError(f"warm shell not ready after {timeout_s}s")

    _ready: threading.Event

    def _read_loop(self) -> None:
        self._ready = threading.Event()
        assert self._proc.stdout is not None
        current_id: str | None = None
        current_start: float = 0.0
        buf: list[str] = []

        for line in self._proc.stdout:
            if not self._ready.is_set() and _READY_MARK in line:
                self._ready.set()
                continue

            m = _BEGIN_RE.match(line)
            if m:
                current_id = m.group(1)
                current_start = time.time()
                buf = []
                continue

            m = _END_RE.match(line)
            if m:
                rid, status = m.group(1), m.group(2)
                output = "".join(buf).rstrip("\n")
                result = ShellResult(
                    id=rid, ok=(status == "OK"), output=output,
                    duration_s=time.time() - current_start,
                )
                with self._lock:
                    replyq = self._pending.pop(rid, None)
                if replyq is not None:
                    replyq.put(result)
                current_id = None
                buf = []
                continue

            if current_id is not None:
                buf.append(line)
            else:
                # Lines outside frames are Quartus chatter (warnings, banner
                # lines). Log them at DEBUG so they're available under -v.
                log.debug("warm[%s]: %s", os.getpid(), line.rstrip())

        # stdout closed: mark everyone as failed
        with self._lock:
            for rid, replyq in list(self._pending.items()):
                replyq.put(ShellResult(
                    id=rid, ok=False,
                    output="warm shell exited before responding",
                    duration_s=0.0,
                ))
            self._pending.clear()


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class ShellPool:
    """A fixed pool of WarmShells, each bound to a specific Quartus dir.

    The pool exists so a single daemon can serve several cores concurrently
    without paying startup cost on every build. Shells are ejected (and
    respawned on next use) if they stop answering pings.
    """

    def __init__(self, *, size: int = 2, startup_timeout_s: float = 60.0) -> None:
        self._size = size
        self._startup = startup_timeout_s
        self._shells: dict[Path, list[WarmShell]] = {}
        self._lock = threading.Lock()

    def acquire(self, cwd: Path) -> WarmShell:
        """Get a warm shell for `cwd`, spawning one if needed."""
        cwd = cwd.resolve()
        with self._lock:
            bucket = self._shells.setdefault(cwd, [])
            while bucket:
                shell = bucket.pop()
                if shell.ping():
                    return shell
                try:
                    shell.close()
                except Exception:
                    pass
            return WarmShell(cwd=cwd, startup_timeout_s=self._startup)

    def release(self, shell: WarmShell, cwd: Path) -> None:
        cwd = cwd.resolve()
        with self._lock:
            bucket = self._shells.setdefault(cwd, [])
            if len(bucket) >= self._size:
                shell.close()
                return
            bucket.append(shell)

    def shutdown(self) -> None:
        with self._lock:
            for bucket in self._shells.values():
                for s in bucket:
                    try:
                        s.close()
                    except Exception:
                        pass
            self._shells.clear()


def can_warm_shell() -> bool:
    """Cheap check agents can call before requesting warm-shell paths."""
    q = quartus_install()
    if not q.is_installed:
        return False
    if not (tcl_dir() / "warm_shell.tcl").exists():
        return False
    return shutil.which(str(q.sh)) is not None or Path(str(q.sh)).exists()
