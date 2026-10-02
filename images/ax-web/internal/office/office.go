package office

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"slices"
	"sort"
	"sync"
	"time"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/harness"
)

// Timings of the office's loops.
const (
	dispatchTick  = 2 * time.Second
	scheduleTick  = 30 * time.Second
	deltaInterval = time.Second
	stallAfter    = 15 * time.Minute
	busyBackoff   = 10 * time.Second
	probeInterval = 30 * time.Second
	launchTimeout = 3 * time.Minute

	maxRingEvents     = 3000
	snapshotJobs      = 400
	snapshotPipelines = 60
	snapshotDecided   = 100
	keepDecided       = 300
	keepPipelines     = 500
	// maxPendingLessons bounds the lesson proposals waiting for a person;
	// beyond it new lessons are not proposed (a warning says so).
	maxPendingLessons = 300
)

// MaxJobsBytes is the byte budget of the jobs' directories (jobs/<id>/)
// on the 1 GiB state volume: beyond it the oldest finished jobs nothing
// needs are deleted, as beyond RetentionJobs. Only the directories of the
// jobs the office knows count.
const MaxJobsBytes = 768 << 20

// Config is how the office is built (see SPEC 4.1).
type Config struct {
	// StateDir is the persistent volume (/var/lib/ax-web).
	StateDir string
	// ClaudeTokenFile is <token_directory>/<token_key>.
	ClaudeTokenFile string
	// OfficeSecretDir is the optional Secret volume with CodexAuthKey and
	// GitHubTokenKey; it may not exist.
	OfficeSecretDir string
	CodexAuthKey    string
	GitHubTokenKey  string
	// Projects are the reviewed projects (config.json).
	Projects []config.Project
	Limits   Limits
	MaxQueue int
	// RetentionJobs bounds the stored jobs.
	RetentionJobs int
	Version       string
	// GitHubAPI is https://api.github.com (tests override it).
	GitHubAPI string
}

// AXProbe tells the office what the executor does not: a host task
// (tarea-*) holding the worker, the reaper's note and a run whose cleanup
// failed. It may call AX; the office calls it at most every 30 s and
// when a launch is refused as busy. Optional.
type AXProbe interface {
	Probe(ctx context.Context) AXState
}

// preparedRun is the spec of the next job to launch, built once (its
// prompt files written and its "envía el encargo" event logged) and kept
// while the sandbox is busy, until it launches or something it was built
// from changes.
type preparedRun struct {
	jobID string
	spec  harness.Spec
	seq   int64 // the job's last event sequence after the preparation
}

// activeRun is the job in the sandbox.
type activeRun struct {
	jobID     string
	run       harness.Run
	task      string
	since     time.Time
	lastEvent time.Time
	seq       int64
	ring      []harness.Event
	log       *eventLog
}

// Office is the agent office. Every method is safe for concurrent use.
type Office struct {
	cfg   Config
	exec  harness.Executor
	probe AXProbe
	now   func() time.Time
	audit *slog.Logger
	store *store
	creds *creds
	gh    *gitHub
	hub   *hub
	wake  chan struct{}

	mu        sync.Mutex
	st        officeFile
	jobs      map[string]*Job
	order     []string // job ids, oldest first
	warnings  []string
	rev       int64
	launching string
	cancelReq bool
	active    *activeRun
	busyUntil time.Time
	axState   AXState
	probeAt   time.Time
	closed    bool
	prBusy    map[string]bool

	dirtyJobs  map[string]bool
	dirtyPipes map[string]bool
	removed    map[string]bool
	invalid    map[string]bool
	deltaDirty bool

	metrics    Metrics
	metricsRev int64
	metricsDay string

	prepared *preparedRun

	// jobBytes are the sizes of the jobs' directories; maxJobBytes is
	// MaxJobsBytes (tests lower it).
	jobBytes      map[string]int64
	jobBytesTotal int64
	maxJobBytes   int64

	finalizers sync.WaitGroup
	stopLoops  context.CancelFunc
	loopsDone  chan struct{}
}

