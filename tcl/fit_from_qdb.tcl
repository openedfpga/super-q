# Fitter + assembler + STA, starting from a pre-synthesized .qdb.
#
# Usage: quartus_sh -t fit_from_qdb.tcl <project-name>
# Env:
#   SUPER_Q_SEED   fitter seed
#   SUPER_Q_QDB    absolute path to the .qdb checkpoint
#
# This is the fast half of split-mode seed sweeps.

source [file join [file dirname [info script]] common.tcl]

superq::init $argv

set qdb [superq::env_or SUPER_Q_QDB ""]
if {$qdb eq ""} {
    puts "super-q: ERROR SUPER_Q_QDB not set"
    exit 2
}

superq::open_project_safe $superq::project
superq::apply_assignments

# Bring in the checkpoint. Newer Quartus: `import_design_partition`; older
# versions read it automatically when placed in qdb/.
if {[llength [info commands ::quartus::import_design_partition]] > 0} {
    if {[catch {import_design_partition $qdb -partition_name Top} err]} {
        puts "super-q: WARN import_design_partition: $err"
    }
}

# Make sure we produce a bitstream even though synthesis already ran.
set_global_assignment -name GENERATE_RBF_FILE ON

if {[catch {execute_module -tool fit} err]} {
    puts "super-q: ERROR fitter failed: $err"
    project_close
    exit 3
}
if {[catch {execute_module -tool asm} err]} {
    puts "super-q: ERROR assembler failed: $err"
    project_close
    exit 3
}
if {[catch {execute_module -tool sta} err]} {
    puts "super-q: ERROR sta failed: $err"
    project_close
    exit 3
}

set out_json [file join [pwd] "output_files" "timing.json"]
if {[catch {superq::dump_timing_json $out_json} err]} {
    puts "super-q: WARN timing json dump failed: $err"
}

project_close
puts "super-q: fitter done seed=$superq::seed"
