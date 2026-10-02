package runs

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
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
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/fakeax"
	"apptolast.com/ax-web/internal/guest"
	"apptolast.com/ax-web/internal/harness"
)

// Fake credentials derived at run time, so no literal looks like one.
var (
	fakeCredential = strings.Repeat("c", 40)
	fakeCodexAuth  = `{"auth_mode":"chatgpt","tokens":{"id_token":"` + strings.Repeat("i", 30) +
		`","access_token":"` + strings.Repeat("a", 30) + `","refresh_token":"` + strings.Repeat("r", 30) +
		`","account_id":"acc"},"last_refresh":"2026-10-02T10:00:00Z"}`
	fakeCodexEnv = base64.StdEncoding.EncodeToString([]byte(fakeCodexAuth))
)

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

// recorder collects what a run reports through its hooks.
type recorder struct {
	mu     sync.Mutex
	events []harness.Event
	states []string
	onInit func()
}

func (rec *recorder) hooks() harness.Hooks {
	return harness.Hooks{
		OnState: func(s string) {
			rec.mu.Lock()
			defer rec.mu.Unlock()
			rec.states = append(rec.states, s)
		},
		OnEvent: func(ev harness.Event) {
			rec.mu.Lock()
			rec.events = append(rec.events, ev)
			hook := rec.onInit
			rec.mu.Unlock()
			if ev.Kind == harness.EventInit && hook != nil {
				hook()
			}
		},
	}
}

func (rec *recorder) Events() []harness.Event {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return append([]harness.Event(nil), rec.events...)
}

func (rec *recorder) States() []string {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return append([]string(nil), rec.states...)
}

// texts are the texts of the events of one kind.
func (rec *recorder) texts(kind string) []string {
	var out []string
	for _, ev := range rec.Events() {
		if ev.Kind == kind {
			out = append(out, ev.Text)
		}
	}
	return out
}

// system is every line of the run manager, joined.
func (rec *recorder) system() string {
	return strings.Join(rec.texts(harness.EventSystem), "\n")
}

// dump is everything the hooks received, for leak checks.
func (rec *recorder) dump() string {
	var b strings.Builder
	for _, ev := range rec.Events() {
		b.WriteString(ev.Text + ev.Tool + ev.Input + "\n")
	}
	return b.String()
}

type savedAuth struct {
	mu      sync.Mutex
	data    [][]byte
	created []time.Time
	err     error
}

func (s *savedAuth) save(data []byte, created time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data = append(s.data, data)
	s.created = append(s.created, created)
	return s.err
}

func (s *savedAuth) calls() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.data)
}

type env struct {
	ax    *fakeax.AX
	guest *fakeax.Guest
	m     *Manager
	audit *lockedBuffer
	base  time.Time
	skew  atomic.Int64 // added to the clock
	creds atomic.Int32 // Credentials calls
	saved *savedAuth
}

// newEnv wires a manager to a fake AX and a fake guest. base is the clock's
// UTC start; it advances with real time (plus skew).
func newEnv(t *testing.T, base time.Time, edit func(*Options)) *env {
	t.Helper()
	e := &env{ax: fakeax.NewAX(), guest: fakeax.NewGuest(), audit: &lockedBuffer{}, base: base,
		saved: &savedAuth{}}
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
		CallTimeout: 2 * time.Second, WatchdogTick: 20 * time.Millisecond, CommandTimeout: 5 * time.Second,
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
		Credentials: func(h string) (map[string]string, error) {
			e.creds.Add(1)
			if h == harness.Codex {
				return map[string]string{CodexAuthEnv: fakeCodexEnv}, nil
			}
			return map[string]string{TokenEnv: fakeCredential}, nil
		},
		SaveCodexAuth: e.saved.save,
		Now:           func() time.Time { return base.Add(time.Since(start) + time.Duration(e.skew.Load())) },
		Audit:         slog.New(slog.NewJSONHandler(e.audit, nil)),
	})
	return e
}

var daytime = time.Date(2026, 9, 26, 10, 0, 0, 0, time.UTC)

func (e *env) ready(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	e.m.Reap(ctx)
	if !e.m.Ready() {
		t.Fatal("the reaper did not finish")
	}
}

func claudeSpec() harness.Spec {
	return harness.Spec{ID: "web-j1", Repo: "https://github.com/a/b", Branch: "main",
		Prompt: "arregla los tests", Harness: harness.Claude, Model: "sonnet", Effort: "high",
		Mode: harness.ModeRead, MaxTurns: 7, Timeout: 10 * time.Minute}
}

func codexSpec() harness.Spec {
	return harness.Spec{ID: "web-j2", Repo: "https://github.com/a/b", Branch: "dev",
		Prompt: "## Tu papel\nRevisa.\n## Encargo\nmejora el README", Harness: harness.Codex,
		Model: "gpt-6-sol", Effort: "high", Mode: harness.ModeFull, Timeout: 45 * time.Minute}
}

func (e *env) launch(t *testing.T, s harness.Spec) (*Run, *recorder) {
	t.Helper()
	rec := &recorder{}
	run, err := e.m.Launch(context.Background(), s, "203.0.113.7", rec.hooks())
	if err != nil {
		t.Fatal(err)
	}
	return run.(*Run), rec
}

func wait(t *testing.T, r *Run) harness.Result {
	t.Helper()
	select {
	case <-r.Done():
	case <-time.After(10 * time.Second):
		t.Fatalf("the run did not end: %s", r.currentState())
	}
	if r.currentState() != StateFinished {
		t.Fatalf("done in state %s", r.currentState())
	}
	return r.Result()
}