// New loads (or seeds) the state under cfg.StateDir, recovers from an
// interrupted run and reconciles the configured projects.
func New(cfg Config, exec harness.Executor, ax AXProbe, now func() time.Time, audit *slog.Logger) (*Office, error) {
	if exec == nil {
		return nil, errors.New("la Oficina necesita un ejecutor")
	}
	if now == nil {
		now = time.Now
	}
	if audit == nil {
		audit = slog.New(slog.DiscardHandler)
	}
	if cfg.MaxQueue <= 0 {
		cfg.MaxQueue = 200
	}
	if cfg.RetentionJobs <= 0 {
		cfg.RetentionJobs = 3000
	}
	if cfg.Limits.MaxTurns <= 0 {
		cfg.Limits.MaxTurns = 150
	}
	if cfg.Limits.MaxTimeoutMinutes <= 0 {
		cfg.Limits.MaxTimeoutMinutes = 90
	}
	cfg.Limits.MaxPromptBytes = MaxJobPromptBytes
	cfg.Limits.MaxSystemPrompt = MaxSystemPromptBytes
	cfg.Limits.MaxQueue = cfg.MaxQueue
	if len(cfg.Limits.RepoHosts) == 0 {
		cfg.Limits.RepoHosts = []string{"github.com"}
	}
	st, err := openStore(cfg.StateDir, now)
	if err != nil {
		return nil, err
	}
	o := &Office{
		cfg: cfg, exec: exec, probe: ax, now: func() time.Time { return now().UTC() }, audit: audit,
		store: st, hub: newHub(), wake: make(chan struct{}, 1),
		jobs: map[string]*Job{}, prBusy: map[string]bool{},
		dirtyJobs: map[string]bool{}, dirtyPipes: map[string]bool{}, removed: map[string]bool{}, invalid: map[string]bool{},
		metricsRev: -1, jobBytes: map[string]int64{}, maxJobBytes: MaxJobsBytes,
	}
	o.creds = &creds{claudeFile: cfg.ClaudeTokenFile, stored: st.codexAuthPath(), now: o.now}
	if cfg.OfficeSecretDir != "" {
		if cfg.CodexAuthKey != "" {
			o.creds.codexFile = filepath.Join(cfg.OfficeSecretDir, cfg.CodexAuthKey)
		}
		if cfg.GitHubTokenKey != "" {
			o.creds.githubFile = filepath.Join(cfg.OfficeSecretDir, cfg.GitHubTokenKey)
		}
	}
	o.gh = newGitHub(cfg.GitHubAPI, o.creds.githubTokens, o.now)
	if err := o.load(); err != nil {
		return nil, err
	}
	o.axState.Ready = exec.Ready()
	return o, nil
}

func (o *Office) load() error {
	now := o.now()
	of, found, warn, err := o.store.loadOffice()
	if err != nil {
		return err
	}
	if warn != "" {
		o.warnings = append(o.warnings, warn)
	}
	if !found {
		of = &officeFile{Settings: defaultSettings(), Agents: SeedAgents(now)}
		for i := range of.Agents {
			clampAgent(&of.Agents[i], o.cfg.Limits)
		}
	}
	jobs, warn, err := o.store.loadJobs()
	if err != nil {
		return err
	}
	if warn != "" {
		o.warnings = append(o.warnings, warn)
	}
	o.st = *of
	o.normalize()
	projects, warns := reconcileProjects(o.st.Projects, o.cfg.Projects, o.cfg.Limits, now)
	o.st.Projects = projects
	o.warnings = append(o.warnings, warns...)
	if o.projectLocked(o.st.Settings.RetroProject) == nil {
		o.st.Settings.RetroProject = ""
		if o.projectLocked(DefaultRetroProject) != nil {
			o.st.Settings.RetroProject = DefaultRetroProject
		} else if len(o.st.Projects) > 0 {
			o.st.Settings.RetroProject = o.st.Projects[0].ID
		}
	}
	sort.SliceStable(jobs, func(i, j int) bool { return jobs[i].Created.Before(jobs[j].Created) })
	for i := range jobs {
		j := jobs[i]
		if !ValidJobID(j.ID) || o.jobs[j.ID] != nil {
			continue
		}
		switch j.Status {
		case StatusPreparing, StatusRunning, StatusCleaning:
			j.Status, j.Message = StatusFailed, "Interrumpido por un reinicio del panel"
			j.Finished = &now
			j.Activity, j.Stalled = "", false
		}
		j.Waiting = ""
		if j.PR != nil {
			// The pull request exists: its files are no longer needed.
			_ = o.store.removeJobFile(j.ID, fileContents)
		}
		o.jobs[j.ID] = &j
		o.order = append(o.order, j.ID)
		o.refreshJobBytesLocked(j.ID)
	}
	// Pipelines whose current step was interrupted (or finished while
	// the panel was down) move on now.
	for i := range o.st.Pipelines {
		if o.st.Pipelines[i].Status == PipelineRunning {
			o.advancePipelineLocked(o.st.Pipelines[i].ID)
		}
	}
	o.retainLocked()
	o.saveLocked(true, true)
	return nil
}

