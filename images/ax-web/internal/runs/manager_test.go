package runs

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/fakeax"
	"apptolast.com/ax-web/internal/guest"
)

// A fake credential derived at run time, so no literal looks like one.
var fakeCredential = strings.Repeat("c", 40)

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

type env struct {
	ax    *fakeax.AX
	guest *fakeax.Guest
	m     *Manager
	audit *lockedBuffer
	base  time.Time
}

// newEnv wires a manager to a fake AX and a fake guest. base is the clock's
// UTC start; it advances with real time.
func newEnv(t *testing.T, base time.Time, edit func(*Options)) *env {
	t.Helper()
	e := &env{ax: fakeax.NewAX(), guest: fakeax.NewGuest(), audit: &lockedBuffer{}, base: base}
	axConn, stopAX := fakeax.Serve(func(s *grpc.Server) { v1alpha1.RegisterAXServer(s, e.ax) })
	guestDial, stopGuest := fakeax.Listen(func(s *grpc.Server) { ateenvv1alpha.RegisterProcessServiceServer(s, e.guest) })
	t.Cleanup(func() { stopAX(); stopGuest() })
	window, _ := config.ParseWindow("22:30-00:40")
	opt := Options{
		Atespace: "default", AgentImage: "localhost:5001/ax-agents@sha256:" + strings.Repeat("ab", 32),
		Blackout: window, WatchdogLead: 5 * time.Minute,
		ReadyTimeout: 2 * time.Second, PollInterval: 10 * time.Millisecond,
		CleanupTimeout: 500 * time.Millisecond, CleanupRetry: 50 * time.Millisecond,
		KillGrace: 100 * time.Millisecond, StartMargin: 3 * time.Minute, CleanupMargin: 2 * time.Minute,
		CallTimeout: 2 * time.Second, WatchdogTick: 20 * time.Millisecond, BufferBytes: 1 << 20,
	}
	if edit != nil {
		edit(&opt)
	}
	start := time.Now()
	e.m = NewManager(opt, Deps{
		AX: v1alpha1.NewAXClient(axConn),
		DialGuest: func(atespace, actor string) (ProcessClient, error) {
			return guest.Dial(fakeax.Target, atespace, actor, guestDial)
		},
		ReadToken: func() (string, error) { return fakeCredential, nil },
		Now:       func() time.Time { return base.Add(time.Since(start)) },
		Audit:     slog.New(slog.NewJSONHandler(e.audit, nil)),
	})
	return e
}

var daytime = time.Date(2026, 9, 26, 10, 0, 0, 0, time.UTC)

func (e *env) ready(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	e.m.Reap(ctx)
	if !e.m.Status().Ready {
		t.Fatal("the reaper did not finish")
	}
}

func spec() Spec {
	return Spec{Repo: "https://github.com/a/b", Branch: "main", Prompt: "arregla los tests",
		Turns: 7, Timeout: 10 * time.Minute, CPU: "2", Memory: "1536Mi"}
}

func wait(t *testing.T, r *Run) View {
	t.Helper()
	select {
	case <-r.Done():
	case <-time.After(10 * time.Second):
		t.Fatalf("the run did not end: %+v", r.View())
	}
	return r.View()
}

