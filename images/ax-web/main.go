// Command ax-web is the Oficina de agentes, the AX web panel (serve), its
// TCP forwarder (forward) and a local demo without AX (demo). See
// docs/AX_WEB.md and the ax-lab role that deploys it.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/demo"
	"apptolast.com/ax-web/internal/forward"
	"apptolast.com/ax-web/internal/guest"
	"apptolast.com/ax-web/internal/office"
	"apptolast.com/ax-web/internal/runs"
	"apptolast.com/ax-web/internal/web"
)

// Version of the panel and its office.
const Version = "1.0.1"

const usage = "usage: ax-web serve --config <file> | " +
	"ax-web forward --listen <addr> --target <host:port> --allow-cidr <cidr>[,<cidr>...] | " +
	"ax-web demo --listen 127.0.0.1:8090 --state <dir> [--speed 1.0]"

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, usage)
		os.Exit(64)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	var err error
	switch os.Args[1] {
	case "serve":
		err = serve(ctx, log, os.Args[2:])
	case "forward":
		err = forwardCmd(ctx, log, os.Args[2:])
	case "demo":
		err = demoCmd(ctx, log, os.Args[2:])
	default:
		fmt.Fprintln(os.Stderr, usage)
		os.Exit(64)
	}
	if err != nil {
		log.Error("ax-web stopped", "error", err)
		os.Exit(1)
	}
}

func forwardCmd(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("forward", flag.ContinueOnError)
	listen := fs.String("listen", ":8443", "listen address")
	target := fs.String("target", "", "host:port of the panel's NodePort")
	allowCIDR := fs.String("allow-cidr", "", "comma-separated networks that may connect: Traefik's overlay (required)")
	maxConns := fs.Int("max-conns", 64, "concurrent connection cap")
	// SSE pings every 15 s, and the panel and Traefik drop idle connections
	// at 180 s, so a live connection is never idle this long.
	idle := fs.Duration("idle-timeout", 5*time.Minute, "idle connection timeout")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if _, _, err := net.SplitHostPort(*target); err != nil || fs.NArg() != 0 {
		return errors.New("--target must be host:port and nothing may follow the flags")
	}
	if *maxConns < 1 || *idle < time.Minute {
		return errors.New("--max-conns must be positive and --idle-timeout at least 1m")
	}
	allow, err := parseAllow(*allowCIDR)
	if err != nil {
		return err
	}
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		return err
	}
	log.Info("forwarding", "listen", *listen, "target", *target, "allow", *allowCIDR)
	f := &forward.Forwarder{
		Target: *target, Allow: allow, MaxConns: *maxConns, IdleTimeout: *idle,
		CloseGrace: 30 * time.Second, DialTimeout: 3 * time.Second, Log: log,
	}
	return f.Serve(ctx, ln)
}

// parseAllow reads --allow-cidr. At least one network is required, and none
// may be a catch-all: a sandbox on the kind bridge must never reach the
// forwarder's slots.
func parseAllow(value string) ([]netip.Prefix, error) {
	var out []netip.Prefix
	for _, part := range strings.Split(value, ",") {
		if part = strings.TrimSpace(part); part == "" {
			continue
		}
		p, err := netip.ParsePrefix(part)
		if err != nil || p.Bits() == 0 {
			return nil, fmt.Errorf("--allow-cidr: %q is not a network narrower than /0", part)
		}
		out = append(out, p.Masked())
	}
	if len(out) == 0 {
		return nil, errors.New("--allow-cidr must name the networks that may connect")
	}
	return out, nil
}

// demoCmd serves the office over a simulated executor on loopback.
func demoCmd(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("demo", flag.ContinueOnError)
	listen := fs.String("listen", "127.0.0.1:8090", "loopback address to serve plain HTTP on")
	state := fs.String("state", "", "directory for the demo's state (required)")
	speed := fs.Float64("speed", 1, "divides the length of each simulated run (1 = about 15 s)")
	samples := fs.Bool("samples", true, "queue a few example jobs on a fresh state")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("nothing may follow the flags")
	}
	return demo.Run(ctx, demo.Options{Listen: *listen, StateDir: *state, Speed: *speed, Version: Version,
		Log: log, Samples: *samples})
}

// axProbe tells the office what blocks the only worker: the run
// manager's state and a host task (tarea-*) in AX.
type axProbe struct {
	ax       v1alpha1.AXClient
	manager  *runs.Manager
	atespace string
}

func (p axProbe) Probe(ctx context.Context) office.AXState {
	st := p.manager.Status()
	out := office.AXState{Ready: st.Ready, ReapNote: st.ReapNote, CleanupFailed: st.Cleanup}
	resp, err := p.ax.ListTasks(ctx, &v1alpha1.ListTasksRequest{Atespace: p.atespace, Limit: 100})
	if err != nil {
		return out
	}
	for _, t := range resp.GetTasks() {
		if n := t.GetMetadata().GetName(); strings.HasPrefix(n, runs.HostRunPrefix) {
			out.Blocking = n
			break
		}
	}
	return out
}

// cgroupMemoryMax is the container's memory limit (cgroup v2).
const cgroupMemoryMax = "/sys/fs/cgroup/memory.max"