func waitState(t *testing.T, r *Run, state string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for r.currentState() != state {
		if time.Now().After(deadline) {
			t.Fatalf("state %s never reached: %s", state, r.currentState())
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func stdout(s string) *ateenvv1alpha.ProcessOutput {
	return &ateenvv1alpha.ProcessOutput{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(s)}}
}

func stderrOut(s string) *ateenvv1alpha.ProcessOutput {
	return &ateenvv1alpha.ProcessOutput{Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: []byte(s)}}
}

func kinds(evs []harness.Event, skip string) []string {
	var out []string
	for _, ev := range evs {
		if ev.Kind != skip {
			out = append(out, ev.Kind)
		}
	}
	return out
}

func (e *env) cleaned(t *testing.T, r *Run) {
	t.Helper()
	if hasTask, hasWS := e.ax.Has(r.ID(), WorkspacePrefix+r.ID()); hasTask || hasWS {
		t.Fatal("the task or workspace survived the cleanup")
	}
}

func TestClaudeRunEndToEnd(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.ax.ReadyAfter = 2
	e.guest.Hold = false
	// stream-json events, one split across two chunks.
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{
		stdout(`{"type":"system","subtype":"init","model":"claude-sonnet-5","tools":["Read","Grep"],"permissionMode":"default"}` + "\n" + `{"type":"assistant","message":{"content":[{"type":"text","te`),
		stdout(`xt":"hola"},{"type":"tool_use","name":"Read","input":{"file_path":"x.go"}}]}}` + "\n"),
		stderrOut("aviso\n"),
		stdout(`{"type":"user","message":{"content":[{"type":"tool_result","content":"package x"}]}}` + "\n"),
		stdout(`{"type":"result","subtype":"success","is_error":false,"result":"listo","num_turns":2,"total_cost_usd":0.5,"duration_ms":1234,"usage":{"input_tokens":10,"output_tokens":20,"cache_read_input_tokens":30,"cache_creation_input_tokens":40}}`),
	}
	s := claudeSpec()
	r, rec := e.launch(t, s)
	if r.ID() != "web-j1" || r.workspace != "ws-web-j1" {
		t.Fatalf("names %s %s", r.ID(), r.workspace)
	}
	res := wait(t, r)
	if res.Outcome != OutcomeExited || res.ExitCode == nil || *res.ExitCode != 0 || res.IsError ||
		res.ResultText != "listo" || res.Changes != nil {
		t.Fatalf("%+v", res)
	}
	if u := res.Usage; u != (harness.Usage{ModelUsed: "claude-sonnet-5", CostUSD: 0.5, InTokens: 10, OutTokens: 20,
		CacheRead: 30, CacheMade: 40, Turns: 2, DurationMS: 1234}) {
		t.Fatalf("usage %+v", u)
	}
	if got := rec.States(); !slices.Equal(got, []string{StatePreparing, StateWaiting, StateRunning,
		StateCleaning, StateFinished}) {
		t.Fatalf("states %v", got)
	}
	want := []string{harness.EventInit, harness.EventText, harness.EventTool, harness.EventStderr,
		harness.EventToolResult, harness.EventResult}
	if got := kinds(rec.Events(), harness.EventSystem); !slices.Equal(got, want) {
		t.Fatalf("events %v", got)
	}
	for _, ev := range rec.Events() {
		if ev.Time.IsZero() || ev.Seq != 0 {
			t.Fatalf("event %+v", ev)
		}
		if ev.Kind == harness.EventTool && (ev.Tool != "Read" || ev.Input != "x.go") {
			t.Fatalf("tool %+v", ev)
		}
	}
	sys := rec.system()
	for _, line := range []string{"Creando el workspace ws-web-j1 con https://github.com/a/b (rama main)…",
		"Creando la tarea web-j1…", "Esperando al sandbox", "Sandbox listo.", "Repositorio clonado en 0123456.",
		"Agente en marcha: claude, modelo sonnet, esfuerzo high, modo lectura; 7 turnos y 10 min como máximo.",
		"El agente salió con código 0.", "Borrando la tarea web-j1 y su workspace…",
		"La tarea y su workspace ya no existen.", "Ejecución terminada (exited)."} {
		if !strings.Contains(sys, line) {
			t.Errorf("no %q in\n%s", line, sys)
		}
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
		ts.GetResources().GetLimits().GetCpu() != LimitCPU || ts.GetResources().GetLimits().GetMemory() != LimitMemory ||
		ts.GetResources().GetRequests().GetCpu() != RequestCPU ||
		len(ts.GetWorkspaces()) != 1 || ts.GetWorkspaces()[0].GetName() != "ws-web-j1" ||
		ts.GetWorkspaces()[0].GetPath() != "/workspace" {
		t.Fatalf("task spec %v", ts)
	}
	git := workspaces[0].GetSpec().GetGit()
	if len(git) != 1 || git[0].GetName() != "origin" || git[0].GetRepo() != s.Repo || git[0].GetBranch() != "main" ||
		git[0].GetDir() != "repo" || git[0].GetDepth() != 1 {
		t.Fatalf("workspace %v", git)
	}

	procs := e.guest.Processes()
	if len(procs) != 2 {
		t.Fatalf("%d processes", len(procs))
	}
	// First the clone check, with no credential.
	if c := procs[0].Request; !slices.Equal(c.GetCommand(), []string{"git", "-C", "/workspace/repo", "rev-parse", "--verify", "HEAD"}) ||
		len(c.GetEnv()) != 0 || c.GetStdin() {
		t.Fatalf("check %v", c)
	}
	p := procs[1].Request
	cmd, _ := harness.Command(s)
	if !slices.Equal(p.GetCommand(), cmd) || p.GetCwd() != "/workspace/repo" || !p.GetStdin() ||
		p.GetTimeout().AsDuration() != 10*time.Minute {
		t.Fatalf("process %v", p)
	}
	if len(p.GetEnv()) != 1 || p.GetEnv()[TokenEnv] != fakeCredential {
		t.Fatal("the credential must travel only in StartProcess.env, and read mode needs nothing else")
	}
	if string(procs[1].Stdin) != s.Prompt || !procs[1].Closed {
		t.Fatalf("stdin %q closed=%v", procs[1].Stdin, procs[1].Closed)
	}
	_, actors, _, _, _ := e.guest.Snapshot()
	for _, a := range actors {
		if a != "default/actor-1" {
			t.Fatalf("actor header %q", a)
		}
	}
	e.cleaned(t, r)

	audit := e.audit.String()
	sum := sha256.Sum256([]byte(s.Prompt))
	for _, want := range []string{`"action":"run.start"`, `"client_ip":"203.0.113.7"`, `"harness":"claude"`,
		hex.EncodeToString(sum[:]), `"action":"run.end"`, `"exit_code":0`, `"cost_usd":0.5`} {
		if !strings.Contains(audit, want) {
			t.Errorf("audit lacks %s", want)
		}
	}
	for _, secret := range []string{s.Prompt, fakeCredential} {
		if strings.Contains(audit, secret) || strings.Contains(rec.dump(), secret) {
			t.Errorf("leaked %q", secret)
		}
	}
	if e.m.ActiveTask() != "" || e.m.Status().Active != "" {
		t.Fatal("the slot must be free")
	}
	// The finished run can be cancelled again without error.
	if err := e.m.Cancel(r.ID(), "tarde"); err != nil {
		t.Fatal(err)
	}
}

// The capture's scripted git commands for a change set of every kind.
func scriptCapture(g *fakeax.Guest, archive []byte) {
	add, patch, numstat, names := CaptureCommands(g.HeadSHA)
	g.Script(fakeax.Reply{}, add...)
	g.Script(fakeax.Reply{Stdout: []byte(testPatch)}, patch...)
	g.Script(fakeax.Reply{Stdout: []byte("3\t1\tsrc/a.go\x005\t0\tbin/run.sh\x000\t2\told.txt\x00" +
		"1\t1\t\x00old/name.go\x00new/name.go\x001\t0\tlink\x00-\t-\timg.png\x00")}, numstat...)
	g.Script(fakeax.Reply{Stdout: []byte("M\x00src/a.go\x00A\x00bin/run.sh\x00D\x00old.txt\x00" +
		"R090\x00old/name.go\x00new/name.go\x00A\x00link\x00A\x00img.png\x00")}, names...)
	g.Script(fakeax.Reply{Stdout: []byte(testTree + "\n")}, gitArgv("write-tree")...)
	g.Script(fakeax.Reply{Stdout: archive}, gitArgv("archive")...)
}

const (
	testTree  = "fedcba9876543210fedcba9876543210fedcba98"
	testPatch = "diff --git a/src/a.go b/src/a.go\nindex 1..2 100644\n--- a/src/a.go\n+++ b/src/a.go\n@@ -1 +1 @@\n-a\n+b\n"
	pngBody   = "\x89PNG\r\n\x1a\n\x00\x01"
)

func fullArchive() []byte {
	return fakeax.Tar(
		fakeax.TarFile{Name: "src", Dir: true},
		fakeax.TarFile{Name: "src/a.go", Body: "package a\n"},
		fakeax.TarFile{Name: "bin", Dir: true},
		fakeax.TarFile{Name: "bin/run.sh", Body: "#!/bin/sh\necho hola\n", Mode: 0o775},
		fakeax.TarFile{Name: "new", Dir: true},
		fakeax.TarFile{Name: "new/name.go", Body: "package name\n"},
		fakeax.TarFile{Name: "link", Link: "src/a.go"},
		fakeax.TarFile{Name: "img.png", Body: pngBody},
	)
}

func TestClaudeFullModeWithPatchAndCapture(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{
		stdout(`{"type":"result","subtype":"success","result":"hecho","num_turns":1}` + "\n")}
	patch := []byte(strings.Repeat("+una línea del paso anterior\n", 20000)) // > one input message
	e.guest.Script(fakeax.Reply{WaitStdin: true}, ApplyCommand()...)
	scriptCapture(e.guest, fullArchive())
	s := claudeSpec()
	s.Mode, s.ApplyPatch, s.CaptureChanges = harness.ModeFull, patch, true
	s.DisallowedTools = []string{"WebFetch"}
	r, rec := e.launch(t, s)
	res := wait(t, r)
	if res.Outcome != OutcomeExited || res.ResultText != "hecho" {
		t.Fatalf("%+v", res)
	}

	add, patchCmd, numstat, names := CaptureCommands(e.guest.HeadSHA)
	cmd, _ := harness.Command(s)
	paths := []string{"src/a.go", "bin/run.sh", "new/name.go", "link", "img.png"}
	wantArgv := [][]string{CloneCheck(), ApplyCommand(), cmd, add, patchCmd, numstat, names,
		gitArgv("write-tree"), ArchiveCommand(testTree, paths)}
	procs := e.guest.Processes()
	if len(procs) != len(wantArgv) {
		t.Fatalf("%d processes", len(procs))
	}
	for i, want := range wantArgv {
		p := procs[i].Request
		if !slices.Equal(p.GetCommand(), want) {
			t.Errorf("process %d: %q, want %q", i, p.GetCommand(), want)
		}
		if i != 2 && (len(p.GetEnv()) != 0 || p.GetCwd() != "/workspace") {
			t.Errorf("process %d carries env or runs elsewhere: %v", i, p)
		}
	}
	if a := ArchiveCommand(testTree, paths); a[len(a)-5] != ":(literal)src/a.go" || a[len(a)-6] != "--" {
		t.Fatalf("archive argv %q", a)
	}
	if !bytes.Equal(procs[1].Stdin, patch) || !procs[1].Closed || !procs[1].Request.GetStdin() {
		t.Fatalf("the patch was not fed to git apply: %d bytes, closed=%v", len(procs[1].Stdin), procs[1].Closed)
	}
	if env := procs[2].Request.GetEnv(); len(env) != 2 || env[TokenEnv] != fakeCredential || env["IS_SANDBOX"] != "1" {
		t.Fatal("full mode needs the credential and IS_SANDBOX=1 only")
	}

	ch := res.Changes
	if ch == nil || ch.Error != "" || ch.BaseSHA != e.guest.HeadSHA || string(ch.Patch) != testPatch ||
		ch.PatchTruncated || !ch.ContentsComplete {
		t.Fatalf("changes %+v", ch)
	}
	wantFiles := []harness.ChangedFile{
		{Path: "src/a.go", Status: "M", Additions: 3, Deletions: 1},
		{Path: "bin/run.sh", Status: "A", Additions: 5},
		{Path: "old.txt", Status: "D", Deletions: 2},
		{Path: "new/name.go", OldPath: "old/name.go", Status: "R", Additions: 1, Deletions: 1},
		{Path: "link", Status: "A", Additions: 1},
		{Path: "img.png", Status: "A", Binary: true},
	}
	if !slices.Equal(ch.Files, wantFiles) {
		t.Fatalf("files %+v", ch.Files)
	}
	wantContents := []harness.FileContent{
		{Path: "src/a.go", Mode: "100644", Data: []byte("package a\n")},
		{Path: "bin/run.sh", Mode: "100755", Data: []byte("#!/bin/sh\necho hola\n")},
		{Path: "old.txt", Deleted: true},
		{Path: "old/name.go", Deleted: true},
		{Path: "new/name.go", Mode: "100644", Data: []byte("package name\n")},
		{Path: "link", Mode: "120000", Data: []byte("src/a.go")},
		{Path: "img.png", Mode: "100644", Data: []byte(pngBody)},
	}
	if len(ch.Contents) != len(wantContents) {
		t.Fatalf("contents %+v", ch.Contents)
	}
	for i, w := range wantContents {
		c := ch.Contents[i]
		if c.Path != w.Path || c.Mode != w.Mode || c.Deleted != w.Deleted || !bytes.Equal(c.Data, w.Data) {
			t.Errorf("content %d: %+v, want %+v", i, c, w)
		}
	}
	sys := rec.system()
	for _, line := range []string{"Aplicando el parche del paso anterior (", "Parche aplicado.",
		"Recogiendo los cambios", "Cambios: 6 ficheros (+10 −4)."} {
		if !strings.Contains(sys, line) {
			t.Errorf("no %q in\n%s", line, sys)
		}
	}
	if strings.Contains(e.audit.String(), "una línea") || strings.Contains(rec.dump(), "una línea") {
		t.Fatal("the patch leaked")
	}
	e.cleaned(t, r)
}

func TestApplyPatchFailure(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	e.guest.Script(fakeax.Reply{Exit: 1, WaitStdin: true,
		Stderr: []byte("error: patch failed: src/a.go:3\nerror: src/a.go: patch does not apply\n")}, ApplyCommand()...)
	s := claudeSpec()
	s.ApplyPatch, s.CaptureChanges = []byte(testPatch), true
	r, rec := e.launch(t, s)
	res := wait(t, r)
	if res.Outcome != OutcomeFailed || res.ExitCode != nil || res.Changes != nil ||
		!strings.HasPrefix(res.Message, "No se pudo aplicar el parche del paso anterior: error: patch failed: src/a.go:3") {
		t.Fatalf("%+v", res)
	}
	if e.guest.Agent() != nil || e.creds.Load() != 0 || len(e.guest.Find("git", "-C", RepoPath, "-c")) != 0 {
		t.Fatal("the agent, its credential or the capture ran after a failed patch")
	}
	if !strings.Contains(rec.system(), "No se pudo aplicar el parche") {
		t.Fatal("the failure is not in the timeline")
	}
	e.cleaned(t, r)
}

func TestCodexRunWithAuthReadBack(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{
		stdout(`{"type":"thread.started","thread_id":"t1"}` + "\n" + `{"type":"turn.started"}` + "\n"),
		stdout(`{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"bash -lc ls","status":"in_progress"}}` + "\n"),
		stderrOut("Reading prompt from stdin...\n"),
		stdout(`{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"bash -lc ls","aggregated_output":"README.md\n","exit_code":0,"status":"completed"}}` + "\n"),
		stdout(`{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"## Resumen\nREADME mejorado."}}` + "\n"),
		stdout(`{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":20,"reasoning_output_tokens":5}}` + "\n"),
	}
	auth := strings.Replace(fakeCodexAuth, "2026-10-02T10:00:00Z", "2026-10-02T10:05:00Z", 1)
	e.guest.Script(fakeax.Reply{Stdout: []byte(auth)}, "cat", CodexAuthPath)
	s := codexSpec()
	r, rec := e.launch(t, s)
	res := wait(t, r)
	if res.Outcome != OutcomeExited || res.IsError || res.ResultText != "## Resumen\nREADME mejorado." {
		t.Fatalf("%+v", res)
	}
	if u := res.Usage; u.ModelUsed != "gpt-6-sol" || u.InTokens != 100 || u.CacheRead != 40 || u.OutTokens != 25 ||
		u.Turns != 1 || u.CostUSD != 0 || u.DurationMS < 0 {
		t.Fatalf("usage %+v", u)
	}
	agent := e.guest.Agent()
	cmd, _ := harness.Command(s)
	if !slices.Equal(agent.GetCommand(), cmd) || cmd[len(cmd)-1] != "-" || !agent.GetStdin() {
		t.Fatalf("agent %v", agent)
	}
	if env := agent.GetEnv(); len(env) != 1 || env[CodexAuthEnv] != fakeCodexEnv {
		t.Fatal("Codex needs its credential only")
	}
	cat := e.guest.Find("cat")
	if len(cat) != 1 || !slices.Equal(cat[0].Request.GetCommand(), []string{"cat", "/root/.codex/auth.json"}) ||
		len(cat[0].Request.GetEnv()) != 0 {
		t.Fatalf("read-back %+v", cat)
	}
	if e.saved.calls() != 1 || string(e.saved.data[0]) != auth || !e.saved.created[0].Equal(r.created) ||
		r.created.Sub(daytime) > 5*time.Second {
		t.Fatalf("saved %d times, created %v", e.saved.calls(), r.created)
	}
	if got := kinds(rec.Events(), harness.EventSystem); !slices.Equal(got, []string{harness.EventInit,
		harness.EventTool, harness.EventStderr, harness.EventToolResult, harness.EventText, harness.EventUsage}) {
		t.Fatalf("events %v", got)
	}
	if !strings.Contains(rec.system(), "Credencial de Codex del sandbox revisada por la Oficina.") ||
		!strings.Contains(rec.system(), "Agente en marcha: codex, modelo gpt-6-sol, esfuerzo high, modo completo; 45 min como máximo.") {
		t.Fatalf("timeline\n%s", rec.system())
	}
	audit := e.audit.String()
	if !strings.Contains(audit, `"action":"run.codex_auth"`) || !strings.Contains(audit, `"result":"saved"`) {
		t.Fatalf("audit %s", audit)
	}
	for _, secret := range []string{fakeCodexEnv, strings.Repeat("r", 30), strings.Repeat("a", 30), "Revisa."} {
		if strings.Contains(audit, secret) || strings.Contains(rec.dump(), secret) {
			t.Errorf("leaked %q", secret)
		}
	}
	e.cleaned(t, r)
}

func TestCodexAuthReadBackProblems(t *testing.T) {
	cases := []struct {
		name  string
		setup func(e *env)
		saves int
		note  string
		audit string
	}{
		{"too big", func(e *env) {
			e.m.lim.codexAuth = 100
			e.guest.Script(fakeax.Reply{Stdout: []byte(strings.Repeat("x", 101))}, "cat")
		}, 0, "la credencial de Codex del sandbox supera 0 KiB; no se guarda.", "too_big"},
		{"unreadable", func(e *env) {
			e.guest.Script(fakeax.Reply{Exit: 1, Stderr: []byte("cat: No such file or directory\n")}, "cat")
		}, 0, "no se pudo leer la credencial de Codex", "unreadable"},
		{"rejected", func(e *env) {
			e.saved.err = errors.New("el refresh_token no cambió")
			e.guest.Script(fakeax.Reply{Stdout: []byte(fakeCodexAuth)}, "cat")
		}, 1, "la Oficina no guardó la credencial de Codex del sandbox: el refresh_token no cambió", "rejected"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEnv(t, daytime, nil)
			e.ready(t)
			e.guest.Hold = false
			c.setup(e)
			r, rec := e.launch(t, codexSpec())
			res := wait(t, r)
			if res.Outcome != OutcomeExited || e.saved.calls() != c.saves {
				t.Fatalf("%+v, %d saves", res, e.saved.calls())
			}
			if !strings.Contains(rec.system(), "Aviso: "+c.note) || !strings.Contains(e.audit.String(), `"result":"`+c.audit+`"`) {
				t.Fatalf("timeline\n%s", rec.system())
			}
			if strings.Contains(rec.dump(), strings.Repeat("r", 30)) {
				t.Fatal("the credential leaked")
			}
		})
	}
	// Claude runs never read it.
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	r, _ := e.launch(t, claudeSpec())
	wait(t, r)
	if len(e.guest.Find("cat")) != 0 || e.saved.calls() != 0 {
		t.Fatal("a Claude run read Codex's credential")
	}
}

// captureRun runs a Claude job that captures its changes, after setup.
func captureRun(t *testing.T, setup func(e *env)) (*env, harness.Result, *recorder) {
	t.Helper()
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	setup(e)
	s := claudeSpec()
	s.CaptureChanges = true
	r, rec := e.launch(t, s)
	res := wait(t, r)
	if res.Outcome != OutcomeExited || res.Changes == nil {
		t.Fatalf("%+v", res)
	}
	e.cleaned(t, r)
	return e, res, rec
}

func TestCaptureTruncatesThePatch(t *testing.T) {
	big := strings.Repeat("+x\n", 5000)
	e, res, rec := captureRun(t, func(e *env) {
		e.m.lim.patch = 4 << 10
		scriptCapture(e.guest, fullArchive())
		_, patch, _, _ := CaptureCommands(e.guest.HeadSHA)
		e.guest.Script(fakeax.Reply{Stdout: []byte(big)}, patch...)
	})
	ch := res.Changes
	if !ch.PatchTruncated || len(ch.Patch) != 4<<10 || string(ch.Patch) != big[:4<<10] || ch.ContentsComplete ||
		ch.Contents != nil || len(ch.Files) != 6 || ch.Error != "" {
		t.Fatalf("changes: truncated=%v %d bytes complete=%v files=%d error=%q", ch.PatchTruncated, len(ch.Patch),
			ch.ContentsComplete, len(ch.Files), ch.Error)
	}
	if len(e.guest.Find(gitArgv("write-tree")...)) != 0 || len(e.guest.Find(gitArgv("archive")...)) != 0 {
		t.Fatal("contents were read for a truncated patch")
	}
	_, patch, _, _ := CaptureCommands(e.guest.HeadSHA)
	if p := e.guest.Find(patch...); len(p) != 1 || !slices.Contains(p[0].Signals, ateenvv1alpha.Signal_SIGNAL_KILL) {
		t.Fatal("the diff was not stopped at the bound")
	}
	if !strings.Contains(rec.system(), "el parche supera 0 MiB y se ha truncado") {
		t.Fatalf("timeline\n%s", rec.system())
	}
}

func TestCaptureContentBounds(t *testing.T) {
	cases := []struct {
		name  string
		setup func(e *env)
	}{
		{"too many files", func(e *env) {
			e.m.lim.contentFiles = 5
			scriptCapture(e.guest, fullArchive())
		}},
		{"too large", func(e *env) {
			e.m.lim.contents = 30
			scriptCapture(e.guest, fullArchive())
		}},
		{"archive over its bound", func(e *env) {
			e.m.lim.contents = 1
			scriptCapture(e.guest, fakeax.Tar(fakeax.TarFile{Name: "src/a.go", Body: strings.Repeat("a", 128<<10)}))
		}},
		{"submodule", func(e *env) {
			archive := fakeax.Tar(
				fakeax.TarFile{Name: "src/a.go", Body: "package a\n"},
				fakeax.TarFile{Name: "bin/run.sh", Body: "x", Mode: 0o775},
				fakeax.TarFile{Name: "new/name.go", Dir: true},
				fakeax.TarFile{Name: "link", Link: "src/a.go"},
				fakeax.TarFile{Name: "img.png", Body: pngBody},
			)
			scriptCapture(e.guest, archive)
		}},
		{"missing path", func(e *env) {
			archive := fakeax.Tar(fakeax.TarFile{Name: "src/a.go", Body: "package a\n"})
			scriptCapture(e.guest, archive)
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, res, _ := captureRun(t, c.setup)
			ch := res.Changes
			if ch.ContentsComplete || ch.Contents != nil || len(ch.Files) != 6 || ch.Error != "" ||
				string(ch.Patch) != testPatch {
				t.Fatalf("%+v", ch)
			}
		})
	}
}

func TestCaptureFailuresKeepTheRun(t *testing.T) {
	add, patch, numstat, _ := CaptureCommands(fakeax.NewGuest().HeadSHA)
	cases := []struct {
		name, err string
		setup     func(e *env)
		patch     bool
	}{
		{"add", "git add falló: fatal: Unable to create '/workspace/repo/.git/index.lock'", func(e *env) {
			e.guest.Script(fakeax.Reply{Exit: 128, Stderr: []byte("fatal: Unable to create '/workspace/repo/.git/index.lock'\n")}, add...)
		}, false},
		{"diff", "git diff falló (código 129)", func(e *env) {
			e.guest.Script(fakeax.Reply{Exit: 129}, patch...)
		}, false},
		{"numstat", "no se pudo listar los cambios (numstat)", func(e *env) {
			scriptCapture(e.guest, fullArchive())
			e.guest.Script(fakeax.Reply{Exit: 1}, numstat...)
		}, true},
		{"list over its bound", "demasiados cambios: la lista supera", func(e *env) {
			scriptCapture(e.guest, fullArchive())
			e.m.lim.list = 16
		}, true},
		{"garbage", "la lista de cambios no se entiende", func(e *env) {
			scriptCapture(e.guest, fullArchive())
			e.guest.Script(fakeax.Reply{Stdout: []byte("nonsense")}, numstat...)
		}, true},
		{"tree", "git write-tree falló", func(e *env) {
			scriptCapture(e.guest, fullArchive())
			e.guest.Script(fakeax.Reply{Stdout: []byte("not a sha\n")}, gitArgv("write-tree")...)
		}, true},
		{"archive", "el archivo tar no se entiende", func(e *env) {
			scriptCapture(e.guest, []byte(strings.Repeat("z", 700)))
		}, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, res, rec := captureRun(t, c.setup)
			ch := res.Changes
			if !strings.HasPrefix(ch.Error, c.err) || ch.ContentsComplete || ch.Contents != nil ||
				(len(ch.Patch) > 0) != c.patch || ch.BaseSHA == "" {
				t.Fatalf("%+v", ch)
			}
			if !strings.Contains(rec.system(), "Aviso: no se pudieron recoger todos los cambios") {
				t.Fatalf("timeline\n%s", rec.system())
			}
		})
	}
}

