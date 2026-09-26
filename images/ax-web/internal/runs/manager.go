// Package runs holds the panel's only way to start work in AX: one run at a
// time of the fixed Claude command on a public repository, with the same
// safeguards as the host's ax-tarea, and its cleanup.
package runs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"

	"apptolast.com/ax-web/internal/config"
)

// Fixed names and the fixed command. The panel never runs anything else.
const (
	TaskPrefix      = "web-"
	WorkspacePrefix = "ws-"
	HostRunPrefix   = "tarea-" // ax-tarea's tasks
	TokenEnv        = "CLAUDE_CODE_OAUTH_TOKEN"
	WorkspacePath   = "/workspace"
	RepoDir         = "repo"
	AgentWrapper    = "ax-agent"
	nameLayout      = "20060102-150405"
	listPage        = 100
	maxListPages    = 100
)

// AgentCommand is the only agent argv the panel starts, before the prompt
// fallback. `claude -p` skips the workspace trust dialog, so a public
// repository's .claude settings, hooks and .mcp.json would otherwise run
// beside the credential: --restricted drops the tools that run commands or
// code and WebFetch, ignores user, project and local settings and confines
// the file tools to the working directory; --strict-mcp-config skips MCP
// servers too (Claude Code 2.1.274 --help). stream-json, which requires
// --verbose with -p, is the only realtime output format.
func AgentCommand(turns int) []string {
	return []string{AgentWrapper, AgentClaude, "-p", "--restricted", "--strict-mcp-config",
		"--output-format", "stream-json", "--verbose", "--max-turns", strconv.Itoa(turns)}
}

// CloneCheck proves the checkout exists before the agent starts: at the
// pinned AX a failed git fetch still reports Ready=True (google/ax f009cc8
// internal/workspace/setup.go:120-122). It runs with no credential.
func CloneCheck() []string {
	return []string{"git", "-C", WorkspacePath + "/" + RepoDir, "rev-parse", "--verify", "HEAD"}
}

// Run states.
const (
	StatePreparing     = "preparing"
	StateWaiting       = "waiting"
	StateRunning       = "running"
	StateCancelling    = "cancelling"
	StateCleaning      = "cleaning"
	StateCleanupFailed = "cleanup_failed"
	StateFinished      = "finished"
)

// Outcomes.
const (
	OutcomeExited    = "exited"
	OutcomeCancelled = "cancelled"
	OutcomeTimeout   = "timeout"
	OutcomeFailed    = "failed"
)

// Errors returned to the web layer.
var (
	ErrNotReady     = errors.New("el panel aún está retirando ejecuciones anteriores")
	ErrBlackout     = errors.New("la ejecución se cruzaría con la ventana del Observatorio")
	ErrNotFound     = errors.New("no existe esa ejecución")
	ErrShuttingDown = errors.New("el panel se está deteniendo")
)

// BusyError names what blocks a new run.
type BusyError struct{ Task string }

func (e *BusyError) Error() string {
	return fmt.Sprintf("ya hay una ejecución en curso (%s)", e.Task)
}

// ProcessClient is a guest ProcessService bound to one actor.
type ProcessClient interface {
	ateenvv1alpha.ProcessServiceClient
	Close() error
}

// Deps are the manager's collaborators.
type Deps struct {
	AX        v1alpha1.AXClient
	DialGuest func(atespace, actor string) (ProcessClient, error)
	ReadToken func() (string, error)
	Now       func() time.Time
	Audit     *slog.Logger
}

// Options are the reviewed settings and the timings, which tests shorten.
type Options struct {
	Atespace       string
	AgentImage     string
	Blackout       config.Window
	WatchdogLead   time.Duration
	PromptInArg    bool
	ReadyTimeout   time.Duration
	PollInterval   time.Duration
	CleanupTimeout time.Duration
	CleanupRetry   time.Duration
	KillGrace      time.Duration
	StartMargin    time.Duration
	CleanupMargin  time.Duration
	CallTimeout    time.Duration
	WatchdogTick   time.Duration
	BufferBytes    int
}

