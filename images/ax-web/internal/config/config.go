// Package config reads the panel's reviewed settings: one strict JSON file
// that the deployment renders from config/ax-lab.yml into a ConfigMap. It
// never carries a credential, only the paths where the mounted ones live.
package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// maxFileBytes bounds the configuration file (it lists up to 100
// projects).
const maxFileBytes = 256 << 10

// Bounds of the office keys.
const (
	MaxProjects       = 100
	maxProjectName    = 60
	maxProjectDesc    = 300
	maxProjectService = 80
)

// Config is the panel's whole configuration. Every field is required
// unless its comment says otherwise.
type Config struct {
	// AXServer is ax-server's in-cluster host:port (plain HTTP/2, gRPC).
	AXServer string `json:"ax_server"`
	// Router is atenet-router's in-cluster host:port, the only way into a
	// sandbox's guest ProcessService (the ax-workers policy admits it only).
	Router string `json:"router"`
	// Atespace holds every task the panel lists and creates.
	Atespace string `json:"atespace"`
	// AgentImage is the agents image by digest, never by tag.
	AgentImage string `json:"agent_image"`
	// RepoHosts are the only hosts a run's repository may use.
	RepoHosts []string `json:"repo_hosts"`
	// Origin is the public origin the browser uses; POSTs from any other
	// origin are refused.
	Origin string `json:"origin"`
	// ExtraOrigins are other public https origins of the same panel (the
	// office's own host name); may be empty.
	ExtraOrigins []string `json:"extra_origins"`
	// Blackout is the daily UTC window with no runs, "HH:MM-HH:MM", or ""
	// for none.
	Blackout string `json:"blackout"`
	// WatchdogLeadMinutes is how long before the blackout an active run is
	// cancelled. It does nothing without a blackout.
	WatchdogLeadMinutes int `json:"watchdog_lead_minutes"`
	// MaxTurns and MaxTimeoutMinutes cap every run.
	MaxTurns          int `json:"max_turns"`
	MaxTimeoutMinutes int `json:"max_timeout_minutes"`
	// PromptMode is "stdin" (the prompt never reaches argv) or "argument",
	// the fallback if the agent ignores stdin.
	PromptMode string `json:"prompt_mode"`
	// TokenDirectory is the read-only Secret volume and TokenKey the file
	// in it that holds the agent credential. It is read once per run.
	TokenDirectory string `json:"token_directory"`
	TokenKey       string `json:"token_key"`
	// TLS material of the mTLS listener, mounted from a Secret.
	TLSCertFile  string `json:"tls_cert_file"`
	TLSKeyFile   string `json:"tls_key_file"`
	ClientCAFile string `json:"client_ca_file"`
	// ClientCommonName is the only client certificate subject accepted.
	ClientCommonName string `json:"client_common_name"`
	// Listen is the mTLS address; HealthListen the plain probe address.
	Listen       string `json:"listen"`
	HealthListen string `json:"health_listen"`
	// StateDir is the office's persistent volume (JSON files).
	StateDir string `json:"state_dir"`
	// OfficeSecretDir is the optional Secret volume with the office's
	// credentials: CodexAuthKey (a ChatGPT auth.json for Codex) and
	// GitHubTokenKey (GitHub tokens, read per use). It may not exist.
	OfficeSecretDir string `json:"office_secret_dir"`
	CodexAuthKey    string `json:"codex_auth_key"`
	GitHubTokenKey  string `json:"github_token_key"`
	// MaxQueue bounds the queued jobs; RetentionJobs the stored ones.
	MaxQueue      int `json:"max_queue"`
	RetentionJobs int `json:"retention_jobs"`
	// Projects are the reviewed repositories the office is seeded with;
	// may be empty.
	Projects []Project `json:"projects"`
}

// Project is a reviewed repository of the office.
type Project struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Repo        string `json:"repo"`
	Branch      string `json:"branch"`
	Description string `json:"description"`
	Service     string `json:"service"`
	URL         string `json:"url"`
}

var (
	digestImage = regexp.MustCompile(
		`^[a-z0-9][a-z0-9.:/_-]{0,200}@sha256:[a-f0-9]{64}$`)
	hostName  = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$`)
	dnsLabel  = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	fileKey   = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)
	blackoutF = regexp.MustCompile(`^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$`)
	projectID = regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)
	githubRe  = regexp.MustCompile(`^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$`)
	serviceRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]*$`)
	branchRe  = regexp.MustCompile(`^[A-Za-z0-9._/-]{1,100}$`)
)

// Load reads and validates the file at path.
func Load(path string) (*Config, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("opening the configuration: %w", err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("reading the configuration: %w", err)
	}
	if !info.Mode().IsRegular() || info.Size() > maxFileBytes {
		return nil, errors.New("the configuration is not a bounded regular file")
	}
	data, err := io.ReadAll(io.LimitReader(f, maxFileBytes+1))
	if err != nil || len(data) > maxFileBytes {
		return nil, errors.New("reading the configuration failed")
	}
	return Parse(data)
}