func TestCaptureWithoutChanges(t *testing.T) {
	// Unscripted git commands succeed with no output.
	e, res, rec := captureRun(t, func(e *env) {})
	ch := res.Changes
	if !ch.ContentsComplete || len(ch.Files) != 0 || len(ch.Patch) != 0 || ch.Error != "" ||
		ch.BaseSHA != e.guest.HeadSHA {
		t.Fatalf("%+v", ch)
	}
	if !strings.Contains(rec.system(), "Sin cambios en el árbol de trabajo.") ||
		len(e.guest.Find(gitArgv("write-tree")...)) != 0 {
		t.Fatalf("timeline\n%s", rec.system())
	}
}

func TestPromptArgumentMode(t *testing.T) {
	for _, s := range []harness.Spec{claudeSpec(), codexSpec()} {
		e := newEnv(t, daytime, func(o *Options) { o.PromptInArg = true })
		e.ready(t)
		e.guest.Hold = false
		r, _ := e.launch(t, s)
		wait(t, r)
		_, _, stdin, _, _ := e.guest.Snapshot()
		agent := e.guest.Agent()
		cmd, _ := harness.Command(s)
		if s.Harness == harness.Codex {
			cmd = cmd[:len(cmd)-1]
		}
		want := append(cmd, "--", s.Prompt)
		if !slices.Equal(agent.GetCommand(), want) || agent.GetStdin() || len(stdin) != 0 {
			t.Fatalf("%v stdin=%q", agent, stdin)
		}
	}
}