// DefaultOptions are the production timings of docs/AX_WEB.md.
func DefaultOptions() Options {
	return Options{
		ReadyTimeout:   180 * time.Second,
		PollInterval:   2 * time.Second,
		CleanupTimeout: 90 * time.Second,
		CleanupRetry:   30 * time.Second,
		KillGrace:      10 * time.Second,
		StartMargin:    3 * time.Minute,
		CleanupMargin:  2 * time.Minute,
		CallTimeout:    15 * time.Second,
		WatchdogTick:   30 * time.Second,
		BufferBytes:    4 << 20,
	}
}

// Run is one execution. Its identity is its task's name.
type Run struct {
	ID        string
	Workspace string
	Output    *Buffer
	done      chan struct{}

	mu           sync.Mutex
	spec         Spec
	created      time.Time
	finished     time.Time
	state        string
	outcome      string
	message      string
	exitCode     *int
	cancelled    bool
	exited       bool
	touched      bool // the panel asked AX to create its objects
	processID    string
	proc         ProcessClient
	prepCancel   context.CancelFunc
	streamCancel context.CancelFunc
}

// View is what the browser sees of a run: never the prompt.
type View struct {
	ID             string `json:"id"`
	Workspace      string `json:"workspace"`
	Repo           string `json:"repo"`
	Branch         string `json:"branch"`
	Turns          int    `json:"turns"`
	TimeoutMinutes int    `json:"timeout_minutes"`
	CPU            string `json:"cpu"`
	Memory         string `json:"memory"`
	State          string `json:"state"`
	Outcome        string `json:"outcome,omitempty"`
	Message        string `json:"message,omitempty"`
	ExitCode       *int   `json:"exit_code,omitempty"`
	Created        string `json:"created"`
	Finished       string `json:"finished,omitempty"`
}

// View snapshots r.
func (r *Run) View() View {
	r.mu.Lock()
	defer r.mu.Unlock()
	v := View{
		ID: r.ID, Workspace: r.Workspace, Repo: r.spec.Repo, Branch: r.spec.Branch,
		Turns: r.spec.Turns, TimeoutMinutes: int(r.spec.Timeout / time.Minute),
		CPU: r.spec.CPU, Memory: r.spec.Memory, State: r.state, Outcome: r.outcome,
		Message: r.message, ExitCode: r.exitCode,
		Created: r.created.UTC().Format(time.RFC3339),
	}
	if !r.finished.IsZero() {
		v.Finished = r.finished.UTC().Format(time.RFC3339)
	}
	return v
}

// Done is closed once the run is over and cleaned up.
func (r *Run) Done() <-chan struct{} { return r.done }

func (r *Run) setState(state, message string) {
	r.mu.Lock()
	r.state, r.message = state, message
	r.mu.Unlock()
}

func (r *Run) log(format string, args ...any) {
	r.Output.Append(StreamSystem, []byte(fmt.Sprintf(format, args...)+"\n"))
}

// Manager owns the single run slot.
type Manager struct {
	opt Options
	dep Deps

	mu       sync.Mutex
	ready    bool
	reapNote string
	stopping bool
	active   *Run
	last     *Run
	wg       sync.WaitGroup
	stop     chan struct{}
	stopOnce sync.Once
}

// NewManager builds a manager. Runs are refused until Reap succeeds.
func NewManager(opt Options, dep Deps) *Manager {
	if dep.Now == nil {
		dep.Now = time.Now
	}
	if dep.Audit == nil {
		dep.Audit = slog.New(slog.DiscardHandler)
	}
	return &Manager{opt: opt, dep: dep, stop: make(chan struct{})}
}

// Current is the active run, or else the last one, or nil.
func (m *Manager) Current() *Run {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.active != nil {
		return m.active
	}
	return m.last
}

// Get finds the active or last run by id.
func (m *Manager) Get(id string) *Run {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range []*Run{m.active, m.last} {
		if r != nil && r.ID == id {
			return r
		}
	}
	return nil
}

// ActiveTask is the task of the run in progress, or "".
func (m *Manager) ActiveTask() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.active != nil {
		return m.active.ID
	}
	return ""
}

// Status is the panel-wide state for the banner.
type Status struct {
	Ready    bool   `json:"ready"`
	ReapNote string `json:"reap_note,omitempty"`
	Active   string `json:"active,omitempty"`
	Cleanup  string `json:"cleanup_failed,omitempty"`
}

