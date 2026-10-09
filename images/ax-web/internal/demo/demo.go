// Package demo runs the Oficina de agentes on a laptop or in a review
// session without AX: the real office and web packages over a simulated
// executor, an in-memory AX and a fake GitHub API (repository listings
// only), on loopback addresses only, with fake credentials. Nothing it
// does reaches a sandbox, a model or GitHub.
package demo

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/office"
	"apptolast.com/ax-web/internal/web"
)

// Options configure a demo.
type Options struct {
	// Listen must be a loopback address (127.0.0.0/8, ::1 or localhost).
	Listen string
	// StateDir keeps the demo's office between runs.
	StateDir string
	// Speed divides the length of each simulated run (1 = about 15 s).
	Speed   float64
	Version string
	Log     *slog.Logger
	// Listening, when set, gets the bound address once serving.
	Listening func(addr string)
	// Samples queues a few jobs and a team on a fresh state.
	Samples bool
}

// Projects are the demo's projects: public repositories, never cloned.
var Projects = []config.Project{
	{ID: "dockerswarm-infra", Name: "Infraestructura AppToLast", Repo: "https://github.com/apptolast/DockerSwarmInfrastrcture",
		Branch: "main", Description: "El servidor de AppToLast como código; la Oficina vive en images/ax-web."},
	{ID: "ax", Name: "AX (google/ax)", Repo: "https://github.com/google/ax", Branch: "main",
		Description: "El plano de control de sandboxes que ejecuta a los agentes."},
	{ID: "grpc-go", Name: "gRPC-Go", Repo: "https://github.com/grpc/grpc-go", Branch: "master",
		Description: "La biblioteca gRPC de Go con la que el panel habla con AX."},
}

// CheckLoopback refuses any listen address that is not loopback.
func CheckLoopback(listen string) error {
	host, port, err := net.SplitHostPort(listen)
	if err != nil || port == "" {
		return errors.New("--listen debe ser host:puerto")
	}
	if host == "localhost" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("la demo solo escucha en loopback (127.0.0.1, ::1 o localhost), no en %q", host)
	}
	return nil
}

// fakeCredentials writes the demo's credential files: a Claude token and
// a Codex auth.json that only say "demo", and no GitHub token.
func fakeCredentials(dir string) (claudeFile, secretDir string, err error) {
	agent := filepath.Join(dir, "demo-secrets", "agent")
	secretDir = filepath.Join(dir, "demo-secrets", "office")
	for _, d := range []string{agent, secretDir} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return "", "", err
		}
	}
	claudeFile = filepath.Join(agent, "claude-oauth-token")
	if err := os.WriteFile(claudeFile, []byte("demo-token-sin-valor\n"), 0o600); err != nil {
		return "", "", err
	}
	auth, _ := json.Marshal(map[string]any{
		"OPENAI_API_KEY": nil,
		"tokens": map[string]string{"id_token": "demo", "access_token": "demo", "refresh_token": "demo",
			"account_id": "demo"},
		"last_refresh": "2026-10-01T00:00:00Z",
	})
	if err := os.WriteFile(filepath.Join(secretDir, "codex-auth-json"), auth, 0o600); err != nil {
		return "", "", err
	}
	return claudeFile, secretDir, nil
}

