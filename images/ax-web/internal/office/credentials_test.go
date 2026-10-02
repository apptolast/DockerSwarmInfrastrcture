package office

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// fakeJWT is an unsigned JWT with these claims: the office never
// verifies one, it only compares the identity two copies name.
func fakeJWT(claims map[string]any) string {
	head := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	payload, _ := json.Marshal(claims)
	return head + "." + base64.RawURLEncoding.EncodeToString(payload) + ".ZmlybWE"
}

// identity are a ChatGPT login's claims, as Codex's tokens carry them.
func identity(sub, account, jti string) map[string]any {
	return map[string]any{"sub": sub, "jti": jti, "exp": 1790000000,
		"https://api.openai.com/auth": map[string]any{"chatgpt_account_id": account, "chatgpt_plan_type": "plus"}}
}

func codexJSON(lastRefresh, refresh string) []byte {
	m := map[string]any{
		"OPENAI_API_KEY": nil,
		"tokens": map[string]any{
			"id_token":      fakeJWT(identity("auth0|ana", "acct-1", "id-"+refresh)),
			"access_token":  fakeJWT(identity("auth0|ana", "acct-1", "acc-"+refresh)),
			"refresh_token": refresh, "account_id": "acct-1",
		},
		"last_refresh": lastRefresh,
	}
	data, _ := json.Marshal(m)
	return data
}