// Status snapshots the manager.
func (m *Manager) Status() Status {
	m.mu.Lock()
	s := Status{Ready: m.ready, ReapNote: m.reapNote}
	r := m.active
	m.mu.Unlock()
	if r != nil {
		v := r.View()
		s.Active = v.ID
		if v.State == StateCleanupFailed {
			s.Cleanup = v.ID
		}
	}
	return s
}

// Start validates the moment, reserves the slot and launches the run.
func (m *Manager) Start(ctx context.Context, spec Spec, clientIP string) (*Run, error) {
	m.mu.Lock()
	switch {
	case m.stopping:
		m.mu.Unlock()
		return nil, ErrShuttingDown
	case !m.ready:
		m.mu.Unlock()
		return nil, ErrNotReady
	case m.active != nil:
		name := m.active.ID
		m.mu.Unlock()
		return nil, &BusyError{Task: name}
	}
	now := m.dep.Now().UTC()
	// The tail covers the cleanup and the watchdog's lead, so the watchdog
	// never cancels a run this check accepted.
	end := now.Add(m.opt.StartMargin + spec.Timeout + max(m.opt.CleanupMargin, m.opt.WatchdogLead))
	if m.opt.Blackout.Overlaps(now, end) {
		m.mu.Unlock()
		return nil, ErrBlackout
	}
	name := TaskPrefix + now.Format(nameLayout)
	if m.last != nil && m.last.ID == name {
		m.mu.Unlock()
		return nil, &BusyError{Task: name}
	}
	r := &Run{
		ID: name, Workspace: WorkspacePrefix + name, Output: NewBuffer(m.opt.BufferBytes),
		done: make(chan struct{}), spec: spec, created: now, state: StatePreparing,
	}
	m.active = r
	m.mu.Unlock()

	blocking, err := m.blockingTask(ctx)
	if err != nil || blocking != "" {
		m.mu.Lock()
		m.active = nil
		m.mu.Unlock()
		if err != nil {
			return nil, fmt.Errorf("no se pudo consultar AX: %w", err)
		}
		return nil, &BusyError{Task: blocking}
	}
	sum := sha256.Sum256([]byte(spec.Prompt))
	m.dep.Audit.Info("audit", "action", "run.start", "task", name,
		"repo", spec.Repo, "branch", spec.Branch, "agent", AgentClaude,
		"turns", spec.Turns, "timeout_minutes", int(spec.Timeout/time.Minute),
		"client_ip", clientIP, "prompt_bytes", len(spec.Prompt),
		"prompt_sha256", hex.EncodeToString(sum[:]))
	m.wg.Add(1)
	go m.lifecycle(r)
	return r, nil
}

// blockingTask finds a run of the panel or of ax-tarea that already exists.
func (m *Manager) blockingTask(ctx context.Context) (string, error) {
	names, err := m.taskNames(ctx)
	if err != nil {
		return "", err
	}
	for _, n := range names {
		if strings.HasPrefix(n, TaskPrefix) || strings.HasPrefix(n, HostRunPrefix) {
			return n, nil
		}
	}
	return "", nil
}

func (m *Manager) taskNames(ctx context.Context) ([]string, error) {
	var names []string
	for page := 0; page < maxListPages; page++ {
		cctx, cancel := context.WithTimeout(ctx, m.opt.CallTimeout)
		resp, err := m.dep.AX.ListTasks(cctx, &v1alpha1.ListTasksRequest{
			Atespace: m.opt.Atespace, Limit: listPage, Offset: int64(page * listPage),
		})
		cancel()
		if err != nil {
			return nil, err
		}
		for _, t := range resp.GetTasks() {
			names = append(names, t.GetMetadata().GetName())
		}
		if len(resp.GetTasks()) < listPage {
			return names, nil
		}
	}
	return nil, errors.New("demasiadas tareas para listarlas")
}