func TestNotReady(t *testing.T) {
	e := newEnv(t, daytime, nil)
	if e.m.Ready() {
		t.Fatal("ready before the reaper")
	}
	_, err := e.m.Launch(context.Background(), claudeSpec(), "x", harness.Hooks{})
	if !errors.Is(err, harness.ErrNotReady) || !errors.Is(err, ErrNotReady) || errors.Is(err, harness.ErrBusy) {
		t.Fatalf("before the reaper: %v", err)
	}
	e.ready(t)
	e.ax.SetListError(status.Error(codes.Unavailable, "ax-server no responde"))
	_, err = e.m.Launch(context.Background(), claudeSpec(), "x", harness.Hooks{})
	if !errors.Is(err, harness.ErrNotReady) || !strings.Contains(err.Error(), "ax-server no responde") {
		t.Fatalf("AX unreachable: %v", err)
	}
	if e.m.ActiveTask() != "" {
		t.Fatal("a refused launch kept the slot")
	}
	e.ax.SetListError(nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	e.m.Shutdown(ctx)
	_, err = e.m.Launch(context.Background(), claudeSpec(), "x", harness.Hooks{})
	if !errors.Is(err, harness.ErrNotReady) || !errors.Is(err, ErrShuttingDown) || e.m.Ready() {
		t.Fatalf("after shutdown: %v", err)
	}
}

func TestBusy(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "tarea-20260926-090000"}})
	_, err := e.m.Launch(context.Background(), claudeSpec(), "x", harness.Hooks{})
	var busy *BusyError
	if !errors.Is(err, harness.ErrBusy) || !errors.As(err, &busy) || busy.Task != "tarea-20260926-090000" ||
		errors.Is(err, harness.ErrNotReady) || err.Error() != "el sandbox está ocupado (tarea-20260926-090000)" {
		t.Fatalf("an ax-tarea run exists: %v", err)
	}
	if e.m.ActiveTask() != "" || len(e.guest.Processes()) != 0 {
		t.Fatal("a refused launch kept the slot or started something")
	}
	tasks, _, _ := e.ax.Snapshot()
	if len(tasks) != 0 {
		t.Fatal("a refused launch created a task")
	}
}

