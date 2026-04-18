"""Timing report parsing (STA output).

We extract the information Pocket-core authors actually care about:
  - worst-case setup/hold slack per clock
  - Fmax summary (restricted + unrestricted)
  - overall pass/fail

The parser reads the .sta.rpt that `quartus_sta` produces. It's a plain
text report with reasonably stable section headers across Quartus 21–24.
If the report format drifts, we fall back to the JSON timing summary our
TCL wrapper also emits.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ClockTiming:
    name: str
    setup_slack_ns: float | None = None
    hold_slack_ns: float | None = None
    fmax_mhz: float | None = None
    restricted_fmax_mhz: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "setup_slack_ns": self.setup_slack_ns,
            "hold_slack_ns": self.hold_slack_ns,
            "fmax_mhz": self.fmax_mhz,
            "restricted_fmax_mhz": self.restricted_fmax_mhz,
        }


@dataclass
class TimingReport:
    passed: bool
    worst_setup_slack_ns: float | None
    worst_hold_slack_ns: float | None
    clocks: list[ClockTiming] = field(default_factory=list)
    summary: str = ""
    source: str = "sta.rpt"

    @property
    def worst_fmax_mhz(self) -> float | None:
        fs = [c.fmax_mhz for c in self.clocks if c.fmax_mhz is not None]
        return min(fs) if fs else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "worst_setup_slack_ns": self.worst_setup_slack_ns,
            "worst_hold_slack_ns": self.worst_hold_slack_ns,
            "worst_fmax_mhz": self.worst_fmax_mhz,
            "clocks": [c.as_dict() for c in self.clocks],
            "summary": self.summary,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_SLACK_HEADER_RE = re.compile(
    r"^\s*;\s*(Clock|Clock Pair)\s*;\s*Slack\s*;", re.IGNORECASE
)
# Flexible section-header matchers: both "Worst-case Setup Slack" (21.x)
# and "Setup Summary" (24.x) variants. The leading `;` + trailing whitespace
# padding are optional so we catch unboxed and boxed versions alike.
_SETUP_HEADER_RE = re.compile(r"(worst-case setup slack|(?<![a-z])setup summary(?![a-z]))")
_HOLD_HEADER_RE = re.compile(r"(worst-case hold slack|(?<![a-z])hold summary(?![a-z]))")
_SLACK_ROW_RE = re.compile(
    r"^\s*;\s*([^;]+?)\s*;\s*([+-]?\d+(?:\.\d+)?)\s*;.*$"
)
_FMAX_ROW_RE = re.compile(
    r"^\s*;\s*([+-]?\d+(?:\.\d+)?)\s+MHz\s*;\s*([+-]?\d+(?:\.\d+)?)\s+MHz\s*;\s*([^;]+?)\s*;.*$"
)
_SUMMARY_HEADER_RE = re.compile(r"Worst-case (Setup|Hold) Slack", re.IGNORECASE)


def parse_sta_report(path: Path) -> TimingReport:
    """Parse a `quartus_sta` text report (`<proj>.sta.rpt`).

    This handles Quartus's `+---+` boxed tables by looking for the `;`
    delimiters that appear in the machine-readable copy of every table.
    """
    if not path.exists():
        return TimingReport(
            passed=False,
            worst_setup_slack_ns=None,
            worst_hold_slack_ns=None,
            summary=f"STA report missing at {path}",
            source=str(path),
        )
    lines = path.read_text(errors="ignore").splitlines()
    clocks: dict[str, ClockTiming] = {}
    current_section: str | None = None
    in_fmax_table = False

    for line in lines:
        low = line.strip().lower()

        # Section detection — Quartus labels change between major releases.
        # 21.x used "Worst-case Setup Slack" headers; 24.1 dropped that and
        # uses "Setup Summary" / "Hold Summary" only. Match both shapes so
        # a parser regression in either direction is impossible.
        if _SETUP_HEADER_RE.search(low):
            current_section = "setup"
            in_fmax_table = False
            continue
        if _HOLD_HEADER_RE.search(low):
            current_section = "hold"
            in_fmax_table = False
            continue
        if "fmax summary" in low or "clock fmax summary" in low:
            current_section = "fmax"
            in_fmax_table = True
            continue
        if "timing analyzer summary" in low:
            current_section = "summary"
            in_fmax_table = False
            continue

        if current_section in ("setup", "hold") and line.strip().startswith(";"):
            m = _SLACK_ROW_RE.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            if name.lower() == "clock" or "-----" in name:
                continue
            slack = _parse_float(m.group(2))
            c = clocks.setdefault(name, ClockTiming(name=name))
            if current_section == "setup":
                c.setup_slack_ns = slack
            else:
                c.hold_slack_ns = slack

        elif in_fmax_table and line.strip().startswith(";"):
            m = _FMAX_ROW_RE.match(line)
            if not m:
                continue
            fmax = _parse_float(m.group(1))
            restricted = _parse_float(m.group(2))
            name = m.group(3).strip()
            if name.lower() in ("clock name", ""):
                continue
            c = clocks.setdefault(name, ClockTiming(name=name))
            c.fmax_mhz = fmax
            c.restricted_fmax_mhz = restricted

    setup_slacks = [c.setup_slack_ns for c in clocks.values() if c.setup_slack_ns is not None]
    hold_slacks = [c.hold_slack_ns for c in clocks.values() if c.hold_slack_ns is not None]
    worst_setup = min(setup_slacks) if setup_slacks else None
    worst_hold = min(hold_slacks) if hold_slacks else None

    passed = True
    if worst_setup is not None and worst_setup < 0:
        passed = False
    if worst_hold is not None and worst_hold < 0:
        passed = False
    if worst_setup is None and worst_hold is None:
        passed = False

    summary = _describe(worst_setup, worst_hold, list(clocks.values()))

    return TimingReport(
        passed=passed,
        worst_setup_slack_ns=worst_setup,
        worst_hold_slack_ns=worst_hold,
        clocks=list(clocks.values()),
        summary=summary,
        source=str(path),
    )


def parse_timing_json(path: Path) -> TimingReport | None:
    """Fallback: read the JSON our TCL wrapper emits alongside the .rpt.

    Used when STA text parsing can't find tables (e.g. Quartus changed
    formatting) but our TCL dumped a structured summary.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    clocks = [
        ClockTiming(
            name=c["name"],
            setup_slack_ns=c.get("setup_slack_ns"),
            hold_slack_ns=c.get("hold_slack_ns"),
            fmax_mhz=c.get("fmax_mhz"),
            restricted_fmax_mhz=c.get("restricted_fmax_mhz"),
        )
        for c in data.get("clocks", [])
    ]
    return TimingReport(
        passed=bool(data.get("passed", False)),
        worst_setup_slack_ns=data.get("worst_setup_slack_ns"),
        worst_hold_slack_ns=data.get("worst_hold_slack_ns"),
        clocks=clocks,
        summary=data.get("summary", ""),
        source=str(path),
    )


