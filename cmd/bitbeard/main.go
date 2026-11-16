// Command bitbeard is the Bitbeard Bootstrap binary: the deployment entry point
// for the stack. Run it on a fresh machine to prepare the machine (setup),
// check it (doctor), or update an existing install (update).
//
// The subcommands are stubs for now; their behaviour lands in later tickets.
package main

import (
	"fmt"
	"os"
)

// Injected at build time via -ldflags "-X main.version=... -X main.commit=...".
var (
	version = "dev"
	commit  = "unknown"
)

// USER-FACING
const usage = `bitbeard — the Bitbeard Bootstrap binary

Prepares a machine to run the Bitbeard stack, checks it, and keeps it current.

Usage:

  bitbeard <command>

Commands:

  setup    prepare this machine to run the stack
  doctor   check prerequisites, configuration and stack health
  update   update the bitbeard binary and the running stack

  version  print the bitbeard version
  help     show this help

Use "bitbeard <command>" to see what a command will do.`

// USER-FACING
const usageSetup = `bitbeard setup

Prepare this machine to run the Bitbeard stack: install prerequisites, fetch the
repository, collect credentials, and bring the stack up. Rerunning it enables a
skipped Optional Subsystem or revisits any credential.

(Not implemented yet — this stub only reports what it will do.)`

// USER-FACING
const usageDoctor = `bitbeard doctor

Check this machine and the running stack without changing anything: verifies
prerequisites, configuration, and stack health.

(Not implemented yet — this stub only reports what it will do.)`

// USER-FACING
const usageUpdate = `bitbeard update

Update the bitbeard binary and the running stack to the latest release.

(Not implemented yet — this stub only reports what it will do.)`

func main() {
	if len(os.Args) < 2 {
		// USER-FACING
		fmt.Fprintln(os.Stdout, usage)
		os.Exit(0)
	}

	switch os.Args[1] {
	case "setup":
		// USER-FACING
		fmt.Fprintln(os.Stdout, usageSetup)
	case "doctor":
		// USER-FACING
		fmt.Fprintln(os.Stdout, usageDoctor)
	case "update":
		// USER-FACING
		fmt.Fprintln(os.Stdout, usageUpdate)
	case "version", "--version":
		// USER-FACING
		fmt.Fprintf(os.Stdout, "bitbeard %s (commit %s)\n", version, commit)
	case "help", "-h", "--help":
		// USER-FACING
		fmt.Fprintln(os.Stdout, usage)
	default:
		// USER-FACING
		fmt.Fprintf(os.Stderr, "bitbeard: unknown command %q\n\n", os.Args[1])
		// USER-FACING
		fmt.Fprintln(os.Stderr, usage)
		os.Exit(1)
	}
}
