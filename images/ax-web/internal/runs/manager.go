// Package runs is the office's only way to start work in AX: one run at a
// time of an agent CLI (Claude Code or Codex, see package harness) on a
// public repository, with the same safeguards as the host's ax-tarea, the
// capture of the checkout's changes, and the cleanup. *Manager implements
// harness.Executor.
package runs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/harness"
)

// Fixed names and paths. The manager never runs anything but the agent
// command of package harness and the fixed argv of this package.
const (
	TaskPrefix      = "web-"
	WorkspacePrefix = "ws-"
	HostRunPrefix   = "tarea-" // ax-tarea's tasks
	// TokenEnv and CodexAuthEnv are the credentials' variables, which
	// only ever travel in StartProcess.env.
	TokenEnv      = "CLAUDE_CODE_OAUTH_TOKEN"
	CodexAuthEnv  = "CODEX_AUTH_JSON_B64"
	WorkspacePath = "/workspace"
	RepoDir       = "repo"
	RepoPath      = WorkspacePath + "/" + RepoDir
	// CodexAuthPath is where ax-agent puts Codex's auth.json, which Codex
	// may renew during a run (its refresh token is single-use).
	CodexAuthPath = "/root/.codex/auth.json"
	MaxCodexAuth  = 64 << 10
	listPage      = 100
	maxListPages  = 100
)

// Task resources, recorded on the task only: AX does not enforce them
// (google/ax#369); the worker pod and the timeout are the real caps.
const (
	RequestCPU    = "250m"
	RequestMemory = "512Mi"
	LimitCPU      = "1"
	LimitMemory   = "1Gi"
)

// Run states and outcomes, as in package harness.
const (
	StatePreparing     = harness.StatePreparing
	StateWaiting       = harness.StateWaiting
	StateRunning       = harness.StateRunning
	StateCancelling    = harness.StateCancelling
	StateCleaning      = harness.StateCleaning
	StateCleanupFailed = harness.StateCleanupFailed
	StateFinished      = harness.StateFinished

	OutcomeExited    = harness.OutcomeExited
	OutcomeCancelled = harness.OutcomeCancelled
	OutcomeTimeout   = harness.OutcomeTimeout
	OutcomeFailed    = harness.OutcomeFailed
)

// CloneCheck proves the checkout exists before the agent starts, and
// prints the base commit of the capture: at the pinned AX a failed git
// fetch still reports Ready=True (google/ax f009cc8
// internal/workspace/setup.go:120-122). It runs with no credential.
func CloneCheck() []string {
	return gitArgv("rev-parse", "--verify", "HEAD")
}

// Errors of Launch and Cancel. ErrNotReady, ErrShuttingDown and
// ErrBlackout satisfy errors.Is(err, harness.ErrNotReady); *BusyError
// satisfies errors.Is(err, harness.ErrBusy).
var (
	ErrNotReady     error = &stateError{"el panel aún está retirando ejecuciones anteriores", harness.ErrNotReady}
	ErrShuttingDown error = &stateError{"el panel se está deteniendo", harness.ErrNotReady}
	// ErrBlackout is a "not now", like ErrNotReady: the office keeps the
	// job queued and tries again after the window.
	ErrBlackout error = &stateError{"la ejecución se cruzaría con la ventana del Observatorio", harness.ErrNotReady}
	ErrNotFound       = errors.New("no existe esa ejecución")
)

type stateError struct {
	msg  string
	kind error
}

func (e *stateError) Error() string        { return e.msg }
func (e *stateError) Is(target error) bool { return target == e.kind }

// BusyError names what holds the only worker: the run in progress or a
// task of ax-tarea.
type BusyError struct{ Task string }

func (e *BusyError) Error() string {
	return fmt.Sprintf("el sandbox está ocupado (%s)", e.Task)
}

// Is makes a BusyError match harness.ErrBusy.
func (e *BusyError) Is(target error) bool { return target == harness.ErrBusy }

// ProcessClient is a guest ProcessService bound to one actor.
type ProcessClient interface {
	ateenvv1alpha.ProcessServiceClient
	Close() error
}

// Deps are the manager's collaborators.
type Deps struct {
	AX        v1alpha1.AXClient
	DialGuest func(atespace, actor string) (ProcessClient, error)
	// Credentials returns the env holding the harness credential
	// (CLAUDE_CODE_OAUTH_TOKEN or CODEX_AUTH_JSON_B64). Called once per
	// run, right before the agent starts. Never logged.
	Credentials func(harnessName string) (map[string]string, error)
	// SaveCodexAuth receives /root/.codex/auth.json read back after a
	// Codex run (≤ 64 KiB) and the task's creation time. Optional.
	SaveCodexAuth func(data []byte, taskCreated time.Time) error
	Now           func() time.Time
	Audit         *slog.Logger
}