func TestSingleRun(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	r, _ := e.launch(t, claudeSpec())
	if e.m.ActiveTask() != r.ID() {
		t.Fatal("no active task")
	}
	s := claudeSpec()
	s.ID = "web-j9"
	_, err := e.m.Launch(context.Background(), s, "x", harness.Hooks{})
	var busy *BusyError
	if !errors.Is(err, harness.ErrBusy) || !errors.As(err, &busy) || busy.Task != r.ID() {
		t.Fatalf("second run: %v", err)
	}
	waitState(t, r, StateRunning)
	if err := e.m.Cancel(r.ID(), "test"); err != nil {
		t.Fatal(err)
	}
	wait(t, r)
	if err := e.m.Cancel("web-x", "test"); !errors.Is(err, ErrNotFound) {
		t.Fatal(err)
	}
}

func TestLaunchRejectsInvalidSpecs(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	for name, edit := range map[string]func(*harness.Spec){
		"id":     func(s *harness.Spec) { s.ID = "tarea-x" },
		"model":  func(s *harness.Spec) { s.Model = "--x" },
		"branch": func(s *harness.Spec) { s.Branch = "--upload-pack=x" },
		"repo":   func(s *harness.Spec) { s.Repo = "https://user@github.com/a/b" },
		"ssh":    func(s *harness.Spec) { s.Repo = "git@github.com:a/b" },
		"patch":  func(s *harness.Spec) { s.ApplyPatch = make([]byte, harness.MaxPatchBytes+1) },
	} {
		s := claudeSpec()
		edit(&s)
		_, err := e.m.Launch(context.Background(), s, "x", harness.Hooks{})
		var se *harness.SpecError
		var fe *FieldError
		if err == nil || errors.Is(err, harness.ErrBusy) || errors.Is(err, harness.ErrNotReady) ||
			!(errors.As(err, &se) || errors.As(err, &fe)) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if e.m.ActiveTask() != "" {
		t.Fatal("an invalid spec kept the slot")
	}
}

func TestBlackoutRefusal(t *testing.T) {
	// 22:00 + 3 min + 30 min + 5 min (the watchdog's lead) reaches 22:38.
	e := newEnv(t, time.Date(2026, 9, 26, 22, 0, 0, 0, time.UTC), nil)
	e.ready(t)
	s := claudeSpec()
	s.Timeout = 30 * time.Minute
	if _, err := e.m.Launch(context.Background(), s, "x", harness.Hooks{}); !errors.Is(err, ErrBlackout) {
		t.Fatalf("got %v", err)
	}
	// 22:00 + 3 + 5 + 5 = 22:13 fits.
	s.Timeout = 5 * time.Minute
	r, _ := e.launch(t, s)
	_ = e.m.Cancel(r.ID(), "test")
	wait(t, r)
	// Just after midnight the window still holds.
	e2 := newEnv(t, time.Date(2026, 9, 27, 0, 30, 0, 0, time.UTC), nil)
	e2.ready(t)
	if _, err := e2.m.Launch(context.Background(), s, "x", harness.Hooks{}); !errors.Is(err, ErrBlackout) {
		t.Fatalf("00:30: %v", err)
	}
}

// With no blackout, a run starts inside the hours of the Observatorio window.
func TestNoBlackout(t *testing.T) {
	e := newEnv(t, time.Date(2026, 9, 26, 23, 0, 0, 0, time.UTC), func(o *Options) {
		o.Blackout = config.Window{}
	})
	e.ready(t)
	s := claudeSpec()
	s.Timeout = 45 * time.Minute
	r, _ := e.launch(t, s)
	_ = e.m.Cancel(r.ID(), "test")
	wait(t, r)
}

func TestCancelAfterTheAgentStarted(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{stdout(`{"type":"thread.started","thread_id":"t"}` + "\n")}
	e.guest.Script(fakeax.Reply{Stdout: []byte(fakeCodexAuth)}, "cat")
	scriptCapture(e.guest, fullArchive())
	s := codexSpec()
	s.CaptureChanges = true
	r, rec := e.launch(t, s)
	waitState(t, r, StateRunning)
	if err := e.m.Cancel(r.ID(), "cancelada por 203.0.113.7"); err != nil {
		t.Fatal(err)
	}
	if err := e.m.Cancel(r.ID(), "otra vez"); err != nil {
		t.Fatal("cancel is idempotent")
	}
	res := wait(t, r)
	_, _, _, _, signals := e.guest.Snapshot()
	if res.Outcome != OutcomeCancelled || res.ExitCode == nil || *res.ExitCode != 143 ||
		signals[0] != ateenvv1alpha.Signal_SIGNAL_TERM {
		t.Fatalf("%+v %v", res, signals)
	}
	// After the agent: the auth read-back, then the capture.
	if e.saved.calls() != 1 || res.Changes == nil || !res.Changes.ContentsComplete || len(res.Changes.Files) != 6 {
		t.Fatalf("after a cancel: %d saves, changes %+v", e.saved.calls(), res.Changes)
	}
	if got := rec.States(); !slices.Contains(got, StateCancelling) {
		t.Fatalf("states %v", got)
	}
	if !strings.Contains(rec.system(), "Cancelando: cancelada por 203.0.113.7") ||
		strings.Count(rec.system(), "Cancelando") != 1 ||
		!strings.Contains(e.audit.String(), `"action":"run.cancel"`) {
		t.Fatalf("timeline\n%s", rec.system())
	}
	e.cleaned(t, r)
}

func TestCancelEscalatesToKill(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.IgnoreTerm = true
	r, _ := e.launch(t, claudeSpec())
	waitState(t, r, StateRunning)
	_ = e.m.Cancel(r.ID(), "test")
	res := wait(t, r)
	_, _, _, _, signals := e.guest.Snapshot()
	if res.Outcome != OutcomeCancelled || *res.ExitCode != 137 ||
		!slices.Equal(signals, []ateenvv1alpha.Signal{ateenvv1alpha.Signal_SIGNAL_TERM, ateenvv1alpha.Signal_SIGNAL_KILL}) {
		t.Fatalf("%+v %v", res, signals)
	}
}

func TestCancelBeforeTheAgentStarted(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.ax.NeverReady = true
	s := claudeSpec()
	s.CaptureChanges = true
	r, rec := e.launch(t, s)
	waitState(t, r, StateWaiting)
	_ = e.m.Cancel(r.ID(), "test")
	res := wait(t, r)
	if res.Outcome != OutcomeCancelled || res.Message != "Cancelada antes de arrancar el agente." ||
		len(e.guest.Processes()) != 0 || e.creds.Load() != 0 || res.Changes != nil {
		t.Fatalf("%+v, %d processes", res, len(e.guest.Processes()))
	}
	if got := rec.States(); slices.Contains(got, StateRunning) || slices.Contains(got, StateCancelling) {
		t.Fatalf("states %v", got)
	}
	e.cleaned(t, r)
}

func TestCancelDuringTheCloneCheck(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Script(fakeax.Reply{Hold: true}, CloneCheck()...)
	s := claudeSpec()
	s.CaptureChanges = true
	r, _ := e.launch(t, s)
	deadline := time.Now().Add(5 * time.Second)
	for len(e.guest.Find(CloneCheck()...)) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("no clone check")
		}
		time.Sleep(5 * time.Millisecond)
	}
	_ = e.m.Cancel(r.ID(), "test")
	res := wait(t, r)
	check := e.guest.Find(CloneCheck()...)
	if res.Outcome != OutcomeCancelled || e.guest.Agent() != nil || e.creds.Load() != 0 ||
		!slices.Contains(check[0].Signals, ateenvv1alpha.Signal_SIGNAL_KILL) || len(e.guest.Processes()) != 1 {
		t.Fatalf("%+v %+v", res, check)
	}
	e.cleaned(t, r)
}

