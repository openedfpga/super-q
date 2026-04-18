# Shared helpers for super-q TCL wrappers.
#
# `quartus_sh -t <script>` runs these scripts inside Quartus's tclsh.
# We rely on the ::env array for super-q-provided settings and on the
# ::quartus::project / ::quartus::flow packages to drive compilation.

package require ::quartus::project
package require ::quartus::flow

namespace eval superq {
    variable seed 1
    variable project ""
    variable extra_list {}

    # Read a value from $::env with a default fallback.
    proc env_or {name default} {
        if {[info exists ::env($name)]} {
            return $::env($name)
        }
        return $default
    }

    # Parse a simple `k=v;k=v` string into a flat list alternating keys/values.
    proc parse_extra {s} {
        set out {}
        if {$s eq ""} { return $out }
        foreach pair [split $s ";"] {
            set idx [string first "=" $pair]
            if {$idx < 0} { continue }
            set k [string trim [string range $pair 0 [expr {$idx - 1}]]]
            set v [string trim [string range $pair [expr {$idx + 1}] end]]
            lappend out $k $v
        }
        return $out
    }

    # Apply global assignments: seed + any extras from SUPER_Q_EXTRA.
    proc apply_assignments {} {
        variable seed
        variable extra_list

        set_global_assignment -name SEED $seed
        # Faster re-runs: keep existing Quartus's own intermediate dbs off
        # the shared filesystem when possible.
        set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL

        foreach {name val} $extra_list {
            # Let the user pass either `MY_NAME=VALUE` or
            # `MY_NAME[ENTITY]=VALUE` (instance assignment — not parsed here,
            # falls through to the global path which Quartus will warn on if
            # wrong).
            set_global_assignment -name $name $val
        }
    }

    # Open or create the project reliably. Quartus requires a project name
    # (no extension); we accept either form from argv.
    proc open_project_safe {name} {
        # Strip .qpf if the caller supplied one.
        set stem [file rootname $name]
        if {[project_exists $stem]} {
            project_open $stem -revision $stem -force
        } else {
            puts "super-q: ERROR no such project $stem in [pwd]"
            exit 2
        }
    }

