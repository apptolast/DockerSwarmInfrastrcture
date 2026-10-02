package office

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// Credential environment variables and bounds.
const (
	EnvClaudeToken = "CLAUDE_CODE_OAUTH_TOKEN"
	EnvCodexAuth   = "CODEX_AUTH_JSON_B64"

	maxClaudeToken  = 8 << 10
	maxCodexAuth    = 64 << 10
	maxCodexToken   = 16 << 10
	maxGitHubTokens = 16 << 10
	authClockSkew   = 2 * time.Minute
	credStatusTTL   = 10 * time.Second

	SourceStored = "guardada"
	SourceSecret = "secreto"
)

var (
	errCredential = errors.New("credencial no disponible")
	ownerRe       = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,38}$`)
	ghTokenRe     = regexp.MustCompile(`^[A-Za-z0-9_.-]{8,255}$`)
	// codexTokenRe is the shape of Codex's id, access and refresh tokens
	// (JWTs are base64url segments joined by dots).
	codexTokenRe = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
)

// codexIdentityTokens are the tokens of auth.json that are JWTs naming
// the account; their identity claims must survive a refresh.
var codexIdentityTokens = []string{"id_token", "access_token"}

// jwtClaims decodes the payload of a JWT without verifying it: the
// office only compares the identity two copies of the same credential
// name, it trusts neither for anything else.
func jwtClaims(raw json.RawMessage) (map[string]any, bool) {
	var tok string
	if json.Unmarshal(raw, &tok) != nil {
		return nil, false
	}
	parts := strings.Split(tok, ".")
	if len(parts) != 3 || parts[1] == "" {
		return nil, false
	}
	data, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return nil, false
	}
	var claims map[string]any
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if dec.Decode(&claims) != nil || claims == nil {
		return nil, false
	}
	return claims, true
}

func claimString(c map[string]any, key string) string {
	s, _ := c[key].(string)
	return s
}

// accountClaim is the ChatGPT account a token names: the
// chatgpt_account_id of its "https://api.openai.com/auth" claim, else a
// top-level chatgpt_account_id or account_id claim, else "".
func accountClaim(c map[string]any) string {
	if auth, ok := c["https://api.openai.com/auth"].(map[string]any); ok {
		if s := claimString(auth, "chatgpt_account_id"); s != "" {
			return s
		}
	}
	if s := claimString(c, "chatgpt_account_id"); s != "" {
		return s
	}
	return claimString(c, "account_id")
}

// readSecretFile reads a file of a Secret volume: the kubelet publishes
// each key as a symlink into a timestamped directory, so the path is
// resolved first, must stay inside its directory, and the resolved file
// is opened without following links and checked on the descriptor. No
// error carries the content.
func readSecretFile(path string, max int64) ([]byte, error) {
	root, err := filepath.EvalSymlinks(filepath.Dir(path))
	if err != nil {
		return nil, errCredential
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil || !strings.HasPrefix(resolved, root+string(filepath.Separator)) {
		return nil, errCredential
	}
	f, err := os.OpenFile(resolved, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, errCredential
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > max {
		return nil, errCredential
	}
	data, err := io.ReadAll(io.LimitReader(f, max+1))
	if err != nil || int64(len(data)) > max {
		return nil, errCredential
	}
	return data, nil
}

type creds struct {
	claudeFile string
	codexFile  string // <OfficeSecretDir>/<CodexAuthKey>, or ""
	githubFile string // <OfficeSecretDir>/<GitHubTokenKey>, or ""
	stored     string // <StateDir>/credentials/codex-auth.json
	now        func() time.Time

	mu       sync.Mutex
	status   Credentials
	statusAt time.Time
}

func (c *creds) claude() (map[string]string, error) {
	if c.claudeFile == "" {
		return nil, errCredential
	}
	data, err := readSecretFile(c.claudeFile, maxClaudeToken)
	if err != nil {
		return nil, err
	}
	t := strings.TrimSpace(string(data))
	if t == "" || strings.ContainsAny(t, "\x00\r\n") {
		return nil, errCredential
	}
	return map[string]string{EnvClaudeToken: t}, nil
}

// codexAuth is one parsed candidate auth.json.
type codexAuth struct {
	source      string
	data        []byte
	top         map[string]json.RawMessage
	tokens      map[string]json.RawMessage
	lastRefresh time.Time
}

func parseCodexAuth(source string, data []byte) (*codexAuth, error) {
	if len(data) == 0 || len(data) > maxCodexAuth {
		return nil, errors.New("supera 64 KiB o está vacío")
	}
	a := &codexAuth{source: source, data: data}
	if err := json.Unmarshal(data, &a.top); err != nil || a.top == nil {
		return nil, errors.New("no es un objeto JSON")
	}
	raw, ok := a.top["tokens"]
	if !ok || json.Unmarshal(raw, &a.tokens) != nil || a.tokens == nil {
		return nil, errors.New("no tiene el objeto tokens")
	}
	if lr, ok := a.top["last_refresh"]; ok {
		var s string
		if json.Unmarshal(lr, &s) == nil {
			if t, err := time.Parse(time.RFC3339Nano, s); err == nil {
				a.lastRefresh = t.UTC()
			}
		}
	}
	return a, nil
}

// currentCodex is the candidate with the newest last_refresh (a missing
// one is the oldest); on a tie the stored copy wins.
func (c *creds) currentCodex() *codexAuth {
	var best *codexAuth
	try := func(source string, data []byte, err error) {
		if err != nil {
			return
		}
		a, perr := parseCodexAuth(source, data)
		if perr != nil {
			return
		}
		if best == nil || a.lastRefresh.After(best.lastRefresh) {
			best = a
		}
	}
	if c.stored != "" {
		data, err := readStoredFile(c.stored, maxCodexAuth)
		try(SourceStored, data, err)
	}
	if c.codexFile != "" {
		data, err := readSecretFile(c.codexFile, maxCodexAuth)
		try(SourceSecret, data, err)
	}
	return best
}

func readStoredFile(path string, max int64) ([]byte, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > max {
		return nil, errCredential
	}
	data, err := io.ReadAll(io.LimitReader(f, max+1))
	if err != nil || int64(len(data)) > max {
		return nil, errCredential
	}
	return data, nil
}

func (c *creds) codex() (map[string]string, error) {
	a := c.currentCodex()
	if a == nil {
		return nil, errCredential
	}
	return map[string]string{EnvCodexAuth: base64.StdEncoding.EncodeToString(a.data)}, nil
}

// get returns the credential environment of a harness.
func (c *creds) get(h string) (map[string]string, error) {
	switch h {
	case harness.Claude:
		return c.claude()
	case harness.Codex:
		return c.codex()
	}
	return nil, errCredential
}

// checkJSONObject verifies data is one JSON object with no duplicate key
// at any depth.
func checkJSONObject(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	tok, err := dec.Token()
	if err != nil || tok != json.Delim('{') {
		return errors.New("no es un objeto JSON")
	}
	if err := walkObject(dec, 0); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		return errors.New("tiene datos tras el objeto JSON")
	}
	return nil
}

func walkObject(dec *json.Decoder, depth int) error {
	if depth > 32 {
		return errors.New("anida demasiado")
	}
	seen := map[string]bool{}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return errors.New("no es un objeto JSON válido")
		}
		key, ok := tok.(string)
		if !ok {
			return errors.New("no es un objeto JSON válido")
		}
		if seen[key] {
			return errors.New("tiene claves duplicadas")
		}
		seen[key] = true
		if err := walkValue(dec, depth); err != nil {
			return err
		}
	}
	if _, err := dec.Token(); err != nil {
		return errors.New("no es un objeto JSON válido")
	}
	return nil
}

func walkValue(dec *json.Decoder, depth int) error {
	tok, err := dec.Token()
	if err != nil {
		return errors.New("no es un objeto JSON válido")
	}
	switch tok {
	case json.Delim('{'):
		return walkObject(dec, depth+1)
	case json.Delim('['):
		for dec.More() {
			if err := walkValue(dec, depth+1); err != nil {
				return err
			}
		}
		if _, err := dec.Token(); err != nil {
			return errors.New("no es un objeto JSON válido")
		}
	}
	return nil
}

func sameKeys(a, b map[string]json.RawMessage) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if _, ok := b[k]; !ok {
			return false
		}
	}
	return true
}

func compactEqual(a, b json.RawMessage) bool {
	var ca, cb bytes.Buffer
	if json.Compact(&ca, a) != nil || json.Compact(&cb, b) != nil {
		return false
	}
	return bytes.Equal(ca.Bytes(), cb.Bytes())
}

// saveCodexAuth keeps a renewed auth.json read back after a Codex run,
// only when it is plausibly the same account's refreshed credential.
func (c *creds) saveCodexAuth(data []byte, created time.Time) error {
	reject := func(rule string) error { return fmt.Errorf("auth.json de Codex rechazado: %s", rule) }
	if len(data) > maxCodexAuth {
		return reject("supera 64 KiB")
	}
	if err := checkJSONObject(data); err != nil {
		return reject(err.Error())
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	cur := c.currentCodex()
	if cur == nil {
		return reject("no hay una credencial de Codex con la que compararlo")
	}
	if compactEqual(data, cur.data) {
		return nil
	}
	next, err := parseCodexAuth("", data)
	if err != nil {
		return reject(err.Error())
	}
	if !sameKeys(next.top, cur.top) || !sameKeys(next.tokens, cur.tokens) {
		return reject("sus claves no coinciden con las de la credencial actual")
	}
	for k, v := range next.tokens {
		var s string
		if json.Unmarshal(v, &s) == nil && strings.TrimSpace(s) == "" {
			return reject("el token " + k + " está vacío")
		}
	}
	if !compactEqual(next.tokens["account_id"], cur.tokens["account_id"]) {
		return reject("account_id cambió")
	}
	for _, k := range []string{"id_token", "access_token", "refresh_token"} {
		var v string
		if raw, ok := next.tokens[k]; !ok || json.Unmarshal(raw, &v) != nil {
			return reject("el token " + k + " falta o no es texto")
		}
		if len(v) > maxCodexToken || !codexTokenRe.MatchString(v) {
			return reject("el token " + k + " no tiene la forma esperada")
		}
	}
	// The renewed tokens must name the same person and account as the
	// current ones: their (unverified) JWT claims are compared.
	for _, k := range codexIdentityTokens {
		was, ok := jwtClaims(cur.tokens[k])
		if !ok || claimString(was, "sub") == "" {
			return reject("el " + k + " actual no permite comparar la identidad")
		}
		now, ok := jwtClaims(next.tokens[k])
		if !ok {
			return reject("el " + k + " no es un JWT legible")
		}
		if claimString(now, "sub") != claimString(was, "sub") {
			return reject("el sub del " + k + " cambió")
		}
		if acct := accountClaim(was); acct != "" && accountClaim(now) != acct {
			return reject("la cuenta de ChatGPT del " + k + " cambió")
		}
	}
	for k, v := range next.top {
		if k == "tokens" || k == "last_refresh" {
			continue
		}
		if !compactEqual(v, cur.top[k]) {
			return reject("el campo " + k + " cambió")
		}
	}
	if compactEqual(next.tokens["refresh_token"], cur.tokens["refresh_token"]) {
		return reject("refresh_token no cambió")
	}
	var lr string
	raw, ok := next.top["last_refresh"]
	if !ok || json.Unmarshal(raw, &lr) != nil {
		return reject("last_refresh falta o no es texto")
	}
	t, err := time.Parse(time.RFC3339Nano, lr)
	if _, off := t.Zone(); err != nil || off != 0 {
		return reject("last_refresh no es RFC 3339 en UTC")
	}
	switch {
	case !t.After(cur.lastRefresh):
		return reject("last_refresh no es posterior al actual")
	case !created.IsZero() && t.Before(created.Add(-authClockSkew)):
		return reject("last_refresh es anterior a la tarea")
	case t.After(c.now().Add(authClockSkew)):
		return reject("last_refresh está en el futuro")
	}
	if err := writeAtomic(c.stored, data, false); err != nil {
		return reject("no se pudo guardar")
	}
	c.statusAt = time.Time{}
	return nil
}

// forgetStored deletes the stored copy of auth.json; it reports whether
// there was one.
func (c *creds) forgetStored() (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.statusAt = time.Time{}
	if c.stored == "" {
		return false, nil
	}
	err := os.Remove(c.stored)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return false, nil
	case err != nil:
		return false, err
	}
	_ = syncDir(filepath.Dir(c.stored))
	return true, nil
}

// ghTokens are the GitHub tokens by owner ("*" is the default).
type ghTokens map[string]string

func (t ghTokens) forOwner(owner string) string {
	if tok, ok := t[strings.ToLower(owner)]; ok {
		return tok
	}
	return t["*"]
}

// githubTokens reads the token file: one bare token (every owner) or
// "owner=token" lines ("*" the default, "#" comments). It is read on
// every use, so a replaced Secret is picked up at once.
func (c *creds) githubTokens() (ghTokens, error) {
	if c.githubFile == "" {
		return nil, errCredential
	}
	data, err := readSecretFile(c.githubFile, maxGitHubTokens)
	if err != nil {
		return nil, err
	}
	out := ghTokens{}
	var bare []string
	pairs := 0
	for l := range strings.Lines(string(data)) {
		l = strings.TrimSpace(l)
		if l == "" || strings.HasPrefix(l, "#") {
			continue
		}
		owner, tok, ok := strings.Cut(l, "=")
		if !ok {
			bare = append(bare, l)
			continue
		}
		owner, tok = strings.ToLower(strings.TrimSpace(owner)), strings.TrimSpace(tok)
		if (owner != "*" && !ownerRe.MatchString(owner)) || !ghTokenRe.MatchString(tok) {
			return nil, errors.New("el fichero de tokens de GitHub no es válido")
		}
		out[owner] = tok
		pairs++
	}
	switch {
	case len(bare) == 1 && pairs == 0 && ghTokenRe.MatchString(bare[0]):
		out["*"] = bare[0]
	case len(bare) > 0:
		return nil, errors.New("el fichero de tokens de GitHub no es válido")
	}
	if len(out) == 0 {
		return nil, errCredential
	}
	return out, nil
}

// snapshot reports which credentials exist, never their values.
func (c *creds) snapshot() Credentials {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	if !c.statusAt.IsZero() && now.Sub(c.statusAt) < credStatusTTL && now.After(c.statusAt.Add(-time.Second)) {
		return cloneCredentials(c.status)
	}
	st := Credentials{GitHubOwners: []string{}}
	if _, err := c.claude(); err == nil {
		st.Claude = true
	}
	if a := c.currentCodex(); a != nil {
		st.Codex, st.CodexSource = true, a.source
		if !a.lastRefresh.IsZero() {
			t := a.lastRefresh
			st.CodexRefreshed = &t
		}
	}
	if toks, err := c.githubTokens(); err == nil {
		st.GitHub = true
		for owner := range toks {
			if owner != "*" {
				st.GitHubOwners = append(st.GitHubOwners, owner)
			}
		}
		slices.Sort(st.GitHubOwners)
		if _, ok := toks["*"]; ok {
			st.GitHubOwners = append(st.GitHubOwners, "todas")
		}
	}
	c.status, c.statusAt = st, now
	return cloneCredentials(st)
}

func cloneCredentials(c Credentials) Credentials {
	c.GitHubOwners = slices.Clone(c.GitHubOwners)
	if c.CodexRefreshed != nil {
		t := *c.CodexRefreshed
		c.CodexRefreshed = &t
	}
	return c
}

func (c *creds) invalidate() {
	c.mu.Lock()
	c.statusAt = time.Time{}
	c.mu.Unlock()
}