func waitState(t *testing.T, r *Run, state string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for r.View().State != state {
		if time.Now().After(deadline) {
			t.Fatalf("state %s never reached: %+v", state, r.View())
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func output(r *Run) string {
	chunks, _, _, _ := r.Output.Read(0)
	var b strings.Builder
	for _, c := range chunks {
		b.WriteString(c.Text)
	}
	return b.String()
}

func TestRunHappyPath(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.ax.ReadyAfter = 2
	e.guest.Hold = false
	// stream-json events, one split across two chunks.
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{
		{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(`{"type":"system","subtype":"init","model":"m"}` + "\n" + `{"type":"assistant","message":{"content":[{"type":"text","te`)}},
		{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(`xt":"hola"},{"type":"tool_use","name":"Read","input":{"file_path":"x"}}]}}` + "\n")}},
		{Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: []byte("aviso\n")}},
		{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(`{"type":"result","subtype":"success","result":"listo","num_turns":2,"total_cost_usd":0.5}` + "\n")}},
	}
	s := spec()
	r, err := e.m.Start(context.Background(), s, "203.0.113.7")
	if err != nil {
		t.Fatal(err)
	}
	if r.ID != "web-20260926-100000" || r.Workspace != "ws-web-20260926-100000" {
		t.Fatalf("names %s %s", r.ID, r.Workspace)
	}
	v := wait(t, r)
	if v.State != StateFinished || v.Outcome != OutcomeExited || v.ExitCode == nil || *v.ExitCode != 0 {
		t.Fatalf("%+v", v)
	}

	tasks, workspaces, _ := e.ax.Snapshot()
	if len(tasks) != 1 || len(workspaces) != 1 {
		t.Fatalf("created %d tasks, %d workspaces", len(tasks), len(workspaces))
	}
	ts := tasks[0].GetSpec()
	if len(ts.GetEnv()) != 0 {
		t.Fatal("the task spec carries env: the credential would be persisted")
	}
	if !ts.GetDebug() || ts.GetImage() != e.m.opt.AgentImage || len(ts.GetCommand()) != 0 ||
		ts.GetResources().GetLimits().GetCpu() != "2" || ts.GetResources().GetLimits().GetMemory() != "1536Mi" ||
		len(ts.GetWorkspaces()) != 1 || ts.GetWorkspaces()[0].GetName() != r.Workspace ||
		ts.GetWorkspaces()[0].GetPath() != "/workspace" {
		t.Fatalf("task spec %v", ts)
	}
	git := workspaces[0].GetSpec().GetGit()
	if len(git) != 1 || git[0].GetRepo() != s.Repo || git[0].GetBranch() != "main" || git[0].GetDir() != "repo" ||
		git[0].GetDepth() != 1 {
		t.Fatalf("workspace %v", git)
	}

	started, actors, stdin, closed, _ := e.guest.Snapshot()
	if len(started) != 2 {
		t.Fatalf("%d processes", len(started))
	}
	// First the clone check, with no credential.
	if c := started[0]; !slices.Equal(c.GetCommand(), []string{"git", "-C", "/workspace/repo", "rev-parse", "--verify", "HEAD"}) ||
		len(c.GetEnv()) != 0 || c.GetStdin() {
		t.Fatalf("check %v", c)
	}
	p := started[1]
	if !slices.Equal(p.GetCommand(), []string{"ax-agent", "claude", "-p", "--restricted", "--strict-mcp-config",
		"--output-format", "stream-json", "--verbose", "--max-turns", "7"}) ||
		p.GetCwd() != "/workspace/repo" || !p.GetStdin() || p.GetTimeout().AsDuration() != 10*time.Minute {
		t.Fatalf("process %v", p)
	}
	if len(p.GetEnv()) != 1 || p.GetEnv()[TokenEnv] != fakeCredential {
		t.Fatal("the credential must travel only in StartProcess.env")
	}
	if string(stdin) != s.Prompt || !closed {
		t.Fatalf("stdin %q closed=%v", stdin, closed)
	}
	for _, a := range actors {
		if a != "default/actor-1" {
			t.Fatalf("actor header %q", a)
		}
	}
	if !strings.Contains(output(r), "[sesión iniciada, modelo m]\nhola\n[herramienta: Read]\n") ||
		!strings.Contains(output(r), "aviso\n") || !strings.Contains(output(r), "listo") ||
		strings.Contains(output(r), "file_path") {
		t.Fatalf("output %q", output(r))
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("the task or workspace survived the cleanup")
	}

	audit := e.audit.String()
	sum := sha256.Sum256([]byte(s.Prompt))
	for _, want := range []string{`"action":"run.start"`, `"client_ip":"203.0.113.7"`,
		hex.EncodeToString(sum[:]), `"action":"run.end"`, `"exit_code":0`} {
		if !strings.Contains(audit, want) {
			t.Errorf("audit lacks %s", want)
		}
	}
	for _, secret := range []string{s.Prompt, fakeCredential} {
		if strings.Contains(audit, secret) || strings.Contains(output(r), secret) {
			t.Errorf("leaked %q", secret)
		}
	}
	if e.m.Current() != r || e.m.ActiveTask() != "" {
		t.Fatal("the finished run must be the last one and the slot free")
	}
}

