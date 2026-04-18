# Run synthesis only and save a post-synth checkpoint the fitter can reuse.
#
# Usage: quartus_sh -t synth_only.tcl <project-name>
#
# This is the first half of the two-stage "split" seed sweep flow.
# Synthesis is seed-independent, so we pay for it once and then many
# seeds run fitter-only against the resulting qdb.

source [file join [file dirname [info script]] common.tcl]

superq::init $argv
superq::open_project_safe $superq::project

# Synthesis-only run. We disable assembly/STA here — the fitter stage does that.
set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL
set_global_assignment -name OPTIMIZATION_MODE "HIGH PERFORMANCE EFFORT"

# Run analysis + synthesis.
if {[catch {execute_module -tool syn} err]} {
    puts "super-q: ERROR quartus_syn failed: $err"
    project_close
    exit 3
}

# Export a qdb checkpoint we can reuse from the fitter.
file mkdir qdb
set qdb_path [file join [pwd] "qdb" "$superq::project.qdb"]
if {[catch {
    # Quartus Prime 21.1+ exposes export_design_partition; earlier versions
    # used write_project_database. We try the modern path first.
    if {[llength [info commands ::quartus::export_design_partition]] > 0} {
        export_design_partition -snapshot synthesized -partition_name Top -file $qdb_path
    } else {
        # Fallback: re-run with --write_qdb=<path>
        exec quartus_syn [file tail $superq::project] --write_qdb=$qdb_path
    }
} err]} {
    puts "super-q: WARN qdb export failed: $err"
}

project_close
puts "super-q: synthesis done, qdb at $qdb_path"