// normalize fills nil slices and maps of a loaded state.
func (o *Office) normalize() {
	s := &o.st
	if s.Seq == nil {
		s.Seq = map[string]int64{}
	}
	if s.Agents == nil {
		s.Agents = []Agent{}
	}
	for i := range s.Agents {
		if s.Agents[i].History == nil {
			s.Agents[i].History = []AgentRev{}
		}
		if s.Agents[i].DisallowedTools == nil {
			s.Agents[i].DisallowedTools = []string{}
		}
	}
	if s.Projects == nil {
		s.Projects = []Project{}
	}
	if s.Pipelines == nil {
		s.Pipelines = []Pipeline{}
	}
	if s.Schedules == nil {
		s.Schedules = []Schedule{}
	}
	if s.Proposals == nil {
		s.Proposals = []Proposal{}
	}
	if s.Evals == nil {
		s.Evals = []Eval{}
	}
	d := defaultSettings()
	if s.Settings.OfficeName == "" {
		s.Settings.OfficeName = d.OfficeName
	}
	if s.Settings.AutoLessons == "" {
		s.Settings.AutoLessons = d.AutoLessons
	}
	if s.Settings.MaxIterations < 1 || s.Settings.MaxIterations > MaxIterationsLimit {
		s.Settings.MaxIterations = d.MaxIterations
	}
}

// saveLocked writes office.json and/or jobs.json. A failure is logged
// and shown as a warning; the in-memory state stays authoritative and the
// next save retries.
func (o *Office) saveLocked(office, jobs bool) {
	if office {
		if err := o.store.saveOffice(&o.st); err != nil {
			o.audit.Error("office.save", "file", officeFileName, "error", err.Error())
			if errors.Is(err, errTooBig) {
				o.warnOnce("office.json supera 32 MiB y no se guarda: borra evaluaciones, turnos o memoria que ya no uses.")
			} else {
				o.warnOnce("No se pudo guardar office.json; se reintentará en el próximo cambio.")
			}
		}
	}
	if jobs {
		if err := o.saveJobsLocked(); err != nil {
			o.audit.Error("office.save", "file", jobsFileName, "error", err.Error())
			o.warnOnce("No se pudo guardar jobs.json; se reintentará en el próximo cambio.")
		}
	}
}

// saveJobsLocked writes jobs.json; while it would be over fileMaxBytes,
// the oldest tenth of the finished jobs nothing needs is deleted first.
func (o *Office) saveJobsLocked() error {
	for {
		list := make([]Job, 0, len(o.order))
		for _, id := range o.order {
			list = append(list, *o.jobs[id])
		}
		err := o.store.saveJobs(list)
		if !errors.Is(err, errTooBig) {
			return err
		}
		if o.dropOldestLocked(max(1, len(o.order)/10), 0) == 0 {
			return err
		}
		o.invalidateLocked("metrics")
		o.warnOnce("jobs.json superaba 32 MiB: se borraron los trabajos terminados más antiguos.")
	}
}

// refreshJobBytesLocked measures a job's directory again.
func (o *Office) refreshJobBytesLocked(id string) {
	n := o.store.jobDirSize(id)
	o.jobBytesTotal += n - o.jobBytes[id]
	o.jobBytes[id] = n
}

