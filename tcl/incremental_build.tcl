# Incremental compile: preserve db/incremental_db across runs and keep
# the placement of the untouched Top partition from the previous build.
#
# The speed win comes from Quartus skipping re-synthesis of partitions
# that haven't changed. For Pocket cores, typical small RTL edits touch
# one sub-entity; synth + place for everything else is reused.
#
# Usage (via warm shell or one-shot):
#   quartus_sh -t incremental_build.tcl <project-name>
#
# Preconditions the Python caller sets up:
#   * SUPER_Q_SEED may be provided (defaults to the last used seed)
#   * the core's quartus_dir IS the cwd (no sandbox copy in incremental mode)

source [file join [file dirname [info script]] common.tcl]

superq::init $argv
superq::open_project_safe $superq::project

# Turn on the mode. Quartus will no-op these if already set.
set_global_assignment -name INCREMENTAL_COMPILATION FULL_INCREMENTAL_COMPILATION
set_global_assignment -name PRESERVE_UNUSED_XCVR_CHANNEL ON

# A minimal single-partition scheme: mark the top as a preserved partition
# on the first build; subsequent builds will benefit. If the design already
# declares partitions, we don't clobber them.
set has_partitions 0
foreach a [get_all_assignments -type global -name PARTITION_NETLIST_TYPE] {
    incr has_partitions
    break
}
if {!$has_partitions} {
    if {![catch {
        set_instance_assignment -name PARTITION_HIERARCHY root_partition \
            -to | -section_id Top
        set_global_assignment -name PARTITION_NETLIST_TYPE POST_FIT \
            -section_id Top
        set_global_assignment -name PARTITION_FITTER_PRESERVATION_LEVEL \
            PLACEMENT_AND_ROUTING -section_id Top
        set_global_assignment -name PARTITION_COLOR 16764057 -section_id Top
    } err]} {
        puts "super-q: WARN could not declare Top partition: $err"
    }
}

superq::apply_assignments
set_global_assignment -name GENERATE_RBF_FILE ON

# Fast-path: incremental compile. Quartus internally skips synth for
# unchanged partitions and reuses placement when the preservation level
# is PLACEMENT_AND_ROUTING (and timing still meets). If it can't reuse,
# it falls back to a full run silently.
if {[catch {execute_flow -compile} err]} {
    puts "super-q: ERROR incremental compile failed: $err"
    project_close
    exit 3
}

set out_json [file join [pwd] "output_files" "timing.json"]
if {[catch {superq::dump_timing_json $out_json} err]} {
    puts "super-q: WARN timing json dump failed: $err"
}

project_close
puts "super-q: incremental build done seed=$superq::seed"
