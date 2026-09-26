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
	"os"
	"regexp"
	"strings"
	"time"
)

// maxFileBytes bounds the configuration file.
const maxFileBytes = 64 << 10

// Config is the panel's whole configuration. Every field is required.
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
	// Blackout is the daily UTC window with no runs, "HH:MM-HH:MM".
	Blackout string `json:"blackout"`
	// WatchdogLeadMinutes is how long before the blackout an active run is
	// cancelled.
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
}

var (
	digestImage = regexp.MustCompile(
		`^[a-z0-9][a-z0-9.:/_-]{0,200}@sha256:[a-f0-9]{64}$`)
	hostName  = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$`)
	dnsLabel  = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	fileKey   = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)
	blackoutF = regexp.MustCompile(`^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$`)
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
	if !strings.HasPrefix(c.Origin, "https://") ||
		!hostName.MatchString(strings.TrimPrefix(c.Origin, "https://")) {
		return errors.New("origin must be https://<host> with no path or port")
	}
	if _, err := c.Window(); err != nil {
		return err
	}
	if c.WatchdogLeadMinutes < 1 || c.WatchdogLeadMinutes > 60 {
		return errors.New("watchdog_lead_minutes must be 1-60")
	}
	if c.MaxTurns < 1 || c.MaxTurns > 50 {
		return errors.New("max_turns must be 1-50")
	}
	// The guest SIGKILLs at the timeout, and SSE streams must end well
	// within Traefik's 3600 s entryPoint readTimeout.
	if c.MaxTimeoutMinutes < 5 || c.MaxTimeoutMinutes > 45 {
		return errors.New("max_timeout_minutes must be 5-45")
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
	return nil
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
// End < Start means it wraps past midnight, like 22:30-00:40.
type Window struct {
	Start, End int
}

// ParseWindow reads "HH:MM-HH:MM".
func ParseWindow(s string) (Window, error) {
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

// String prints w as it is configured.
func (w Window) String() string {
	return fmt.Sprintf("%02d:%02d-%02d:%02d", w.Start/60, w.Start%60, w.End/60, w.End%60)
}