func TestPromptArgumentMode(t *testing.T) {
	e := newEnv(t, daytime, func(o *Options) { o.PromptInArg = true })
	e.ready(t)
	e.guest.Hold = false
	r, err := e.m.Start(context.Background(), spec(), "x")
	if err != nil {
		t.Fatal(err)
	}
	wait(t, r)
	_, _, stdin, _, _ := e.guest.Snapshot()
	agent := e.guest.Agent()
	want := append(AgentCommand(7), "--", spec().Prompt)
	if !slices.Equal(agent.GetCommand(), want) || agent.GetStdin() || len(stdin) != 0 {
		t.Fatalf("%v stdin=%q", agent, stdin)
	}
}

func TestRefusals(t *testing.T) {
	e := newEnv(t, daytime, nil)
	if _, err := e.m.Start(context.Background(), spec(), "x"); !errors.Is(err, ErrNotReady) {
		t.Fatalf("before the reaper: %v", err)
	}
	e.ready(t)
	e.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "tarea-20260926-090000"}})
	var busy *BusyError
	if _, err := e.m.Start(context.Background(), spec(), "x"); !errors.As(err, &busy) ||
		busy.Task != "tarea-20260926-090000" {
		t.Fatalf("an ax-tarea run exists: %v", err)
	}
	if e.m.ActiveTask() != "" {
		t.Fatal("a refused start kept the slot")
	}
}

func TestSingleRun(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	r, err := e.m.Start(context.Background(), spec(), "x")
	if err != nil {
		t.Fatal(err)
	}
	var busy *BusyError
	if _, err := e.m.Start(context.Background(), spec(), "x"); !errors.As(err, &busy) || busy.Task != r.ID {
		t.Fatalf("second run: %v", err)
	}
	waitState(t, r, StateRunning)
	if err := e.m.Cancel(r.ID, "test"); err != nil {
		t.Fatal(err)
	}
	wait(t, r)
}

func TestBlackoutRefusal(t *testing.T) {
	// 22:00 + 3 min + 30 min + 5 min (the watchdog's lead) reaches 22:38.
	e := newEnv(t, time.Date(2026, 9, 26, 22, 0, 0, 0, time.UTC), nil)
	e.ready(t)
	s := spec()
	s.Timeout = 30 * time.Minute
	if _, err := e.m.Start(context.Background(), s, "x"); !errors.Is(err, ErrBlackout) {
		t.Fatalf("got %v", err)
	}
	// 22:00 + 3 + 5 + 5 = 22:13 fits.
	s.Timeout = 5 * time.Minute
	r, err := e.m.Start(context.Background(), s, "x")
	if err != nil {
		t.Fatal(err)
	}
	_ = e.m.Cancel(r.ID, "test")
	wait(t, r)
	// Just after midnight the window still holds.
	e2 := newEnv(t, time.Date(2026, 9, 27, 0, 30, 0, 0, time.UTC), nil)
	e2.ready(t)
	if _, err := e2.m.Start(context.Background(), s, "x"); !errors.Is(err, ErrBlackout) {
		t.Fatalf("00:30: %v", err)
	}
}