func (m *Manager) lifecycle(r *Run) {
	defer m.wg.Done()
	prepCtx, cancel := context.WithCancel(context.Background())
	r.mu.Lock()
	r.prepCancel = cancel
	cancelled := r.cancelled
	r.mu.Unlock()
	if cancelled {
		cancel()
	}
	outcome, message, code := m.execute(prepCtx, r)
	cancel()

	r.mu.Lock()
	r.outcome, r.exitCode = outcome, code
	r.spec.Prompt = ""
	touched := r.touched
	r.mu.Unlock()
	if message != "" {
		r.log("%s", message)
	}
	r.setState(StateCleaning, message)
	if touched {
		r.log("Borrando la tarea %s y su workspace…", r.ID)
		m.cleanupUntilGone(r, message)
	}
	r.mu.Lock()
	r.state, r.finished = StateFinished, m.dep.Now()
	r.mu.Unlock()
	r.log("Ejecución terminada (%s).", outcome)
	exit := -1
	if code != nil {
		exit = *code
	}
	m.dep.Audit.Info("audit", "action", "run.end", "task", r.ID,
		"outcome", outcome, "exit_code", exit)
	m.mu.Lock()
	m.active, m.last = nil, r
	m.mu.Unlock()
	r.Output.Close()
	close(r.done)
}

func failed(format string, args ...any) (string, string, *int) {
	return OutcomeFailed, fmt.Sprintf(format, args...), nil
}

// execute creates the workspace and task, waits for them, starts the agent
// and follows it to its exit.
func (m *Manager) execute(ctx context.Context, r *Run) (string, string, *int) {
	stopped := func() (string, string, *int) {
		return OutcomeCancelled, "Cancelada antes de arrancar el agente.", nil
	}
	call := func() (context.Context, context.CancelFunc) {
		return context.WithTimeout(ctx, m.opt.CallTimeout)
	}
	spec := r.spec
	// Never overwrite something that is not ours.
	cctx, cancel := call()
	_, errT := m.dep.AX.GetTask(cctx, &v1alpha1.GetTaskRequest{Atespace: m.opt.Atespace, Name: r.ID})
	_, errW := m.dep.AX.GetWorkspace(cctx, &v1alpha1.GetWorkspaceRequest{Atespace: m.opt.Atespace, Name: r.Workspace})
	cancel()
	if ctx.Err() != nil {
		return stopped()
	}
	if status.Code(errT) != codes.NotFound || status.Code(errW) != codes.NotFound {
		return failed("La tarea %s o su workspace ya existen, o AX no responde.", r.ID)
	}
	r.mu.Lock()
	r.touched = true
	r.mu.Unlock()

	meta := func(name string) *v1alpha1.ObjectMeta {
		return &v1alpha1.ObjectMeta{Name: name, Atespace: m.opt.Atespace}
	}
	cctx, cancel = call()
	_, err := m.dep.AX.UpdateWorkspace(cctx, &v1alpha1.UpdateWorkspaceRequest{
		Workspace: &v1alpha1.Workspace{
			ApiVersion: v1alpha1.APIVersion, Kind: v1alpha1.KindWorkspace,
			Metadata: meta(r.Workspace),
			Spec: &v1alpha1.WorkspaceSpec{Git: []*v1alpha1.GitRepo{{
				Name: "origin", Repo: spec.Repo, Branch: spec.Branch, Dir: RepoDir, Depth: 1,
			}}},
		},
	})
	cancel()
	if ctx.Err() != nil {
		return stopped()
	}
	if err != nil {
		return failed("AX rechazó el workspace: %s", status.Convert(err).Message())
	}
	// No env: the credential only ever travels in StartProcess.
	cctx, cancel = call()
	_, err = m.dep.AX.UpdateTask(cctx, &v1alpha1.UpdateTaskRequest{
		Task: &v1alpha1.Task{
			ApiVersion: v1alpha1.APIVersion, Kind: v1alpha1.KindTask,
			Metadata: meta(r.ID),
			Spec: &v1alpha1.TaskSpec{
				Debug: true,
				Image: m.opt.AgentImage,
				Resources: &v1alpha1.ResourceReqs{
					Requests: &v1alpha1.ResourceList{Cpu: RequestCPU, Memory: RequestMemory},
					Limits:   &v1alpha1.ResourceList{Cpu: spec.CPU, Memory: spec.Memory},
				},
				Workspaces: []*v1alpha1.WorkspaceRef{{Name: r.Workspace, Path: WorkspacePath}},
			},
		},
	})
	cancel()
	if ctx.Err() != nil {
		return stopped()
	}
	if err != nil {
		return failed("AX rechazó la tarea: %s", status.Convert(err).Message())
	}
	r.setState(StateWaiting, "")
	r.log("Tarea %s creada; esperando al sandbox y al workspace (hasta %s)…",
		r.ID, m.opt.ReadyTimeout)

	actor, err := m.waitReady(ctx, r.ID)
	if ctx.Err() != nil {
		return stopped()
	}
	if err != nil {
		return failed("%s", err.Error())
	}

	proc, err := m.dep.DialGuest(m.opt.Atespace, actor)
	if err != nil {
		return failed("No se pudo conectar con el sandbox.")
	}
	defer proc.Close()
	if code, detail := m.check(ctx, proc, CloneCheck()); code != 0 {
		if ctx.Err() != nil {
			return stopped()
		}
		return failed("El repositorio no se clonó (¿es público y existe la rama?). %s", detail)
	}
	token, err := m.dep.ReadToken()
	if err != nil {
		return failed("No se pudo leer la credencial del agente.")
	}

	command := AgentCommand(spec.Turns)
	if m.opt.PromptInArg {
		command = append(command, "--", spec.Prompt)
	}
	cctx, cancel = call()
	started, err := proc.StartProcess(cctx, &ateenvv1alpha.StartProcessRequest{
		Command: command,
		Cwd:     WorkspacePath + "/" + RepoDir,
		Env:     map[string]string{TokenEnv: token},
		Stdin:   !m.opt.PromptInArg,
		Timeout: durationpb.New(spec.Timeout),
	})
	cancel()
	if err != nil {
		return failed("No se pudo arrancar el agente: %s", status.Convert(err).Message())
	}
	pid := started.GetProcessId()
	begin := m.dep.Now()
	r.mu.Lock()
	r.processID, r.proc = pid, proc
	r.state = StateRunning
	cancelled := r.cancelled
	r.mu.Unlock()
	r.log("Agente en marcha: claude, %d turnos como máximo, %s como máximo.",
		spec.Turns, spec.Timeout)
	if cancelled {
		m.stopProcess(r)
	}
	if !m.opt.PromptInArg {
		if err := m.sendPrompt(proc, pid, spec.Prompt); err != nil {
			m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
			return failed("No se pudo enviar la instrucción al agente.")
		}
	}
	return m.follow(r, proc, pid, spec.Timeout, begin)
}