// Parse decodes strict JSON: unknown keys, trailing data and any invalid
// value are errors.
func Parse(data []byte) (*Config, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var c Config
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("decoding the configuration: %w", err)
	}
	if dec.More() {
		return nil, errors.New("the configuration has trailing data")
	}
	if err := c.validate(); err != nil {
		return nil, err
	}
	return &c, nil
}

func (c *Config) validate() error {
	for name, addr := range map[string]string{
		"ax_server": c.AXServer, "router": c.Router,
	} {
		host, port, err := net.SplitHostPort(addr)
		if err != nil || host == "" || port == "" {
			return fmt.Errorf("%s must be host:port", name)
		}
	}
	for name, addr := range map[string]string{
		"listen": c.Listen, "health_listen": c.HealthListen,
	} {
		if _, port, err := net.SplitHostPort(addr); err != nil || port == "" {
			return fmt.Errorf("%s must be [host]:port", name)
		}
	}
	if c.Listen == c.HealthListen {
		return errors.New("listen and health_listen must differ")
	}
	if !dnsLabel.MatchString(c.Atespace) {
		return errors.New("atespace must be a DNS label")
	}
	if !digestImage.MatchString(c.AgentImage) {
		return errors.New("agent_image must be pinned by sha256 digest")
	}
	if len(c.RepoHosts) == 0 {
		return errors.New("repo_hosts must name at least one host")
	}
	for _, h := range c.RepoHosts {
		if !hostName.MatchString(h) {
			return fmt.Errorf("repo_hosts: %q is not a lower-case host name", h)
		}
	}
	if !httpsOrigin(c.Origin) {
		return errors.New("origin must be https://<host> with no path or port")
	}
	for _, o := range c.ExtraOrigins {
		if !httpsOrigin(o) || o == c.Origin {
			return fmt.Errorf("extra_origins: %q must be another https://<host> with no path or port", o)
		}
	}
	if _, err := c.Window(); err != nil {
		return err
	}
	if c.WatchdogLeadMinutes < 1 || c.WatchdogLeadMinutes > 60 {
		return errors.New("watchdog_lead_minutes must be 1-60")
	}
	if c.MaxTurns < 1 || c.MaxTurns > 500 {
		return errors.New("max_turns must be 1-500")
	}
	// The guest SIGKILLs at the timeout. The browser's event stream does
	// not follow a run's length: the panel ends it every 30 minutes and
	// EventSource reconnects, well within Traefik's 3600 s readTimeout.
	if c.MaxTimeoutMinutes < 5 || c.MaxTimeoutMinutes > 180 {
		return errors.New("max_timeout_minutes must be 5-180")
	}
	if c.PromptMode != "stdin" && c.PromptMode != "argument" {
		return errors.New(`prompt_mode must be "stdin" or "argument"`)
	}
	if !strings.HasPrefix(c.TokenDirectory, "/") || !fileKey.MatchString(c.TokenKey) {
		return errors.New("token_directory must be absolute and token_key a file name")
	}
	for name, p := range map[string]string{
		"tls_cert_file": c.TLSCertFile, "tls_key_file": c.TLSKeyFile,
		"client_ca_file": c.ClientCAFile,
	} {
		if !strings.HasPrefix(p, "/") {
			return fmt.Errorf("%s must be an absolute path", name)
		}
	}
	if c.ClientCommonName == "" {
		return errors.New("client_common_name is required")
	}
	return c.validateOffice()
}

func (c *Config) validateOffice() error {
	for name, p := range map[string]string{
		"state_dir": c.StateDir, "office_secret_dir": c.OfficeSecretDir,
	} {
		if !strings.HasPrefix(p, "/") || strings.Contains(p, "..") {
			return fmt.Errorf("%s must be an absolute path", name)
		}
	}
	if !fileKey.MatchString(c.CodexAuthKey) || !fileKey.MatchString(c.GitHubTokenKey) ||
		c.CodexAuthKey == c.GitHubTokenKey {
		return errors.New("codex_auth_key and github_token_key must be two different file names")
	}
	if c.MaxQueue < 1 || c.MaxQueue > 1000 {
		return errors.New("max_queue must be 1-1000")
	}
	if c.RetentionJobs < 100 || c.RetentionJobs > 20000 {
		return errors.New("retention_jobs must be 100-20000")
	}
	if len(c.Projects) > MaxProjects {
		return fmt.Errorf("projects: at most %d", MaxProjects)
	}
	github := false
	for _, h := range c.RepoHosts {
		github = github || h == "github.com"
	}
	seen := map[string]bool{}
	for i, p := range c.Projects {
		if err := p.validate(github); err != nil {
			return fmt.Errorf("projects[%d]: %w", i, err)
		}
		if seen[p.ID] {
			return fmt.Errorf("projects[%d]: duplicate id %q", i, p.ID)
		}
		seen[p.ID] = true
	}
	return nil
}

