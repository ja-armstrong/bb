// Package doctor implements Bitbeard Bootstrap's check-only mode: it
// verifies prerequisites, the stack's .env guardrails, and the health of the
// running stack, reporting pass/fail per check in plain language and
// pointing at the fix for every failure. It changes nothing: every probe is
// a read — prerequisite detection, a file read, `docker compose ps`, and
// plain HTTP GETs.
package doctor

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ja-armstrong/bb/internal/fetch"
	"github.com/ja-armstrong/bb/internal/prereqs"
	"github.com/ja-armstrong/bb/internal/wizard"
)

// WebUIURL is where the Webui is published on the host.
const WebUIURL = "http://127.0.0.1:8081"

// APIHealthURL is where the API is reachable from the host: the Webui proxy
// forwards /api/ into gluetun's network namespace, which is the only route
// the host has to the API Subsystem.
const APIHealthURL = WebUIURL + "/api/health"

// Check is the outcome of one doctor check.
type Check struct {
	// Name is the short label shown in the report line.
	Name string
	// Pass is true when the check passed.
	Pass bool
	// Detail explains the outcome in plain language; "" means the name
	// alone says it all.
	Detail string
	// Fix is what to do about a failure; empty on a pass.
	Fix string
}

// Result reports what the doctor found.
type Result struct {
	// Checks are every check that ran, in report order.
	Checks []Check
	// Failed is true when at least one check failed.
	Failed bool
}

// Options parameterise Run.
type Options struct {
	// StackDir is the stack repository root; "" discovers it (the fetch
	// stage's state file, then the default ~/bitbeard).
	StackDir string
	// Compose is the docker-compose boundary; nil means the real CLI.
	// The doctor only ever calls PS — check-only by construction.
	Compose wizard.Compose
	// Prober issues the HTTP reachability probes; nil means a plain
	// client with a short timeout.
	Prober Prober
	// Runner is the command boundary for prerequisite detection; nil
	// means the production Runner.
	Runner prereqs.Runner
	// Out receives the report (os.Stdout outside tests).
	Out io.Writer
}

// Prober is the slice of HTTP the doctor's reachability probes need. GET
// only: the doctor never mutates anything.
type Prober interface {
	// Get issues a GET and returns the response status code. Any status
	// code is a result; transport errors are errors.
	Get(url string) (int, error)
}

// HTTPProber is the production Prober: real net/http GETs.
type HTTPProber struct {
	// Client is the HTTP client; nil means a 5-second-timeout client.
	Client *http.Client
}

// Get issues a GET and returns the response status code.
func (p HTTPProber) Get(url string) (int, error) {
	c := p.Client
	if c == nil {
		c = &http.Client{Timeout: 5 * time.Second}
	}
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	resp, err := c.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body) // drained so the connection is reusable
	return resp.StatusCode, nil
}

// FindStackDir resolves where the stack repository lives: an explicit
// stackDir wins; otherwise the location remembered by the fetch stage; else
// the default ~/bitbeard. Read-only: a missing or wrong directory is
// reported by the checks, never fixed here.
func FindStackDir(stackDir string) string {
	if stackDir != "" {
		return stackDir
	}
	if configDir, err := os.UserConfigDir(); err == nil {
		if st, err := fetch.LoadState(fetch.StatePath(configDir)); err == nil && st.StackPath != "" {
			return st.StackPath
		}
	}
	if home, err := os.UserHomeDir(); err == nil {
		return fetch.DefaultTarget(home)
	}
	return "bitbeard"
}