func TestCancelRunning(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	r, _ := e.m.Start(context.Background(), spec(), "x")
	waitState(t, r, StateRunning)
	if err := e.m.Cancel(r.ID, "test"); err != nil {
		t.Fatal(err)
	}
	v := wait(t, r)
	_, _, _, _, signals := e.guest.Snapshot()
	if v.Outcome != OutcomeCancelled || *v.ExitCode != 143 || signals[0] != ateenvv1alpha.Signal_SIGNAL_TERM {
		t.Fatalf("%+v %v", v, signals)
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
	if err := e.m.Cancel("web-x", "test"); !errors.Is(err, ErrNotFound) {
		t.Fatal(err)
	}
}

func TestCancelEscalatesToKill(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.IgnoreTerm = true
	r, _ := e.m.Start(context.Background(), spec(), "x")
	waitState(t, r, StateRunning)
	_ = e.m.Cancel(r.ID, "test")
	v := wait(t, r)
	_, _, _, _, signals := e.guest.Snapshot()
	if v.Outcome != OutcomeCancelled || *v.ExitCode != 137 ||
		!slices.Equal(signals, []ateenvv1alpha.Signal{ateenvv1alpha.Signal_SIGNAL_TERM, ateenvv1alpha.Signal_SIGNAL_KILL}) {
		t.Fatalf("%+v %v", v, signals)
	}
}

func TestCancelWhileWaiting(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.ax.NeverReady = true
	r, _ := e.m.Start(context.Background(), spec(), "x")
	waitState(t, r, StateWaiting)
	_ = e.m.Cancel(r.ID, "test")
	v := wait(t, r)
	started, _, _, _, _ := e.guest.Snapshot()
	if v.Outcome != OutcomeCancelled || len(started) != 0 {
		t.Fatalf("%+v, %d processes", v, len(started))
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
}

func TestReadyTimeout(t *testing.T) {
	e := newEnv(t, daytime, func(o *Options) { o.ReadyTimeout = 150 * time.Millisecond })
	e.ready(t)
	e.ax.NeverReady = true
	r, _ := e.m.Start(context.Background(), spec(), "x")
	v := wait(t, r)
	if v.Outcome != OutcomeFailed || !strings.Contains(v.Message, "reparación") {
		t.Fatalf("%+v", v)
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
}

func TestCleanupFailureBannerAndRetry(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	e.ax.SetStuck(true)
	r, _ := e.m.Start(context.Background(), spec(), "x")
	waitState(t, r, StateCleanupFailed)
	if e.m.Status().Cleanup != r.ID {
		t.Fatalf("no banner: %+v", e.m.Status())
	}
	var busy *BusyError
	if _, err := e.m.Start(context.Background(), spec(), "x"); !errors.As(err, &busy) {
		t.Fatalf("a new run while the old one persists: %v", err)
	}
	e.ax.SetStuck(false)
	v := wait(t, r)
	if v.State != StateFinished || e.m.Status().Cleanup != "" {
		t.Fatalf("%+v", v)
	}
}

func TestNeverClobbers(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	// Same second as the run the clock will name.
	e.ax.AddWorkspace(&v1alpha1.Workspace{Metadata: &v1alpha1.ObjectMeta{Name: "ws-web-20260926-100000"}})
	r, err := e.m.Start(context.Background(), spec(), "x")
	if err != nil {
		t.Fatal(err)
	}
	v := wait(t, r)
	tasks, workspaces, _ := e.ax.Snapshot()
	if v.Outcome != OutcomeFailed || len(tasks) != 0 || len(workspaces) != 0 {
		t.Fatalf("%+v", v)
	}
	if _, hasWS := e.ax.Has("", "ws-web-20260926-100000"); !hasWS {
		t.Fatal("a workspace the run did not create was deleted")
	}
}

func TestReaper(t *testing.T) {
	e := newEnv(t, daytime, nil)
	for _, n := range []string{"web-20260925-100000", "tarea-x", "otra"} {
		e.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: n}})
	}
	for _, n := range []string{"ws-web-20260925-100000", "ws-web-huerfano", "ws-tarea-x"} {
		e.ax.AddWorkspace(&v1alpha1.Workspace{Metadata: &v1alpha1.ObjectMeta{Name: n}})
	}
	e.ready(t)
	for _, gone := range []string{"web-20260925-100000"} {
		if hasTask, _ := e.ax.Has(gone, ""); hasTask {
			t.Errorf("%s survived", gone)
		}
	}
	for _, gone := range []string{"ws-web-20260925-100000", "ws-web-huerfano"} {
		if _, hasWS := e.ax.Has("", gone); hasWS {
			t.Errorf("%s survived", gone)
		}
	}
	if hasTask, hasWS := e.ax.Has("tarea-x", "ws-tarea-x"); !hasTask || !hasWS {
		t.Error("the reaper touched ax-tarea's objects")
	}
	if hasTask, _ := e.ax.Has("otra", ""); !hasTask {
		t.Error("the reaper touched another task")
	}
}