func (p Project) validate(githubAllowed bool) error {
	if !projectID.MatchString(p.ID) {
		return errors.New("id must match ^[a-z][a-z0-9-]{1,31}$")
	}
	if strings.TrimSpace(p.Name) == "" || utf8.RuneCountInString(p.Name) > maxProjectName || hasControl(p.Name) {
		return fmt.Errorf("name must be 1-%d characters on one line", maxProjectName)
	}
	if !githubAllowed || !githubRe.MatchString(p.Repo) || strings.Contains(p.Repo, "..") ||
		strings.HasSuffix(p.Repo, ".") || strings.HasSuffix(p.Repo, ".git") {
		return errors.New("repo must be https://github.com/<owner>/<repo> (and github.com a repo host)")
	}
	if !validBranch(p.Branch) {
		return errors.New("branch is not a valid branch name")
	}
	if utf8.RuneCountInString(p.Description) > maxProjectDesc || hasControl(p.Description) {
		return fmt.Errorf("description must be at most %d characters on one line", maxProjectDesc)
	}
	if p.Service != "" && (len(p.Service) > maxProjectService || !serviceRe.MatchString(p.Service)) {
		return errors.New("service must match ^[a-z0-9][a-z0-9_.-]*$ (at most 80) or be empty")
	}
	if p.URL != "" {
		u, err := url.Parse(p.URL)
		if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Opaque != "" ||
			len(p.URL) > 300 || strings.ContainsAny(p.URL, " <>\"'") {
			return errors.New("url must be an https URL or empty")
		}
	}
	return nil
}

// validBranch mirrors runs.ValidateBranch (the safe subset of git
// check-ref-format); runs imports this package, so it cannot be reused.
func validBranch(b string) bool {
	return branchRe.MatchString(b) &&
		!strings.HasPrefix(b, "-") && !strings.HasPrefix(b, "/") && !strings.HasPrefix(b, ".") &&
		!strings.HasSuffix(b, "/") && !strings.HasSuffix(b, ".") && !strings.HasSuffix(b, ".lock") &&
		!strings.Contains(b, "..") && !strings.Contains(b, "//") && !strings.Contains(b, "/.")
}

func hasControl(s string) bool {
	if !utf8.ValidString(s) {
		return true
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return true
		}
	}
	return false
}

func httpsOrigin(o string) bool {
	return strings.HasPrefix(o, "https://") && hostName.MatchString(strings.TrimPrefix(o, "https://"))
}

// Window parses Blackout.
func (c *Config) Window() (Window, error) {
	return ParseWindow(c.Blackout)
}

// MaxTimeout is MaxTimeoutMinutes as a duration.
func (c *Config) MaxTimeout() time.Duration {
	return time.Duration(c.MaxTimeoutMinutes) * time.Minute
}

// Window is a daily UTC interval [Start, End) in minutes after midnight.
// End < Start means it wraps past midnight, like 22:30-00:40. The zero
// Window is no window at all: it overlaps nothing.
type Window struct {
	Start, End int
}

// ParseWindow reads "HH:MM-HH:MM", or "" as the zero Window.
func ParseWindow(s string) (Window, error) {
	if s == "" {
		return Window{}, nil
	}
	if !blackoutF.MatchString(s) {
		return Window{}, errors.New(`blackout must be "HH:MM-HH:MM" in UTC`)
	}
	minutes := func(hhmm string) int {
		return int(hhmm[0]-'0')*600 + int(hhmm[1]-'0')*60 +
			int(hhmm[3]-'0')*10 + int(hhmm[4]-'0')
	}
	w := Window{Start: minutes(s[:5]), End: minutes(s[6:])}
	if w.Start == w.End {
		return Window{}, errors.New("blackout must not be empty")
	}
	return w, nil
}

// Overlaps reports whether [from, to) meets any daily instance of w.
func (w Window) Overlaps(from, to time.Time) bool {
	if w.None() {
		return false
	}
	from, to = from.UTC(), to.UTC()
	if !to.After(from) {
		to = from.Add(time.Nanosecond)
	}
	day := time.Date(from.Year(), from.Month(), from.Day(), 0, 0, 0, 0, time.UTC)
	// A wrapping instance that began the day before still covers from.
	for d := day.AddDate(0, 0, -1); d.Before(to); d = d.AddDate(0, 0, 1) {
		start := d.Add(time.Duration(w.Start) * time.Minute)
		end := d.Add(time.Duration(w.End) * time.Minute)
		if w.End < w.Start {
			end = end.Add(24 * time.Hour)
		}
		if start.Before(to) && from.Before(end) {
			return true
		}
	}
	return false
}

// Contains reports whether t falls inside w.
func (w Window) Contains(t time.Time) bool {
	return w.Overlaps(t, t.Add(time.Nanosecond))
}

// None reports whether w is the zero Window, which ParseWindow returns
// only for "": it refuses any other empty interval.
func (w Window) None() bool {
	return w.Start == w.End
}

// String prints w as it is configured.
func (w Window) String() string {
	if w.None() {
		return ""
	}
	return fmt.Sprintf("%02d:%02d-%02d:%02d", w.Start/60, w.Start%60, w.End/60, w.End%60)
}