// Run executes every doctor check and writes the report to o.Out. It never
// changes anything; the returned Result tells the caller whether any check
// failed (a non-zero exit is the caller's job).
func Run(o Options) Result {
	if o.Out == nil {
		o.Out = os.Stdout
	}
	if o.Runner == nil {
		o.Runner = prereqs.NewRunner()
	}
	if o.Compose == nil {
		o.Compose = wizard.DockerCompose{}
	}
	if o.Prober == nil {
		o.Prober = HTTPProber{}
	}
	out := o.Out
	var res Result

	say := func(c Check) {
		res.Checks = append(res.Checks, c)
		if !c.Pass {
			res.Failed = true
		}
		printCheck(out, c)
	}

	// USER-FACING
	fmt.Fprintln(out, "bitbeard doctor — checking prerequisites, configuration and stack health (changes nothing)")
	// USER-FACING
	fmt.Fprintln(out, "")

	// Prerequisites: detect only — the doctor never installs anything.
	docker, compose := prereqs.Detect(o.Runner)
	say(Check{
		Name: "Docker",
		Pass: docker,
		// USER-FACING
		Detail: passFailDetail(docker, "present", "not found"),
		// USER-FACING
		Fix: "rerun `bitbeard setup` to install it",
	})
	say(Check{
		Name: "Docker Compose",
		Pass: compose,
		// USER-FACING
		Detail: passFailDetail(compose, "present", "not found"),
		// USER-FACING
		Fix: "rerun `bitbeard setup` to install it",
	})

	// Everything below needs the stack directory; a missing one fails
	// once here instead of eight times below.
	stackDir := FindStackDir(o.StackDir)
	composeDir, derr := wizard.FindComposeDir(stackDir)
	if derr != nil {
		say(Check{
			Name: "Stack directory",
			Pass: false,
			// USER-FACING
			Detail: derr.Error(),
			// USER-FACING
			Fix: "rerun `bitbeard setup` to fetch the stack",
		})
		printSummary(out, res)
		return res
	}

	// .env guardrails: exists, parses, required keys set, no change-me
	// placeholders, and gitignored. All read-only.
	envPath := filepath.Join(composeDir, ".env")
	data, rerr := os.ReadFile(envPath)
	var env wizard.Env
	switch {
	case rerr != nil:
		say(Check{
			Name: ".env",
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("not found at %s", envPath),
			// USER-FACING
			Fix: "rerun `bitbeard setup` — it writes .env from fresh answers",
		})
	default:
		var perr error
		env, perr = wizard.ParseEnv(string(data))
		if perr != nil {
			say(Check{
				Name: ".env",
				Pass: false,
				// USER-FACING
				Detail: fmt.Sprintf("%s could not be parsed: %v", envPath, perr),
				// USER-FACING
				Fix: "rerun `bitbeard setup` — it diagnoses and can rewrite .env from fresh answers",
			})
		} else {
			say(Check{
				Name: ".env",
				Pass: true,
				// USER-FACING
				Detail: envPath,
			})
			probs := wizard.GuardrailProblems(env)
			if len(probs) == 0 {
				say(Check{
					Name: ".env guardrails",
					Pass: true,
					// USER-FACING
					Detail: "every required key is set and no change-me placeholder is left",
				})
			} else {
				say(Check{
					Name: ".env guardrails",
					Pass: false,
					// USER-FACING
					Detail: strings.Join(probs, "; "),
					// USER-FACING
					Fix: "rerun `bitbeard setup` — a valid .env is kept, a broken one can be rewritten from fresh answers",
				})
			}
			if wizard.EnvIsIgnored(composeDir) {
				say(Check{
					Name: ".env is gitignored",
					Pass: true,
				})
			} else {
				say(Check{
					Name: ".env is gitignored",
					Pass: false,
					// USER-FACING
					Detail: fmt.Sprintf("%s does not ignore .env", filepath.Join(composeDir, ".gitignore")),
					// USER-FACING
					Fix: "add `/.env` to that .gitignore yourself — the doctor changes nothing, but the credentials file must stay out of git",
				})
			}
		}
	}

	// Stack health: what docker actually reports about the mandatory
	// Subsystems.
	var byName map[string]wizard.ComposeContainer
	ps, perr := o.Compose.PS(composeDir)
	cs, cerr := wizard.ParseComposePS(ps)
	switch {
	case perr != nil:
		say(Check{
			Name: "Running stack",
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("docker compose ps failed: %v", perr),
			// USER-FACING
			Fix: "if the Docker daemon is stopped, start it; if the stack was never booted, rerun `bitbeard setup`",
		})
	case cerr != nil:
		say(Check{
			Name: "Running stack",
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("could not read the docker compose ps output: %v", cerr),
			// USER-FACING
			Fix: "rerun `bitbeard doctor` — if this repeats, the docker CLI is behaving oddly",
		})
	default:
		byName = make(map[string]wizard.ComposeContainer, len(cs))
		for _, c := range cs {
			byName[strings.TrimPrefix(c.Name, "/")] = c
		}
		for _, name := range wizard.MandatorySubsystemNames() {
			say(containerCheck(name, byName[name], composeDir))
		}
		for _, name := range wizard.MandatoryInitSubsystemNames() {
			say(initCheck(name, byName[name], composeDir))
		}
	}

	// Reachability from the host. Independent of the ps output above: a
	// ps hiccup should not hide what the ports actually do.
	say(reachCheck("Webui answers on host port 8081", WebUIURL, o.Prober,
		// USER-FACING
		"check `docker compose logs webui` in "+composeDir+"; the host port mapping lives in the gluetun Subsystem's ports in docker-compose.yml"))
	say(reachCheck("API answers (GET /api/health through the Webui proxy)", APIHealthURL, o.Prober,
		// USER-FACING
		"check `docker compose logs api` in "+composeDir+"; note the API is only reachable from the host through the Webui proxy"))

	printSummary(out, res)
	return res
}