func (o *Office) warnOnce(w string) {
	if !slices.Contains(o.warnings, w) {
		o.warnings = append(o.warnings, w)
		o.invalidateLocked("settings")
	}
}

// Change tracking for the coalesced deltas.

func (o *Office) touchJobLocked(id string) {
	o.rev++
	o.dirtyJobs[id] = true
	o.deltaDirty = true
}

func (o *Office) touchPipelineLocked(id string) {
	o.rev++
	o.dirtyPipes[id] = true
	o.invalid["pipelines"] = true
	o.deltaDirty = true
}

func (o *Office) invalidateLocked(keys ...string) {
	o.rev++
	for _, k := range keys {
		o.invalid[k] = true
		switch k {
		case "agents", "projects", "settings":
			// The prepared spec was built from these.
			o.prepared = nil
		}
	}
	o.deltaDirty = true
}

func (o *Office) kick() {
	select {
	case o.wake <- struct{}{}:
	default:
	}
}

// Counts are the header's numbers.
type Counts struct {
	PendingProposals int `json:"pending_proposals"`
	Queued           int `json:"queued"`
	Running          int `json:"running"`
}

// Delta is one coalesced "office" event of the stream.
type Delta struct {
	Rev        int64      `json:"rev"`
	Jobs       []Job      `json:"jobs"`
	Removed    []string   `json:"removed,omitempty"`
	Pipelines  []Pipeline `json:"pipelines,omitempty"`
	Active     *Active    `json:"active"`
	Queue      []string   `json:"queue"`
	AX         AXState    `json:"ax"`
	Counts     Counts     `json:"counts"`
	Invalidate []string   `json:"invalidate,omitempty"`
}

// flushDelta publishes the pending changes as one delta.
func (o *Office) flushDelta() {
	ready := o.exec.Ready()
	o.mu.Lock()
	if !o.deltaDirty && o.axState.Ready == ready {
		o.mu.Unlock()
		return
	}
	if o.axState.Ready != ready {
		o.axState.Ready = ready
		o.rev++
	}
	d := Delta{Rev: o.rev, Jobs: []Job{}, Queue: o.queueLocked(), Active: o.activeViewLocked(),
		AX: o.axState, Counts: o.countsLocked()}
	for id := range o.dirtyJobs {
		if j := o.jobs[id]; j != nil {
			d.Jobs = append(d.Jobs, *j)
		}
	}
	sort.Slice(d.Jobs, func(a, b int) bool { return d.Jobs[a].Created.Before(d.Jobs[b].Created) })
	for id := range o.removed {
		d.Removed = append(d.Removed, id)
	}
	if len(d.Removed) > 0 {
		// Clients that only follow invalidate reload and drop them too.
		o.invalid["jobs"] = true
	}
	for id := range o.dirtyPipes {
		if p := o.pipelineLocked(id); p != nil {
			d.Pipelines = append(d.Pipelines, clonePipeline(*p))
		}
	}
	for k := range o.invalid {
		d.Invalidate = append(d.Invalidate, k)
	}
	sort.Strings(d.Invalidate)
	sort.Strings(d.Removed)
	clear(o.dirtyJobs)
	clear(o.dirtyPipes)
	clear(o.removed)
	clear(o.invalid)
	o.deltaDirty = false
	o.mu.Unlock()
	data, err := json.Marshal(d)
	if err != nil {
		return
	}
	o.hub.publish(Message{Event: "office", Data: data}, "")
}

// Subscribe starts a stream subscription; job is the job whose log
// events it also receives, or "".
func (o *Office) Subscribe(job string) *Subscription { return o.hub.subscribe(job) }

// Unsubscribe ends a subscription.
func (o *Office) Unsubscribe(s *Subscription) { o.hub.unsubscribe(s) }

// Rev is the state's revision.
func (o *Office) Rev() int64 {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.rev
}

