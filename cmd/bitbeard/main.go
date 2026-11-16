// Command bitbeard is the Bitbeard Bootstrap binary: the deployment entry point
// for the stack. Run it on a fresh machine to prepare the machine (setup),
// check it (doctor), or update an existing install (update).
//
// setup runs the prerequisite stage, the fetch stage (clone/reuse of the
// stack repository), the configuration and boot stage, and finally the
// doctor as its verification step. update pulls the stack clone to the
// latest revision, re-checks prerequisites, rebuilds and restarts the
// stack, and finishes with the doctor.
package main

import (
	"errors"
	"fmt"
	"os"

	"github.com/ja-armstrong/bb/internal/doctor"
	"github.com/ja-armstrong/bb/internal/fetch"
	"github.com/ja-armstrong/bb/internal/prereqs"
	"github.com/ja-armstrong/bb/internal/update"
	"github.com/ja-armstrong/bb/internal/wizard"
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

func main() {
	if len(os.Args) < 2 {
		// USER-FACING
		fmt.Fprintln(os.Stdout, usage)
		os.Exit(0)
	}

	switch os.Args[1] {
	case "setup":
		// USER-FACING
		fmt.Fprintln(os.Stdout, "bitbeard setup — stage 1: prerequisites")
		// Prerequisite stage of the Bootstrap; later stages land in their own tickets.
		res, err := prereqs.Ensure(prereqs.NewRunner(), os.Stdout, "")
		switch {
		case err == nil:
			// continue to the daemon-reachability stop and the fetch stage below
		case errors.Is(err, prereqs.ErrUnsupportedDistro):
			// Instructions were printed by the prerequisite stage.
			os.Exit(1)
		default:
			// USER-FACING
			fmt.Fprintf(os.Stderr, "bitbeard setup: %v\n", err)
			os.Exit(1)
		}
		// The operator did nothing wrong, so this stop is a clean exit: the
		// prerequisite stage could not reach the Docker daemon from this
		// session (for example, the freshly added `docker` group only takes
		// effect in new login sessions). Walking on would only fail later,
		// when the wizard boots the stack with `docker compose`.
		if !res.DaemonReachable {
			if res.Status == prereqs.StatusOK {
				// USER-FACING
				fmt.Fprintln(os.Stdout, "Docker is present, but this session cannot reach the Docker daemon: it may not be running, or this session may not have permission to it.")
			} else {
				// USER-FACING
				fmt.Fprintln(os.Stdout, "Docker was installed, but this session cannot reach the Docker daemon yet: permission changes such as group membership only take effect in a new login session.")
			}
			// USER-FACING
			fmt.Fprintln(os.Stdout, "Open a new shell (or log out and back in), then rerun `bitbeard setup`. Rerunning is safe and idempotent: installed prerequisites are skipped, the clone is reused, and the wizard runs in the new session.")
			os.Exit(0)
		}
		// Fetch stage of the Bootstrap: clone or reuse the stack repository.
		fetchRes, err := fetch.Setup(os.Stdout)
		if err != nil {
			// USER-FACING
			fmt.Fprintf(os.Stderr, "bitbeard: %v\n", err)
			os.Exit(1)
		}
		// Configuration and boot stage of the Bootstrap: prompt for every
		// required key and the Optional Subsystems (Media Server choice,
		// Bot keys), write .env, and bring the stack up.
		if _, err := wizard.Run(wizard.Options{
			StackDir: fetchRes.StackPath,
			In:       os.Stdin,
			Out:      os.Stdout,
		}); err != nil {
			// USER-FACING
			fmt.Fprintf(os.Stderr, "bitbeard: %v\n", err)
			os.Exit(1)
		}
		// Verification step of the Bootstrap: the doctor's check-only sweep
		// of prerequisites, .env guardrails and stack health.
		// USER-FACING
		fmt.Fprintln(os.Stdout, "bitbeard setup — final verification (the doctor; it changes nothing)")
		doctorRes := doctor.Run(doctor.Options{
			StackDir: fetchRes.StackPath,
			Out:      os.Stdout,
		})
		if doctorRes.Failed {
			// USER-FACING
			fmt.Fprintln(os.Stdout, "bitbeard setup is done, but the doctor reported problems above. Follow the fix hints, then rerun `bitbeard doctor` to confirm.")
			os.Exit(1)
		}
	case "doctor":
		// Check-only mode of the Bootstrap: report, never change.
		if doctor.Run(doctor.Options{Out: os.Stdout}).Failed {
			os.Exit(1)
		}
	case "update":
		// One command: pull, re-check prerequisites, rebuild + up, doctor
		// last. A stopping point (dirty tree, failed pull, vanished
		// prerequisite, failed build or up) is an error and exits non-zero.
		res, err := update.Run(update.Options{Out: os.Stdout})
		if err != nil {
			// USER-FACING
			fmt.Fprintf(os.Stderr, "bitbeard update: %v\n", err)
			os.Exit(1)
		}
		if res.DoctorFailed {
			// USER-FACING
			fmt.Fprintln(os.Stdout, "bitbeard update is done, but the doctor reported problems above. Follow the fix hints, then rerun `bitbeard doctor` to confirm.")
			os.Exit(1)
		}
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