// check runs a fixed argv with no credential and returns its exit code and
// the start of its output, or -1 on any failure.
func (m *Manager) check(ctx context.Context, proc ProcessClient, argv []string) (int, string) {
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	p, err := proc.StartProcess(ctx, &ateenvv1alpha.StartProcessRequest{
		Command: argv, Cwd: WorkspacePath, Timeout: durationpb.New(time.Minute),
	})
	if err != nil {
		return -1, ""
	}
	st, err := proc.StreamProcessOutput(ctx, &ateenvv1alpha.StreamProcessOutputRequest{
		ProcessId: p.GetProcessId(), Follow: true,
	})
	if err != nil {
		return -1, ""
	}
	var out []byte
	for {
		msg, err := st.Recv()
		if err != nil {
			return -1, ""
		}
		if exit := msg.GetExit(); exit != nil {
			detail := strings.TrimSpace(strings.ToValidUTF8(string(out), "?"))
			return int(exit.GetExitCode()), detail
		}
		if len(out) < 512 {
			out = append(out, msg.GetStdout()...)
			out = append(out, msg.GetStderr()...)
			if len(out) > 512 {
				out = out[:512]
			}
		}
	}
}

// waitReady polls GetTask until Ready=True (the actor runs and the
// workspace is set up, google/ax internal/controller/reconciler.go).
func (m *Manager) waitReady(ctx context.Context, name string) (string, error) {
	deadline := time.NewTimer(m.opt.ReadyTimeout)
	defer deadline.Stop()
	for {
		cctx, cancel := context.WithTimeout(ctx, m.opt.CallTimeout)
		t, err := m.dep.AX.GetTask(cctx, &v1alpha1.GetTaskRequest{Atespace: m.opt.Atespace, Name: name})
		cancel()
		if err == nil {
			if t.GetStatus().GetPhase() == "Failed" {
				return "", errors.New("AX marcó la tarea como fallida.")
			}
			for _, c := range t.GetStatus().GetConditions() {
				if c.GetType() == "Ready" && c.GetStatus() == "True" && t.GetStatus().GetActor() != "" {
					return t.GetStatus().GetActor(), nil
				}
			}
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-deadline.C:
			return "", errors.New("El sandbox no quedó listo a tiempo: el laboratorio " +
				"necesita reparación (proxy_arp o workers anteriores al arranque del nodo).")
		case <-time.After(m.opt.PollInterval):
		}
	}
}