// Options are the reviewed settings and the timings, which tests shorten.
type Options struct {
	Atespace     string
	AgentImage   string
	Blackout     config.Window
	WatchdogLead time.Duration
	// PromptInArg passes the prompt after "--" instead of on stdin.
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
	// CommandTimeout bounds each command after the agent (the capture's
	// git commands, reading Codex's auth.json); 2 minutes when zero.
	CommandTimeout time.Duration
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
		CommandTimeout: 2 * time.Minute,
	}
}

// Manager owns the single run slot.
type Manager struct {
	opt Options
	dep Deps
	lim limits

	mu       sync.Mutex
	ready    bool
	reapNote string
	stopping bool
	active   *Run
	last     *Run
	wg       sync.WaitGroup
	// quit is closed when Shutdown starts (it aborts a capture); stop
	// once it stopped waiting (it ends cleanup and reap retries).
	quit     chan struct{}
	stop     chan struct{}
	quitOnce sync.Once
	stopOnce sync.Once
}

var _ harness.Executor = (*Manager)(nil)

// NewManager builds a manager. Runs are refused until Reap succeeds.
func NewManager(opt Options, dep Deps) *Manager {
	if dep.Now == nil {
		dep.Now = time.Now
	}
	if dep.Audit == nil {
		dep.Audit = slog.New(slog.DiscardHandler)
	}
	return &Manager{opt: opt, dep: dep, lim: defaultLimits(),
		quit: make(chan struct{}), stop: make(chan struct{})}
}

func (m *Manager) commandTimeout() time.Duration {
	if m.opt.CommandTimeout > 0 {
		return m.opt.CommandTimeout
	}
	return 2 * time.Minute
}

// Ready is true once the start-up reap succeeded, until Shutdown.
func (m *Manager) Ready() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.ready && !m.stopping
}

// ActiveTask is the task of the run in progress, or "".
func (m *Manager) ActiveTask() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.active != nil {
		return m.active.id
	}
	return ""
}

// Status is the manager's state for the office's AX banner.
type Status struct {
	Ready    bool   `json:"ready"`
	ReapNote string `json:"reap_note,omitempty"`
	Active   string `json:"active,omitempty"`
	Cleanup  string `json:"cleanup_failed,omitempty"`
}

// Status snapshots the manager.
func (m *Manager) Status() Status {
	m.mu.Lock()
	s := Status{Ready: m.ready && !m.stopping, ReapNote: m.reapNote}
	r := m.active
	m.mu.Unlock()
	if r != nil {
		s.Active = r.id
		if r.currentState() == StateCleanupFailed {
			s.Cleanup = r.id
		}
	}
	return s
}

// agentCommand is the agent's argv, with the prompt appended in argument
// mode.
func (m *Manager) agentCommand(spec harness.Spec) ([]string, error) {
	argv, err := harness.Command(spec)
	if err != nil {
		return nil, err
	}
	if m.opt.PromptInArg {
		if spec.Harness == harness.Codex {
			argv = argv[:len(argv)-1] // "-": stdin
		}
		argv = append(argv, "--", spec.Prompt)
	}
	return argv, nil
}