// passFailDetail picks the detail for a boolean check.
func passFailDetail(pass bool, ok, bad string) string {
	if pass {
		return ok
	}
	return bad
}

// containerCheck judges one mandatory Subsystem's container. Gluetun gets
// its own failure hint because the VPN credentials are the usual cause.
func containerCheck(name string, c wizard.ComposeContainer, composeDir string) Check {
	if c.Name == "" {
		return Check{
			Name: name,
			Pass: false,
			// USER-FACING
			Detail: "is not part of the running stack",
			// USER-FACING
			Fix: fmt.Sprintf("bring it up with `docker compose up -d` in %s", composeDir),
		}
	}
	if c.State == "running" {
		return Check{
			Name: name,
			Pass: true,
			// USER-FACING
			Detail: "running",
		}
	}
	if name == "gluetun" {
		return Check{
			Name: name,
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("is %s (expected running)", c.State),
			// USER-FACING
			Fix: "check `docker compose logs gluetun` in " + composeDir + " — the VPN credentials in .env are the usual cause; then rerun `bitbeard setup` to reboot the stack",
		}
	}
	return Check{
		Name: name,
		Pass: false,
		// USER-FACING
		Detail: fmt.Sprintf("is %s (expected running)", c.State),
		// USER-FACING
		Fix: fmt.Sprintf("bring it up with `docker compose up -d` in %s; `docker compose logs %s` shows why it stopped", composeDir, name),
	}
}

// initCheck judges a one-shot init container: absent (cleaned up after
// completing), running (first boot still settling), or exited 0 all pass;
// anything else failed.
func initCheck(name string, c wizard.ComposeContainer, composeDir string) Check {
	switch {
	case c.Name == "":
		return Check{
			Name: name,
			Pass: true,
			// USER-FACING
			Detail: "not present (it finished and was cleaned up)",
		}
	case c.State == "running":
		return Check{
			Name: name,
			Pass: true,
			// USER-FACING
			Detail: "still working (first boot)",
		}
	case c.State == "exited" && c.ExitCode == 0:
		return Check{
			Name: name,
			Pass: true,
			// USER-FACING
			Detail: "exited 0",
		}
	default:
		return Check{
			Name: name,
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("is %s with exit code %d (expected a clean exit 0)", c.State, c.ExitCode),
			// USER-FACING
			Fix: fmt.Sprintf("rerun it with `docker compose up init-configs` in %s; its output shows what failed", composeDir),
		}
	}
}

// reachCheck probes url and judges the answer: a 200 passes.
func reachCheck(name, url string, p Prober, fix string) Check {
	status, err := p.Get(url)
	switch {
	case err != nil:
		return Check{
			Name: name,
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("%s did not answer: %v", url, err),
			// USER-FACING
			Fix: fix,
		}
	case status != http.StatusOK:
		return Check{
			Name: name,
			Pass: false,
			// USER-FACING
			Detail: fmt.Sprintf("%s answered %d (expected 200)", url, status),
			// USER-FACING
			Fix: fix,
		}
	default:
		return Check{
			Name: name,
			Pass: true,
			// USER-FACING
			Detail: fmt.Sprintf("%s answered 200", url),
		}
	}
}

// printCheck writes one report line, plus the fix hint on a failure.
func printCheck(out io.Writer, c Check) {
	mark := "[ ok ]"
	if !c.Pass {
		mark = "[FAIL]"
	}
	line := c.Name
	if c.Detail != "" {
		line = c.Name + ": " + c.Detail
	}
	// USER-FACING
	fmt.Fprintf(out, "  %-6s %s\n", mark, line)
	if !c.Pass && c.Fix != "" {
		// USER-FACING
		fmt.Fprintf(out, "         fix: %s\n", c.Fix)
	}
}

// printSummary writes the closing count and the plain verdict.
func printSummary(out io.Writer, res Result) {
	passed := 0
	for _, c := range res.Checks {
		if c.Pass {
			passed++
		}
	}
	total := len(res.Checks)
	if res.Failed {
		// USER-FACING
		fmt.Fprintf(out, "\nSummary: %d of %d checks passed; %d failed. Follow the fix hints above, then rerun `bitbeard doctor` to confirm.\n", passed, total, total-passed)
		return
	}
	// USER-FACING
	fmt.Fprintf(out, "\nSummary: all %d checks passed. The stack looks healthy.\n", total)
}
