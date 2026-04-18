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