// Run serves the demo until ctx ends.
func Run(ctx context.Context, opt Options) error {
	if err := CheckLoopback(opt.Listen); err != nil {
		return err
	}
	if opt.StateDir == "" {
		return errors.New("--state es obligatorio")
	}
	if opt.Speed <= 0 || opt.Speed > 1000 {
		return errors.New("--speed va de 0.01 a 1000")
	}
	log := opt.Log
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	state, err := filepath.Abs(opt.StateDir)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(state, 0o700); err != nil {
		return err
	}
	claudeFile, secretDir, err := fakeCredentials(state)
	if err != nil {
		return err
	}
	ghLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	gh := &http.Server{Handler: fakeGitHub(), ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = gh.Serve(ghLn) }()
	defer gh.Close()
	ax := NewAX(time.Now())
	exec := NewExecutor(opt.Speed, ax, time.Now)
	off, err := office.New(office.Config{
		StateDir: filepath.Join(state, "office"), ClaudeTokenFile: claudeFile, OfficeSecretDir: secretDir,
		CodexAuthKey: "codex-auth-json", GitHubTokenKey: "github-token", Projects: Projects,
		Limits:   office.Limits{MaxTurns: 150, MaxTimeoutMinutes: 90, RepoHosts: []string{"github.com"}},
		MaxQueue: 200, RetentionJobs: 3000, Version: opt.Version, GitHubAPI: "http://" + ghLn.Addr().String(),
	}, exec, nil, time.Now, log)
	if err != nil {
		return err
	}
	exec.Credentials, exec.SaveCodexAuth = off.Credentials, off.SaveCodexAuth

	ln, err := net.Listen("tcp", opt.Listen)
	if err != nil {
		return err
	}
	_, port, _ := net.SplitHostPort(ln.Addr().String())
	stop := make(chan struct{})
	app := &web.Server{
		AX: ax, Tasks: ax, Office: off, Atespace: "default",
		Origin: "http://127.0.0.1:" + port, ExtraOrigins: []string{"http://localhost:" + port, "http://[::1]:" + port},
		Certs: &web.CertWatch{}, Now: time.Now, Audit: log, PingInterval: 15 * time.Second,
		CallTimeout: 5 * time.Second, DeleteWait: 2 * time.Second, DeleteTimeout: 10 * time.Second,
		Stop: stop, Version: opt.Version,
	}
	srv := &http.Server{
		Handler: app.Handler(), ReadHeaderTimeout: 10 * time.Second, ReadTimeout: time.Minute,
		IdleTimeout: 180 * time.Second, MaxHeaderBytes: 32 << 10,
		ErrorLog: slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	loops, stopLoops := context.WithCancel(context.Background())
	defer stopLoops()
	go off.Run(loops)
	if opt.Samples && len(off.Snapshot().Jobs) == 0 {
		seedSamples(off)
	}
	errs := make(chan error, 1)
	go func() { errs <- srv.Serve(ln) }()
	log.Info("demo", "url", "http://127.0.0.1:"+port+"/", "state", state)
	if opt.Listening != nil {
		opt.Listening(ln.Addr().String())
	}
	select {
	case <-ctx.Done():
	case err = <-errs:
	}
	close(stop)
	shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	stopLoops()
	exec.Shutdown(shutdown)
	off.Close(shutdown)
	_ = srv.Shutdown(shutdown)
	if errors.Is(err, http.ErrServerClosed) {
		err = nil
	}
	return err
}

// seedSamples gives a fresh demo something to show.
func seedSamples(off *office.Office) {
	ctx := context.Background()
	const ip = "demo"
	_, _ = off.CreateJob(ctx, office.JobRequest{ProjectID: "dockerswarm-infra", AgentID: "becario", Kind: office.KindAsk,
		Prompt: "¿Dónde se configura el panel web de AX y qué valida al arrancar?"}, ip)
	_, _ = off.CreateJob(ctx, office.JobRequest{ProjectID: "dockerswarm-infra", AgentID: "linus", Kind: office.KindChange,
		Prompt: "Añade al README una sección corta sobre cómo arrancar la demo de la Oficina."}, ip)
	_, _ = off.CreatePipeline(ctx, office.PipelineRequest{Template: office.TplTeam, ProjectID: "ax",
		Task: "Documenta en el README cómo se reapan las tareas web-* al arrancar."}, ip)
	_, _ = off.CreateJob(ctx, office.JobRequest{ProjectID: "grpc-go", AgentID: "ada", Kind: office.KindAsk,
		Prompt: "Explica en cinco pasos cómo funciona un stream de servidor en grpc-go.", Priority: ptr(0)}, ip)
}

func ptr[T any](v T) *T { return &v }
