package wizard

import (
	"strings"
	"testing"
)

// psRunning is a `docker compose ps --format json` payload with every
// mandatory container running and the init container exited 0.
var psRunning = "" +
	`{"Name":"gluetun","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"qbittorrent","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"jackett","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"api","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"manager","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"webui","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"init-configs","State":"exited","ExitCode":0}` + "\n"

func TestParseComposePSJSONLines(t *testing.T) {
	cs, err := parseComposePS(psRunning)
	if err != nil {
		t.Fatalf("parseComposePS: %v", err)
	}
	if len(cs) != 7 {
		t.Errorf("parsed %d containers, want 7", len(cs))
	}
}

func TestParseComposePSArrayForm(t *testing.T) {
	in := `[{"Name":"api","State":"running","ExitCode":0},{"Name":"gluetun","State":"restarting","ExitCode":0}]`
	cs, err := parseComposePS(in)
	if err != nil {
		t.Fatalf("parseComposePS: %v", err)
	}
	if len(cs) != 2 || cs[1].State != "restarting" {
		t.Errorf("parsed %v", cs)
	}
}

func TestParseComposePSRejectsGarbage(t *testing.T) {
	if _, err := parseComposePS(""); err == nil {
		t.Error("empty output accepted")
	}
	if _, err := parseComposePS("not json"); err == nil {
		t.Error("garbage accepted")
	}
}

func TestVerifyMandatoryRunning(t *testing.T) {
	cs, err := parseComposePS(psRunning)
	if err != nil {
		t.Fatal(err)
	}
	notRunning, failedInit := verifyMandatoryRunning(cs)
	if len(notRunning) != 0 || len(failedInit) != 0 {
		t.Errorf("healthy stack reported notRunning=%v failedInit=%v", notRunning, failedInit)
	}

	// A restarting gluetun and a failed init container are both named.
	in := strings.ReplaceAll(psRunning, `"Name":"gluetun","State":"running"`, `"Name":"gluetun","State":"restarting"`)
	in = strings.ReplaceAll(in, `"Name":"init-configs","State":"exited","ExitCode":0`, `"Name":"init-configs","State":"exited","ExitCode":2`)
	cs, err = parseComposePS(in)
	if err != nil {
		t.Fatal(err)
	}
	notRunning, failedInit = verifyMandatoryRunning(cs)
	if len(notRunning) != 1 || notRunning[0] != "gluetun" {
		t.Errorf("notRunning = %v, want [gluetun]", notRunning)
	}
	if len(failedInit) != 1 || !strings.Contains(failedInit[0], "init-configs") {
		t.Errorf("failedInit = %v, want init-configs", failedInit)
	}

	// A missing container is as bad as a down one.
	cs, err = parseComposePS(strings.ReplaceAll(psRunning, `{"Name":"manager","State":"running","ExitCode":0}`+"\n", ""))
	if err != nil {
		t.Fatal(err)
	}
	notRunning, _ = verifyMandatoryRunning(cs)
	if len(notRunning) != 1 || notRunning[0] != "manager" {
		t.Errorf("notRunning = %v, want [manager]", notRunning)
	}
}

func TestVerifyMandatoryRunningListMatchesCompose(t *testing.T) {
	// The mandatory set mirrors bitbeard/docker-compose.yml's services
	// without compose profiles.
	want := "gluetun qbittorrent jackett api manager webui init-configs"
	var got []string
	got = append(got, mandatoryRunning...)
	got = append(got, mandatoryExited...)
	if strings.Join(got, " ") != want {
		t.Errorf("mandatory containers = %s, want %s", strings.Join(got, " "), want)
	}
}
