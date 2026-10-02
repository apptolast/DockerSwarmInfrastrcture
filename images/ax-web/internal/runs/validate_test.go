package runs

import (
	"errors"
	"strings"
	"testing"
)

var hosts = []string{"github.com"}

func field(err error) string {
	var fe *FieldError
	if errors.As(err, &fe) {
		return fe.Field
	}
	return ""
}

func TestValidateRejects(t *testing.T) {
	repos := map[string]string{
		"http":          "http://github.com/a/b",
		"ssh":           "git@github.com:a/b.git",
		"userinfo":      "https://x:y@github.com/a/b",
		"port":          "https://github.com:443/a/b",
		"query":         "https://github.com/a/b?x=1",
		"empty query":   "https://github.com/a/b?",
		"fragment":      "https://github.com/a/b#x",
		"other host":    "https://gitlab.com/a/b",
		"lookalike":     "https://github.com.evil.io/a/b",
		"upper host":    "https://GitHub.com/a/b",
		"escape":        "https://github.com/a/%2e%2e",
		"dotdot":        "https://github.com/a/..",
		"trailing dots": "https://github.com/0/0..",
		"trailing dot":  "https://github.com/a/b.",
		"deep":          "https://github.com/a/b/c",
		"short":         "https://github.com/a",
		"dash":          "https://github.com/-a/b",
		"space":         "https://github.com/a/b c",
		"long":          "https://github.com/a/" + strings.Repeat("b", 300),
		"empty":         "",
	}
	for name, raw := range repos {
		if _, err := ValidateRepo(raw, hosts); field(err) != "repo" {
			t.Errorf("%s: got %v, want a repo error", name, err)
		}
	}
	for _, b := range []string{"--upload-pack=x", "a..b", "a b", "a.lock", "/a", "a/", "a//b", "a/.b", ".a",
		strings.Repeat("a", 101), ""} {
		if err := ValidateBranch(b); field(err) != "branch" {
			t.Errorf("%q: got %v, want a branch error", b, err)
		}
	}
	// Launch's re-check keeps the shape rules whatever the host.
	for _, raw := range []string{"https://x@github.com/a/b", "http://github.com/a/b", "https://gitlab.com/a/b/c"} {
		if checkRepo(raw) == nil {
			t.Errorf("%s accepted", raw)
		}
	}
	if err := checkRepo("https://gitlab.com/a/b"); err != nil {
		t.Error(err)
	}
}

func TestValidateAccepts(t *testing.T) {
	for _, b := range []string{"main", "feature/x-1", "release-1.2", "v1.2.3", "a_b"} {
		if err := ValidateBranch(b); err != nil {
			t.Errorf("%s: %v", b, err)
		}
	}
	for _, r := range []string{"https://github.com/apptolast/DockerSwarmInfrastrcture", "https://github.com/a-b/c.d_e.git"} {
		if _, err := ValidateRepo(r, hosts); err != nil {
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