// Run runs the dispatcher, the scheduler and the delta publisher until
// ctx ends.
func (o *Office) Run(ctx context.Context) {
	ctx, cancel := context.WithCancel(ctx)
	o.mu.Lock()
	if o.stopLoops != nil || o.closed {
		o.mu.Unlock()
		cancel()
		return
	}
	o.stopLoops = cancel
	o.loopsDone = make(chan struct{})
	done := o.loopsDone
	o.mu.Unlock()
	defer close(done)

	var wg sync.WaitGroup
	wg.Go(func() {
		t := time.NewTicker(deltaInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				o.flushDelta()
			}
		}
	})
	wg.Go(func() {
		t := time.NewTicker(scheduleTick)
		defer t.Stop()
		o.refreshProbe(ctx, false)
		o.scheduleTick()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				o.refreshProbe(ctx, false)
				o.scheduleTick()
			}
		}
	})
	t := time.NewTicker(dispatchTick)
	defer t.Stop()
	o.dispatchOnce(ctx)
	for {
		select {
		case <-ctx.Done():
			wg.Wait()
			return
		case <-o.wake:
			o.dispatchOnce(ctx)
		case <-t.C:
			o.checkStalled()
			o.dispatchOnce(ctx)
		}
	}
}

// refreshProbe asks the probe about AX, at most every probeInterval
// unless forced.
func (o *Office) refreshProbe(ctx context.Context, force bool) {
	if o.probe == nil {
		return
	}
	o.mu.Lock()
	if !force && !o.probeAt.IsZero() && o.now().Sub(o.probeAt) < probeInterval {
		o.mu.Unlock()
		return
	}
	o.probeAt = o.now()
	o.mu.Unlock()
	pctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	st := o.probe.Probe(pctx)
	cancel()
	o.mu.Lock()
	defer o.mu.Unlock()
	st.Ready = o.axState.Ready
	if o.axState.CleanupFailed != "" && st.CleanupFailed == "" && o.active != nil {
		st.CleanupFailed = o.axState.CleanupFailed
	}
	if st != o.axState {
		o.axState = st
		o.rev++
		o.deltaDirty = true
	}
}

// Close stops the loops, waits for the runs being finalized (the run
// manager's shutdown ends them) until ctx ends, saves and ends every
// stream.
func (o *Office) Close(ctx context.Context) {
	o.mu.Lock()
	o.closed = true
	stop, done := o.stopLoops, o.loopsDone
	o.mu.Unlock()
	if stop != nil {
		stop()
		select {
		case <-done:
		case <-ctx.Done():
		}
	}
	waited := make(chan struct{})
	go func() {
		o.finalizers.Wait()
		close(waited)
	}()
	select {
	case <-waited:
	case <-ctx.Done():
	}
	o.mu.Lock()
	o.saveLocked(true, true)
	o.mu.Unlock()
	o.flushDelta()
	o.hub.close()
}

// Credentials returns the env holding a harness's credential, for
// runs.Deps. The value is never logged.
func (o *Office) Credentials(h string) (map[string]string, error) {
	env, err := o.creds.get(h)
	if err != nil {
		return nil, fmt.Errorf("la credencial de %s no está disponible", h)
	}
	return env, nil
}

// SaveCodexAuth keeps the auth.json read back after a Codex run when it
// passes every rule (see creds.saveCodexAuth), for runs.Deps.
func (o *Office) SaveCodexAuth(data []byte, created time.Time) error {
	err := o.creds.saveCodexAuth(data, created)
	sum := sha256.Sum256(data)
	if err != nil {
		o.audit.Warn("audit", "action", "codex.auth.rejected", "reason", err.Error(), "bytes", len(data))
		return err
	}
	o.audit.Info("audit", "action", "codex.auth.saved", "bytes", len(data), "sha256_prefix", hex.EncodeToString(sum[:4]))
	o.mu.Lock()
	o.invalidateLocked("settings")
	o.mu.Unlock()
	return nil
}

// ForgetCodexAuth deletes the stored (renewed) copy of Codex's auth.json;
// the Secret's copy, if any, is used again from the next run.
func (o *Office) ForgetCodexAuth(clientIP string) error {
	existed, err := o.creds.forgetStored()
	o.auditAction("codex.auth.forget", clientIP, "existed", existed, "ok", err == nil)
	if err != nil {
		return &Error{Status: 500, Message: "No se pudo borrar la credencial guardada de Codex"}
	}
	o.mu.Lock()
	o.invalidateLocked("settings")
	o.mu.Unlock()
	return nil
}

