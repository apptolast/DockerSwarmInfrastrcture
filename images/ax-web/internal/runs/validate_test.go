package runs

import (
	"errors"
	"strings"
	"testing"
	"time"
)

var lim = Limits{RepoHosts: []string{"github.com"}, MaxTurns: 50, MaxTimeout: 45 * time.Minute}

func intp(n int) *int { return &n }

func TestValidateDefaults(t *testing.T) {
	s, err := Validate(Request{Repo: "https://github.com/apptolast/demo.git", Prompt: "hola"}, lim)
	if err != nil {
		t.Fatal(err)
	}
	if s.Repo != "https://github.com/apptolast/demo.git" || s.Branch != "main" || s.Turns != 20 ||
		s.Timeout != 30*time.Minute || s.CPU != "1" || s.Memory != "1Gi" {
		t.Fatalf("defaults: %+v", s)
	}
}

func field(err error) string {
	var fe *FieldError
	if errors.As(err, &fe) {
		return fe.Field
	}
	return ""
}

func TestValidateRejects(t *testing.T) {
	ok := Request{Repo: "https://github.com/a/b", Prompt: "x"}
	cases := []struct {
		name  string
		edit  func(*Request)
		field string
	}{
		{"http", func(r *Request) { r.Repo = "http://github.com/a/b" }, "repo"},
		{"ssh", func(r *Request) { r.Repo = "git@github.com:a/b.git" }, "repo"},
		{"userinfo", func(r *Request) { r.Repo = "https://x:y@github.com/a/b" }, "repo"},
		{"port", func(r *Request) { r.Repo = "https://github.com:443/a/b" }, "repo"},
		{"query", func(r *Request) { r.Repo = "https://github.com/a/b?x=1" }, "repo"},
		{"empty query", func(r *Request) { r.Repo = "https://github.com/a/b?" }, "repo"},
		{"fragment", func(r *Request) { r.Repo = "https://github.com/a/b#x" }, "repo"},
		{"other host", func(r *Request) { r.Repo = "https://gitlab.com/a/b" }, "repo"},
		{"lookalike", func(r *Request) { r.Repo = "https://github.com.evil.io/a/b" }, "repo"},
		{"upper host", func(r *Request) { r.Repo = "https://GitHub.com/a/b" }, "repo"},
		{"escape", func(r *Request) { r.Repo = "https://github.com/a/%2e%2e" }, "repo"},
		{"dotdot", func(r *Request) { r.Repo = "https://github.com/a/.." }, "repo"},
		{"trailing dots", func(r *Request) { r.Repo = "https://github.com/0/0.." }, "repo"},
		{"trailing dot", func(r *Request) { r.Repo = "https://github.com/a/b." }, "repo"},
		{"deep", func(r *Request) { r.Repo = "https://github.com/a/b/c" }, "repo"},
		{"short", func(r *Request) { r.Repo = "https://github.com/a" }, "repo"},
		{"dash", func(r *Request) { r.Repo = "https://github.com/-a/b" }, "repo"},
		{"space", func(r *Request) { r.Repo = "https://github.com/a/b c" }, "repo"},
		{"long", func(r *Request) { r.Repo = "https://github.com/a/" + strings.Repeat("b", 300) }, "repo"},
		{"branch option", func(r *Request) { r.Branch = "--upload-pack=x" }, "branch"},
		{"branch dots", func(r *Request) { r.Branch = "a..b" }, "branch"},
		{"branch space", func(r *Request) { r.Branch = "a b" }, "branch"},
		{"branch lock", func(r *Request) { r.Branch = "a.lock" }, "branch"},
		{"branch slash", func(r *Request) { r.Branch = "/a" }, "branch"},
		{"branch long", func(r *Request) { r.Branch = strings.Repeat("a", 101) }, "branch"},
		{"codex", func(r *Request) { r.Agent = "codex" }, "agent"},
		{"empty prompt", func(r *Request) { r.Prompt = "  \n" }, "prompt"},
		{"big prompt", func(r *Request) { r.Prompt = strings.Repeat("a", MaxPromptBytes+1) }, "prompt"},
		{"nul prompt", func(r *Request) { r.Prompt = "a\x00b" }, "prompt"},
		{"bad utf8", func(r *Request) { r.Prompt = "a\xffb" }, "prompt"},
		{"turns 0", func(r *Request) { r.Turns = intp(0) }, "turns"},
		{"turns 51", func(r *Request) { r.Turns = intp(51) }, "turns"},
		{"timeout 4", func(r *Request) { r.TimeoutMinutes = intp(4) }, "timeout_minutes"},
		{"timeout 46", func(r *Request) { r.TimeoutMinutes = intp(46) }, "timeout_minutes"},
		{"cpu", func(r *Request) { r.CPU = "4" }, "cpu"},
		{"memory", func(r *Request) { r.Memory = "8Gi" }, "memory"},
	}
	for _, c := range cases {
		req := ok
		c.edit(&req)
		_, err := Validate(req, lim)
		if field(err) != c.field {
			t.Errorf("%s: got %v, want a %s error", c.name, err, c.field)
		}
	}
	arg := lim
	arg.PromptInArg = true
	if _, err := Validate(Request{Repo: "https://github.com/a/b", Prompt: "--help"}, arg); field(err) != "prompt" {
		t.Error("an option-like prompt was accepted in argument mode")
	}
	if _, err := Validate(Request{Repo: "https://github.com/a/b", Prompt: "--help"}, lim); err != nil {
		t.Errorf("stdin mode must accept it: %v", err)
	}
}

