"""Command-line interface — the thing agents actually call.

Design goals, in order:
  1. A new agent should be able to run a useful build after reading only
     `--help` on the root command.
  2. Every command has `--json` so output is trivially parseable.
  3. Exit codes are meaningful:
        0   success (timing met / healthy)
        1   user error (bad args, no core found)
        2   toolchain problem (Quartus missing, bad install)
        3   build ran but timing failed
        4   job cancelled
  4. Commands never prompt interactively; agents shouldn't get stuck.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from super_q import quartus
from super_q.backends import get_backend
from super_q.config import banner, host_capacity, paths, quartus_install
from super_q.db import Store
from super_q.pool_config import PoolConfigError, resolve_backend
from super_q.progress import make_progress
from super_q.project import CoreDetectionError, PocketCore, detect_core, find_cores
from super_q.scheduler import Scheduler, batch_run
from super_q.seeds import SeedPlan, rank

app = typer.Typer(
    name="superq",
    help=f"{banner()}\n\nUltra-fast distributed Quartus build for Analogue Pocket cores.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

console = Console()
err_console = Console(stderr=True)


@app.callback()
def _root(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable debug logging"),
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Make `superq` alias-safe for agents that used the old hyphenated name.
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# info / verify
# ---------------------------------------------------------------------------

@app.command("info")
def info_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show host + Quartus info so agents can sanity-check the environment."""
    q = quartus_install()
    h = host_capacity()
    p = paths()
    data = {
        "version": _pkg_version(),
        "quartus": {
            "installed": q.is_installed,
            "version": q.version,
            "root": str(q.root) if q.root else None,
            "bin_dir": str(q.bin_dir) if q.bin_dir else None,
        },
        "host": {
            "platform": h.platform_name,
            "cpus": h.cpu_count,
            "mem_gb": round(h.mem_gb, 1),
            "suggested_parallel": h.quartus_parallel,
        },
        "paths": {
            "state_dir": str(p.state_dir),
            "cache_dir": str(p.cache_dir),
            "db": str(p.db_path),
        },
        "issues": quartus.verify_toolchain(),
    }
    if as_json:
        _echo_json(data)
    else:
        _pretty_info(data)