// sendPrompt writes the prompt to stdin and closes it, so it never appears
// in argv or in GetProcess.command.
func (m *Manager) sendPrompt(proc ProcessClient, pid, prompt string) error {
	ctx, cancel := context.WithTimeout(context.Background(), m.opt.CallTimeout)
	defer cancel()
	st, err := proc.WriteProcessInput(ctx)
	if err != nil {
		return err
	}
	if err := st.Send(&ateenvv1alpha.WriteProcessInputRequest{
		ProcessId: pid, Data: []byte(prompt), Close: true,
	}); err != nil {
		return err
	}
	_, err = st.CloseAndRecv()
	return err
}

// follow streams the output into the run's buffer until the process exits.
func (m *Manager) follow(r *Run, proc ProcessClient, pid string, timeout time.Duration,
	begin time.Time) (string, string, *int) {
	// A backstop after the guest's own SIGKILL at the timeout.
	ctx, cancel := context.WithTimeout(context.Background(), timeout+time.Minute)
	defer cancel()
	r.mu.Lock()
	r.streamCancel = cancel
	r.mu.Unlock()
	var outOff, errOff int64
	var render Renderer
	defer func() { r.Output.Append(StreamStdout, render.Flush()) }()
	failures := 0
	for {
		st, err := proc.StreamProcessOutput(ctx, &ateenvv1alpha.StreamProcessOutputRequest{
			ProcessId: pid, StdoutOffset: outOff, StderrOffset: errOff, Follow: true,
		})
		for err == nil {
			var msg *ateenvv1alpha.ProcessOutput
			msg, err = st.Recv()
			if err != nil {
				break
			}
			if exit := msg.GetExit(); exit != nil {
				return m.finish(r, int(exit.GetExitCode()), timeout, begin)
			}
			if d := msg.GetStdout(); len(d) > 0 {
				outOff += int64(len(d))
				r.Output.Append(StreamStdout, render.Feed(d))
				failures = 0
			}
			if d := msg.GetStderr(); len(d) > 0 {
				errOff += int64(len(d))
				r.Output.Append(StreamStderr, d)
				failures = 0
			}
		}
		if ctx.Err() != nil {
			m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
			r.mu.Lock()
			r.exited = true
			cancelled := r.cancelled
			r.mu.Unlock()
			if cancelled {
				return OutcomeCancelled, "El agente no terminó tras la cancelación.", nil
			}
			return OutcomeTimeout, "Se agotó el tiempo máximo.", nil
		}
		// The stream ended without an exit message. atenet-router's Envoy
		// cuts every stream at its route timeout (10 s unless the router
		// runs with a longer --route-timeout), so a silent agent that is
		// still thinking loses its stream too: ask the guest directly and,
		// while the agent runs, follow it again from the offsets read.
		// Only an unreachable guest counts towards giving up.
		cctx, ccancel := context.WithTimeout(ctx, m.opt.CallTimeout)
		p, gerr := proc.GetProcess(cctx, &ateenvv1alpha.GetProcessRequest{ProcessId: pid})
		ccancel()
		switch {
		case gerr == nil && p.GetState() == ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED:
			return m.finish(r, int(p.GetExitCode()), timeout, begin)
		case gerr == nil:
			failures = 0
		default:
			failures++
		}
		if failures > 5 {
			m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
			r.mu.Lock()
			r.exited = true
			r.mu.Unlock()
			return failed("Se perdió la conexión con el sandbox.")
		}
		select {
		case <-ctx.Done():
		case <-time.After(m.opt.PollInterval):
		}
	}
}