def merge_reports(primary: TimingReport, fallback: TimingReport | None) -> TimingReport:
    """Prefer the structured JSON when present.

    Previously we fell back to JSON only if the text-scraper came up
    totally empty. That masked a bug where Quartus 24.1 changed the
    STA panel header names and our regexes silently matched nothing
    — every seed returned `passed=False` even when the bitstream was
    fine. The TCL-emitted `timing.json` uses `::quartus::report`'s
    panel API, which is stable across versions, so trust it first.
    """
    if fallback is None:
        return primary
    fallback_has = (
        fallback.worst_setup_slack_ns is not None
        or fallback.clocks
        or fallback.passed   # TCL dump_timing_json sets passed=True unconditionally until it proves otherwise
    )
    if fallback_has:
        return fallback
    return primary


def _parse_float(s: str) -> float | None:
    try:
        v = float(s)
        return None if math.isnan(v) else v
    except ValueError:
        return None


def _describe(setup: float | None, hold: float | None, clocks: list[ClockTiming]) -> str:
    parts: list[str] = []
    if setup is not None:
        parts.append(f"setup {setup:+.3f} ns")
    if hold is not None:
        parts.append(f"hold {hold:+.3f} ns")
    if clocks:
        fmaxes = [c.fmax_mhz for c in clocks if c.fmax_mhz is not None]
        if fmaxes:
            parts.append(f"worst Fmax {min(fmaxes):.2f} MHz")
    return ", ".join(parts) if parts else "no timing data"
