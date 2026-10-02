package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"

	"apptolast.com/ax-web/internal/fakeax"
)

func freePort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().String()
}

func pemFile(t *testing.T, dir, name, kind string, der []byte) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, pem.EncodeToMemory(&pem.Block{Type: kind, Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestServeWiring runs the real serve command against a fake ax-server on
// TCP: configuration, mTLS over HTTP/2, the reaper, the probes and a clean
// shutdown.
func TestServeWiring(t *testing.T) {
	ax := fakeax.NewAX()
	ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "otra"}})
	grpcLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	gs := grpc.NewServer()
	v1alpha1.RegisterAXServer(gs, ax)
	go func() { _ = gs.Serve(grpcLn) }()
	defer gs.Stop()

	dir := t.TempDir()
	key := func() *ecdsa.PrivateKey {
		k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		return k
	}
	caKey, srvKey, cliKey := key(), key(), key()
	until := time.Now().Add(24 * time.Hour)
	caTmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "ca"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: until, IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign}
	caDER, _ := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	ca, _ := x509.ParseCertificate(caDER)
	leaf := func(n int64, cn string, eku x509.ExtKeyUsage, k *ecdsa.PrivateKey) []byte {
		der, err := x509.CreateCertificate(rand.Reader, &x509.Certificate{
			SerialNumber: big.NewInt(n), Subject: pkix.Name{CommonName: cn}, DNSNames: []string{cn},
			NotBefore: time.Now().Add(-time.Hour), NotAfter: until, ExtKeyUsage: []x509.ExtKeyUsage{eku},
			KeyUsage: x509.KeyUsageDigitalSignature,
		}, ca, &k.PublicKey, caKey)
		if err != nil {
			t.Fatal(err)
		}
		return der
	}
	srvDER := leaf(2, "ax-web", x509.ExtKeyUsageServerAuth, srvKey)
	cliDER := leaf(3, "edge-traefik", x509.ExtKeyUsageClientAuth, cliKey)
	srvKeyDER, _ := x509.MarshalECPrivateKey(srvKey)

	listen, health := freePort(t), freePort(t)
	cfg := map[string]any{
		"ax_server": grpcLn.Addr().String(), "router": "127.0.0.1:1", "atespace": "default",
		"agent_image":           "localhost:5001/ax-agents@sha256:" + strings.Repeat("ab", 32),
		"repo_hosts":            []string{"github.com"},
		"origin":                "https://ax.apptolast.com",
		"extra_origins":         []string{"https://oficina.apptolast.com"},
		"blackout":              "22:30-00:40",
		"watchdog_lead_minutes": 5, "max_turns": 150, "max_timeout_minutes": 90,
		"prompt_mode": "stdin", "token_directory": dir, "token_key": "agent",
		"tls_cert_file":      pemFile(t, dir, "server.crt", "CERTIFICATE", srvDER),
		"tls_key_file":       pemFile(t, dir, "server.pem", "EC PRIVATE KEY", srvKeyDER),
		"client_ca_file":     pemFile(t, dir, "ca.crt", "CERTIFICATE", caDER),
		"client_common_name": "edge-traefik", "listen": listen, "health_listen": health,
		"state_dir": filepath.Join(dir, "state"), "office_secret_dir": filepath.Join(dir, "office"),
		"codex_auth_key": "codex-auth-json", "github_token_key": "github-token",
		"max_queue": 50, "retention_jobs": 100,
		"projects": []map[string]string{{"id": "web", "name": "Web", "repo": "https://github.com/apptolast/web",
			"branch": "main", "description": "", "service": "", "url": ""}},
	}
	data, _ := json.Marshal(cfg)
	cfgPath := filepath.Join(dir, "config.json")
	if err := os.WriteFile(cfgPath, data, 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- serve(ctx, slog.New(slog.DiscardHandler), []string{"--config", cfgPath}) }()

	roots := x509.NewCertPool()
	roots.AddCert(ca)
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		ForceAttemptHTTP2: true,
		TLSClientConfig: &tls.Config{RootCAs: roots, ServerName: "ax-web", MinVersion: tls.VersionTLS13,
			Certificates: []tls.Certificate{{Certificate: [][]byte{cliDER}, PrivateKey: cliKey}}},
	}}
	var body string
	var proto string
	deadline := time.Now().Add(10 * time.Second)
	for {
		resp, err := client.Get("https://" + listen + "/api/tasks")
		if err == nil {
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			body, proto = string(b), resp.Proto
			break
		}
		if time.Now().After(deadline) {
			t.Fatal(err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	if !strings.Contains(body, `"name":"otra"`) || proto != "HTTP/2.0" {
		t.Fatalf("%s %s", proto, body)
	}
	// The office answers with its seeded state, kept under state_dir.
	resp, err := client.Get("https://" + listen + "/api/office")
	if err != nil {
		t.Fatal(err)
	}
	var snap struct {
		Version  string           `json:"version"`
		Agents   []map[string]any `json:"agents"`
		Projects []map[string]any `json:"projects"`
	}
	json.NewDecoder(resp.Body).Decode(&snap)
	resp.Body.Close()
	if snap.Version != Version || len(snap.Agents) != 9 || len(snap.Projects) != 1 {
		t.Fatalf("%+v", snap)
	}
	if _, err := os.Stat(filepath.Join(dir, "state", "office.json")); err != nil {
		t.Fatal(err)
	}
	resp, err = http.Get("http://" + health + "/readyz")
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("readyz: %v", err)
	}
	resp.Body.Close()
	// Without the client certificate nothing is served.
	anon := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: roots, ServerName: "ax-web"},
	}}
	if resp, err := anon.Get("https://" + listen + "/api/tasks"); err == nil {
		resp.Body.Close()
		t.Fatal("served without a client certificate")
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("serve did not stop")
	}
}