func TestValidateAccepts(t *testing.T) {
	for _, b := range []string{"main", "feature/x-1", "release-1.2", "v1.2.3", "a_b"} {
		if err := ValidateBranch(b); err != nil {
			t.Errorf("%s: %v", b, err)
		}
	}
	for _, r := range []string{"https://github.com/apptolast/DockerSwarmInfrastrcture", "https://github.com/a-b/c.d_e.git"} {
		if _, err := ValidateRepo(r, lim.RepoHosts); err != nil {
			t.Errorf("%s: %v", r, err)
		}
	}
}

// FuzzValidateRepo: whatever is accepted is canonical https on an allowed
// host with exactly owner/name and nothing that git could read as an option
// or a credential.
func FuzzValidateRepo(f *testing.F) {
	for _, s := range []string{"https://github.com/a/b", "https://github.com/a/b.git", "https://x@github.com/a/b",
		"https://github.com/a/b?x", "file:///etc/passwd", "https://github.com/../b", "ext::sh -c x"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, raw string) {
		got, err := ValidateRepo(raw, []string{"github.com"})
		if err != nil {
			return
		}
		if !strings.HasPrefix(got, "https://github.com/") || got != raw && got+"/" != raw {
			t.Fatalf("%q accepted as %q", raw, got)
		}
		rest := strings.TrimPrefix(got, "https://github.com/")
		parts := strings.Split(rest, "/")
		if len(parts) != 2 || strings.ContainsAny(rest, "@?#%:\\ ") || strings.Contains(rest, "..") {
			t.Fatalf("%q accepted", raw)
		}
		for _, p := range parts {
			if p == "" || p[0] == '-' || p[0] == '.' {
				t.Fatalf("%q accepted", raw)
			}
		}
	})
}

// FuzzValidateBranch: an accepted branch can never be read as an option or
// climb out of refs/heads.
func FuzzValidateBranch(f *testing.F) {
	for _, s := range []string{"main", "-x", "a..b", "a/.b", "a b", "refs/heads/x"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, b string) {
		if ValidateBranch(b) != nil {
			return
		}
		if b == "" || len(b) > 100 || b[0] == '-' || b[0] == '/' || b[0] == '.' ||
			strings.Contains(b, "..") || strings.ContainsAny(b, " \t\n~^:?*[\\@{") {
			t.Fatalf("%q accepted", b)
		}
	})
}