func (m *Manager) finish(r *Run, code int, timeout time.Duration, begin time.Time) (string, string, *int) {
	r.mu.Lock()
	r.exited = true
	cancelled := r.cancelled
	r.mu.Unlock()
	switch {
	case cancelled:
		return OutcomeCancelled, fmt.Sprintf("Cancelada; el agente salió con código %d.", code), &code
	case code == 137 && m.dep.Now().Sub(begin) >= timeout-time.Second:
		return OutcomeTimeout, "Se agotó el tiempo máximo (SIGKILL).", &code
	default:
		return OutcomeExited, fmt.Sprintf("El agente salió con código %d.", code), &code
	}
}

// Cancel stops the run: SIGTERM, then SIGKILL after KillGrace. Before the
// agent starts it just abandons the preparation. It is idempotent.
func (m *Manager) Cancel(id, reason string) error {
	m.mu.Lock()
	r := m.active
	m.mu.Unlock()
	if r == nil || r.ID != id {
		return ErrNotFound
	}
	r.mu.Lock()
	if r.cancelled || r.state == StateCleaning || r.state == StateCleanupFailed ||
		r.state == StateFinished {
		r.mu.Unlock()
		return nil
	}
	r.cancelled = true
	if r.state == StateRunning {
		r.state = StateCancelling
	}
	pid, prep := r.processID, r.prepCancel
	r.mu.Unlock()
	m.dep.Audit.Info("audit", "action", "run.cancel", "task", id, "reason", reason)
	r.log("Cancelando: %s", reason)
	if pid == "" {
		if prep != nil {
			prep()
		}
		return nil
	}
	m.stopProcess(r)
	return nil
}

// stopProcess sends SIGTERM now and SIGKILL after the grace period, and
// abandons the stream if even that is not observed.
func (m *Manager) stopProcess(r *Run) {
	r.mu.Lock()
	proc, pid := r.proc, r.processID
	r.mu.Unlock()
	if proc == nil {
		return
	}
	m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_TERM)
	time.AfterFunc(m.opt.KillGrace, func() {
		r.mu.Lock()
		exited := r.exited
		r.mu.Unlock()
		if exited {
			return
		}
		m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
		time.AfterFunc(2*m.opt.KillGrace, func() {
			r.mu.Lock()
			exited, stop := r.exited, r.streamCancel
			r.mu.Unlock()
			if !exited && stop != nil {
				stop()
			}
		})
	})
}

func (m *Manager) signal(proc ProcessClient, pid string, sig ateenvv1alpha.Signal) {
	ctx, cancel := context.WithTimeout(context.Background(), m.opt.CallTimeout)
	defer cancel()
	_, _ = proc.SignalProcess(ctx, &ateenvv1alpha.SignalProcessRequest{ProcessId: pid, Signal: sig})
}

// cleanupUntilGone deletes the run's task and workspace and waits until AX
// no longer has them, retrying until it succeeds or the panel stops.
func (m *Manager) cleanupUntilGone(r *Run, message string) {
	for {
		err := m.DeleteAndWait(r.ID, r.Workspace, m.opt.CleanupTimeout)
		if err == nil {
			r.log("La tarea y su workspace ya no existen.")
			return
		}
		note := "La tarea " + r.ID + " o su workspace siguen existiendo; " +
			"se reintenta cada " + m.opt.CleanupRetry.String() + "."
		r.setState(StateCleanupFailed, note)
		r.log("%s", note)
		m.dep.Audit.Warn("audit", "action", "run.cleanup_failed", "task", r.ID)
		select {
		case <-m.stop:
			return
		case <-time.After(m.opt.CleanupRetry):
		}
		r.setState(StateCleaning, message)
	}
}