func TestDemoFlags(t *testing.T) {
	log := slog.New(slog.DiscardHandler)
	for _, args := range [][]string{
		{"--listen", "0.0.0.0:8090", "--state", t.TempDir()},
		{"--listen", ":8090", "--state", t.TempDir()},
		{"--listen", "127.0.0.1:0"},
		{"--listen", "127.0.0.1:0", "--state", t.TempDir(), "extra"},
		{"--listen", "127.0.0.1:0", "--state", t.TempDir(), "--speed", "0"},
	} {
		if err := demoCmd(context.Background(), log, args); err == nil {
			t.Errorf("%v accepted", args)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	if err := demoCmd(ctx, log, []string{"--listen", "127.0.0.1:0", "--state", t.TempDir(), "--samples=false"}); err != nil {
		t.Fatal(err)
	}
}

func TestForwardFlags(t *testing.T) {
	log := slog.New(slog.DiscardHandler)
	for _, args := range [][]string{
		{"--target", "no-port"},
		{"--target", "a:1", "extra"},
		{"--target", "a:1", "--max-conns", "0"},
		{"--target", "a:1", "--allow-cidr", "10.0.0.0/24", "--idle-timeout", "1s"},
		// The forwarder must name who may connect, and never everyone.
		{"--target", "a:1"},
		{"--target", "a:1", "--allow-cidr", ""},
		{"--target", "a:1", "--allow-cidr", "10.0.0.0"},
		{"--target", "a:1", "--allow-cidr", "10.0.0.0/24,0.0.0.0/0"},
		{"--target", "a:1", "--allow-cidr", "::/0"},
	} {
		if err := forwardCmd(context.Background(), log, args); err == nil {
			t.Errorf("%v accepted", args)
		}
	}
	allow, err := parseAllow(" 10.0.9.7/24 , fd00::1/64")
	if err != nil || len(allow) != 2 || allow[0].String() != "10.0.9.0/24" || allow[1].String() != "fd00::/64" {
		t.Fatalf("%v %v", allow, err)
	}
	// Valid flags serve until the context ends.
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if err := forwardCmd(ctx, log, []string{"--listen", "127.0.0.1:0", "--target", "a:1",
		"--allow-cidr", "10.0.0.0/24"}); err != nil {
		t.Fatal(err)
	}
}

func TestMemoryLimit(t *testing.T) {
	env := func(v string) func(string) string { return func(string) string { return v } }
	file := func(content string, err error) func(string) ([]byte, error) {
		return func(p string) ([]byte, error) {
			if p != cgroupMemoryMax {
				t.Errorf("read %s", p)
			}
			return []byte(content), err
		}
	}
	if n, ok := memoryLimit(env(""), file("402653184\n", nil)); !ok || n != 322122544 {
		t.Fatalf("384 MiB: %d %v", n, ok)
	}
	for name, c := range map[string]struct {
		env, content string
		err          error
	}{
		"GOMEMLIMIT wins": {"300MiB", "402653184\n", nil},
		"no limit":        {"", "max\n", nil},
		"no cgroup v2":    {"", "", os.ErrNotExist},
		"garbage":         {"", "-1\n", nil},
	} {
		if n, ok := memoryLimit(env(c.env), file(c.content, c.err)); ok {
			t.Errorf("%s: %d", name, n)
		}
	}
}
