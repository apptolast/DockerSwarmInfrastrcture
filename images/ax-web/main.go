// Command ax-web is the AX web panel (serve) and its TCP forwarder
// (forward). See docs/AX.md and the ax-lab role that deploys it.
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
	"strings"
	"syscall"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/forward"
	"apptolast.com/ax-web/internal/guest"
	"apptolast.com/ax-web/internal/runs"
	"apptolast.com/ax-web/internal/web"
)

const usage = "usage: ax-web serve --config <file> | " +
	"ax-web forward --listen <addr> --target <host:port> --allow-cidr <cidr>[,<cidr>...]"

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

func serve(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	path := fs.String("config", "/etc/ax-web/config.json", "configuration file")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("nothing may follow the flags")
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
	manager := runs.NewManager(opt, runs.Deps{
		AX: ax,
		DialGuest: func(atespace, actor string) (runs.ProcessClient, error) {
			return guest.Dial(cfg.Router, atespace, actor)
		},
		ReadToken: runs.TokenReader(cfg.TokenDirectory, cfg.TokenKey),
		Audit:     log,
	})
	background, stopBackground := context.WithCancel(context.Background())
	defer stopBackground()
	go manager.Reap(background)
	go manager.Watchdog(background)

	stopStreams := make(chan struct{})
	app := &web.Server{
		AX: ax, Runs: manager, Atespace: cfg.Atespace, Origin: cfg.Origin,
		Limits: runs.Limits{
			RepoHosts: cfg.RepoHosts, MaxTurns: cfg.MaxTurns,
			MaxTimeout: cfg.MaxTimeout(), PromptInArg: opt.PromptInArg,
		},
		Blackout: window, WatchdogLead: opt.WatchdogLead, Certs: certs,
		Now: time.Now, Audit: log, PingInterval: 15 * time.Second,
		CallTimeout: opt.CallTimeout, DeleteWait: 5 * time.Second,
		DeleteTimeout: opt.CleanupTimeout, Stop: stopStreams,
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
	log.Info("serving", "listen", cfg.Listen, "health", cfg.HealthListen)

	select {
	case <-ctx.Done():
	case err = <-errs:
	}
	// Cancel and clean up the active run inside the pod's
	// terminationGracePeriodSeconds (120 s), then stop serving.
	close(stopStreams)
	shutdown, cancel := context.WithTimeout(context.Background(), 110*time.Second)
	defer cancel()
	manager.Shutdown(shutdown)
	stopBackground()
	_ = srv.Shutdown(shutdown)
	_ = health.Shutdown(shutdown)
	if errors.Is(err, http.ErrServerClosed) {
		err = nil
	}
	return err
}
