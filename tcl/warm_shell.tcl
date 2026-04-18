# Persistent `quartus_sh` REPL.
#
# Source this from `quartus_sh -t` and the shell becomes a synchronous
# request/response loop on stdin/stdout. One line of input is an opaque
# request id followed by a TCL body; we echo a framed response with the
# same id so the Python side can correlate without ordering assumptions.
#
# Protocol (on a single line per message):
#
#   client → shell:   "<id>\t<tcl body>\n"
#   shell  → client:  "<<<BEGIN id>>>\n<multi-line output>\n<<<END id status>>>\n"
#                      where status is "OK" or "ERR"
#
# This is deliberately newline-framed and minimally escaped because
# everything that runs here is code we control (TCL wrappers shipped
# inside this repo). If we ever want to let users dispatch arbitrary
# TCL, we'd escape embedded newlines into \n first.

source [file join [file dirname [info script]] common.tcl]

# Flush immediately — Quartus pipes stdout with line buffering only when
# isatty() is true, which subprocess pipes are not.
fconfigure stdout -buffering line -translation lf
fconfigure stdin  -buffering line -translation lf

namespace eval superq_repl {
    variable running 1
    variable open_project ""

    proc send_frame {id status body} {
        puts "<<<BEGIN $id>>>"
        if {$body ne ""} { puts -nonewline $body; if {[string index $body end] ne "\n"} { puts "" } }
        puts "<<<END $id $status>>>"
        flush stdout
    }

    proc handle_line {line} {
        variable running

        # Split off request id (first whitespace-separated token) from body.
        set line [string trim $line]
        if {$line eq ""} { return }
        set tab [string first "\t" $line]
        if {$tab < 0} {
            # No body — maybe a control word.
            set id $line
            set body ""
        } else {
            set id [string range $line 0 [expr {$tab - 1}]]
            set body [string range $line [expr {$tab + 1}] end]
        }

        if {$body eq "__SUPERQ_QUIT__"} {
            send_frame $id OK "bye"
            set running 0
            return
        }
        if {$body eq "__SUPERQ_PING__"} {
            send_frame $id OK "pong"
            return
        }

        # Capture stdout produced by the TCL body so the framer stays clean.
        set chan [chan create write [namespace current]::capture_chan]
        set ::superq_capture ""
        chan configure $chan -translation lf -buffering none
        interp alias {} ::_superq_puts {} puts
        rename puts ::_superq_real_puts
        proc puts {args} {
            # Route plain puts calls through our capture channel unless the
            # caller explicitly names a channel.
            if {[llength $args] >= 2 && [lindex $args 0] ni {-nonewline}} {
                ::_superq_real_puts {*}$args
                return
            }
            set idx 0
            set nonewline 0
            if {[lindex $args 0] eq "-nonewline"} { set nonewline 1; incr idx }
            set text [lindex $args $idx]
            append ::superq_capture $text
            if {!$nonewline} { append ::superq_capture "\n" }
        }

        set rc [catch {uplevel #0 $body} result]

        # Restore puts.
        rename puts {}
        rename ::_superq_real_puts puts
        catch {chan close $chan}

        set combined $::superq_capture
        if {$result ne ""} { append combined $result }
        send_frame $id [expr {$rc == 0 ? "OK" : "ERR"}] $combined
    }

    # Dummy channel handler — we never actually use the channel chan itself,
    # just need `puts $chan …` to be callable. We capture via the shim above.
    proc capture_chan {args} { return 0 }

    proc loop {} {
        variable running
        while {$running} {
            if {[eof stdin]} { return }
            set line [gets stdin]
            if {$line eq "" && [eof stdin]} { return }
            handle_line $line
        }
    }
}

# Announce ready so the client knows we're past Quartus's startup banner.
puts "<<<SUPERQ-WARM-READY>>>"
flush stdout
superq_repl::loop