// memoryLimit is the Go runtime's soft memory limit: 80 % of the
// container's memory.max, so the garbage collector works harder before
// the kernel kills the pod. None when GOMEMLIMIT is set (it wins) or the
// cgroup has no numeric limit ("max").
func memoryLimit(getenv func(string) string, read func(string) ([]byte, error)) (int64, bool) {
	if getenv("GOMEMLIMIT") != "" {
		return 0, false
	}
	data, err := read(cgroupMemoryMax)
	if err != nil {
		return 0, false
	}
	n, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	if err != nil || n <= 0 {
		return 0, false
	}
	return n / 10 * 8, true
}

// officeConfig maps the reviewed configuration to the office's.
func officeConfig(cfg *config.Config) office.Config {
	return office.Config{
		StateDir: cfg.StateDir, ClaudeTokenFile: filepath.Join(cfg.TokenDirectory, cfg.TokenKey),
		OfficeSecretDir: cfg.OfficeSecretDir, CodexAuthKey: cfg.CodexAuthKey, GitHubTokenKey: cfg.GitHubTokenKey,
		Projects: cfg.Projects,
		Limits: office.Limits{MaxTurns: cfg.MaxTurns, MaxTimeoutMinutes: cfg.MaxTimeoutMinutes,
			RepoHosts: cfg.RepoHosts},
		MaxQueue: cfg.MaxQueue, RetentionJobs: cfg.RetentionJobs, Version: Version,
		GitHubAPI: "https://api.github.com",
	}
}

func serve(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	path := fs.String("config", "/etc/ax-web/config.json", "configuration file")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("nothing may follow the flags")
	}
	if limit, ok := memoryLimit(os.Getenv, os.ReadFile); ok {
		debug.SetMemoryLimit(limit)
		log.Info("memory limit", "gomemlimit_bytes", limit, "source", cgroupMemoryMax)
	}
	cfg, err := config.Load(*path)
	if err != nil {
		return err
	}
	window, err := cfg.Window()
	if err != nil {
		return err
	}
	tlsCfg, certs, err := web.ServerTLS(cfg.TLSCertFile, cfg.TLSKeyFile, cfg.ClientCAFile, cfg.ClientCommonName)
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient(cfg.AXServer, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer conn.Close()
	ax := v1alpha1.NewAXClient(conn)

	opt := runs.DefaultOptions()
	opt.Atespace, opt.AgentImage, opt.Blackout = cfg.Atespace, cfg.AgentImage, window
	opt.WatchdogLead = time.Duration(cfg.WatchdogLeadMinutes) * time.Minute
	opt.PromptInArg = cfg.PromptMode == "argument"
	// The office and the run manager need each other: the manager asks
	// the office for credentials when a run starts, the office launches
	// runs on the manager.
	var off *office.Office
	manager := runs.NewManager(opt, runs.Deps{
		AX: ax,
		DialGuest: func(atespace, actor string) (runs.ProcessClient, error) {
			return guest.Dial(cfg.Router, atespace, actor)
		},
		Credentials:   func(h string) (map[string]string, error) { return off.Credentials(h) },
		SaveCodexAuth: func(data []byte, created time.Time) error { return off.SaveCodexAuth(data, created) },
		Audit:         log,
	})
	off, err = office.New(officeConfig(cfg), manager, axProbe{ax: ax, manager: manager, atespace: cfg.Atespace},
		time.Now, log)
	if err != nil {
		return err
	}
	background, stopBackground := context.WithCancel(context.Background())
	defer stopBackground()
	go manager.Reap(background)
	go manager.Watchdog(background)
	officeLoops, stopOffice := context.WithCancel(background)
	defer stopOffice()
	go off.Run(officeLoops)

	stopStreams := make(chan struct{})
	app := &web.Server{
		AX: ax, Tasks: manager, Office: off, Atespace: cfg.Atespace, Origin: cfg.Origin,
		ExtraOrigins: cfg.ExtraOrigins, Blackout: window, WatchdogLead: opt.WatchdogLead, Certs: certs,
		Now: time.Now, Audit: log, PingInterval: 15 * time.Second,
		CallTimeout: opt.CallTimeout, DeleteWait: 5 * time.Second,
		DeleteTimeout: opt.CleanupTimeout, Stop: stopStreams, Version: Version,
	}
	srv := &http.Server{
		Addr: cfg.Listen, Handler: app.Handler(), TLSConfig: tlsCfg,
		ReadHeaderTimeout: 10 * time.Second, ReadTimeout: time.Minute,
		IdleTimeout: 180 * time.Second, MaxHeaderBytes: 32 << 10,
		ErrorLog: slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	health := web.HealthServer(cfg.HealthListen)
	errs := make(chan error, 2)
	go func() { errs <- srv.ListenAndServeTLS("", "") }()
	go func() { errs <- health.ListenAndServe() }()
	log.Info("serving", "listen", cfg.Listen, "health", cfg.HealthListen, "version", Version)

	select {
	case <-ctx.Done():
	case err = <-errs:
	}
	// Within the pod's terminationGracePeriodSeconds (120 s): stop
	// dispatching, cancel and clean up the active run, record how it
	// ended, then stop serving.
	close(stopStreams)
	shutdown, cancel := context.WithTimeout(context.Background(), 110*time.Second)
	defer cancel()
	stopOffice()
	manager.Shutdown(shutdown)
	off.Close(shutdown)
	stopBackground()
	_ = srv.Shutdown(shutdown)
	_ = health.Shutdown(shutdown)
	if errors.Is(err, http.ErrServerClosed) {
		err = nil
	}
	return err
}
