package wizard

import (
	"bytes"
	"strings"
	"testing"
)

// runPrompt drives a prompter with scripted input and returns its output.
func runPrompt(t *testing.T, input string, f func(p *prompter) error) (string, error) {
	t.Helper()
	var out bytes.Buffer
	p := newPrompter(strings.NewReader(input), &out)
	err := f(p)
	return out.String(), err
}

func TestAskTextSequencesPromptsAndRetries(t *testing.T) {
	out, err := runPrompt(t, "\n\nchange-me\ngood value\n", func(p *prompter) error {
		_, err := p.askText("OPENVPN_USER", "explanation\n", ValidateNonEmpty("OPENVPN_USER"))
		return err
	})
	if err != nil {
		t.Fatalf("askText: %v", err)
	}
	// Explanation printed once, before all prompts.
	if i := strings.Index(out, "explanation\n"); i != -1 {
		if j := strings.Index(out, "OPENVPN_USER"); j < i {
			t.Errorf("explanation printed after the first prompt:\n%s", out)
		}
	} else {
		t.Errorf("explanation missing:\n%s", out)
	}
	if n := strings.Count(out, "OPENVPN_USER:"); n != 4 {
		t.Errorf("prompted %d times, want 4 (empty, empty, change-me, good):\n%s", n, out)
	}
	if !strings.Contains(out, "cannot contain a change-me placeholder") {
		t.Errorf("change-me refusal missing:\n%s", out)
	}
	if !strings.Contains(out, "cannot be empty") {
		t.Errorf("emptiness message missing:\n%s", out)
	}
}

func TestAskGeneratedAcceptsSuggestionAndReplacement(t *testing.T) {
	// Empty answer accepts the generated value.
	out, err := runPrompt(t, "\n", func(p *prompter) error {
		v, err := p.askGenerated("SERVICE_TOKEN", "explanation\n", "generated-token-abc", 12)
		if err != nil {
			return err
		}
		if v != "generated-token-abc" {
			t.Errorf("got %q, want the generated value", v)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("askGenerated: %v", err)
	}
	if !strings.Contains(out, "Suggested SERVICE_TOKEN: generated-token-abc") {
		t.Errorf("generated value not shown:\n%s", out)
	}

	// A typed replacement is validated: too short, change-me, then good.
	out, err = runPrompt(t, "short\nchange-me-xxxxxxxx\nreplacement-value-1\n", func(p *prompter) error {
		v, err := p.askGenerated("SERVICE_TOKEN", "explanation\n", "generated-token-abc", 12)
		if err != nil {
			return err
		}
		if v != "replacement-value-1" {
			t.Errorf("got %q, want replacement-value-1", v)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("askGenerated: %v", err)
	}
	if !strings.Contains(out, "too short") || !strings.Contains(out, "change-me placeholder") {
		t.Errorf("replacement validation messages missing:\n%s", out)
	}
}

func TestAskProviderByNumberNameAndRefusal(t *testing.T) {
	out, err := runPrompt(t, "nonsense\nacme\nNordVPN\n", func(p *prompter) error {
		pv, err := p.askProvider("explanation\n")
		if err != nil {
			return err
		}
		if pv.ID != "nordvpn" {
			t.Errorf("got %q, want nordvpn", pv.ID)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("askProvider: %v", err)
	}
	if !strings.Contains(out, "NordVPN") || !strings.Contains(out, "Custom — I have my own .ovpn file") {
		t.Errorf("provider list incomplete:\n%s", out)
	}
	if !strings.Contains(out, "is not on the list") {
		t.Errorf("unknown-provider refusal missing:\n%s", out)
	}

	// By number, including the trailing custom option.
	nCustom := len(VPNProviders) + 1
	input := itoa(nCustom) + "\n"
	out, err = runPrompt(t, input, func(p *prompter) error {
		pv, err := p.askProvider("explanation\n")
		if err != nil {
			return err
		}
		if pv.ID != customProvider {
			t.Errorf("got %q, want custom", pv.ID)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("askProvider: %v", err)
	}
	_ = out
}

func TestAskYesNoDefaultsToNo(t *testing.T) {
	for _, in := range []string{"\n", "n\n", "N\n"} {
		ok, err := runPromptYesNo(in)
		if err != nil || ok {
			t.Errorf("input %q: ok=%v err=%v; want false, nil", in, ok, err)
		}
	}
	for _, in := range []string{"y\n", "YES\n"} {
		ok, err := runPromptYesNo(in)
		if err != nil || !ok {
			t.Errorf("input %q: ok=%v err=%v; want true, nil", in, ok, err)
		}
	}
	if _, err := runPromptYesNo("maybe\ny\n"); err != nil {
		t.Fatalf("re-ask after garbage: %v", err)
	}
}

func runPromptYesNo(input string) (bool, error) {
	var out bytes.Buffer
	p := newPrompter(strings.NewReader(input), &out)
	return p.askYesNo("Replace the file?")
}

func TestInputEndsIsReported(t *testing.T) {
	_, err := runPrompt(t, "", func(p *prompter) error {
		_, err := p.askText("K", "explanation\n", ValidateNonEmpty("K"))
		return err
	})
	if err == nil || !strings.Contains(err.Error(), "input ended") {
		t.Errorf("err = %v, want input-ended error", err)
	}
}

// itoa avoids importing strconv for one call in a test.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