// lookups (callers hold o.mu)

func (o *Office) agentLocked(id string) *Agent {
	for i := range o.st.Agents {
		if o.st.Agents[i].ID == id {
			return &o.st.Agents[i]
		}
	}
	return nil
}

func (o *Office) projectLocked(id string) *Project {
	for i := range o.st.Projects {
		if o.st.Projects[i].ID == id {
			return &o.st.Projects[i]
		}
	}
	return nil
}

func (o *Office) pipelineLocked(id string) *Pipeline {
	for i := range o.st.Pipelines {
		if o.st.Pipelines[i].ID == id {
			return &o.st.Pipelines[i]
		}
	}
	return nil
}

func (o *Office) scheduleLocked(id string) *Schedule {
	for i := range o.st.Schedules {
		if o.st.Schedules[i].ID == id {
			return &o.st.Schedules[i]
		}
	}
	return nil
}

func (o *Office) evalLocked(id string) *Eval {
	for i := range o.st.Evals {
		if o.st.Evals[i].ID == id {
			return &o.st.Evals[i]
		}
	}
	return nil
}

func (o *Office) proposalLocked(id string) *Proposal {
	for i := range o.st.Proposals {
		if o.st.Proposals[i].ID == id {
			return &o.st.Proposals[i]
		}
	}
	return nil
}

func (o *Office) nextSeq(kind string) int64 {
	o.st.Seq[kind]++
	return o.st.Seq[kind]
}

// newJobID is j<yymmdd>-<hhmmss>-<4 hex>: unique even after the state
// was reset, since it names the job's directory and its AX task.
func (o *Office) newJobIDLocked() string {
	for {
		var b [2]byte
		_, _ = rand.Read(b[:])
		id := "j" + o.now().Format("060102-150405") + "-" + hex.EncodeToString(b[:])
		if o.jobs[id] == nil {
			return id
		}
	}
}

func isTerminal(status string) bool {
	return status == StatusDone || status == StatusFailed || status == StatusCancelled
}

func isRunning(status string) bool {
	return status == StatusPreparing || status == StatusRunning || status == StatusCleaning
}

// queueLocked lists the queued jobs in dispatch order.
func (o *Office) queueLocked() []string {
	var q []*Job
	for _, id := range o.order {
		if j := o.jobs[id]; j.Status == StatusQueued {
			q = append(q, j)
		}
	}
	sortQueue(q)
	out := make([]string, 0, len(q))
	for _, j := range q {
		out = append(out, j.ID)
	}
	return out
}

// sortQueue orders by priority (highest first), then age (oldest first).
// q comes in creation order (o.order), and the stable sort keeps it for
// jobs created at the same instant: the id's random suffix must not decide.
func sortQueue(q []*Job) {
	sort.SliceStable(q, func(a, b int) bool {
		if q[a].Priority != q[b].Priority {
			return q[a].Priority > q[b].Priority
		}
		return q[a].Created.Before(q[b].Created)
	})
}

func (o *Office) countsLocked() Counts {
	var c Counts
	for _, j := range o.jobs {
		switch {
		case j.Status == StatusQueued:
			c.Queued++
		case isRunning(j.Status):
			c.Running++
		}
	}
	for _, p := range o.st.Proposals {
		if p.Status == ProposalPending {
			c.PendingProposals++
		}
	}
	return c
}

func (o *Office) activeViewLocked() *Active {
	a := o.active
	if a == nil {
		return nil
	}
	j := o.jobs[a.jobID]
	if j == nil {
		return nil
	}
	return &Active{JobID: j.ID, Task: a.task, State: j.Status, Activity: j.Activity, Since: a.since}
}

func cloneAgent(a Agent) Agent {
	a.History = slices.Clone(a.History)
	a.DisallowedTools = slices.Clone(a.DisallowedTools)
	return a
}

func cloneProject(p Project) Project {
	p.Memory = slices.Clone(p.Memory)
	return p
}

func clonePipeline(p Pipeline) Pipeline {
	p.Steps = slices.Clone(p.Steps)
	if p.Participants != nil {
		m := make(map[string]string, len(p.Participants))
		for k, v := range p.Participants {
			m[k] = v
		}
		p.Participants = m
	}
	return p
}

