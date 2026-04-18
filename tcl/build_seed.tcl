# Full compile driven by $SUPER_Q_SEED.
#
# Usage: quartus_sh -t build_seed.tcl <project-name>
# Env:
#   SUPER_Q_SEED    fitter seed (default: 1)
#   SUPER_Q_EXTRA   optional `k=v;k=v` Quartus global assignments
#
# Produces:
#   output_files/<project>.rbf       (if GENERATE_RBF_FILE is on)
#   output_files/<project>.sta.rpt
#   output_files/timing.json

source [file join [file dirname [info script]] common.tcl]

superq::init $argv
superq::open_project_safe $superq::project

# Apply seed + extras before flow runs.
superq::apply_assignments

# Make sure a bit-stream is actually emitted. Users sometimes forget this
# in the .qsf and then wonder why there's no .rbf. We don't force the
# filename — Quartus will pick `<project>.rbf`.
set_global_assignment -name GENERATE_RBF_FILE ON
set_global_assignment -name OUTPUT_IO_TIMING_FAR_END_VMEAS "HALF VCCIO"

# Advanced timing-closure nudges that are safe defaults for Pocket cores.
# Users can override any of these via SUPER_Q_EXTRA.
set_global_assignment -name OPTIMIZATION_MODE "HIGH PERFORMANCE EFFORT"
set_global_assignment -name PLACEMENT_EFFORT_MULTIPLIER 1.0
set_global_assignment -name ROUTER_TIMING_OPTIMIZATION_LEVEL MAXIMUM
set_global_assignment -name FITTER_EFFORT "STANDARD FIT"
set_global_assignment -name PHYSICAL_SYNTHESIS_EFFORT EXTRA

# Re-apply extras so user overrides win over our defaults.
superq::apply_assignments

# Run full flow: analysis & synthesis → fitter → assembler → STA.
if {[catch {execute_flow -compile} err]} {
    puts "super-q: ERROR execute_flow failed: $err"
    project_close
    exit 3
}

# Dump timing JSON for the Python side.
set out_json [file join [pwd] "output_files" "timing.json"]
if {[catch {superq::dump_timing_json $out_json} err]} {
    puts "super-q: WARN timing json dump failed: $err"
}

project_close
puts "super-q: done seed=$superq::seed"
