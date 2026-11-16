package wizard

// prompt.go is the wizard's prompt engine: one key at a time, an
// explanation before each question, live validation of every answer, and
// generated secrets shown for confirmation. All input comes through a
// single bufio.Scanner over the configured reader so nothing buffered is
// lost between prompts, which keeps the flow testable with fake stdin.

import (
	"bufio"
	"fmt"
	"io"
	"strings"
)

// prompter asks the operator questions and prints explanations.
type prompter struct {
	sc  *bufio.Scanner
	out io.Writer
}

// newPrompter builds a prompter reading one line at a time from in and
// writing to out. A single Scanner is reused for every question.
func newPrompter(in io.Reader, out io.Writer) *prompter {
	sc := bufio.NewScanner(in)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	return &prompter{sc: sc, out: out}
}

// print writes s as-is.
func (p *prompter) print(s string) { fmt.Fprint(p.out, s) }

// printf writes a formatted string.
func (p *prompter) printf(format string, args ...any) { fmt.Fprintf(p.out, format, args...) }

// readLine returns the next trimmed line of input.
func (p *prompter) readLine() (string, error) {
	if !p.sc.Scan() {
		if err := p.sc.Err(); err != nil {
			return "", err
		}
		// USER-FACING
		return "", fmt.Errorf("input ended before the wizard finished; nothing more was asked and nothing was changed")
	}
	return strings.TrimSpace(p.sc.Text()), nil
}

// explain prints an explanation block once, before the question.
func (p *prompter) explain(s string) {
	p.print(s)
}

// askText asks one question, validating the answer until it is accepted.
// label is the question line, explanation the background printed once.
func (p *prompter) askText(label, explanation string, validate Validator) (string, error) {
	p.explain(explanation)
	for {
		// USER-FACING
		p.printf("%s: ", label)
		line, err := p.readLine()
		if err != nil {
			return "", err
		}
		v, verr := validate(line)
		if verr == nil {
			return v, nil
		}
		// USER-FACING
		p.printf("  %v\n", verr)
	}
}

// askGenerated offers a generated secret: pressing Enter accepts it, typed
// input replaces it (validated, minLen characters, never change-me).
func (p *prompter) askGenerated(label, explanation, generated string, minLen int) (string, error) {
	p.explain(explanation)
	// USER-FACING
	p.printf("Suggested %s: %s\n", label, generated)
	for {
		// USER-FACING
		p.printf("Press Enter to accept the suggested %s, or type your own: ", label)
		line, err := p.readLine()
		if err != nil {
			return "", err
		}
		if line == "" {
			return generated, nil
		}
		if err := checkSecretReplacement(label, line, minLen); err != nil {
			// USER-FACING
			p.printf("  %v\n", err)
			continue
		}
		return line, nil
	}
}

// checkSecretReplacement validates a hand-typed replacement for a
// generated secret.
func checkSecretReplacement(k, v string, minLen int) error {
	if v := strings.TrimSpace(v); v == "" {
		// USER-FACING
		return fmt.Errorf("%s cannot be empty; press Enter to accept the suggested value", k)
	}
	if len(strings.TrimSpace(v)) < minLen {
		// USER-FACING
		return fmt.Errorf("%s is too short; use at least %d characters, or press Enter to accept the suggested value", k, minLen)
	}
	return rejectChangeMe(k, strings.TrimSpace(v))
}

// askProvider offers gluetun's supported providers plus the custom .ovpn
// option; answers by number or name.
func (p *prompter) askProvider(explanation string) (VPNProvider, error) {
	p.explain(explanation)
	options := make([]VPNProvider, 0, len(VPNProviders)+1)
	options = append(options, VPNProviders...)
	// USER-FACING
	options = append(options, VPNProvider{ID: customProvider, Name: "Custom — I have my own .ovpn file"})
	for _, o := range options {
		// USER-FACING
		p.printf("  %d. %s\n", indexOf(options, o)+1, o.Name)
	}
	for {
		// USER-FACING
		p.printf("VPN provider (number or name): ")
		line, err := p.readLine()
		if err != nil {
			return VPNProvider{}, err
		}
		if line == "" {
			// USER-FACING
			p.printf("  Choose a provider by number or name\n")
			continue
		}
		if n, ok := parseNumber(line); ok && n >= 1 && n <= len(options) {
			return options[n-1], nil
		}
		id := normalizeProvider(line)
		if KnownVPNProvider(id) {
			for _, o := range options {
				if o.ID == id {
					return o, nil
				}
			}
		}
		// USER-FACING
		p.printf("  %q is not on the list; pick one of the numbers or names above\n", line)
	}
}

// askYesNo asks a yes/no question. Empty input answers no, so destructive
// steps need an explicit yes.
func (p *prompter) askYesNo(question string) (bool, error) {
	for {
		// USER-FACING
		p.printf("%s [y/N]: ", question)
		line, err := p.readLine()
		if err != nil {
			return false, err
		}
		switch strings.ToLower(line) {
		case "y", "yes":
			return true, nil
		case "", "n", "no":
			return false, nil
		}
		// USER-FACING
		p.printf("  Answer y or n\n")
	}
}

// parseNumber reports whether s is a plain positive integer, and its value.
func parseNumber(s string) (int, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, false
	}
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0, false
		}
		n = n*10 + int(r-'0')
		if n > 1<<30 {
			return 0, false
		}
	}
	return n, true
}

// indexOf returns the position of v in slice (linear; the list is tiny).
func indexOf(slice []VPNProvider, v VPNProvider) int {
	for i, x := range slice {
		if x == v {
			return i
		}
	}
	return -1
}