// A hook may call back into the manager: hooks run on the run's own
// goroutine, never under the manager's or the run's locks.
func TestHooksMayCancel(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{stdout(`{"type":"system","subtype":"init","model":"m"}` + "\n")}
	rec := &recorder{}
	rec.onInit = func() {
		_ = e.m.Cancel(e.m.ActiveTask(), "desde un hook")
		_ = e.m.Status()
	}
	run, err := e.m.Launch(context.Background(), claudeSpec(), "x", rec.hooks())
	if err != nil {
		t.Fatal(err)
	}
	res := wait(t, run.(*Run))
	if res.Outcome != OutcomeCancelled {
		t.Fatalf("%+v", res)
	}
}

func TestStderrIsBounded(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	var b strings.Builder
	b.WriteString(strings.Repeat("ñ", 2000) + "\n")
	for i := 0; i < 2100; i++ {
		b.WriteString("línea\r\n")
	}
	e.guest.Output = []*ateenvv1alpha.ProcessOutput{stderrOut(b.String()), stderrOut("sin salto final")}
	r, rec := e.launch(t, claudeSpec())
	wait(t, r)
	lines := rec.texts(harness.EventStderr)
	if len(lines) != maxStderrEvents || len(lines[0]) > maxStderrLine || !strings.HasSuffix(lines[0], "…") ||
		lines[1] != "línea" {
		t.Fatalf("%d stderr events, first %d bytes", len(lines), len(lines[0]))
	}
	if strings.Count(rec.system(), "stderr truncado") != 1 {
		t.Fatal("no single truncation notice")
	}
}

