from pathlib import Path

from super_q.timing import parse_sta_report, parse_timing_json


_SAMPLE_STA = """\
; Worst-case Setup Slack                                                     ;
; Clock                     ; Slack     ;
; clk_74_25                 ; +0.082    ;
; spi_clk                   ; +1.734    ;

; Worst-case Hold Slack                                                     ;
; Clock                     ; Slack     ;
; clk_74_25                 ; +0.342    ;
; spi_clk                   ; +0.192    ;

; Clock Fmax Summary                                                          ;
; 74.60 MHz ; 74.60 MHz ; clk_74_25 ;   ;
; 123.45 MHz ; 120.10 MHz ; spi_clk   ;   ;
"""


def test_parse_passing(tmp_path: Path) -> None:
    p = tmp_path / "proj.sta.rpt"
    p.write_text(_SAMPLE_STA)
    r = parse_sta_report(p)
    assert r.passed is True
    assert r.worst_setup_slack_ns == 0.082
    assert r.worst_hold_slack_ns == 0.192
    names = {c.name for c in r.clocks}
    assert "clk_74_25" in names
    assert "spi_clk" in names


def test_parse_failing(tmp_path: Path) -> None:
    bad = _SAMPLE_STA.replace("+0.082", "-0.123")
    p = tmp_path / "proj.sta.rpt"
    p.write_text(bad)
    r = parse_sta_report(p)
    assert r.passed is False
    assert r.worst_setup_slack_ns == -0.123


def test_parse_json_fallback(tmp_path: Path) -> None:
    p = tmp_path / "timing.json"
    p.write_text(
        '{"passed": true, "worst_setup_slack_ns": 0.5, "worst_hold_slack_ns": 0.1,'
        ' "clocks":[{"name":"c","setup_slack_ns":0.5,"fmax_mhz":80.0}]}'
    )
    r = parse_timing_json(p)
    assert r and r.passed
    assert r.worst_setup_slack_ns == 0.5
    assert r.clocks[0].name == "c"


def test_missing_report(tmp_path: Path) -> None:
    r = parse_sta_report(tmp_path / "missing")
    assert r.passed is False


def test_merge_prefers_json_fallback_when_text_parser_empty(tmp_path: Path) -> None:
    """Regression: Quartus 24.1 changed STA panel headers from
    "Worst-case Setup Slack" → "Setup Summary", silently breaking
    the text scraper. The TCL-emitted timing.json uses the structured
    panel API and is version-stable — merge should prefer it."""
    from super_q.timing import merge_reports

    # Simulate a parser that returned nothing (no panels recognized).
    empty = parse_sta_report(tmp_path / "missing-sta")

    # Simulate a healthy timing.json from TCL.
    jsn = tmp_path / "timing.json"
    jsn.write_text(
        '{"passed": true, "worst_setup_slack_ns": 0.42,'
        ' "worst_hold_slack_ns": 0.12,'
        ' "clocks":[{"name":"clk_74_25","setup_slack_ns":0.42,"fmax_mhz":80}]}'
    )
    healthy = parse_timing_json(jsn)

    merged = merge_reports(empty, healthy)
    assert merged.passed is True
    assert merged.worst_setup_slack_ns == 0.42
    assert merged.clocks  # truthy


def test_parser_handles_setup_summary_header(tmp_path: Path) -> None:
    """Quartus 24.1 panel header. Must be recognized."""
    p = tmp_path / "proj.sta.rpt"
    p.write_text(
        "+--------------------------------+\n"
        "; Setup Summary                  ;\n"
        "+----------+--------+------------+\n"
        "; Clock    ; Slack  ; End Point  ;\n"
        "; clk_a    ; +0.082 ; some_reg   ;\n"
        "; clk_b    ; +0.500 ; other_reg  ;\n"
        "+----------+--------+------------+\n"
    )
    r = parse_sta_report(p)
    assert r.worst_setup_slack_ns == 0.082
    assert {c.name for c in r.clocks} >= {"clk_a", "clk_b"}
