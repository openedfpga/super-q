# Re-run STA on an existing compile and emit timing.json.
#
# Usage: quartus_sh -t timing_report.tcl <project-name>
#
# Used by `superq inspect <dir>` — lets agents get structured timing
# without triggering a rebuild. Handy after a manual Quartus GUI run.

source [file join [file dirname [info script]] common.tcl]

superq::init $argv
superq::open_project_safe $superq::project

if {[catch {execute_module -tool sta} err]} {
    puts "super-q: ERROR sta failed: $err"
    project_close
    exit 3
}

set out_json [file join [pwd] "output_files" "timing.json"]
superq::dump_timing_json $out_json

project_close
puts "super-q: timing report dumped to $out_json"