// Launch validates spec, reserves the slot and starts the run. It fails
// with a *BusyError (errors.Is harness.ErrBusy) while a run or any
// web-*/tarea-* task exists, and with an error matching
// harness.ErrNotReady before the reap, while stopping, or when AX cannot
// be asked. ctx bounds only that check: the run outlives it.
func (m *Manager) Launch(ctx context.Context, spec harness.Spec, clientIP string,
	hooks harness.Hooks) (harness.Run, error) {
	command, err := m.agentCommand(spec)
	if err != nil {
		return nil, err
	}
	if err := ValidateBranch(spec.Branch); err != nil {
		return nil, err
	}
	if err := checkRepo(spec.Repo); err != nil {
		return nil, err
	}
	if len(spec.ApplyPatch) > harness.MaxPatchBytes {
		return nil, &FieldError{"apply_patch", "el parche del paso anterior supera 4 MiB"}
	}
	m.mu.Lock()
	switch {
	case m.stopping:
		m.mu.Unlock()
		return nil, ErrShuttingDown
	case !m.ready:
		m.mu.Unlock()
		return nil, ErrNotReady
	case m.active != nil:
		name := m.active.id
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
	r := newRun(spec, command, hooks, now, m.dep.Now)
	m.active = r
	// Counted now, so a Shutdown from here on waits for it.
	m.wg.Add(1)
	m.mu.Unlock()

	blocking, err := m.blockingTask(ctx)
	if err != nil || blocking != "" {
		m.mu.Lock()
		m.active = nil
		m.mu.Unlock()
		m.wg.Done()
		if err != nil {
			return nil, fmt.Errorf("no se pudo consultar AX (%s): %w",
				status.Convert(err).Message(), harness.ErrNotReady)
		}
		return nil, &BusyError{Task: blocking}
	}
	sum := sha256.Sum256([]byte(spec.Prompt))
	m.dep.Audit.Info("audit", "action", "run.start", "task", r.id,
		"repo", spec.Repo, "branch", spec.Branch, "harness", spec.Harness,
		"model", spec.Model, "effort", spec.Effort, "mode", spec.Mode,
		"max_turns", spec.MaxTurns, "timeout_minutes", int(spec.Timeout/time.Minute),
		"apply_patch_bytes", len(spec.ApplyPatch), "capture", spec.CaptureChanges,
		"client_ip", clientIP, "prompt_bytes", len(spec.Prompt),
		"prompt_sha256", hex.EncodeToString(sum[:]))
	m.begin(r)
	return r, nil
}

// begin starts a reserved run whose wg count is taken.
func (m *Manager) begin(r *Run) {
	go r.deliver()
	go m.lifecycle(r)
}

// blockingTask finds a run of the manager or of ax-tarea that already
// exists.
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
	r.setState(StatePreparing)
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
	if message != "" {
		r.system("%s", message)
	}

	r.mu.Lock()
	spec := r.spec
	r.spec.Prompt, r.spec.ApplyPatch, r.command = "", nil, nil
	started, proc, touched, final := r.agentStarted, r.proc, r.touched, r.final
	r.mu.Unlock()
	r.setState(StateCleaning)
	var changes *harness.Changes
	if proc != nil {
		if started && outcome != OutcomeFailed {
			changes = m.afterAgent(r, proc, spec)
		}
		_ = proc.Close()
	}
	if touched {
		r.system("Borrando la tarea %s y su workspace…", r.id)
		m.cleanupUntilGone(r)
	}

	if final.Usage.ModelUsed == "" && spec.Harness == harness.Codex {
		final.Usage.ModelUsed = spec.Model
	}
	result := harness.Result{
		Outcome: outcome, Message: message, ExitCode: code, IsError: final.IsError,
		ResultText: final.ResultText, Usage: final.Usage, Changes: changes,
		Windows: final.Windows,
	}
	r.mu.Lock()
	r.result, r.finished = result, m.dep.Now()
	r.mu.Unlock()
	r.system("Ejecución terminada (%s).", outcome)
	r.setState(StateFinished)
	exit := -1
	if code != nil {
		exit = *code
	}
	files := 0
	if changes != nil {
		files = len(changes.Files)
	}
	m.dep.Audit.Info("audit", "action", "run.end", "task", r.id, "outcome", outcome,
		"exit_code", exit, "is_error", final.IsError, "cost_usd", final.Usage.CostUSD,
		"changed_files", files)
	m.mu.Lock()
	m.active, m.last = nil, r
	m.mu.Unlock()
	r.closeEvents()
	close(r.done)
}

func failed(format string, args ...any) (string, string, *int) {
	return OutcomeFailed, fmt.Sprintf(format, args...), nil
}