func writeCodex(t *testing.T, path, lastRefresh, refresh string) {
	t.Helper()
	if err := os.WriteFile(path, codexJSON(lastRefresh, refresh), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestClaudeCredential(t *testing.T) {
	h := newHarness(t)
	env, err := h.o.Credentials(harness.Claude)
	if err != nil || env[EnvClaudeToken] != testClaudeToken || len(env) != 1 {
		t.Fatalf("%v %v", env, err)
	}
	os.WriteFile(h.cfg.ClaudeTokenFile, []byte("\n  \n"), 0o600)
	if _, err := h.o.Credentials(harness.Claude); err == nil {
		t.Fatal("blank token accepted")
	}
	os.WriteFile(h.cfg.ClaudeTokenFile, []byte(strings.Repeat("a", 9<<10)), 0o600)
	if _, err := h.o.Credentials(harness.Claude); err == nil {
		t.Fatal("oversized token accepted")
	}
	// A symlink escaping the volume is refused.
	os.Remove(h.cfg.ClaudeTokenFile)
	outside := filepath.Join(t.TempDir(), "secret")
	os.WriteFile(outside, []byte("x"), 0o600)
	os.Symlink(outside, h.cfg.ClaudeTokenFile)
	if _, err := h.o.Credentials(harness.Claude); err == nil {
		t.Fatal("escaping symlink followed")
	}
	if _, err := h.o.Credentials("gemini"); err == nil {
		t.Fatal("unknown harness")
	}
}

func TestCodexPicksNewestCandidate(t *testing.T) {
	h := newHarness(t)
	if _, err := h.o.Credentials(harness.Codex); err == nil {
		t.Fatal("codex without any credential")
	}
	secret := filepath.Join(h.cfg.OfficeSecretDir, h.cfg.CodexAuthKey)
	writeCodex(t, secret, "2026-10-01T10:00:00Z", "secret-r")
	stored := h.o.store.codexAuthPath()
	os.WriteFile(stored, codexJSON("2026-10-01T09:00:00Z", "stored-r"), 0o600)
	env, err := h.o.Credentials(harness.Codex)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := base64.StdEncoding.DecodeString(env[EnvCodexAuth])
	if !strings.Contains(string(data), "secret-r") {
		t.Fatalf("picked %s", data)
	}
	os.WriteFile(stored, codexJSON("2026-10-01T11:00:00Z", "stored-r"), 0o600)
	env, _ = h.o.Credentials(harness.Codex)
	data, _ = base64.StdEncoding.DecodeString(env[EnvCodexAuth])
	if !strings.Contains(string(data), "stored-r") {
		t.Fatalf("picked %s", data)
	}
	st := h.o.creds.snapshot()
	if !st.Codex || st.CodexSource != SourceStored || st.CodexRefreshed == nil || st.CodexRefreshed.Hour() != 11 {
		t.Fatalf("%+v", st)
	}
	// A candidate missing last_refresh is the oldest; one without tokens
	// is no candidate.
	os.WriteFile(stored, []byte(`{"tokens":{"refresh_token":"x"}}`), 0o600)
	h.o.creds.invalidate()
	if st := h.o.creds.snapshot(); st.CodexSource != SourceSecret {
		t.Fatalf("%+v", st)
	}
	os.WriteFile(secret, []byte(`{"no":"tokens"}`), 0o600)
	h.o.creds.invalidate()
	if st := h.o.creds.snapshot(); st.CodexSource != SourceStored {
		t.Fatalf("%+v", st)
	}
}

func TestSaveCodexAuthRules(t *testing.T) {
	created := time.Date(2026, 10, 2, 9, 55, 0, 0, time.UTC)
	cur := codexJSON("2026-10-01T10:00:00Z", "r1")
	good := codexJSON("2026-10-02T09:58:00Z", "r2")
	mutate := func(f func(m map[string]any)) []byte {
		var m map[string]any
		json.Unmarshal(good, &m)
		f(m)
		data, _ := json.Marshal(m)
		return data
	}
	tokens := func(m map[string]any) map[string]any { return m["tokens"].(map[string]any) }
	cases := []struct {
		name string
		data []byte
		rule string
	}{
		{"accepted", good, ""},
		{"equal is a no-op", cur, ""},
		{"too big", []byte(`{"x":"` + strings.Repeat("a", 64<<10) + `"}`), "64 KiB"},
		{"not an object", []byte(`[1]`), "objeto JSON"},
		{"trailing data", append(append([]byte{}, good...), []byte(`{}`)...), "datos tras"},
		{"duplicate top key", []byte(`{"tokens":{},"tokens":{}}`), "duplicadas"},
		{"duplicate token key", []byte(strings.Replace(string(good), `"refresh_token":"r2"`, `"refresh_token":"r2","refresh_token":"r3"`, 1)), "duplicadas"},
		{"extra key", mutate(func(m map[string]any) { m["extra"] = 1 }), "claves"},
		{"missing token key", mutate(func(m map[string]any) { delete(tokens(m), "id_token") }), "claves"},
		{"empty token", mutate(func(m map[string]any) { tokens(m)["access_token"] = "" }), "vacío"},
		{"account changed", mutate(func(m map[string]any) { tokens(m)["account_id"] = "acct-2" }), "account_id"},
		{"field changed", mutate(func(m map[string]any) { m["OPENAI_API_KEY"] = "sk" }), "OPENAI_API_KEY"},
		{"same refresh token", mutate(func(m map[string]any) { tokens(m)["refresh_token"] = "r1" }), "refresh_token no cambió"},
		{"no last_refresh", mutate(func(m map[string]any) { m["last_refresh"] = 5 }), "last_refresh falta"},
		{"not utc", mutate(func(m map[string]any) { m["last_refresh"] = "2026-10-02T11:58:00+02:00" }), "UTC"},
		{"not later", mutate(func(m map[string]any) { m["last_refresh"] = "2026-10-01T10:00:00Z" }), "posterior"},
		{"before the task", mutate(func(m map[string]any) { m["last_refresh"] = "2026-10-02T09:50:00Z" }), "anterior a la tarea"},
		{"future", mutate(func(m map[string]any) { m["last_refresh"] = "2026-10-02T10:05:00Z" }), "futuro"},
		{"odd token", mutate(func(m map[string]any) { tokens(m)["refresh_token"] = "r2 r3" }), "forma esperada"},
		{"huge token", mutate(func(m map[string]any) { tokens(m)["refresh_token"] = strings.Repeat("r", 16<<10+1) }), "forma esperada"},
		{"token not text", mutate(func(m map[string]any) { tokens(m)["refresh_token"] = 7 }), "no es texto"},
		{"not a jwt", mutate(func(m map[string]any) { tokens(m)["id_token"] = "abc.def" }), "no es un JWT legible"},
		{"other person", mutate(func(m map[string]any) {
			tokens(m)["id_token"] = fakeJWT(identity("auth0|eva", "acct-1", "x"))
		}), "sub del id_token cambió"},
		{"other chatgpt account", mutate(func(m map[string]any) {
			tokens(m)["access_token"] = fakeJWT(identity("auth0|ana", "acct-9", "x"))
		}), "cuenta de ChatGPT del access_token cambió"},
		{"account claim dropped", mutate(func(m map[string]any) {
			tokens(m)["access_token"] = fakeJWT(map[string]any{"sub": "auth0|ana"})
		}), "cuenta de ChatGPT del access_token cambió"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			secret := filepath.Join(h.cfg.OfficeSecretDir, h.cfg.CodexAuthKey)
			os.WriteFile(secret, cur, 0o600)
			err := h.o.SaveCodexAuth(c.data, created)
			stored, _ := os.ReadFile(h.o.store.codexAuthPath())
			switch {
			case c.rule == "" && err != nil:
				t.Fatal(err)
			case c.rule != "" && (err == nil || !strings.Contains(err.Error(), c.rule)):
				t.Fatalf("err %v, want %q", err, c.rule)
			case c.rule != "" && len(stored) != 0:
				t.Fatal("stored a rejected file")
			}
			if err != nil && (strings.Contains(err.Error(), "r2") || strings.Contains(err.Error(), "acct") ||
				strings.Contains(err.Error(), "auth0") || strings.Contains(err.Error(), "eyJ")) {
				t.Fatalf("error leaks a value: %v", err)
			}
			if c.name == "accepted" {
				info, _ := os.Stat(h.o.store.codexAuthPath())
				env, _ := h.o.Credentials(harness.Codex)
				data, _ := base64.StdEncoding.DecodeString(env[EnvCodexAuth])
				if info.Mode().Perm() != 0o600 || !strings.Contains(string(data), `"r2"`) {
					t.Fatalf("%v %s", info.Mode(), data)
				}
				if a := h.audit.String(); strings.Contains(a, "r2") || !strings.Contains(a, "codex.auth.saved") {
					t.Fatalf("audit %s", a)
				}
			}
		})
	}
}

func TestGitHubTokens(t *testing.T) {
	h := newHarness(t)
	path := filepath.Join(h.cfg.OfficeSecretDir, h.cfg.GitHubTokenKey)
	if st := h.o.creds.snapshot(); st.GitHub || len(st.GitHubOwners) != 0 {
		t.Fatalf("%+v", st)
	}
	cases := []struct {
		content string
		ok      bool
		owners  string
		forA    string
	}{
		{"tok-single0123456789\n", true, "todas", "tok-single0123456789"},
		{"# tokens\nApptoLast=tok-aaaaaaaaaaaa\n* = tok-default000000\n", true, "apptolast,todas", "tok-aaaaaaaaaaaa"},
		{"otro=tok-bbbbbbbbbbbb\n", true, "otro", ""},
		{"tok-one000000000\ntok-two000000000\n", false, "", ""},
		{"bad owner!=tok-cccccccccccc\n", false, "", ""},
		{"apptolast=short\n", false, "", ""},
		{"# solo comentarios\n", false, "", ""},
	}
	for _, c := range cases {
		os.WriteFile(path, []byte(c.content), 0o600)
		toks, err := h.o.creds.githubTokens()
		if (err == nil) != c.ok {
			t.Errorf("%q: %v", c.content, err)
			continue
		}
		h.o.creds.invalidate()
		st := h.o.creds.snapshot()
		if strings.Join(st.GitHubOwners, ",") != c.owners || st.GitHub != c.ok {
			t.Errorf("%q: %+v", c.content, st)
		}
		if c.ok && toks.forOwner("APPTOLAST") != c.forA {
			t.Errorf("%q: token for apptolast", c.content)
		}
	}
}

// A current copy whose tokens are not JWTs naming someone cannot vouch
// for a renewed one: it is refused.
func TestSaveCodexAuthNeedsComparableIdentity(t *testing.T) {
	h := newHarness(t)
	secret := filepath.Join(h.cfg.OfficeSecretDir, h.cfg.CodexAuthKey)
	os.WriteFile(secret, []byte(`{"tokens":{"id_token":"opaco","access_token":"opaco","refresh_token":"r1","account_id":"a"},`+
		`"last_refresh":"2026-10-01T10:00:00Z"}`), 0o600)
	next := []byte(`{"tokens":{"id_token":"opaco","access_token":"opaco","refresh_token":"r2","account_id":"a"},` +
		`"last_refresh":"2026-10-02T09:58:00Z"}`)
	err := h.o.SaveCodexAuth(next, time.Date(2026, 10, 2, 9, 55, 0, 0, time.UTC))
	if err == nil || !strings.Contains(err.Error(), "no permite comparar la identidad") {
		t.Fatalf("%v", err)
	}
}

func TestAccountClaim(t *testing.T) {
	for want, claims := range map[string]map[string]any{
		"a1": {"https://api.openai.com/auth": map[string]any{"chatgpt_account_id": "a1"}, "account_id": "otro"},
		"a2": {"chatgpt_account_id": "a2", "account_id": "otro"},
		"a3": {"account_id": "a3"},
		"":   {"sub": "x", "https://api.openai.com/auth": "no es un objeto"},
	} {
		if got := accountClaim(claims); got != want {
			t.Errorf("%v: %q", claims, got)
		}
	}
	if _, ok := jwtClaims(json.RawMessage(`"a.%%%.c"`)); ok {
		t.Fatal("bad base64 accepted")
	}
	if c, ok := jwtClaims(json.RawMessage(`"` + fakeJWT(map[string]any{"sub": "s"}) + `"`)); !ok || c["sub"] != "s" {
		t.Fatalf("%v %v", c, ok)
	}
}

// Forgetting the stored copy goes back to the Secret's.
func TestForgetCodexAuth(t *testing.T) {
	h := newHarness(t)
	secret := filepath.Join(h.cfg.OfficeSecretDir, h.cfg.CodexAuthKey)
	os.WriteFile(secret, codexJSON("2026-10-01T10:00:00Z", "r1"), 0o600)
	if err := h.o.SaveCodexAuth(codexJSON("2026-10-02T09:58:00Z", "r2"), time.Date(2026, 10, 2, 9, 55, 0, 0, time.UTC)); err != nil {
		t.Fatal(err)
	}
	if st := h.o.Snapshot().Credentials; st.CodexSource != SourceStored {
		t.Fatalf("%+v", st)
	}
	rev := h.o.Rev()
	if err := h.o.ForgetCodexAuth("203.0.113.9"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(h.o.store.codexAuthPath()); !os.IsNotExist(err) {
		t.Fatalf("stored copy still there: %v", err)
	}
	if st := h.o.Snapshot().Credentials; !st.Codex || st.CodexSource != SourceSecret {
		t.Fatalf("%+v", st)
	}
	if h.o.Rev() == rev || !strings.Contains(h.audit.String(), `"action":"codex.auth.forget"`) ||
		!strings.Contains(h.audit.String(), `"client_ip":"203.0.113.9"`) {
		t.Fatalf("audit %s", h.audit.String())
	}
	// Idempotent; with no Secret either, there is no Codex at all.
	os.Remove(secret)
	if err := h.o.ForgetCodexAuth("x"); err != nil {
		t.Fatal(err)
	}
	if st := h.o.Snapshot().Credentials; st.Codex || st.CodexSource != "" {
		t.Fatalf("%+v", st)
	}
}
