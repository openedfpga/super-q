"""MCP server — exposes super-q as a set of tools to Claude agents.

Start with `super-q mcp` (stdio transport) or `super-q-mcp` directly.
Pair with Claude Code / Claude Desktop by registering a server entry:

    {
      "mcpServers": {
        "super-q": { "command": "super-q-mcp" }
      }
    }

The tool surface is intentionally small and procedural — each tool maps
one-to-one to a CLI command. Agents can also read the CLI's `--json`
output if they prefer the command-line form.

Requires `mcp>=1.0`; install with `pip install super-q[mcp]`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("superq.mcp")


def main() -> None:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError:
        raise SystemExit(
            "super-q MCP server requires the 'mcp' package. "
            "Install with `pip install super-q[mcp]`."
        )

    from super_q.backends import get_backend
    from super_q.config import banner, paths, quartus_install
    from super_q.db import Store
    from super_q.project import detect_core, find_cores
    from super_q.scheduler import Scheduler
    from super_q.seeds import SeedPlan

    server = Server("super-q")

    def _tool(name: str, description: str, schema: dict) -> Tool:
        return Tool(name=name, description=description, inputSchema=schema)

    # -- schema snippets we reuse --
    path_prop = {"type": "string", "description": "Path to the Pocket core folder (root with dist/ or src/fpga/)."}

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            _tool(
                "info",
                "Report environment: Quartus install, host capacity, paths. Call this first to check the agent can build.",
                {"type": "object", "properties": {}},
            ),
            _tool(
                "verify_core",
                "Validate that a folder is a Pocket core and is buildable. Returns warnings and errors.",
                {
                    "type": "object",
                    "properties": {"path": path_prop},
                    "required": ["path"],
                },
            ),
            _tool(
                "find_cores",
                "Enumerate every Pocket core under one or more paths. Use for batch operations.",
                {
                    "type": "object",
                    "properties": {
                        "paths": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["paths"],
                },
            ),
            _tool(
                "build_core",
                "Run a single seed compile. Blocks until done. Returns timing + artifact paths.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_prop,
                        "seed": {"type": "integer", "default": 1, "description": "Fitter seed."},
                        "backend": {"type": "string", "enum": ["local", "docker", "aws"], "default": "local"},
                        "threads": {"type": "integer", "default": 2},
                        "timeout_s": {"type": "integer", "default": 3600},
                    },
                    "required": ["path"],
                },
            ),
            _tool(
                "sweep_seeds",
                "Parallel seed exploration. Early-exits on first passing seed unless stop_on_pass=false.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_prop,
                        "min_seed": {"type": "integer", "default": 1},
                        "max_seed": {"type": "integer", "default": 16},
                        "count": {"type": "integer", "default": 0, "description": "Sample N seeds instead of range."},
                        "strategy": {"type": "string", "enum": ["range", "random", "spaced"], "default": "range"},
                        "parallel": {"type": "integer", "default": 4},
                        "threads": {"type": "integer", "default": 2},
                        "stop_on_pass": {"type": "boolean", "default": True},
                        "mode": {"type": "string", "enum": ["full", "split-fit"], "default": "full"},
                        "backend": {"type": "string", "enum": ["local", "docker", "aws"], "default": "local"},
                        "target_slack_ns": {"type": "number", "default": 0.0},
                    },
                    "required": ["path"],
                },
            ),
            _tool(
                "batch_sweep",
                "Run a seed sweep across many cores. Returns per-core outcomes.",
                {
                    "type": "object",
                    "properties": {
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "min_seed": {"type": "integer", "default": 1},
                        "max_seed": {"type": "integer", "default": 8},
                        "parallel": {"type": "integer", "default": 4},
                        "parallel_cores": {"type": "integer", "default": 2},
                        "backend": {"type": "string", "default": "local"},
                    },
                    "required": ["paths"],
                },
            ),
            _tool(
                "list_jobs",
                "List recent jobs (optionally filtered by status).",
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["queued", "running", "passed", "failed", "cancelled"]},
                        "limit": {"type": "integer", "default": 20},
                    },
                },
            ),
            _tool(
                "job_status",
                "Full breakdown of a specific job, including every seed's outcome.",
                {
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                },
            ),
            _tool(
                "cancel_job",
                "Cancel a running job. Returns number of tasks cancelled.",
                {
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                },
            ),
            _tool(
                "inspect_timing",
                "Re-parse timing from the last compile in a core's output_files. No rebuild.",
                {
                    "type": "object",
                    "properties": {"path": path_prop},
                    "required": ["path"],
                },
            ),
            _tool(
                "incremental_build",
                "Fast rebuild using Quartus partition reuse + the warm shell. Serial per core. Best for edit→rebuild loops.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_prop,
                        "seed": {"type": "integer", "default": 1},
                        "use_warm_shell": {"type": "boolean", "default": True},
                    },
                    "required": ["path"],
                },
            ),
            _tool(
                "explore",
                "Adaptive seed exploration with a wall-clock budget. Escalates strategies until pass or timeout.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_prop,
                        "budget_s": {"type": "integer", "default": 1800, "description": "Time budget in seconds."},
                        "parallel": {"type": "integer", "default": 4},
                        "pool": {"type": "string", "description": "Named pool (e.g. 'modal', 'homelab')."},
                        "backend": {"type": "string", "enum": ["local", "docker", "modal", "fly", "ssh", "gha", "aws"]},
                    },
                    "required": ["path"],
                },
            ),
            _tool(
                "list_pools",
                "List named remote worker pools from ~/.superq/config.toml and the default pool.",
                {"type": "object", "properties": {}},
            ),
            _tool(
                "daemon_ping",
                "Check whether the local super-q daemon is responding. Returns {alive, uptime_s}.",
                {"type": "object", "properties": {}},
            ),
        ]

    # ---- tool implementations ------------------------------------------

    def _ok(data: Any) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(data, default=str, indent=2))]

    def _err(msg: str) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps({"error": msg}))]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            return await _dispatch(name, arguments or {})
        except Exception as e:
            log.exception("tool %s failed", name)
            return _err(f"{type(e).__name__}: {e}")

    async def _dispatch(name: str, args: dict) -> list[TextContent]:
        store = Store(paths().db_path)

        if name == "info":
            q = quartus_install()
            return _ok({
                "version": banner(),
                "quartus": {"installed": q.is_installed, "version": q.version, "root": str(q.root) if q.root else None},
                "state_dir": str(paths().state_dir),
            })

        if name == "verify_core":
            core = detect_core(args["path"])
            from super_q.quartus import verify_toolchain
            return _ok({
                "ok": True,
                "core": core.as_dict(),
                "toolchain": verify_toolchain(),
            })

        if name == "find_cores":
            cores = find_cores(args["paths"])
            return _ok({"cores": [c.as_dict() for c in cores], "count": len(cores)})

        if name == "build_core":
            core = detect_core(args["path"])
            plan = SeedPlan(seeds=[int(args.get("seed", 1))], stop_on_first_pass=True, max_parallel=1)
            backend = get_backend(args.get("backend", "local"))
            sched = Scheduler(store, backend)
            outcome = await asyncio.to_thread(
                sched.run_sweep, core, plan,
                mode="full",
                threads_per_task=int(args.get("threads", 2)),
                timeout_s=int(args.get("timeout_s", 3600)),
            )
            return _ok(outcome.as_dict())

        if name == "sweep_seeds":
            core = detect_core(args["path"])
            strategy = args.get("strategy", "range")
            common = dict(
                max_parallel=int(args.get("parallel", 4)),
                stop_on_first_pass=bool(args.get("stop_on_pass", True)),
                target_slack_ns=float(args.get("target_slack_ns", 0.0)),
            )
            if strategy == "random":
                plan = SeedPlan.random(count=int(args.get("count", 16)), **common)
            elif strategy == "spaced":
                plan = SeedPlan.spaced(count=int(args.get("count", 16)), **common)
            else:
                plan = SeedPlan.range(
                    start=int(args.get("min_seed", 1)),
                    end=int(args.get("max_seed", 16)),
                    **common,
                )
            backend = get_backend(args.get("backend", "local"))
            sched = Scheduler(store, backend)
            outcome = await asyncio.to_thread(
                sched.run_sweep, core, plan,
                mode=args.get("mode", "full"),
                threads_per_task=int(args.get("threads", 2)),
            )
            return _ok(outcome.as_dict())

        if name == "batch_sweep":
            from super_q.scheduler import batch_run
            cores = find_cores(args["paths"])
            if not cores:
                return _err("no cores found")
            backend = get_backend(args.get("backend", "local"))

            def factory(_c):
                return SeedPlan.range(
                    start=int(args.get("min_seed", 1)),
                    end=int(args.get("max_seed", 8)),
                    max_parallel=int(args.get("parallel", 4)),
                    stop_on_first_pass=True,
                )
            outcomes = await asyncio.to_thread(
                batch_run, store, backend, cores, factory,
                parallel_cores=int(args.get("parallel_cores", 2)),
            )
            return _ok({"cores": [o.as_dict() for o in outcomes]})

        if name == "list_jobs":
            return _ok({"jobs": store.list_jobs(status=args.get("status"), limit=int(args.get("limit", 20)))})

        if name == "job_status":
            job = store.get_job(args["job_id"])
            if job is None:
                return _err(f"no job {args['job_id']}")
            tasks = store.list_tasks(args["job_id"])
            return _ok({"job": job, "tasks": tasks})

        if name == "cancel_job":
            n = store.cancel_job(args["job_id"])
            return _ok({"cancelled": n})

        if name == "inspect_timing":
            core = detect_core(args["path"])
            from super_q.timing import merge_reports, parse_sta_report, parse_timing_json
            out = core.output_dir
            rpt = parse_sta_report(out / f"{core.project_name}.sta.rpt")
            merged = merge_reports(rpt, parse_timing_json(out / "timing.json"))
            return _ok({"core": core.as_dict(), "timing": merged.as_dict()})

        if name == "incremental_build":
            core = detect_core(args["path"])
            from super_q.incremental import IncrementalBuilder
            builder = IncrementalBuilder()
            try:
                res = await asyncio.to_thread(
                    builder.run, core,
                    job_id=f"mcp-{int(__import__('time').time())}",
                    seed=int(args.get("seed", 1)),
                    use_warm_shell=bool(args.get("use_warm_shell", True)),
                )
            finally:
                builder.shutdown()
            return _ok({
                "ok": res.ok,
                "seed": res.seed,
                "duration_s": res.duration_s,
                "reused_warm_shell": res.reused_warm_shell,
                "timing": res.timing.as_dict() if res.timing else None,
                "rbf_r_path": str(res.rbf_r_path) if res.rbf_r_path else None,
                "error": res.error,
            })

        if name == "explore":
            from super_q.explore import explore as run_explore
            core = detect_core(args["path"])
            backend_name = args.get("backend")
            if backend_name:
                backend = get_backend(backend_name)
            else:
                from super_q.pool_config import resolve_backend
                backend = resolve_backend(args.get("pool") or None)
            out = await asyncio.to_thread(
                run_explore, store, backend, core,
                budget_s=int(args.get("budget_s", 1800)),
                parallel=int(args.get("parallel", 4)),
            )
            return _ok(out.as_dict())

        if name == "list_pools":
            from super_q.pool_config import describe
            return _ok(describe())

        if name == "daemon_ping":
            from super_q.daemon import Client, is_running
            if not is_running():
                return _ok({"alive": False})
            with Client() as c:
                info = c.call("info")
            return _ok({"alive": True, **info})

        return _err(f"unknown tool: {name}")

    async def _run():
        async with stdio_server() as (rx, tx):
            await server.run(rx, tx, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