@app.command("verify")
def verify_cmd(
    path: Path = typer.Argument(..., exists=True),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Validate that a folder looks like a Pocket core and is buildable."""
    findings: dict[str, Any] = {"path": str(path), "ok": True, "warnings": [], "errors": []}
    try:
        core = detect_core(path)
        findings["core"] = core.as_dict()
    except CoreDetectionError as e:
        findings["ok"] = False
        findings["errors"].append(str(e))
        _emit(findings, as_json)
        raise typer.Exit(code=1)

    if core.qsf is None:
        findings["warnings"].append("No .qsf found; using .qpf defaults")
    if not core.sdc_files:
        findings["warnings"].append("No .sdc found; timing constraints may be missing")
    if core.device.upper() != "5CEBA4F23C8" and "5CEBA4" not in core.device.upper():
        findings["warnings"].append(f"device {core.device} is not the Pocket Cyclone V")

    findings["toolchain"] = quartus.verify_toolchain()
    if findings["toolchain"]:
        findings["warnings"].extend(findings["toolchain"])

    _emit(findings, as_json)


# ---------------------------------------------------------------------------
# build / sweep
# ---------------------------------------------------------------------------

@app.command("build")
def build_cmd(
    path: Path = typer.Argument(..., exists=True, help="Core directory"),
    seed: int = typer.Option(1, "--seed", help="Fitter seed to use"),
    threads: int = typer.Option(0, "--threads", help="Quartus threads (0=auto)"),
    backend: str = typer.Option("local", "--backend", help="local|docker|aws"),
    as_json: bool = typer.Option(False, "--json"),
    timeout: int = typer.Option(3600, "--timeout", help="Seconds per build"),
) -> None:
    """Build a single seed (single revision). Useful for smoke-checks."""
    core = _detect(path)
    plan = SeedPlan(seeds=[seed], stop_on_first_pass=True, max_parallel=1)
    outcome = _run_sweep(core, plan, backend, as_json,
                        threads=threads, timeout_s=timeout)
    _exit_for_outcome(outcome)


@app.command("sweep")
def sweep_cmd(
    path: Path = typer.Argument(..., exists=True),
    min_seed: int = typer.Option(1, "--min", "--min-seed"),
    max_seed: int = typer.Option(16, "--max", "--max-seed"),
    count: int = typer.Option(0, "--count", help="Sample N random seeds instead of range"),
    strategy: str = typer.Option(
        "range",
        "--strategy",
        help="range|random|spaced — how to pick seeds",
    ),
    rng_seed: int = typer.Option(0, "--rng-seed", help="RNG seed for reproducible 'random' strategy"),
    parallel: int = typer.Option(4, "--parallel", help="Concurrent seeds"),
    threads: int = typer.Option(2, "--threads", help="Quartus threads per seed"),
    stop_on_pass: bool = typer.Option(
        True,
        "--stop-on-pass/--no-stop-on-pass",
        help="Early-exit on first passing seed",
    ),
    mode: str = typer.Option(
        "full",
        "--mode",
        help="full | split-fit (share synthesis across seeds)",
    ),
    backend: str = typer.Option("local", "--backend"),
    target_slack: float = typer.Option(0.0, "--target-slack-ns"),
    target_fmax: float | None = typer.Option(None, "--target-fmax-mhz"),
    as_json: bool = typer.Option(False, "--json"),
    timeout: int = typer.Option(3600, "--timeout"),
) -> None:
    """Explore seeds in parallel and keep the best bitstream.

    Examples:
      superq sweep . --min=1 --max=32 --parallel=8
      superq sweep . --strategy=random --count=24 --rng-seed=42
      superq sweep . --mode=split-fit --parallel=8    (synth once, fit 8x)
    """
    core = _detect(path)
    plan = _make_plan(min_seed, max_seed, count, strategy, rng_seed,
                     parallel, stop_on_pass, target_slack, target_fmax)
    outcome = _run_sweep(core, plan, backend, as_json, mode=mode,
                        threads=threads, timeout_s=timeout)
    _exit_for_outcome(outcome)


@app.command("batch")
def batch_cmd(
    paths_arg: list[Path] = typer.Argument(..., help="One or more core dirs or parent dirs"),
    min_seed: int = typer.Option(1, "--min"),
    max_seed: int = typer.Option(8, "--max"),
    parallel: int = typer.Option(4, "--parallel"),
    parallel_cores: int = typer.Option(2, "--parallel-cores"),
    backend: str = typer.Option("local", "--backend"),
    mode: str = typer.Option("full", "--mode"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run seed sweeps across many Pocket cores, in parallel.

    Pass either specific core roots or a parent directory — we auto-detect
    every .qpf underneath. Designed for CI ("every core at every merge")
    and for bulk seed-exploration sessions.
    """
    cores = find_cores([*paths_arg])
    if not cores:
        err_console.print("[red]no Pocket cores found[/red]")
        raise typer.Exit(1)

    store = Store(paths().db_path)
    be = _mk_backend(backend)

    def factory(_c: PocketCore) -> SeedPlan:
        return SeedPlan.range(
            start=min_seed, end=max_seed,
            max_parallel=parallel, stop_on_first_pass=True,
        )

    if as_json:
        outcomes = batch_run(store, be, cores, factory, parallel_cores=parallel_cores, mode=mode)
        _echo_json({"cores": [o.as_dict() for o in outcomes]})
        raise typer.Exit(0)

    console.print(f"batch: {len(cores)} cores · {parallel_cores}x core-parallel · {parallel}x seed-parallel")
    outcomes = batch_run(store, be, cores, factory, parallel_cores=parallel_cores, mode=mode)
    _print_batch_table(outcomes)
    passed = sum(1 for o in outcomes if o.best is not None)
    raise typer.Exit(0 if passed == len(outcomes) else 3)


# ---------------------------------------------------------------------------
# inspection / status
# ---------------------------------------------------------------------------

@app.command("status")
def status_cmd(
    job_id: str = typer.Argument("", help="Specific job id, or empty for list"),
    limit: int = typer.Option(20, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent jobs (or one job's task table)."""
    store = Store(paths().db_path)
    if job_id:
        job = store.get_job(job_id)
        if job is None:
            err_console.print(f"[red]no job {job_id}[/red]")
            raise typer.Exit(1)
        tasks = store.list_tasks(job_id)
        payload = {"job": job, "tasks": tasks}
        if as_json:
            _echo_json(payload)
        else:
            _print_job(job, tasks)
        return

    jobs = store.list_jobs(limit=limit)
    if as_json:
        _echo_json({"jobs": jobs})
    else:
        _print_jobs(jobs)


@app.command("watch")
def watch_cmd(
    job_id: str = typer.Argument(..., help="Job id to watch"),
    interval: float = typer.Option(1.0, "--interval"),
) -> None:
    """Tail an in-flight job. Ctrl-C to stop."""
    import time as _t
    store = Store(paths().db_path)
    while True:
        job = store.get_job(job_id)
        if job is None:
            err_console.print(f"[red]no job {job_id}[/red]")
            raise typer.Exit(1)
        tasks = store.list_tasks(job_id)
        console.clear()
        _print_job(job, tasks)
        if job["status"] in ("passed", "failed", "cancelled"):
            return
        _t.sleep(interval)


@app.command("cancel")
def cancel_cmd(job_id: str) -> None:
    """Cancel a running job."""
    store = Store(paths().db_path)
    n = store.cancel_job(job_id)
    console.print(f"cancelled {n} row(s)")
    raise typer.Exit(0 if n else 1)


@app.command("workers")
def workers_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """List live workers currently heartbeating to this state DB."""
    store = Store(paths().db_path)
    workers = store.live_workers()
    if as_json:
        _echo_json({"workers": workers})
        return
    t = Table(title=f"workers ({len(workers)} live)")
    for c in ("id", "host", "backend", "slots", "heartbeat"):
        t.add_column(c)
    import time as _t
    now = _t.time()
    for w in workers:
        t.add_row(
            w["id"], w["host"], w["backend"], str(w["slots"]),
            f"{now - w['heartbeat_at']:.0f}s ago",
        )
    console.print(t)


@app.command("inspect")
def inspect_cmd(
    path: Path = typer.Argument(..., exists=True),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Re-run STA on the current compile and emit timing.

    No rebuild. Useful after a manual Quartus GUI run to get structured
    timing into the same JSON shape the sweep uses.
    """
    core = _detect(path)
    work = paths().cache_dir / "inspect" / core.full_name
    work.mkdir(parents=True, exist_ok=True)
    try:
        from super_q.timing import merge_reports, parse_sta_report, parse_timing_json
        out = core.output_dir
        sta_rpt = out / f"{core.project_name}.sta.rpt"
        report = merge_reports(parse_sta_report(sta_rpt), parse_timing_json(out / "timing.json"))
        _emit({"core": core.as_dict(), "timing": report.as_dict()}, as_json)
    except Exception as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)


@app.command("clean")
def clean_cmd(
    path: Path = typer.Argument(..., exists=True),
    include_cache: bool = typer.Option(False, "--cache", help="Also wipe ~/.superq cache"),
) -> None:
    """Remove .superq/ build sandbox + artifacts from a core."""
    import shutil
    core = _detect(path)
    if core.superq_dir.exists():
        shutil.rmtree(core.superq_dir)
        console.print(f"removed {core.superq_dir}")
    if include_cache:
        p = paths()
        for d in (p.cache_dir / "work", p.cache_dir / "synth"):
            if d.exists():
                shutil.rmtree(d)
                console.print(f"removed {d}")


# ---------------------------------------------------------------------------
# MCP + install bootstraps
# ---------------------------------------------------------------------------

@app.command("mcp")
def mcp_cmd() -> None:
    """Start the MCP server over stdio. Pipe this into a Claude agent."""
    try:
        from super_q.mcp_server import main as mcp_main
    except ImportError:
        err_console.print(
            "[red]mcp dependency missing[/red]. install with "
            "[green]pip install super-q\\[mcp][/green]"
        )
        raise typer.Exit(2)
    mcp_main()


@app.command("install-quartus")
def install_quartus_cmd(
    version: str = typer.Option("24.1", "--version"),
    accept_eula: bool = typer.Option(
        False, "--accept-eula",
        help="Attests you've read Intel's license at "
             "https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html",
    ),
    dry_run: bool = typer.Option(False, "--dry-run",
                                  help="Print the installer path + exit"),
) -> None:
    """Download + install Quartus Lite into `/opt/intelFPGA_lite/<ver>/`.

    Runs the bundled, idempotent installer — a no-op if the target is
    already populated (i.e. via `actions/cache`). Designed to be called
    from CI and from local workstations alike.
    """
    import os
    import subprocess

    # The script ships inside the wheel at super_q/_resources/install_quartus.sh.
    # We locate it via importlib so it works regardless of install method.
    installer = _locate_bundled_installer()

    if dry_run:
        console.print(f"installer: {installer}")
        raise typer.Exit(0)

    if not accept_eula:
        err_console.print(
            "[red]must pass --accept-eula[/red] — read Intel's license first at:\n"
            "  https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html"
        )
        raise typer.Exit(1)

    env = os.environ.copy()
    env["SUPERQ_ACCEPT_EULA"] = "1"
    env["QUARTUS_VERSION"] = version
    rc = subprocess.call(["bash", str(installer), version], env=env)
    raise typer.Exit(rc)


def _locate_bundled_installer() -> Path:
    """Find `install_quartus.sh` inside the installed package."""
    # Package-installed (via pip/wheel): super_q/_resources/install_quartus.sh
    pkg_path = Path(__file__).resolve().parent / "_resources" / "install_quartus.sh"
    if pkg_path.exists():
        return pkg_path
    # Editable checkout: fall back to repo-root docker/install-quartus.sh
    repo_path = Path(__file__).resolve().parent.parent.parent / "docker" / "install-quartus.sh"
    if repo_path.exists():
        return repo_path
    raise RuntimeError(
        "install-quartus.sh not found alongside super-q. "
        "Reinstall with `pip install --force-reinstall super-q`."
    )


# ---------------------------------------------------------------------------
# iterative: watch, explore, daemon
# ---------------------------------------------------------------------------

@app.command("watch-build")
def watch_build_cmd(
    path: Path = typer.Argument(..., exists=True),
    seed: int = typer.Option(1, "--seed"),
    debounce_ms: int = typer.Option(500, "--debounce-ms"),
    as_json: bool = typer.Option(False, "--json"),
    warm: bool = typer.Option(True, "--warm/--no-warm", help="Keep quartus_sh alive between rebuilds"),
) -> None:
    """Incrementally rebuild on every saved source change. Ctrl-C to stop."""
    from super_q.progress import make_progress
    from super_q.watch import WatchLoop
    core = _detect(path)
    _, handler = make_progress(total=1, json_mode=as_json,
                              title=f"watching {core.full_name}")
    loop = WatchLoop(core, seed=seed, debounce_ms=debounce_ms,
                    use_warm_shell=warm, on_event=handler)
    if not as_json:
        console.print(
            f"[bold]watching[/bold] {core.root}\n"
            f"  initial build starting; save a file to trigger more\n"
            f"  press Ctrl-C to stop\n"
        )
    loop.run()


@app.command("explore")
def explore_cmd(
    path: Path = typer.Argument(..., exists=True),
    budget: str = typer.Option("30m", "--budget", help="Time budget (e.g. 30m, 2h)"),
    parallel: int = typer.Option(4, "--parallel"),
    threads: int = typer.Option(2, "--threads"),
    pool: str = typer.Option("", "--pool", help="Named pool from config.toml, or 'local'/'modal'/etc"),
    backend: str = typer.Option("", "--backend", help="Explicit backend (overrides --pool)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Adaptive seed exploration with a wall-clock budget.

    Escalates through rungs (quick → wider → random → high-effort → retime)
    until a passing seed is found or the budget runs out. Good when you
    don't care how it meets timing, just that it does.
    """
    from super_q.explore import explore as run_explore
    from super_q.progress import make_progress

    core = _detect(path)
    be = _mk_backend_smart(backend, pool)
    store = Store(paths().db_path)
    budget_s = _parse_duration(budget)

    progress, handler = make_progress(
        total=0, json_mode=as_json,
        title=f"explore {core.full_name} · budget={budget}",
    )
    with progress:
        out = run_explore(store, be, core,
                          budget_s=budget_s,
                          parallel=parallel,
                          threads_per_task=threads,
                          on_event=handler)

    if as_json:
        _echo_json(out.as_dict())
    else:
        _print_explore_result(out)
    raise typer.Exit(0 if out.best else (4 if out.timed_out else 3))


daemon_app = typer.Typer(no_args_is_help=True, help="Long-lived local dispatcher (warm Quartus, fast RPC).")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("start")
def daemon_start(
    pool: str = typer.Option("", "--pool"),
    parallel: int = typer.Option(4, "--parallel"),
    foreground: bool = typer.Option(False, "--fg", help="Run in foreground instead of daemonizing"),
) -> None:
    """Start the super-q daemon. Uses `pool` (or the default) as its backend."""
    from super_q.daemon import is_running, serve
    if is_running():
        console.print("[yellow]daemon already running[/yellow]")
        raise typer.Exit(0)
    if foreground:
        serve(pool_name=pool or None, parallel=parallel)
        return

    # Simple double-fork to detach. We don't attempt log rotation; the
    # daemon logs to stderr and users redirect as they please.
    import sys as _s
    pid = os.fork()
    if pid > 0:
        console.print(f"daemon started (pid {pid})")
        return
    os.setsid()
    pid = os.fork()
    if pid > 0:
        _s._exit(0)
    serve(pool_name=pool or None, parallel=parallel)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Ask the running daemon to shut down."""
    from super_q.daemon import Client, is_running
    if not is_running():
        console.print("daemon not running")
        raise typer.Exit(0)
    with Client() as c:
        c.call("shutdown")
    console.print("shutdown requested")


@daemon_app.command("ping")
def daemon_ping(as_json: bool = typer.Option(False, "--json")) -> None:
    """Cheap health check — exits 0 iff the daemon answers."""
    from super_q.daemon import Client, is_running
    if not is_running():
        if as_json:
            _echo_json({"alive": False})
        else:
            console.print("[red]daemon not running[/red]")
        raise typer.Exit(1)
    with Client() as c:
        info = c.call("info")
    if as_json:
        _echo_json(info)
    else:
        console.print(f"[green]alive[/green] · pool={info.get('pool') or 'default'} · uptime {info.get('uptime_s', 0):.0f}s")


# ---------------------------------------------------------------------------
# remote pool management
# ---------------------------------------------------------------------------

remote_app = typer.Typer(no_args_is_help=True, help="Manage remote worker pools (Modal/Fly/SSH/GHA).")
app.add_typer(remote_app, name="remote")


@remote_app.command("init")
def remote_init() -> None:
    """Write a starter ~/.superq/config.toml if one doesn't exist."""
    from super_q.pool_config import write_example
    p = write_example()
    console.print(f"config at [bold]{p}[/bold] — edit, then `superq remote show`")


@remote_app.command("show")
def remote_show(as_json: bool = typer.Option(False, "--json")) -> None:
    """List named pools and the current default."""
    from super_q.pool_config import describe
    d = describe()
    if as_json:
        _echo_json(d)
        return
    console.print(f"config: [dim]{d['config_path']}[/dim]")
    console.print(f"default: [bold]{d['default_pool'] or '(local)'}[/bold]")
    if not d["pools"]:
        console.print("[yellow]no pools defined — run `superq remote init`[/yellow]")
        return
    t = Table(title="remote pools")
    for c in ("name", "kind", "max_parallel"):
        t.add_column(c)
    for p in d["pools"]:
        t.add_row(p["name"], p["kind"], str(p["max_parallel"]))
    console.print(t)


@remote_app.command("test")
def remote_test(pool: str = typer.Argument(..., help="Pool name")) -> None:
    """Construct a backend for a pool and print its describe() dict."""
    be = _mk_backend_smart("", pool)
    _echo_json(be.describe())


# ---------------------------------------------------------------------------
# CI wrapper — tuned defaults for CI runners
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Modal helpers — guided setup + smoke test + bench
# ---------------------------------------------------------------------------

modal_app_cli = typer.Typer(no_args_is_help=True, help="Set up and test the Modal backend.")
app.add_typer(modal_app_cli, name="modal")


@modal_app_cli.command("check")
def modal_check(as_json: bool = typer.Option(False, "--json")) -> None:
    """Report what's ready and what's missing for a Modal-backed build."""
    findings: dict[str, Any] = {
        "sdk_installed": False,
        "token_present": False,
        "app_importable": False,
        "app_deployed": False,
        "volume_has_quartus": None,
        "next_steps": [],
    }

    try:
        import modal  # noqa: F401
        findings["sdk_installed"] = True
    except ImportError:
        findings["next_steps"].append("pip install 'super-q[modal]'")
        _emit_modal_check(findings, as_json)
        raise typer.Exit(2)

    # Token lives at ~/.modal.toml by default, or via MODAL_TOKEN_ID env.
    from pathlib import Path as _P
    token_file = _P.home() / ".modal.toml"
    findings["token_present"] = token_file.exists() or bool(os.environ.get("MODAL_TOKEN_ID"))
    if not findings["token_present"]:
        findings["next_steps"].append("modal token new")

    try:
        from super_q import modal_app as _ma
        findings["app_importable"] = _ma.modal is not None
        findings["app_name"] = getattr(getattr(_ma, "app", None), "name", None)
    except Exception as e:
        findings["app_importable"] = False
        findings["import_error"] = str(e)

    if findings["sdk_installed"] and findings["token_present"] and findings["app_importable"]:
        try:
            import modal
            _ = modal.Function.from_name("super-q", "smoke_test")
            findings["app_deployed"] = True
        except Exception as e:
            findings["app_deployed"] = False
            findings["lookup_error"] = str(e)
            findings["next_steps"].append("modal deploy super_q.modal_app")

    if findings["app_deployed"]:
        findings["next_steps"].append("superq modal smoke        # verify image + round-trip")
        findings["next_steps"].append(
            "modal run super_q.modal_app::install_quartus --tarball=./Quartus-lite-24.1std.0.917-linux.tar   "
            "# one-time, populates the Volume"
        )
        findings["next_steps"].append("superq modal bench ./my-core    # one real seed end-to-end")

    _emit_modal_check(findings, as_json)
    raise typer.Exit(0 if findings["sdk_installed"] else 2)


def _emit_modal_check(f: dict, as_json: bool) -> None:
    if as_json:
        _echo_json(f)
        return
    def mk(label: str, ok: bool | None) -> str:
        if ok is True:  return f"[green]✓[/green] {label}"
        if ok is False: return f"[red]✗[/red] {label}"
        return f"[yellow]?[/yellow] {label}"
    console.print(f"[bold]Modal backend readiness[/bold]")
    console.print(f"  {mk('SDK installed',     f['sdk_installed'])}")
    console.print(f"  {mk('API token',         f['token_present'])}")
    console.print(f"  {mk('super_q.modal_app importable', f['app_importable'])}")
    console.print(f"  {mk('App `super-q` deployed', f['app_deployed'])}")
    if f.get("next_steps"):
        console.print("\n[bold]next steps[/bold]")
        for s in f["next_steps"]:
            console.print(f"  [cyan]$[/cyan] {s}")
    else:
        console.print("\n[green]ready — try `superq modal smoke`[/green]")


@modal_app_cli.command("deploy")
def modal_deploy() -> None:
    """Run `modal deploy super_q.modal_app`. Requires the SDK + a token."""
    import subprocess as _sp
    rc = _sp.call(["modal", "deploy", "-m", "super_q.modal_app"])
    raise typer.Exit(rc)


@modal_app_cli.command("smoke")
def modal_smoke(as_json: bool = typer.Option(False, "--json")) -> None:
    """Invoke the no-Quartus smoke function to verify image build + RPC.

    This pays the image-build cost on first run (~30–90 s) but doesn't
    require the Quartus install to be finished yet. Use it to confirm
    your Modal account, image build, and super-q import all work before
    you install Quartus.
    """
    try:
        import modal
    except ImportError:
        err_console.print("[red]modal sdk not installed; pip install 'super-q[modal]'[/red]")
        raise typer.Exit(2)
    try:
        fn = modal.Function.from_name("super-q", "smoke_test")
    except Exception as e:
        err_console.print(f"[red]lookup failed: {e}[/red]")
        err_console.print("  did you run [bold]modal deploy super_q.modal_app[/bold]?")
        raise typer.Exit(2)

    import time as _t
    start = _t.time()
    try:
        result = fn.remote()
    except Exception as e:
        err_console.print(f"[red]smoke call failed: {e}[/red]")
        raise typer.Exit(3)
    total = _t.time() - start

    result = dict(result)
    result["_client_round_trip_s"] = round(total, 2)

    if as_json:
        _echo_json(result)
        return
    console.print(f"[green bold]✓ smoke test passed[/green bold] ({total:.1f}s round-trip)\n")
    console.print(f"  host     : {result.get('hostname')}")
    console.print(f"  cpu      : {result.get('cpu')}")
    console.print(f"  vcpus    : {result.get('cpu_count')}")
    console.print(f"  python   : {result.get('python')}")
    console.print(f"  super-q  : {result.get('super_q_version')}")
    console.print(f"  quartus  : "
                  f"{'[green]installed[/green] ' + str(result.get('quartus_version')) if result.get('quartus_installed') else '[yellow]not installed in Volume yet[/yellow]'}")
    console.print(f"  TCL      : {len(result.get('tcl_wrappers') or [])} wrappers baked in")
    if not result.get("quartus_installed"):
        console.print(
            "\nnext: [cyan]modal run super_q.modal_app::install_quartus "
            "--tarball=./Quartus-lite-24.1std.0.917-linux.tar[/cyan]"
        )


@modal_app_cli.command("install-quartus")
def modal_install_quartus(
    tarball: Path = typer.Option(..., "--tarball", exists=True,
                                  help="Path to the Quartus Lite tar you downloaded from Intel"),
    accept_eula: bool = typer.Option(
        False, "--accept-eula",
        help="Attests you've read and accepted Intel's license at "
             "https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html",
    ),
) -> None:
    """Upload Intel's Quartus tar to Modal and unpack it into the persistent Volume.

    Run once per Quartus release. Slow the first time (8 GB upload) but
    the result persists across every future invocation.
    """
    if not accept_eula:
        err_console.print(
            "[red]must pass --accept-eula[/red] — read Intel's license first at:\n"
            "  https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html"
        )
        raise typer.Exit(1)

    try:
        import modal  # noqa: F401
    except ImportError:
        err_console.print("[red]modal sdk missing; pip install 'super-q[modal]'[/red]")
        raise typer.Exit(2)

    import time as _t
    try:
        fn = modal.Function.from_name("super-q", "install_quartus")
    except Exception as e:
        err_console.print(f"[red]deploy first: {e}[/red]")
        raise typer.Exit(2)

    size_mb = tarball.stat().st_size / (1024 * 1024)
    console.print(f"uploading [bold]{tarball.name}[/bold] ({size_mb:.0f} MB) to Modal…")
    start = _t.time()
    try:
        result = fn.remote(tarball.read_bytes(), eula_accepted=True)
    except Exception as e:
        err_console.print(f"[red]install failed: {e}[/red]")
        raise typer.Exit(3)

    console.print(
        f"[green bold]✓ installed[/green bold] in {_t.time()-start:.0f}s\n"
        f"  bin dir     : {result.get('bin_dir')}\n"
        f"  executables : {', '.join(result.get('executables', [])[:6])}..."
    )


@modal_app_cli.command("bench")
def modal_bench(
    path: Path = typer.Argument(..., exists=True),
    seed: int = typer.Option(1, "--seed"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run a single real seed on Modal and print wall-clock + timing.

    Use this as the definitive "does it work end-to-end" check before
    running a full seed sweep. Takes 3–8 minutes depending on core size.
    """
    from super_q.backends.modal import ModalBackend
    core = _detect(path)
    be = ModalBackend()
    plan = SeedPlan(seeds=[seed], stop_on_first_pass=True, max_parallel=1)
    progress, handler = make_progress(
        total=1, json_mode=as_json,
        title=f"modal bench {core.full_name} seed={seed}",
    )
    sched = Scheduler(Store(paths().db_path), be, on_event=handler)
    with progress:
        outcome = sched.run_sweep(core, plan)
    if as_json:
        _echo_json(outcome.as_dict())
    else:
        _print_sweep_result(outcome)
    raise typer.Exit(0 if outcome.best else 3)


@app.command("init")
def init_cmd(
    full_name: str = typer.Argument(
        "",
        help="Repo name in `Author.Name` form (prompts if omitted).",
    ),
    target: Path = typer.Option(
        None, "--target", "-t",
        help="Directory to scaffold into. Defaults to ./<Author>.<Name> for a new repo, or '.' with --ci-only.",
    ),
    description: str = typer.Option("", "--description"),
    version: str = typer.Option("0.1.0", "--version", help="Initial version stamped into core.json"),
    super_q_ref: str = typer.Option("main", "--super-q-ref",
                                     help="super-q Git ref the generated workflows will call"),
    platform_id: list[str] = typer.Option(
        [], "--platform",
        help="Platform id(s) for core.json. Repeat for multiples; defaults to the core shortname.",
    ),
    ci_only: bool = typer.Option(
        False, "--ci-only",
        help="Only write .github/workflows — for adding super-q to an existing core repo.",
    ),
    inline: bool = typer.Option(
        False, "--inline",
        help="Emit self-contained workflow steps (no `uses:` to super-q). "
             "Use this when super-q lives in a private repo you can't share, "
             "or when you want to pin to a specific super-q commit.",
    ),
    super_q_pip: str = typer.Option(
        "", "--super-q-pip",
        help="pip install target for --inline mode (e.g. a private git URL). "
             "Defaults to `super-q @ git+https://github.com/super-q/super-q@<ref>`.",
    ),
    seeds_build: str = typer.Option("1-8", "--seeds-build"),
    seeds_release: str = typer.Option("1-32", "--seeds-release"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
    no_git: bool = typer.Option(False, "--no-git", help="Skip `git init`"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Scaffold a Pocket openFPGA core repo wired to super-q + GHA.

    Examples:
      superq init alice.my-core                       # new repo at ./alice.my-core
      superq init alice.my-core --target=./cores/     # create under cores/alice.my-core
      superq init --ci-only                           # add workflows to current repo
      superq init alice.my-core --platform=my_plat --platform=alt_plat
    """
    from super_q.init import InitError, InitOptions, scaffold

    # Resolve <Author>.<Name> from the positional or from target dir name.
    if full_name:
        if "." not in full_name:
            err_console.print(f"[red]repo name must be `Author.Name`, got {full_name!r}[/red]")
            raise typer.Exit(1)
        author, name = full_name.split(".", 1)
    elif ci_only and target is None:
        # Infer from cwd when just adding CI to an existing repo.
        inferred = Path.cwd().resolve().name
        if "." in inferred:
            author, name = inferred.split(".", 1)
        else:
            author, name = "unknown", inferred
    else:
        err_console.print("[red]missing positional Author.Name[/red]")
        raise typer.Exit(1)

    # Resolve target dir.
    if target is None:
        target = Path.cwd() if ci_only else Path.cwd() / f"{author}.{name}"

    try:
        opts = InitOptions(
            target=target,
            author=author,
            name=name,
            description=description,
            version=version,
            super_q_ref=super_q_ref,
            platform_ids=list(platform_id),
            ci_only=ci_only,
            force=force,
            git_init=not no_git,
            default_seeds_build=seeds_build,
            default_seeds_release=seeds_release,
            inline=inline,
            super_q_pip=super_q_pip,
        )
        result = scaffold(opts)
    except InitError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if as_json:
        _echo_json(result.as_dict())
        return

    console.print(
        f"[green]✓ scaffolded[/green] [bold]{author}.{name}[/bold]  "
        f"→ {result.target}"
    )
    for p in result.created:
        rel = p.relative_to(result.target)
        console.print(f"  [dim]+[/dim] {rel}")
    if result.skipped:
        console.print(f"\n[yellow]{len(result.skipped)} files already existed[/yellow] "
                      "(re-run with --force to overwrite)")
    if not ci_only:
        console.print(
            f"\n[bold]next[/bold]\n"
            f"  cd {result.target}\n"
            f"  # drop your Quartus project under src/fpga/\n"
            f"  superq verify .\n"
            f"  git add . && git commit -m 'initial scaffold'\n"
            f"  # push to GitHub, then tag a release:\n"
            f"  git tag v{version} && git push --tags"
        )


release_app = typer.Typer(no_args_is_help=True, help="Package a Pocket release zip.")
app.add_typer(release_app, name="release")


@release_app.command("pack")
def release_pack(
    core_path: Path = typer.Option(..., "--core-path", exists=True),
    rbf_r: Path = typer.Option(..., "--rbf-r", exists=True,
                                help="Winning bitstream.rbf_r from a seed sweep"),
    out_dir: Path = typer.Option(Path("release"), "--out-dir"),
    version: str = typer.Option("", "--version",
                                 help="Version string (defaults: git tag or core.json)"),
    name: str = typer.Option("", "--name",
                              help="Override <Author>.<CoreName>"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Package a built core into `<out_dir>/<Author>.<CoreName>_<version>.zip`.

    The zip is drop-in for Pocket SD cards: unpack into the root of a
    formatted Pocket card and the core appears in the Author's menu.
    """
    from super_q.pack import PackError, pack

    try:
        result = pack(
            core_path=core_path,
            rbf_r=rbf_r,
            out_dir=out_dir,
            version=version or None,
            name_override=name or None,
        )
    except PackError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if as_json:
        _echo_json(result.as_dict())
    else:
        console.print(
            f"[green]✓ packed[/green] {result.full_name} "
            f"[dim]v{result.version}[/dim]\n"
            f"  {result.zip_path} ({result.bytes/1024/1024:.1f} MB)\n"
            f"  sha256 [dim]{result.sha256}[/dim]"
        )


ci_app = typer.Typer(no_args_is_help=True, help="CI-tuned wrappers (GHA/GitLab/Circle annotations + outputs).")
app.add_typer(ci_app, name="ci")


@ci_app.command("build")
def ci_build(
    path: Path = typer.Argument(..., exists=True),
    min_seed: int = typer.Option(1, "--min"),
    max_seed: int = typer.Option(8, "--max"),
    parallel: int = typer.Option(4, "--parallel"),
    threads: int = typer.Option(2, "--threads"),
    mode: str = typer.Option("full", "--mode"),
    pool: str = typer.Option("", "--pool"),
    backend: str = typer.Option("", "--backend"),
    target_slack: float = typer.Option(0.0, "--target-slack-ns"),
) -> None:
    """Run a sweep tuned for CI: annotations on stderr, JSON on stdout, GHA outputs."""
    from super_q.ci import annotate, detect, render_sweep_summary, set_output, summary_markdown

    env = detect()
    core = _detect(path)
    plan = SeedPlan.range(start=min_seed, end=max_seed,
                          max_parallel=parallel, stop_on_first_pass=True,
                          target_slack_ns=target_slack)
    be = _mk_backend_smart(backend, pool)
    store = Store(paths().db_path)

    sched = Scheduler(store, be)
    outcome = sched.run_sweep(core, plan, mode=mode, threads_per_task=threads)

    set_output(env, "passed", "true" if outcome.best else "false")
    set_output(env, "best_seed", outcome.best.seed if outcome.best else "")
    set_output(env, "best_slack_ns", outcome.best.slack_ns if outcome.best else "")
    set_output(env, "rbf_r_path", outcome.best.rbf_r_path if outcome.best else "")

    if outcome.best is None:
        annotate(env, "error", f"{core.full_name}: no passing seed in {len(plan.seeds)}",
                 file=core.qpf, title="super-q timing failure")
    else:
        annotate(env, "notice",
                 f"{core.full_name}: seed {outcome.best.seed} passed "
                 f"slack={outcome.best.slack_ns:+.3f}ns "
                 f"Fmax={outcome.best.fmax_mhz or '—'}MHz",
                 file=core.qpf, title="super-q")

    summary_markdown(env, render_sweep_summary(outcome))
    _echo_json(outcome.as_dict())

    raise typer.Exit(0 if outcome.best else 3)


@ci_app.command("explore")
def ci_explore(
    path: Path = typer.Argument(..., exists=True),
    budget: str = typer.Option("30m", "--budget"),
    parallel: int = typer.Option(4, "--parallel"),
    threads: int = typer.Option(2, "--threads"),
    pool: str = typer.Option("", "--pool"),
    backend: str = typer.Option("", "--backend"),
) -> None:
    """Explore budget-bounded, then emit CI outputs."""
    from super_q.ci import annotate, detect, set_output
    from super_q.explore import explore as run_explore

    env = detect()
    core = _detect(path)
    be = _mk_backend_smart(backend, pool)
    store = Store(paths().db_path)
    out = run_explore(store, be, core,
                      budget_s=_parse_duration(budget),
                      parallel=parallel, threads_per_task=threads)

    set_output(env, "passed", "true" if out.best else "false")
    set_output(env, "best_seed", out.best.seed if out.best else "")
    set_output(env, "rbf_r_path", out.best.rbf_r_path if out.best else "")
    annotate(env, "notice" if out.best else "error",
             f"{core.full_name}: explore {'passed' if out.best else 'exhausted budget'}",
             file=core.qpf, title="super-q explore")
    _echo_json(out.as_dict())
    raise typer.Exit(0 if out.best else (4 if out.timed_out else 3))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _detect(path: Path) -> PocketCore:
    try:
        return detect_core(path)
    except CoreDetectionError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def _make_plan(min_seed, max_seed, count, strategy, rng_seed, parallel,
              stop_on_pass, target_slack, target_fmax) -> SeedPlan:
    common = dict(
        max_parallel=parallel,
        stop_on_first_pass=stop_on_pass,
        target_slack_ns=target_slack,
        target_fmax_mhz=target_fmax,
    )
    if strategy == "random":
        n = count or max(1, max_seed - min_seed + 1)
        return SeedPlan.random(count=n, rng_seed=rng_seed, **common)
    if strategy == "spaced":
        n = count or max(1, max_seed - min_seed + 1)
        return SeedPlan.spaced(count=n, **common)
    return SeedPlan.range(start=min_seed, end=max_seed, **common)


def _run_sweep(core, plan, backend, as_json, *, mode="full", threads=2, timeout_s=3600):
    store = Store(paths().db_path)
    be = _mk_backend(backend)
    progress, handler = make_progress(
        total=len(plan.seeds),
        json_mode=as_json,
        title=f"{core.full_name} · {len(plan.seeds)} seeds · {backend}",
    )
    sched = Scheduler(store, be, on_event=handler)
    with progress:
        outcome = sched.run_sweep(
            core, plan,
            mode=mode,
            threads_per_task=threads if threads > 0 else 2,
            timeout_s=timeout_s,
        )
    if as_json:
        _echo_json(outcome.as_dict())
    else:
        _print_sweep_result(outcome)
    return outcome


def _mk_backend(name: str):
    try:
        return get_backend(name)
    except Exception as e:
        err_console.print(f"[red]backend '{name}' unavailable: {e}[/red]")
        raise typer.Exit(2)


def _mk_backend_smart(backend: str, pool: str):
    """Resolve a backend from either `--backend` (explicit) or `--pool` (named).

    `--backend` wins if both are set — agents that want something cheap
    and scriptable pass `--backend=local`; users with a named pool just
    pass `--pool=homelab` and we look it up in `config.toml`.
    """
    if backend:
        return _mk_backend(backend)
    try:
        return resolve_backend(pool or None)
    except PoolConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    except Exception as e:
        err_console.print(f"[red]backend/pool resolution failed: {e}[/red]")
        raise typer.Exit(2)


_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)


def _parse_duration(s: str) -> int:
    m = _DUR_RE.match(s)
    if not m:
        raise typer.BadParameter(f"invalid duration: {s!r} (try 30m, 2h, 90s)")
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _print_explore_result(out) -> None:
    if out.best:
        console.print(
            f"\n[green bold]✓ explored to a pass[/green bold] "
            f"after {out.total_duration_s:.0f}s · seed={out.best.seed} "
            f"slack={out.best.slack_ns:+.3f}ns Fmax={out.best.fmax_mhz or '—'}"
        )
    elif out.timed_out:
        console.print(f"\n[yellow bold]⏰ budget exhausted[/yellow bold] after "
                      f"{out.total_duration_s:.0f}s · {len(out.rungs)} rungs tried")
    else:
        console.print(f"\n[red bold]✗ no passing seed[/red bold] after "
                      f"{out.total_duration_s:.0f}s · {len(out.rungs)} rungs tried")

    t = Table(title="exploration rungs")
    for c in ("rung", "duration", "passed", "best seed", "best slack"):
        t.add_column(c)
    for r in out.rungs:
        best = r.outcome.best
        t.add_row(
            r.name,
            f"{r.duration_s:.0f}s",
            "yes" if best else "no",
            str(best.seed) if best else "—",
            f"{best.slack_ns:+.3f}" if best and best.slack_ns is not None else "—",
        )
    console.print(t)


def _exit_for_outcome(outcome) -> None:
    if outcome.best is not None:
        raise typer.Exit(0)
    if any(r.status == "cancelled" for r in (getattr(x, "status", None) for x in outcome.results)):
        raise typer.Exit(4)
    raise typer.Exit(3)


# -- rendering helpers --

def _echo_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str))


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        _echo_json(payload)
    else:
        if payload.get("ok") is False:
            err_console.print("[red]verification failed[/red]")
            for e in payload.get("errors", []):
                err_console.print(f"  ✗ {e}")
        else:
            console.print("[green]ok[/green]")
        for w in payload.get("warnings", []):
            console.print(f"  [yellow]! {w}[/yellow]")
        if core := payload.get("core"):
            console.print(f"  project: [bold]{core['project_name']}[/bold]")
            console.print(f"  device : {core['device']}")
            console.print(f"  qpf    : {core['qpf']}")
        if timing := payload.get("timing"):
            console.print(f"  timing : {timing.get('summary', '')}")


def _pretty_info(d: dict) -> None:
    q = d["quartus"]; h = d["host"]
    console.print(f"[bold]super-q {d['version']}[/bold]")
    if q["installed"]:
        console.print(f"  quartus : [green]{q['version']}[/green] at {q['root']}")
    else:
        console.print("  quartus : [red]not installed[/red]")
    console.print(f"  host    : {h['cpus']} cpus, {h['mem_gb']} GB, {h['platform']}")
    console.print(f"  parallel hint: {h['suggested_parallel']} concurrent builds")
    console.print(f"  db      : {d['paths']['db']}")
    for issue in d["issues"]:
        console.print(f"  [yellow]! {issue}[/yellow]")


def _print_sweep_result(outcome) -> None:
    b = outcome.best
    if b:
        console.print(
            f"\n[green bold]✓ passed[/green bold] "
            f"seed={b.seed} slack={b.slack_ns:+.3f}ns "
            f"Fmax={b.fmax_mhz or '—'} artifact={b.rbf_r_path}"
        )
    else:
        console.print("\n[red bold]✗ no passing seed[/red bold]")

    t = Table(title="seed results", show_lines=False)
    for c in ("seed", "status", "slack (ns)", "Fmax (MHz)", "time", "error"):
        t.add_column(c)
    for r in rank(outcome.results):
        t.add_row(
            str(r.seed),
            "[green]passed[/green]" if r.passed else "[red]failed[/red]",
            f"{r.slack_ns:+.3f}" if r.slack_ns is not None else "—",
            f"{r.fmax_mhz:.2f}" if r.fmax_mhz is not None else "—",
            f"{r.duration_s:.0f}s",
            (r.error or "")[:60],
        )
    console.print(t)
    console.print(
        f"[dim]job {outcome.job_id} · summary: {json.dumps(outcome.summary)}[/dim]"
    )


def _print_jobs(jobs: list[dict]) -> None:
    t = Table(title="recent jobs")
    for c in ("id", "core", "kind", "status", "seeds", "best", "age"):
        t.add_column(c)
    import time as _t
    now = _t.time()
    for j in jobs:
        spec = json.loads(j["spec_json"]) if j.get("spec_json") else {}
        plan = spec.get("plan") or {}
        seeds = plan.get("seeds") or []
        age = now - j["created_at"]
        t.add_row(
            j["id"], j["core_name"], j["kind"], j["status"],
            str(len(seeds)),
            str(j.get("best_seed") or "—"),
            f"{int(age)}s" if age < 120 else f"{int(age/60)}m",
        )
    console.print(t)


def _print_job(job: dict, tasks: list[dict]) -> None:
    console.print(
        f"[bold]{job['id']}[/bold] · {job['core_name']} · {job['kind']} · "
        f"[cyan]{job['status']}[/cyan]"
    )
    t = Table()
    for c in ("seed", "status", "slack", "Fmax", "time"):
        t.add_column(c)
    for tk in tasks:
        dur = ""
        if tk.get("started_at") and tk.get("ended_at"):
            dur = f"{tk['ended_at'] - tk['started_at']:.0f}s"
        t.add_row(
            str(tk["seed"]),
            tk["status"],
            f"{tk['slack_ns']:+.3f}" if tk.get("slack_ns") is not None else "—",
            f"{tk['fmax_mhz']:.2f}" if tk.get("fmax_mhz") is not None else "—",
            dur,
        )
    console.print(t)


def _print_batch_table(outcomes: list) -> None:
    t = Table(title="batch results")
    for c in ("core", "status", "best seed", "slack (ns)", "Fmax (MHz)", "artifact"):
        t.add_column(c)
    for o in outcomes:
        best = o.best
        t.add_row(
            o.core.full_name,
            "[green]pass[/green]" if best else "[red]fail[/red]",
            str(best.seed) if best else "—",
            f"{best.slack_ns:+.3f}" if best and best.slack_ns is not None else "—",
            f"{best.fmax_mhz:.2f}" if best and best.fmax_mhz is not None else "—",
            best.rbf_r_path[-60:] if best and best.rbf_r_path else "—",
        )
    console.print(t)


def _pkg_version() -> str:
    try:
        from super_q import __version__
        return __version__
    except Exception:
        return "dev"


if __name__ == "__main__":
    app()