// Snapshot is everything the browser needs at once.
func (o *Office) Snapshot() Snapshot {
	ready := o.exec.Ready()
	credentials := o.creds.snapshot()
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.axState.Ready != ready {
		o.axState.Ready = ready
		o.rev++
		o.deltaDirty = true
	}
	s := Snapshot{
		Version: o.cfg.Version, Now: o.now(), Rev: o.rev, Settings: o.st.Settings,
		Catalog: CatalogInfo(), Agents: []Agent{}, Projects: []Project{}, Jobs: []Job{},
		Queue: o.queueLocked(), Active: o.activeViewLocked(), Pipelines: []Pipeline{},
		Templates: Templates(), Schedules: []Schedule{}, Proposals: []Proposal{}, Evals: []Eval{},
		Metrics: o.metricsLocked(), AX: o.axState, Credentials: credentials, Limits: o.cfg.Limits,
		Warnings: slices.Clone(o.warnings),
	}
	s.Limits.RepoHosts = slices.Clone(s.Limits.RepoHosts)
	if s.Warnings == nil {
		s.Warnings = []string{}
	}
	for _, a := range o.st.Agents {
		s.Agents = append(s.Agents, cloneAgent(a))
	}
	for _, p := range o.st.Projects {
		s.Projects = append(s.Projects, cloneProject(p))
	}
	others := 0
	for i := len(o.order) - 1; i >= 0; i-- {
		j := o.jobs[o.order[i]]
		if j.Status == StatusQueued || isRunning(j.Status) {
			s.Jobs = append(s.Jobs, *j)
		} else if others < snapshotJobs {
			s.Jobs = append(s.Jobs, *j)
			others++
		}
	}
	for i := len(o.st.Pipelines) - 1; i >= 0 && len(s.Pipelines) < snapshotPipelines; i-- {
		s.Pipelines = append(s.Pipelines, clonePipeline(o.st.Pipelines[i]))
	}
	for _, sc := range o.st.Schedules {
		sc.Days = slices.Clone(sc.Days)
		if next, ok := nextSlot(sc, o.now()); ok && sc.Enabled {
			sc.NextRun = &next
		}
		s.Schedules = append(s.Schedules, sc)
	}
	decided := 0
	for i := len(o.st.Proposals) - 1; i >= 0; i-- {
		p := o.st.Proposals[i]
		if p.Status == ProposalPending {
			s.Proposals = append(s.Proposals, p)
		} else if decided < snapshotDecided {
			s.Proposals = append(s.Proposals, p)
			decided++
		}
	}
	s.Evals = append(s.Evals, o.st.Evals...)
	return s
}

// exportFile is GET /api/export.
type exportFile struct {
	Version  string     `json:"version"`
	Exported time.Time  `json:"exported"`
	Office   officeFile `json:"office"`
	Jobs     jobsFile   `json:"jobs"`
}

// Export is the office's records (office.json and jobs.json as they are
// in memory), without credentials, prompts files or results.
func (o *Office) Export() ([]byte, error) {
	o.mu.Lock()
	ef := exportFile{Version: o.cfg.Version, Exported: o.now(), Office: o.st,
		Jobs: jobsFile{Schema: SchemaVersion, Jobs: make([]Job, 0, len(o.order))}}
	ef.Office.Schema = SchemaVersion
	for _, id := range o.order {
		ef.Jobs.Jobs = append(ef.Jobs.Jobs, *o.jobs[id])
	}
	data, err := json.MarshalIndent(ef, "", " ")
	o.mu.Unlock()
	return data, err
}

// auditPrompt logs a mutating action with a prompt's size and digest,
// never its text.
func (o *Office) auditAction(action, clientIP string, attrs ...any) {
	o.audit.Info("audit", append([]any{"action", action, "client_ip", clientIP}, attrs...)...)
}

func promptAttrs(prompt string) []any {
	sum := sha256.Sum256([]byte(prompt))
	return []any{"prompt_bytes", len(prompt), "prompt_sha256", hex.EncodeToString(sum[:])}
}
