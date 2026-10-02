package office

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/harness"
)

// fakeExec is a harness.Executor driven by the test.
type fakeExec struct {
	mu        sync.Mutex
	ready     bool
	active    string
	err       error // returned by the next launches while set
	launched  []harness.Spec
	runs      map[string]*fakeRun
	cancelled []string
}

type fakeRun struct {
	id    string
	done  chan struct{}
	res   harness.Result
	hooks harness.Hooks
}

func (r *fakeRun) ID() string             { return r.id }
func (r *fakeRun) Done() <-chan struct{}  { return r.done }
func (r *fakeRun) Result() harness.Result { return r.res }

func newFakeExec() *fakeExec { return &fakeExec{ready: true, runs: map[string]*fakeRun{}} }

func (f *fakeExec) Launch(_ context.Context, spec harness.Spec, _ string, hooks harness.Hooks) (harness.Run, error) {
	f.mu.Lock()
	if f.err != nil {
		err := f.err
		f.mu.Unlock()
		return nil, err
	}
	if f.active != "" {
		f.mu.Unlock()
		return nil, fmt.Errorf("%w (%s)", harness.ErrBusy, f.active)
	}
	r := &fakeRun{id: spec.ID, done: make(chan struct{}), hooks: hooks}
	f.active = spec.ID
	f.launched = append(f.launched, spec)
	f.runs[spec.ID] = r
	f.mu.Unlock()
	hooks.OnState(harness.StatePreparing)
	return r, nil
}

func (f *fakeExec) ActiveTask() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.active
}

func (f *fakeExec) Cancel(id, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cancelled = append(f.cancelled, id)
	return nil
}

func (f *fakeExec) Ready() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.ready
}

func (f *fakeExec) setErr(err error) {
	f.mu.Lock()
	f.err = err
	f.mu.Unlock()
}

func (f *fakeExec) last() harness.Spec {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.launched[len(f.launched)-1]
}

func (f *fakeExec) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.launched)
}

func (f *fakeExec) run(id string) *fakeRun {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.runs[id]
}

// finish ends the run of task id with res.
func (f *fakeExec) finish(id string, res harness.Result) {
	f.mu.Lock()
	r := f.runs[id]
	f.active = ""
	f.mu.Unlock()
	r.hooks.OnState(harness.StateCleaning)
	r.res = res
	close(r.done)
}

// abortAll ends every unfinished run as cancelled.
func (f *fakeExec) abortAll() {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range f.runs {
		select {
		case <-r.done:
		default:
			r.res = harness.Result{Outcome: harness.OutcomeCancelled}
			close(r.done)
		}
	}
	f.active = ""
}

// clock is a settable test clock.
type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *clock) Add(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

func (c *clock) Set(t time.Time) {
	c.mu.Lock()
	c.t = t
	c.mu.Unlock()
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

const testClaudeToken = "fake-claude-token-0001"

func testConfig(t *testing.T) Config {
	t.Helper()
	dir := t.TempDir()
	secrets := filepath.Join(dir, "secrets")
	agent := filepath.Join(dir, "agent")
	for _, d := range []string{secrets, agent} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(agent, "claude-oauth-token"), []byte(testClaudeToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return Config{
		StateDir: filepath.Join(dir, "state"), ClaudeTokenFile: filepath.Join(agent, "claude-oauth-token"),
		OfficeSecretDir: secrets, CodexAuthKey: "codex-auth-json", GitHubTokenKey: "github-token",
		Projects: []config.Project{
			{ID: "dockerswarm-infra", Name: "Infraestructura", Repo: "https://github.com/apptolast/DockerSwarmInfrastrcture",
				Branch: "main", Description: "La infraestructura", Service: "", URL: ""},
			{ID: "web", Name: "Web", Repo: "https://github.com/apptolast/web", Branch: "develop",
				Description: "La web", Service: "web", URL: "https://apptolast.com"},
		},
		Limits:   Limits{MaxTurns: 150, MaxTimeoutMinutes: 90, RepoHosts: []string{"github.com"}},
		MaxQueue: 50, RetentionJobs: 1000, Version: "1.0.0",
	}
}

var t0 = time.Date(2026, 10, 2, 10, 0, 0, 0, time.UTC)

type harnessT struct {
	o     *Office
	exec  *fakeExec
	clock *clock
	cfg   Config
	audit *syncBuffer
}

func newHarness(t *testing.T, mutate ...func(*Config)) *harnessT {
	t.Helper()
	cfg := testConfig(t)
	for _, m := range mutate {
		m(&cfg)
	}
	h := &harnessT{exec: newFakeExec(), clock: &clock{t: t0}, cfg: cfg, audit: &syncBuffer{}}
	o, err := New(cfg, h.exec, nil, h.clock.Now, slog.New(slog.NewJSONHandler(h.audit, nil)))
	if err != nil {
		t.Fatal(err)
	}
	h.o = o
	t.Cleanup(func() {
		h.exec.abortAll()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		o.Close(ctx)
	})
	return h
}

// reopen builds a new office on the same state directory.
func (h *harnessT) reopen(t *testing.T) *Office {
	t.Helper()
	o, err := New(h.cfg, h.exec, nil, h.clock.Now, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	return o
}

func (h *harnessT) job(t *testing.T, req JobRequest) Job {
	t.Helper()
	j, err := h.o.CreateJob(context.Background(), req, "198.51.100.7")
	if err != nil {
		t.Fatalf("CreateJob: %v", err)
	}
	return j
}

// dispatch starts the next job and returns its task id ("" if none).
func (h *harnessT) dispatch(t *testing.T) string {
	t.Helper()
	before := h.exec.count()
	h.o.dispatchOnce(context.Background())
	if h.exec.count() == before {
		return ""
	}
	return h.exec.last().ID
}

func exited(code int, text string) harness.Result {
	c := code
	return harness.Result{Outcome: harness.OutcomeExited, ExitCode: &c, ResultText: text,
		Usage: harness.Usage{CostUSD: 0.5, InTokens: 100, OutTokens: 50, Turns: 3, DurationMS: 1000}}
}

// complete finishes the active run and waits for the office to record it.
func (h *harnessT) complete(t *testing.T, task string, res harness.Result) Job {
	t.Helper()
	h.exec.finish(task, res)
	id := task[len("web-"):]
	deadline := time.Now().Add(5 * time.Second)
	for {
		j, err := h.o.Job(id)
		if err != nil {
			t.Fatal(err)
		}
		h.o.mu.Lock()
		free := h.o.active == nil
		h.o.mu.Unlock()
		if isTerminal(j.Status) && free {
			return j
		}
		if time.Now().After(deadline) {
			t.Fatalf("job %s not finalized: %s", id, j.Status)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// runNext dispatches the next job and finishes it with res.
func (h *harnessT) runNext(t *testing.T, res harness.Result) Job {
	t.Helper()
	task := h.dispatch(t)
	if task == "" {
		t.Fatal("nothing was dispatched")
	}
	return h.complete(t, task, res)
}

func withChanges(res harness.Result, files int) harness.Result {
	c := &harness.Changes{BaseSHA: "0123456789abcdef0123456789abcdef01234567", ContentsComplete: true,
		Patch: []byte("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hola\n+adiós\n")}
	for i := range files {
		p := fmt.Sprintf("f%d.txt", i)
		if i == 0 {
			p = "README.md"
		}
		c.Files = append(c.Files, harness.ChangedFile{Path: p, Status: "M", Additions: 1})
		c.Contents = append(c.Contents, harness.FileContent{Path: p, Mode: "100644", Data: []byte("hola\nadiós\n")})
	}
	res.Changes = c
	return res
}