    # Emit a JSON timing summary alongside the .sta.rpt. We parse the STA
    # panels ourselves because the Quartus Tcl API for panel data is stable
    # across Lite 21–24.
    proc dump_timing_json {outfile} {
        package require ::quartus::report

        load_report

        set result(passed) 1
        set result(worst_setup_slack_ns) ""
        set result(worst_hold_slack_ns) ""
        set result(summary) ""
        set clocks [dict create]

        # Setup slack
        set panels [get_report_panel_names]
        foreach panel $panels {
            if {[string match {*Clocks*Setup*Slack*} $panel] ||
                [string match {*Worst-Case Setup Slack*} $panel] ||
                [string match {*Setup Summary*} $panel]} {
                set rows [get_number_of_rows -name $panel]
                for {set r 1} {$r < $rows} {incr r} {
                    set name [get_report_panel_data -name $panel -row $r -col 0]
                    set slack [get_report_panel_data -name $panel -row $r -col 1]
                    if {$name eq "" || $slack eq ""} { continue }
                    if {![string is double -strict $slack]} { continue }
                    if {![dict exists $clocks $name]} {
                        dict set clocks $name [dict create name $name]
                    }
                    dict set clocks $name setup_slack_ns $slack
                    if {$result(worst_setup_slack_ns) eq "" ||
                        $slack < $result(worst_setup_slack_ns)} {
                        set result(worst_setup_slack_ns) $slack
                    }
                    if {$slack < 0} { set result(passed) 0 }
                }
            }
            if {[string match {*Hold Slack*} $panel] ||
                [string match {*Hold Summary*} $panel]} {
                set rows [get_number_of_rows -name $panel]
                for {set r 1} {$r < $rows} {incr r} {
                    set name [get_report_panel_data -name $panel -row $r -col 0]
                    set slack [get_report_panel_data -name $panel -row $r -col 1]
                    if {$name eq "" || $slack eq ""} { continue }
                    if {![string is double -strict $slack]} { continue }
                    if {![dict exists $clocks $name]} {
                        dict set clocks $name [dict create name $name]
                    }
                    dict set clocks $name hold_slack_ns $slack
                    if {$result(worst_hold_slack_ns) eq "" ||
                        $slack < $result(worst_hold_slack_ns)} {
                        set result(worst_hold_slack_ns) $slack
                    }
                    if {$slack < 0} { set result(passed) 0 }
                }
            }
            if {[string match {*Fmax Summary*} $panel]} {
                set rows [get_number_of_rows -name $panel]
                for {set r 1} {$r < $rows} {incr r} {
                    set fmax [get_report_panel_data -name $panel -row $r -col 0]
                    set restricted [get_report_panel_data -name $panel -row $r -col 1]
                    set name [get_report_panel_data -name $panel -row $r -col 2]
                    if {$name eq "" || $fmax eq ""} { continue }
                    regexp {([-+]?[0-9]*\.?[0-9]+)} $fmax fmax_v
                    regexp {([-+]?[0-9]*\.?[0-9]+)} $restricted rest_v
                    if {![dict exists $clocks $name]} {
                        dict set clocks $name [dict create name $name]
                    }
                    if {[info exists fmax_v]} {
                        dict set clocks $name fmax_mhz $fmax_v
                    }
                    if {[info exists rest_v]} {
                        dict set clocks $name restricted_fmax_mhz $rest_v
                    }
                }
            }
        }

        unload_report

        # Build the JSON by hand (tclsh has no json module in Lite).
        set fh [open $outfile w]
        puts $fh "{"
        puts $fh "  \"passed\": [expr {$result(passed) ? "true" : "false"}],"
        puts $fh "  \"worst_setup_slack_ns\": [superq::json_num $result(worst_setup_slack_ns)],"
        puts $fh "  \"worst_hold_slack_ns\": [superq::json_num $result(worst_hold_slack_ns)],"
        puts $fh "  \"summary\": \"[superq::json_str $result(summary)]\","
        puts $fh "  \"clocks\": \["
        set first 1
        dict for {name clk} $clocks {
            if {!$first} { puts $fh "    ," }
            set first 0
            puts $fh "    {"
            puts $fh "      \"name\": \"[superq::json_str $name]\","
            puts $fh "      \"setup_slack_ns\": [superq::json_num [superq::dict_get_or $clk setup_slack_ns ""]],"
            puts $fh "      \"hold_slack_ns\": [superq::json_num [superq::dict_get_or $clk hold_slack_ns ""]],"
            puts $fh "      \"fmax_mhz\": [superq::json_num [superq::dict_get_or $clk fmax_mhz ""]],"
            puts $fh "      \"restricted_fmax_mhz\": [superq::json_num [superq::dict_get_or $clk restricted_fmax_mhz ""]]"
            puts $fh "    }"
        }
        puts $fh "  \]"
        puts $fh "}"
        close $fh
    }

    proc dict_get_or {d k default} {
        if {[dict exists $d $k]} { return [dict get $d $k] }
        return $default
    }

    proc json_num {v} {
        if {$v eq ""} { return "null" }
        if {[string is double -strict $v]} { return $v }
        return "null"
    }

    proc json_str {v} {
        # Escape JSON special chars conservatively.
        set v [string map {\\ \\\\ \" \\\" \n \\n \r \\r \t \\t} $v]
        return $v
    }

    # Initialize from env. Called at top of each wrapper script.
    proc init {argv} {
        variable seed
        variable project
        variable extra_list

        set seed [env_or SUPER_Q_SEED 1]
        if {[llength $argv] >= 1} {
            set project [lindex $argv 0]
        } else {
            set project [env_or SUPER_Q_PROJECT ""]
        }
        if {$project eq ""} {
            puts "super-q: ERROR project name missing (argv[0] or SUPER_Q_PROJECT)"
            exit 2
        }
        set extra_list [parse_extra [env_or SUPER_Q_EXTRA ""]]

        puts "super-q: project=$project seed=$seed"
    }
}