func TestReadyTimeout(t *testing.T) {
	e := newEnv(t, daytime, func(o *Options) { o.ReadyTimeout = 150 * time.Millisecond })
	e.ready(t)
	e.ax.NeverReady = true
	r, _ := e.launch(t, claudeSpec())
	res := wait(t, r)
	if res.Outcome != OutcomeFailed || !strings.Contains(res.Message, "reparación") {
		t.Fatalf("%+v", res)
	}
	e.cleaned(t, r)
}

func TestCleanupFailureBannerAndRetry(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Hold = false
	e.ax.SetStuck(true)
	r, rec := e.launch(t, claudeSpec())
	waitState(t, r, StateCleanupFailed)
	if e.m.Status().Cleanup != r.ID() || e.m.ActiveTask() != r.ID() {
		t.Fatalf("no banner: %+v", e.m.Status())
	}
	s := claudeSpec()
	s.ID = "web-j9"
	if _, err := e.m.Launch(context.Background(), s, "x", harness.Hooks{}); !errors.Is(err, harness.ErrBusy) {
		t.Fatalf("a new run while the old one persists: %v", err)
	}
	e.ax.SetStuck(false)
	wait(t, r)
	if e.m.Status().Cleanup != "" || !slices.Contains(rec.States(), StateCleanupFailed) ||
		!strings.Contains(rec.system(), "siguen existiendo; se reintenta") {
		t.Fatalf("%+v", e.m.Status())
	}
	e.cleaned(t, r)
}