// DeleteAndWait deletes a task and a workspace (either may be "") and polls
// until both are NotFound. Task deletion is two-phase (google/ax
// internal/server/server.go:140-141), so a task still present and not
// Terminating is deleted again.
func (m *Manager) DeleteAndWait(task, workspace string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	for {
		gone := true
		if task != "" {
			cctx, ccancel := context.WithTimeout(ctx, m.opt.CallTimeout)
			t, err := m.dep.AX.GetTask(cctx, &v1alpha1.GetTaskRequest{Atespace: m.opt.Atespace, Name: task})
			if status.Code(err) != codes.NotFound {
				gone = false
				if err == nil && t.GetStatus().GetPhase() != v1alpha1.PhaseTerminating {
					_, _ = m.dep.AX.DeleteTask(cctx, &v1alpha1.DeleteTaskRequest{Atespace: m.opt.Atespace, Name: task})
				}
			}
			ccancel()
		}
		if workspace != "" {
			cctx, ccancel := context.WithTimeout(ctx, m.opt.CallTimeout)
			_, err := m.dep.AX.GetWorkspace(cctx, &v1alpha1.GetWorkspaceRequest{Atespace: m.opt.Atespace, Name: workspace})
			if status.Code(err) != codes.NotFound {
				gone = false
				if err == nil {
					_, _ = m.dep.AX.DeleteWorkspace(cctx, &v1alpha1.DeleteWorkspaceRequest{Atespace: m.opt.Atespace, Name: workspace})
				}
			}
			ccancel()
		}
		if gone {
			return nil
		}
		select {
		case <-ctx.Done():
			return errors.New("siguen existiendo")
		case <-time.After(m.opt.PollInterval):
		}
	}
}

// Reap deletes every web-* task and ws-web-* workspace a previous panel
// process left behind, retrying until it succeeds. Runs are refused until
// then.
func (m *Manager) Reap(ctx context.Context) {
	for {
		err := m.reapOnce(ctx)
		m.mu.Lock()
		if err == nil {
			m.ready, m.reapNote = true, ""
			m.mu.Unlock()
			return
		}
		m.reapNote = "No se pudieron retirar ejecuciones anteriores; se reintenta."
		m.mu.Unlock()
		m.dep.Audit.Warn("audit", "action", "reap.failed")
		select {
		case <-ctx.Done():
			return
		case <-m.stop:
			return
		case <-time.After(m.opt.CleanupRetry):
		}
	}
}

func (m *Manager) reapOnce(ctx context.Context) error {
	names, err := m.taskNames(ctx)
	if err != nil {
		return err
	}
	cctx, cancel := context.WithTimeout(ctx, m.opt.CallTimeout)
	wsResp, err := m.dep.AX.ListWorkspaces(cctx, &v1alpha1.ListWorkspacesRequest{Atespace: m.opt.Atespace})
	cancel()
	if err != nil {
		return err
	}
	workspaces := map[string]bool{}
	for _, w := range wsResp.GetWorkspaces() {
		if n := w.GetMetadata().GetName(); strings.HasPrefix(n, WorkspacePrefix+TaskPrefix) {
			workspaces[n] = true
		}
	}
	var failedNames []string
	for _, n := range names {
		if !strings.HasPrefix(n, TaskPrefix) {
			continue
		}
		ws := WorkspacePrefix + n
		delete(workspaces, ws)
		m.dep.Audit.Info("audit", "action", "reap", "task", n)
		if err := m.DeleteAndWait(n, ws, m.opt.CleanupTimeout); err != nil {
			failedNames = append(failedNames, n)
		}
	}
	for ws := range workspaces {
		m.dep.Audit.Info("audit", "action", "reap", "workspace", ws)
		if err := m.DeleteAndWait("", ws, m.opt.CleanupTimeout); err != nil {
			failedNames = append(failedNames, ws)
		}
	}
	if len(failedNames) > 0 {
		return fmt.Errorf("siguen existiendo: %s", strings.Join(failedNames, ", "))
	}
	return nil
}

// Watchdog cancels the active run shortly before the blackout.
func (m *Manager) Watchdog(ctx context.Context) {
	t := time.NewTicker(m.opt.WatchdogTick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			now := m.dep.Now()
			if !m.opt.Blackout.Overlaps(now, now.Add(m.opt.WatchdogLead)) {
				continue
			}
			if id := m.ActiveTask(); id != "" {
				_ = m.Cancel(id, "se acerca la ventana del Observatorio")
			}
		}
	}
}

// Shutdown refuses new runs, cancels the active one and waits for its
// cleanup until ctx ends.
func (m *Manager) Shutdown(ctx context.Context) {
	m.mu.Lock()
	m.stopping = true
	id := ""
	if m.active != nil {
		id = m.active.ID
	}
	m.mu.Unlock()
	if id != "" {
		_ = m.Cancel(id, "el panel se detiene")
	}
	done := make(chan struct{})
	go func() {
		m.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
	}
	m.stopOnce.Do(func() { close(m.stop) })
}