func TestReaperRetriesUntilClean(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "web-old"}})
	e.ax.SetStuck(true)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { e.m.Reap(ctx); close(done) }()
	deadline := time.Now().Add(5 * time.Second)
	for e.m.Status().ReapNote == "" {
		if time.Now().After(deadline) {
			t.Fatal("no note")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if e.m.Status().Ready {
		t.Fatal("ready while web-old persists")
	}
	e.ax.SetStuck(false)
	<-done
	if !e.m.Status().Ready {
		t.Fatal("not ready")
	}
}

func TestWatchdogCancelsBeforeBlackout(t *testing.T) {
	// 22:26: the 5 minute lead reaches 22:31.
	e := newEnv(t, time.Date(2026, 9, 26, 22, 26, 0, 0, time.UTC), nil)
	e.ready(t)
	e.ax.NeverReady = true
	// Start is refused this close to the window, so the run begins through
	// the lifecycle directly, as if started earlier.
	r := &Run{ID: "web-x", Workspace: "ws-web-x", Output: NewBuffer(1 << 10), done: make(chan struct{}),
		spec: spec(), state: StatePreparing}
	e.m.mu.Lock()
	e.m.active = r
	e.m.mu.Unlock()
	e.m.wg.Add(1)
	go e.m.lifecycle(r)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go e.m.Watchdog(ctx)
	v := wait(t, r)
	if v.Outcome != OutcomeCancelled || !strings.Contains(e.audit.String(), "Observatorio") {
		t.Fatalf("%+v", v)
	}
}

func TestShutdownCancelsAndCleans(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	r, _ := e.m.Start(context.Background(), spec(), "x")
	waitState(t, r, StateRunning)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	e.m.Shutdown(ctx)
	v := r.View()
	if v.State != StateFinished || v.Outcome != OutcomeCancelled {
		t.Fatalf("%+v", v)
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
	if _, err := e.m.Start(context.Background(), spec(), "x"); !errors.Is(err, ErrShuttingDown) {
		t.Fatal(err)
	}
}

func TestTokenFailureStopsBeforeTheAgent(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.m.dep.ReadToken = func() (string, error) { return "", errors.New("gone") }
	e.ready(t)
	r, _ := e.m.Start(context.Background(), spec(), "x")
	v := wait(t, r)
	if v.Outcome != OutcomeFailed || e.guest.Agent() != nil {
		t.Fatalf("%+v", v)
	}
}

func TestFailedCloneStopsBeforeTheCredential(t *testing.T) {
	e := newEnv(t, daytime, nil)
	reads := 0
	e.m.dep.ReadToken = func() (string, error) { reads++; return fakeCredential, nil }
	e.ready(t)
	e.guest.GitExit = 128
	e.guest.GitOutput = []byte("fatal: not a git repository\n")
	r, _ := e.m.Start(context.Background(), spec(), "x")
	v := wait(t, r)
	if v.Outcome != OutcomeFailed || !strings.Contains(v.Message, "no se clonó") ||
		!strings.Contains(v.Message, "not a git repository") || e.guest.Agent() != nil || reads != 0 {
		t.Fatalf("%+v reads=%d", v, reads)
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
}

// The tail margin is the watchdog's lead when that is longer than the
// cleanup margin: a run the check accepts is never cut by the watchdog.
func TestStartLeavesRoomForTheWatchdog(t *testing.T) {
	s := spec()
	s.Timeout = 45 * time.Minute
	// 21:39 + 3 + 45 = 22:27: the cleanup margin (2 min) would fit, but the
	// watchdog cancels from 22:25.
	e := newEnv(t, time.Date(2026, 9, 26, 21, 39, 0, 0, time.UTC), nil)
	e.ready(t)
	if _, err := e.m.Start(context.Background(), s, "x"); !errors.Is(err, ErrBlackout) {
		t.Fatalf("21:39 with 45 min: %v", err)
	}
	// 21:36 + 3 + 45 + 5 = 22:29 fits.
	e2 := newEnv(t, time.Date(2026, 9, 26, 21, 36, 0, 0, time.UTC), nil)
	e2.ready(t)
	r, err := e2.m.Start(context.Background(), s, "x")
	if err != nil {
		t.Fatalf("21:36 with 45 min: %v", err)
	}
	_ = e2.m.Cancel(r.ID, "test")
	wait(t, r)
}

// cutter resets every output stream after a few milliseconds, as
// atenet-router's Envoy does at its route timeout (10 s by default).
type cutter struct {
	ProcessClient
	after time.Duration
	cuts  *atomic.Int32
}

func (c cutter) StreamProcessOutput(ctx context.Context, in *ateenvv1alpha.StreamProcessOutputRequest,
	opts ...grpc.CallOption) (grpc.ServerStreamingClient[ateenvv1alpha.ProcessOutput], error) {
	sctx, cancel := context.WithCancel(ctx)
	st, err := c.ProcessClient.StreamProcessOutput(sctx, in, opts...)
	if err != nil {
		cancel()
		return nil, err
	}
	time.AfterFunc(c.after, func() {
		c.cuts.Add(1)
		cancel()
	})
	return st, nil
}

func (e *env) cutStreams(after time.Duration) *atomic.Int32 {
	var cuts atomic.Int32
	dial := e.m.dep.DialGuest
	e.m.dep.DialGuest = func(atespace, actor string) (ProcessClient, error) {
		p, err := dial(atespace, actor)
		if err != nil {
			return nil, err
		}
		return cutter{ProcessClient: p, after: after, cuts: &cuts}, nil
	}
	return &cuts
}

func TestStreamCutsFollowARunningAgent(t *testing.T) {
	e := newEnv(t, daytime, nil)
	cuts := e.cutStreams(20 * time.Millisecond)
	e.ready(t)
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{
		{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(
			`{"type":"assistant","message":{"content":[{"type":"text","text":"uno"}]}}` + "\n")}},
		{Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: []byte("aviso\n")}},
	}
	r, err := e.m.Start(context.Background(), spec(), "x")
	if err != nil {
		t.Fatal(err)
	}
	waitState(t, r, StateRunning)
	// Many more cuts than the failures the follower tolerates, while the
	// agent runs and says nothing.
	deadline := time.Now().Add(5 * time.Second)
	for cuts.Load() < 15 {
		if time.Now().After(deadline) {
			t.Fatalf("only %d cuts", cuts.Load())
		}
		time.Sleep(5 * time.Millisecond)
	}
	_, _, _, _, signals := e.guest.Snapshot()
	if v := r.View(); v.State != StateRunning || len(signals) != 0 {
		t.Fatalf("a silent running agent was stopped: %+v %v", v, signals)
	}
	e.guest.Exit(0)
	v := wait(t, r)
	if v.Outcome != OutcomeExited || v.ExitCode == nil || *v.ExitCode != 0 {
		t.Fatalf("%+v", v)
	}
	// Every reconnect resumes from the offsets already read.
	if out := output(r); strings.Count(out, "uno") != 1 || strings.Count(out, "aviso") != 1 {
		t.Fatalf("output lost or repeated: %q", out)
	}
}

func TestUnreachableGuestEndsTheRun(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.cutStreams(20 * time.Millisecond)
	e.ready(t)
	r, err := e.m.Start(context.Background(), spec(), "x")
	if err != nil {
		t.Fatal(err)
	}
	waitState(t, r, StateRunning)
	e.guest.SetUnreachable(true)
	v := wait(t, r)
	_, _, _, _, signals := e.guest.Snapshot()
	if v.Outcome != OutcomeFailed || !strings.Contains(v.Message, "Se perdió la conexión") ||
		!slices.Contains(signals, ateenvv1alpha.Signal_SIGNAL_KILL) {
		t.Fatalf("%+v %v", v, signals)
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
}

func TestTimeoutOutcome(t *testing.T) {
	s := spec()
	s.Timeout = 1200 * time.Millisecond
	// The guest SIGKILLs the agent at the timeout.
	e := newEnv(t, daytime, nil)
	e.ready(t)
	r, err := e.m.Start(context.Background(), s, "x")
	if err != nil {
		t.Fatal(err)
	}
	v := wait(t, r)
	if v.Outcome != OutcomeTimeout || v.ExitCode == nil || *v.ExitCode != 137 {
		t.Fatalf("%+v", v)
	}
	// A SIGKILL long before the timeout, such as the OOM killer's, is an
	// ordinary exit.
	e2 := newEnv(t, daytime, nil)
	e2.ready(t)
	r2, err := e2.m.Start(context.Background(), s, "x")
	if err != nil {
		t.Fatal(err)
	}
	waitState(t, r2, StateRunning)
	e2.guest.Exit(137)
	v2 := wait(t, r2)
	if v2.Outcome != OutcomeExited || v2.ExitCode == nil || *v2.ExitCode != 137 {
		t.Fatalf("%+v", v2)
	}
}

func TestFailedTaskStopsTheWait(t *testing.T) {
	e := newEnv(t, daytime, func(o *Options) { o.ReadyTimeout = 30 * time.Second })
	e.ready(t)
	e.ax.FailTasks = true
	begin := time.Now()
	r, err := e.m.Start(context.Background(), spec(), "x")
	if err != nil {
		t.Fatal(err)
	}
	v := wait(t, r)
	if v.Outcome != OutcomeFailed || !strings.Contains(v.Message, "fallida") ||
		time.Since(begin) > 5*time.Second || e.guest.Agent() != nil {
		t.Fatalf("%+v after %s", v, time.Since(begin))
	}
	if hasTask, hasWS := e.ax.Has(r.ID, r.Workspace); hasTask || hasWS {
		t.Fatal("not cleaned up")
	}
}
