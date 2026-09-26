package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var valid = strings.ReplaceAll(template, "DIGEST", strings.Repeat("ab", 32))

const template = `{
  "ax_server": "ax-server.ax-system.svc:8080",
  "router": "atenet-router.ate-system.svc:80",
  "atespace": "default",
  "agent_image": "localhost:5001/ax-agents@sha256:DIGEST",
  "repo_hosts": ["github.com"],
  "origin": "https://ax.apptolast.com",
  "blackout": "22:30-00:40",
  "watchdog_lead_minutes": 5,
  "max_turns": 50,
  "max_timeout_minutes": 45,
  "prompt_mode": "stdin",
  "token_directory": "/var/run/ax-web/agent",
  "token_key": "claude-oauth-token",
  "tls_cert_file": "/var/run/ax-web/tls/tls.crt",
  "tls_key_file": "/var/run/ax-web/tls/tls.key",
  "client_ca_file": "/var/run/ax-web/tls/client-ca.crt",
  "client_common_name": "edge-traefik",
  "listen": ":8443",
  "health_listen": ":8081"
}`

func TestParseValid(t *testing.T) {
	c, err := Parse([]byte(valid))
	if err != nil {
		t.Fatal(err)
	}
	if c.MaxTimeout() != 45*time.Minute || c.Origin != "https://ax.apptolast.com" {
		t.Fatalf("unexpected %+v", c)
	}
}

func TestLoad(t *testing.T) {
	p := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(p, []byte(valid), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(p); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(filepath.Dir(p)); err == nil {
		t.Fatal("a directory was accepted")
	}
}

func TestParseRejects(t *testing.T) {
	cases := map[string][2]string{
		"unknown key":      {`"atespace": "default",`, `"atespace": "default", "extra": 1,`},
		"tag image":        {`@sha256:` + strings.Repeat("ab", 32), `:latest`},
		"http origin":      {`"https://ax.apptolast.com"`, `"http://ax.apptolast.com"`},
		"origin with path": {`"https://ax.apptolast.com"`, `"https://ax.apptolast.com/x"`},
		"upper host":       {`["github.com"]`, `["GitHub.com"]`},
		"no hosts":         {`["github.com"]`, `[]`},
		"bad window":       {`"22:30-00:40"`, `"22:30-24:00"`},
		"empty window":     {`"22:30-00:40"`, `"22:30-22:30"`},
		"turns":            {`"max_turns": 50`, `"max_turns": 51`},
		"timeout":          {`"max_timeout_minutes": 45`, `"max_timeout_minutes": 60`},
		"prompt mode":      {`"stdin"`, `"shell"`},
		"token key":        {`"claude-oauth-token"`, `"../x"`},
		"relative tls":     {`"/var/run/ax-web/tls/tls.key"`, `"tls.key"`},
		"same ports":       {`":8081"`, `":8443"`},
		"no port":          {`"ax-server.ax-system.svc:8080"`, `"ax-server.ax-system.svc"`},
	}
	for name, c := range cases {
		mutated := strings.Replace(valid, c[0], c[1], 1)
		if mutated == valid {
			t.Fatalf("%s: mutation did not apply", name)
		}
		if _, err := Parse([]byte(mutated)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := Parse([]byte(valid + "{}")); err == nil {
		t.Error("trailing data accepted")
	}
}

func at(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestWindowAroundMidnight(t *testing.T) {
	w, err := ParseWindow("22:30-00:40")
	if err != nil {
		t.Fatal(err)
	}
	contains := map[string]bool{
		"2026-09-25T22:29:59Z": false,
		"2026-09-25T22:30:00Z": true,
		"2026-09-25T23:59:59Z": true,
		"2026-09-26T00:00:00Z": true,
		"2026-09-26T00:39:59Z": true,
		"2026-09-26T00:40:00Z": false,
		"2026-09-26T12:00:00Z": false,
		// A non-UTC clock is converted first.
		"2026-09-26T00:35:00+02:00": true,
	}
	for s, want := range contains {
		if got := w.Contains(at(s)); got != want {
			t.Errorf("Contains(%s) = %v", s, got)
		}
	}
	overlaps := []struct {
		from, to string
		want     bool
	}{
		{"2026-09-25T21:00:00Z", "2026-09-25T22:30:00Z", false},
		{"2026-09-25T21:00:00Z", "2026-09-25T22:30:01Z", true},
		{"2026-09-26T00:40:00Z", "2026-09-26T22:00:00Z", false},
		{"2026-09-26T00:39:00Z", "2026-09-26T01:00:00Z", true},
		{"2026-09-25T21:50:00Z", "2026-09-25T22:40:00Z", true},
		// A run spanning a whole instance.
		{"2026-09-25T22:00:00Z", "2026-09-26T01:00:00Z", true},
		// New Year's Eve.
		{"2026-12-31T23:00:00Z", "2026-12-31T23:10:00Z", true},
	}
	for _, c := range overlaps {
		if got := w.Overlaps(at(c.from), at(c.to)); got != c.want {
			t.Errorf("Overlaps(%s, %s) = %v", c.from, c.to, got)
		}
	}
	day, _ := ParseWindow("08:00-09:00")
	if day.Contains(at("2026-09-26T07:59:59Z")) || !day.Contains(at("2026-09-26T08:00:00Z")) ||
		day.Contains(at("2026-09-26T09:00:00Z")) {
		t.Error("non-wrapping window")
	}
	if w.String() != "22:30-00:40" {
		t.Error(w.String())
	}
}