func TestNeverClobbers(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.ax.AddWorkspace(&v1alpha1.Workspace{Metadata: &v1alpha1.ObjectMeta{Name: "ws-web-j1"}})
	r, _ := e.launch(t, claudeSpec())
	res := wait(t, r)
	tasks, workspaces, _ := e.ax.Snapshot()
	if res.Outcome != OutcomeFailed || len(tasks) != 0 || len(workspaces) != 0 {
		t.Fatalf("%+v", res)
	}
	if _, hasWS := e.ax.Has("", "ws-web-j1"); !hasWS {
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
	if hasTask, _ := e.ax.Has("web-20260925-100000", ""); hasTask {
		t.Error("web-20260925-100000 survived")
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
	if e.m.Ready() {
		t.Fatal("ready while web-old persists")
	}
	e.ax.SetStuck(false)
	<-done
	if !e.m.Ready() {
		t.Fatal("not ready")
	}
}

func TestWatchdogCancelsBeforeBlackout(t *testing.T) {
	// 22:26: the 5 minute lead reaches 22:31.
	e := newEnv(t, time.Date(2026, 9, 26, 22, 26, 0, 0, time.UTC), nil)
	e.ready(t)
	e.ax.NeverReady = true
	// Launch is refused this close to the window, so the run begins
	// through the lifecycle directly, as if started earlier.
	s := claudeSpec()
	cmd, _ := harness.Command(s)
	rec := &recorder{}
	r := newRun(s, cmd, rec.hooks(), e.m.dep.Now(), e.m.dep.Now)
	e.m.mu.Lock()
	e.m.active = r
	e.m.wg.Add(1)
	e.m.mu.Unlock()
	e.m.begin(r)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go e.m.Watchdog(ctx)
	res := wait(t, r)
	if res.Outcome != OutcomeCancelled || !strings.Contains(e.audit.String(), "Observatorio") {
		t.Fatalf("%+v", res)
	}
}

func TestShutdownCancelsAndCleans(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.Script(fakeax.Reply{Stdout: []byte(fakeCodexAuth)}, "cat")
	s := codexSpec()
	s.CaptureChanges = true
	r, rec := e.launch(t, s)
	waitState(t, r, StateRunning)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	e.m.Shutdown(ctx)
	if r.currentState() != StateFinished {
		t.Fatalf("state %s", r.currentState())
	}
	res := r.Result()
	if res.Outcome != OutcomeCancelled || res.Changes == nil || res.Changes.Error == "" {
		t.Fatalf("%+v", res)
	}
	// The single-use Codex credential is still read back; the capture is
	// skipped.
	if e.saved.calls() != 1 || len(e.guest.Find("git", "-C", RepoPath, "-c")) != 0 ||
		!strings.Contains(rec.system(), "el panel se está deteniendo") {
		t.Fatalf("%d saves\n%s", e.saved.calls(), rec.system())
	}
	e.cleaned(t, r)
	if _, err := e.m.Launch(context.Background(), claudeSpec(), "x", harness.Hooks{}); !errors.Is(err, ErrShuttingDown) {
		t.Fatal(err)
	}
}

func TestCredentialFailureStopsBeforeTheAgent(t *testing.T) {
	for _, creds := range []func(string) (map[string]string, error){
		func(string) (map[string]string, error) { return nil, errors.New("gone " + fakeCredential) },
		func(string) (map[string]string, error) { return map[string]string{}, nil },
	} {
		e := newEnv(t, daytime, nil)
		e.m.dep.Credentials = creds
		e.ready(t)
		r, rec := e.launch(t, claudeSpec())
		res := wait(t, r)
		if res.Outcome != OutcomeFailed || e.guest.Agent() != nil ||
			res.Message != "No se pudo leer la credencial del agente." {
			t.Fatalf("%+v", res)
		}
		if strings.Contains(rec.dump(), fakeCredential) || strings.Contains(e.audit.String(), fakeCredential) {
			t.Fatal("the credential error leaked")
		}
		e.cleaned(t, r)
	}
}

func TestFailedCloneStopsBeforeTheCredential(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.ready(t)
	e.guest.GitExit = 128
	e.guest.GitOutput = []byte("fatal: not a git repository\n")
	r, _ := e.launch(t, claudeSpec())
	res := wait(t, r)
	if res.Outcome != OutcomeFailed || !strings.Contains(res.Message, "no se clonó") ||
		!strings.Contains(res.Message, "not a git repository") || e.guest.Agent() != nil || e.creds.Load() != 0 {
		t.Fatalf("%+v reads=%d", res, e.creds.Load())
	}
	e.cleaned(t, r)
	// A clone check that prints no commit is a failure too.
	e2 := newEnv(t, daytime, nil)
	e2.ready(t)
	e2.guest.HeadSHA = "HEAD"
	r2, _ := e2.launch(t, claudeSpec())
	if res := wait(t, r2); res.Outcome != OutcomeFailed || e2.guest.Agent() != nil {
		t.Fatalf("%+v", res)
	}
}

// The tail margin is the watchdog's lead when that is longer than the
// cleanup margin: a run the check accepts is never cut by the watchdog.
func TestStartLeavesRoomForTheWatchdog(t *testing.T) {
	s := claudeSpec()
	s.Timeout = 45 * time.Minute
	// 21:39 + 3 + 45 = 22:27: the cleanup margin (2 min) would fit, but the
	// watchdog cancels from 22:25.
	e := newEnv(t, time.Date(2026, 9, 26, 21, 39, 0, 0, time.UTC), nil)
	e.ready(t)
	if _, err := e.m.Launch(context.Background(), s, "x", harness.Hooks{}); !errors.Is(err, ErrBlackout) {
		t.Fatalf("21:39 with 45 min: %v", err)
	}
	// 21:36 + 3 + 45 + 5 = 22:29 fits.
	e2 := newEnv(t, time.Date(2026, 9, 26, 21, 36, 0, 0, time.UTC), nil)
	e2.ready(t)
	r, _ := e2.launch(t, s)
	_ = e2.m.Cancel(r.ID(), "test")
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
		stdout(`{"type":"assistant","message":{"content":[{"type":"text","text":"uno"}]}}` + "\n"),
		stderrOut("aviso\n"),
	}
	r, rec := e.launch(t, claudeSpec())
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
	if s := r.currentState(); s != StateRunning || len(signals) != 0 {
		t.Fatalf("a silent running agent was stopped: %s %v", s, signals)
	}
	e.guest.Exit(0)
	res := wait(t, r)
	if res.Outcome != OutcomeExited || res.ExitCode == nil || *res.ExitCode != 0 {
		t.Fatalf("%+v", res)
	}
	// Every reconnect resumes from the offsets already read.
	if got := rec.texts(harness.EventText); !slices.Equal(got, []string{"uno"}) {
		t.Fatalf("output lost or repeated: %q", got)
	}
	if got := rec.texts(harness.EventStderr); !slices.Equal(got, []string{"aviso"}) {
		t.Fatalf("stderr lost or repeated: %q", got)
	}
}

func TestUnreachableGuestEndsTheRun(t *testing.T) {
	e := newEnv(t, daytime, nil)
	e.cutStreams(20 * time.Millisecond)
	e.ready(t)
	s := codexSpec()
	s.CaptureChanges = true
	r, _ := e.launch(t, s)
	waitState(t, r, StateRunning)
	e.guest.SetUnreachable(true)
	res := wait(t, r)
	_, _, _, _, signals := e.guest.Snapshot()
	if res.Outcome != OutcomeFailed || !strings.Contains(res.Message, "Se perdió la conexión") ||
		!slices.Contains(signals, ateenvv1alpha.Signal_SIGNAL_KILL) {
		t.Fatalf("%+v %v", res, signals)
	}
	// Nothing more is asked of a guest that stopped answering.
	if len(e.guest.Find("cat")) != 0 || res.Changes != nil {
		t.Fatal("the read-back or the capture ran against a lost guest")
	}
	e.cleaned(t, r)
}

func TestTimeoutOutcome(t *testing.T) {
	// The guest SIGKILLs the agent at the timeout: exit 137 once the
	// timeout has elapsed.
	e := newEnv(t, daytime, nil)
	e.ready(t)
	s := claudeSpec()
	s.Timeout = time.Minute
	r, _ := e.launch(t, s)
	waitState(t, r, StateRunning)
	e.skew.Store(int64(2 * time.Minute))
	e.guest.Exit(137)
	if res := wait(t, r); res.Outcome != OutcomeTimeout || res.ExitCode == nil || *res.ExitCode != 137 {
		t.Fatalf("%+v", res)
	}
	// A SIGKILL long before the timeout, such as the OOM killer's, is an
	// ordinary exit.
	e2 := newEnv(t, daytime, nil)
	e2.ready(t)
	r2, _ := e2.launch(t, s)
	waitState(t, r2, StateRunning)
	e2.guest.Exit(137)
	if res := wait(t, r2); res.Outcome != OutcomeExited || res.ExitCode == nil || *res.ExitCode != 137 {
		t.Fatalf("%+v", res)
	}
}

func TestFailedTaskStopsTheWait(t *testing.T) {
	e := newEnv(t, daytime, func(o *Options) { o.ReadyTimeout = 30 * time.Second })
	e.ready(t)
	e.ax.FailTasks = true
	begin := time.Now()
	r, _ := e.launch(t, claudeSpec())
	res := wait(t, r)
	if res.Outcome != OutcomeFailed || !strings.Contains(res.Message, "fallida") ||
		time.Since(begin) > 5*time.Second || e.guest.Agent() != nil {
		t.Fatalf("%+v after %s", res, time.Since(begin))
	}
	e.cleaned(t, r)
}