// execute creates the workspace and task, waits for them, checks the
// clone, applies the previous step's patch, starts the agent and follows
// it to its exit.
func (m *Manager) execute(ctx context.Context, r *Run) (string, string, *int) {
	stopped := func() (string, string, *int) {
		return OutcomeCancelled, "Cancelada antes de arrancar el agente.", nil
	}
	call := func() (context.Context, context.CancelFunc) {
		return context.WithTimeout(ctx, m.opt.CallTimeout)
	}
	r.mu.Lock()
	spec, command := r.spec, r.command
	r.mu.Unlock()
	// Never overwrite something that is not ours.
	cctx, cancel := call()
	_, errT := m.dep.AX.GetTask(cctx, &v1alpha1.GetTaskRequest{Atespace: m.opt.Atespace, Name: r.id})
	_, errW := m.dep.AX.GetWorkspace(cctx, &v1alpha1.GetWorkspaceRequest{Atespace: m.opt.Atespace, Name: r.workspace})
	cancel()
	if ctx.Err() != nil {
		return stopped()
	}
	if status.Code(errT) != codes.NotFound || status.Code(errW) != codes.NotFound {
		return failed("La tarea %s o su workspace ya existen, o AX no responde.", r.id)
	}
	r.mu.Lock()
	r.touched = true
	r.mu.Unlock()

	meta := func(name string) *v1alpha1.ObjectMeta {
		return &v1alpha1.ObjectMeta{Name: name, Atespace: m.opt.Atespace}
	}
	r.system("Creando el workspace %s con %s (rama %s)…", r.workspace, spec.Repo, spec.Branch)
	cctx, cancel = call()
	_, err := m.dep.AX.UpdateWorkspace(cctx, &v1alpha1.UpdateWorkspaceRequest{
		Workspace: &v1alpha1.Workspace{
			ApiVersion: v1alpha1.APIVersion, Kind: v1alpha1.KindWorkspace,
			Metadata: meta(r.workspace),
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
	r.system("Creando la tarea %s…", r.id)
	cctx, cancel = call()
	_, err = m.dep.AX.UpdateTask(cctx, &v1alpha1.UpdateTaskRequest{
		Task: &v1alpha1.Task{
			ApiVersion: v1alpha1.APIVersion, Kind: v1alpha1.KindTask,
			Metadata: meta(r.id),
			Spec: &v1alpha1.TaskSpec{
				Debug: true,
				Image: m.opt.AgentImage,
				Resources: &v1alpha1.ResourceReqs{
					Requests: &v1alpha1.ResourceList{Cpu: RequestCPU, Memory: RequestMemory},
					Limits:   &v1alpha1.ResourceList{Cpu: LimitCPU, Memory: LimitMemory},
				},
				Workspaces: []*v1alpha1.WorkspaceRef{{Name: r.workspace, Path: WorkspacePath}},
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
	r.setState(StateWaiting)
	r.system("Esperando al sandbox y al workspace (hasta %s)…", m.opt.ReadyTimeout)

	actor, err := m.waitReady(ctx, r.id)
	if ctx.Err() != nil {
		return stopped()
	}
	if err != nil {
		return failed("%s", err.Error())
	}
	r.system("Sandbox listo.")

	proc, err := m.dep.DialGuest(m.opt.Atespace, actor)
	if err != nil {
		return failed("No se pudo conectar con el sandbox.")
	}
	r.mu.Lock()
	r.proc = proc
	r.mu.Unlock()
	clone, err := m.run(ctx, proc, CloneCheck(), nil, maxDetail, time.Minute)
	if ctx.Err() != nil {
		return stopped()
	}
	if err != nil || clone.code != 0 {
		detail := strings.TrimSpace(strings.ToValidUTF8(string(clone.detail), "?"))
		return failed("El repositorio no se clonó (¿es público y existe la rama?). %s",
			harness.Clip(detail, 512))
	}
	base := strings.TrimSpace(string(clone.stdout))
	if !objectName.MatchString(base) {
		return failed("La comprobación del clon no devolvió un commit.")
	}
	r.mu.Lock()
	r.base = base
	r.mu.Unlock()
	r.system("Repositorio clonado en %s.", base[:7])

	if len(spec.ApplyPatch) > 0 {
		r.system("Aplicando el parche del paso anterior (%d bytes)…", len(spec.ApplyPatch))
		applied, err := m.run(ctx, proc, ApplyCommand(), spec.ApplyPatch, maxDetail, m.commandTimeout())
		if ctx.Err() != nil {
			return stopped()
		}
		if err != nil || applied.code != 0 {
			msg := "No se pudo aplicar el parche del paso anterior"
			if d := strings.TrimSpace(string(applied.detail)); d != "" {
				return failed("%s: %s", msg, harness.Clip(d, 300))
			}
			return failed("%s.", msg)
		}
		r.system("Parche aplicado.")
	}

	creds, err := m.dep.Credentials(spec.Harness)
	if err != nil || len(creds) == 0 {
		return failed("No se pudo leer la credencial del agente.")
	}
	if ctx.Err() != nil {
		return stopped()
	}
	env := make(map[string]string, len(creds)+1)
	for k, v := range creds {
		env[k] = v
	}
	for k, v := range harness.ExtraEnv(spec) {
		env[k] = v
	}
	// Not under ctx: once asked, the agent may be running, and only a
	// signal stops it.
	sctx, scancel := context.WithTimeout(context.Background(), m.opt.CallTimeout)
	started, err := proc.StartProcess(sctx, &ateenvv1alpha.StartProcessRequest{
		Command: command,
		Cwd:     RepoPath,
		Env:     env,
		Stdin:   !m.opt.PromptInArg,
		Timeout: durationpb.New(spec.Timeout),
	})
	scancel()
	clear(env)
	if err != nil {
		if r.isCancelled() {
			return stopped()
		}
		return failed("No se pudo arrancar el agente: %s", status.Convert(err).Message())
	}
	pid := started.GetProcessId()
	begin := m.dep.Now()
	r.mu.Lock()
	r.processID, r.agentStarted = pid, true
	cancelled := r.cancelled
	if cancelled {
		r.setStateLocked(StateCancelling)
	} else {
		r.setStateLocked(StateRunning)
	}
	r.mu.Unlock()
	r.system("Agente en marcha: %s.", describe(spec))
	switch {
	case cancelled:
		m.stopProcess(r)
	case !m.opt.PromptInArg:
		if err := m.writeStdin(context.Background(), proc, pid, []byte(spec.Prompt)); err != nil &&
			!r.isCancelled() {
			m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
			r.mu.Lock()
			r.exited = true
			r.mu.Unlock()
			return failed("No se pudo enviar la instrucción al agente.")
		}
	}
	return m.followAgent(r, proc, pid, spec, begin)
}

// describe is the agent line of the timeline.
func describe(s harness.Spec) string {
	or := func(v, def string) string {
		if v == "" {
			return def
		}
		return v
	}
	text := fmt.Sprintf("%s, modelo %s, esfuerzo %s, modo %s", s.Harness,
		or(s.Model, "por defecto"), or(s.Effort, "por defecto"), s.Mode)
	limit := fmt.Sprintf("%d min", int(s.Timeout/time.Minute))
	if s.Timeout%time.Minute != 0 {
		limit = s.Timeout.String()
	}
	if s.Harness == harness.Claude {
		return fmt.Sprintf("%s; %d turnos y %s como máximo", text, s.MaxTurns, limit)
	}
	return fmt.Sprintf("%s; %s como máximo", text, limit)
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

// followAgent turns the agent's output into events until it exits.
func (m *Manager) followAgent(r *Run, proc ProcessClient, pid string, spec harness.Spec,
	begin time.Time) (string, string, *int) {
	// A backstop after the guest's own SIGKILL at the timeout.
	ctx, cancel := context.WithTimeout(context.Background(), spec.Timeout+time.Minute)
	defer cancel()
	r.mu.Lock()
	r.streamCancel = cancel
	r.mu.Unlock()
	parser := harness.NewParser(spec.Harness)
	var stderr stderrLines
	code, err := m.follow(ctx, proc, pid, func(out, errOut []byte) bool {
		for _, ev := range parser.Feed(out) {
			r.emit(ev)
		}
		stderr.feed(errOut, r.emit)
		return true
	})
	for _, ev := range parser.Flush() {
		r.emit(ev)
	}
	stderr.flush(r.emit)
	final := parser.Final()
	if final.Usage.DurationMS == 0 {
		final.Usage.DurationMS = m.dep.Now().Sub(begin).Milliseconds()
	}
	r.mu.Lock()
	r.exited, r.final = true, final
	cancelled := r.cancelled
	r.mu.Unlock()
	switch {
	case err == nil:
		return m.finish(cancelled, code, spec.Timeout, begin)
	case errors.Is(err, errGuestLost):
		m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
		return failed("Se perdió la conexión con el sandbox.")
	default:
		m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
		if cancelled {
			return OutcomeCancelled, "El agente no terminó tras la cancelación.", nil
		}
		return OutcomeTimeout, "Se agotó el tiempo máximo.", nil
	}
}

func (m *Manager) finish(cancelled bool, code int, timeout time.Duration, begin time.Time) (string, string, *int) {
	switch {
	case cancelled:
		return OutcomeCancelled, fmt.Sprintf("Cancelada; el agente salió con código %d.", code), &code
	case code == 137 && m.dep.Now().Sub(begin) >= timeout-time.Second:
		return OutcomeTimeout, "Se agotó el tiempo máximo (SIGKILL).", &code
	default:
		return OutcomeExited, fmt.Sprintf("El agente salió con código %d.", code), &code
	}
}

// afterAgent runs once the agent process ended: it hands Codex's possibly
// renewed auth.json to the office first (its refresh token is
// single-use, so this matters most), then captures the changes unless
// the manager is stopping.
func (m *Manager) afterAgent(r *Run, proc ProcessClient, spec harness.Spec) *harness.Changes {
	if spec.Harness == harness.Codex {
		m.readBackCodexAuth(r, proc)
	}
	if !spec.CaptureChanges {
		return nil
	}
	r.mu.Lock()
	base := r.base
	r.mu.Unlock()
	select {
	case <-m.quit:
		r.warn("no se recogen los cambios porque el panel se está deteniendo.")
		return &harness.Changes{BaseSHA: base, Error: "el panel se detuvo antes de recoger los cambios"}
	default:
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		select {
		case <-m.quit:
			cancel()
		case <-ctx.Done():
		}
	}()
	return m.capture(ctx, r, proc)
}

// readBackCodexAuth reads /root/.codex/auth.json from the sandbox and
// gives it to SaveCodexAuth, which validates it. Neither the value nor
// anything derived from it is ever logged or emitted.
func (m *Manager) readBackCodexAuth(r *Run, proc ProcessClient) {
	if m.dep.SaveCodexAuth == nil {
		return
	}
	o, err := m.run(context.Background(), proc, []string{"cat", CodexAuthPath}, nil,
		m.lim.codexAuth, m.commandTimeout())
	defer func() {
		clear(o.stdout)
		clear(o.detail)
	}()
	result := "saved"
	switch {
	case o.cut:
		result = "too_big"
		r.warn("la credencial de Codex del sandbox supera %d KiB; no se guarda.", m.lim.codexAuth>>10)
	case err != nil || o.code != 0 || len(o.stdout) == 0:
		result = "unreadable"
		r.warn("no se pudo leer la credencial de Codex del sandbox; se mantiene la guardada.")
	default:
		// The office gets its own copy; this one is wiped.
		if serr := m.dep.SaveCodexAuth(append([]byte(nil), o.stdout...), r.created); serr != nil {
			result = "rejected"
			r.warn("la Oficina no guardó la credencial de Codex del sandbox: %s",
				harness.Clip(serr.Error(), 200))
		} else {
			r.system("Credencial de Codex del sandbox revisada por la Oficina.")
		}
	}
	m.dep.Audit.Info("audit", "action", "run.codex_auth", "task", r.id, "result", result)
}

// Cancel stops the run: SIGTERM, then SIGKILL after KillGrace. Before the
// agent starts it abandons the preparation. It is idempotent, and a no-op
// for the run that just finished.
func (m *Manager) Cancel(id, reason string) error {
	m.mu.Lock()
	r, last := m.active, m.last
	m.mu.Unlock()
	if r == nil || r.id != id {
		if last != nil && last.id == id {
			return nil
		}
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
		r.setStateLocked(StateCancelling)
	}
	pid, prep := r.processID, r.prepCancel
	r.mu.Unlock()
	m.dep.Audit.Info("audit", "action", "run.cancel", "task", id, "reason", reason)
	r.system("Cancelando: %s", reason)
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
	if proc == nil || pid == "" {
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

// cleanupUntilGone deletes the run's task and workspace and waits until AX
// no longer has them, retrying until it succeeds or the manager stops.
func (m *Manager) cleanupUntilGone(r *Run) {
	for {
		err := m.DeleteAndWait(r.id, r.workspace, m.opt.CleanupTimeout)
		if err == nil {
			r.system("La tarea y su workspace ya no existen.")
			return
		}
		note := "La tarea " + r.id + " o su workspace siguen existiendo; " +
			"se reintenta cada " + m.opt.CleanupRetry.String() + "."
		r.setState(StateCleanupFailed)
		r.system("%s", note)
		m.dep.Audit.Warn("audit", "action", "run.cleanup_failed", "task", r.id)
		select {
		case <-m.stop:
			return
		case <-time.After(m.opt.CleanupRetry):
		}
		r.setState(StateCleaning)
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

// Shutdown refuses new runs, cancels the active one (skipping its
// capture, not Codex's auth read-back) and waits for its cleanup until
// ctx ends.
func (m *Manager) Shutdown(ctx context.Context) {
	m.mu.Lock()
	m.stopping = true
	id := ""
	if m.active != nil {
		id = m.active.id
	}
	m.mu.Unlock()
	m.quitOnce.Do(func() { close(m.quit) })
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
