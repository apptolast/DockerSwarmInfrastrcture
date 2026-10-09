// Package office is the agent office: the team of agents (personas), the
// projects they work on, the persistent job queue and its dispatcher, the
// multi-agent pipelines, the schedules and the human-gated improvement
// loop (lessons, prompt versions, metrics and evaluations). Execution is
// delegated to a harness.Executor, one run at a time.
//
// This file is the data model and the JSON contract with the browser.
package office

import (
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// SchemaVersion of the files under the state directory.
const SchemaVersion = 1

// Job kinds.
const (
	KindAsk    = "pregunta" // analyse and answer
	KindPlan   = "plan"     // design a plan, no changes
	KindChange = "cambio"   // change the code; changes are captured
	KindReview = "revision" // review changes or a PR; ends with a verdict
	KindRetro  = "retro"    // the coach proposes a better persona prompt
	KindJudge  = "juez"     // an evaluation judge scores another job
)

// Job statuses.
const (
	StatusQueued    = "en_cola"
	StatusPreparing = "preparando"
	StatusRunning   = "en_curso"
	StatusCleaning  = "limpiando"
	StatusDone      = "hecho"
	StatusFailed    = "fallido"
	StatusCancelled = "cancelado"
)

// Review verdicts parsed from "VEREDICTO: ..." lines.
const (
	VerdictApproved = "aprobado"
	VerdictChanges  = "cambios"
)

// Proposal types and statuses.
const (
	ProposalLesson = "leccion"
	ProposalPrompt = "prompt"

	ProposalPending  = "pendiente"
	ProposalApproved = "aprobada"
	ProposalRejected = "rechazada"
)

// Lesson policies (Settings.AutoLessons).
const (
	LessonsPropose = "proponer" // lessons become proposals for a human
	LessonsApprove = "aprobar"  // lessons go straight into memory
	LessonsOff     = "off"      // lessons are ignored
)

// Settings are the office-wide preferences.
type Settings struct {
	OfficeName string `json:"office_name"`
	// DefaultAgent is preselected in the new job form.
	DefaultAgent string `json:"default_agent"`
	// AutoLessons is LessonsPropose, LessonsApprove or LessonsOff.
	AutoLessons string `json:"auto_lessons"`
	// MaxLessonsInPrompt bounds the project memory injected in a prompt.
	MaxLessonsInPrompt int `json:"max_lessons_in_prompt"`
	// QueuePaused stops the dispatcher from starting queued jobs.
	QueuePaused bool `json:"queue_paused"`
	// RetroProject is the project whose checkout coach and judge jobs run
	// in (they only read their prompt; any small public repo works).
	RetroProject string `json:"retro_project"`
	// MaxIterations bounds fix/review rounds of a pipeline.
	MaxIterations int `json:"max_iterations"`
}

// Agent is a persona of the team: a harness, a model, how hard it thinks,
// what it may do and who it is. Every change to the fields that shape its
// behaviour (harness, model, effort, mode, system prompt) bumps Version and
// keeps the previous one in History, so metrics can compare versions.
type Agent struct {
	ID              string     `json:"id"`
	Name            string     `json:"name"`
	Role            string     `json:"role"`
	Emoji           string     `json:"emoji"`
	Color           string     `json:"color"`
	Harness         string     `json:"harness"`
	Model           string     `json:"model"`
	FallbackModel   string     `json:"fallback_model"`
	Advisor         string     `json:"advisor"`
	Effort          string     `json:"effort"`
	Mode            string     `json:"mode"`
	MaxTurns        int        `json:"max_turns"`
	TimeoutMinutes  int        `json:"timeout_minutes"`
	SystemPrompt    string     `json:"system_prompt"`
	DisallowedTools []string   `json:"disallowed_tools"`
	Enabled         bool       `json:"enabled"`
	Version         int        `json:"version"`
	History         []AgentRev `json:"history"`
	Created         time.Time  `json:"created"`
	Updated         time.Time  `json:"updated"`
}

// AgentRev is a previous version of an agent's behaviour.
type AgentRev struct {
	Version      int       `json:"version"`
	Harness      string    `json:"harness"`
	Model        string    `json:"model"`
	Effort       string    `json:"effort"`
	Mode         string    `json:"mode"`
	SystemPrompt string    `json:"system_prompt"`
	Note         string    `json:"note"`
	At           time.Time `json:"at"`
}

// MaxAgentHistory bounds Agent.History.
const MaxAgentHistory = 20

// Project is a repository the office works on. Seeded projects come from
// the reviewed configuration and cannot be deleted, only archived.
type Project struct {
	ID          string       `json:"id"`
	Name        string       `json:"name"`
	Repo        string       `json:"repo"`
	Branch      string       `json:"branch"`
	Description string       `json:"description"`
	Service     string       `json:"service"`
	URL         string       `json:"url"`
	Notes       string       `json:"notes"`
	Seeded      bool         `json:"seeded"`
	Archived    bool         `json:"archived"`
	Memory      []MemoryItem `json:"memory"`
	Created     time.Time    `json:"created"`
}

// MemoryItem is an approved lesson about a project, injected into the
// prompts of later jobs on it while Active.
type MemoryItem struct {
	ID        string    `json:"id"`
	Text      string    `json:"text"`
	SourceJob string    `json:"source_job,omitempty"`
	Active    bool      `json:"active"`
	Created   time.Time `json:"created"`
}

// Overrides change an agent's settings for one job only.
type Overrides struct {
	Harness        string `json:"harness,omitempty"`
	Model          string `json:"model,omitempty"`
	FallbackModel  string `json:"fallback_model,omitempty"`
	Advisor        string `json:"advisor,omitempty"`
	Effort         string `json:"effort,omitempty"`
	Mode           string `json:"mode,omitempty"`
	MaxTurns       int    `json:"max_turns,omitempty"`
	TimeoutMinutes int    `json:"timeout_minutes,omitempty"`
}

// Source tells where a job came from.
type Source struct {
	// Type is "manual", "issue", "pr", "job", "pipeline", "schedule",
	// "coach" or "eval".
	Type   string `json:"type"`
	Number int    `json:"number,omitempty"`
	JobID  string `json:"job_id,omitempty"`
	Ref    string `json:"ref,omitempty"`
}

// AgentSnapshot is the agent as it was when the job was created.
type AgentSnapshot struct {
	Name           string `json:"name"`
	Emoji          string `json:"emoji"`
	Version        int    `json:"version"`
	Harness        string `json:"harness"`
	Model          string `json:"model"`
	FallbackModel  string `json:"fallback_model"`
	Advisor        string `json:"advisor"`
	Effort         string `json:"effort"`
	Mode           string `json:"mode"`
	MaxTurns       int    `json:"max_turns"`
	TimeoutMinutes int    `json:"timeout_minutes"`
}

// Rating is a human's verdict on a job.
type Rating struct {
	Score int       `json:"score"` // -1, 0 or 1
	Note  string    `json:"note"`
	At    time.Time `json:"at"`
}

// PullRequest is the draft pull request built from a job's changes.
type PullRequest struct {
	Number int    `json:"number"`
	URL    string `json:"url"`
	Branch string `json:"branch"`
}

// ChangeSummary is what the job list shows about captured changes.
type ChangeSummary struct {
	Files            int    `json:"files"`
	Additions        int    `json:"additions"`
	Deletions        int    `json:"deletions"`
	PatchTruncated   bool   `json:"patch_truncated,omitempty"`
	ContentsComplete bool   `json:"contents_complete"`
	BaseSHA          string `json:"base_sha"`
	Error            string `json:"error,omitempty"`
}

// Job is one unit of work for one agent on one project. The big parts
// (prompt, result text, events, patch, file contents) live in files of
// the job's directory, not in this record.
type Job struct {
	// ModelCost is what each model cost in the run that finished the job.
	ModelCost  map[string]float64 `json:"model_cost,omitempty"`
	ID         string             `json:"id"`
	Title      string             `json:"title"`
	ProjectID  string             `json:"project_id"`
	AgentID    string             `json:"agent_id"`
	Agent      AgentSnapshot      `json:"agent"`
	Kind       string             `json:"kind"`
	Branch     string             `json:"branch"`
	Priority   int                `json:"priority"` // 0 baja, 1 normal, 2 alta
	Status     string             `json:"status"`
	Source     Source             `json:"source"`
	PipelineID string             `json:"pipeline_id,omitempty"`
	Step       int                `json:"step,omitempty"`
	Created    time.Time          `json:"created"`
	Started    *time.Time         `json:"started,omitempty"`
	Finished   *time.Time         `json:"finished,omitempty"`
	Task       string             `json:"task,omitempty"`
	Outcome    string             `json:"outcome,omitempty"`
	Message    string             `json:"message,omitempty"`
	ExitCode   *int               `json:"exit_code,omitempty"`
	Usage      harness.Usage      `json:"usage"`
	Summary    string             `json:"summary,omitempty"`
	Lessons    []string           `json:"lessons,omitempty"`
	Verdict    string             `json:"verdict,omitempty"`
	Score      *int               `json:"score,omitempty"` // judge jobs: 0-10
	Changes    *ChangeSummary     `json:"changes,omitempty"`
	PR         *PullRequest       `json:"pr,omitempty"`
	Rating     *Rating            `json:"rating,omitempty"`
	// ApplyFrom names the job whose patch is applied before this one
	// runs (a fix step continues the previous changes).
	ApplyFrom string `json:"apply_from,omitempty"`
	// Overrides are the per-job settings it was created with (a retry
	// reuses them). Additive to the 1.0.0 contract.
	Overrides *Overrides `json:"overrides,omitempty"`
	// Context names the jobs whose results are given to this one as
	// "Contexto de pasos anteriores" (pipeline steps, follow-ups).
	// Additive to the 1.0.0 contract.
	Context []string `json:"context,omitempty"`
	// Activity is the last thing the agent did, for the office view.
	Activity string `json:"activity,omitempty"`
	// Waiting tells why a queued job has not started (e.g. a host
	// ax-tarea task holds the worker).
	Waiting string `json:"waiting,omitempty"`
	// Stalled is set when a running job printed nothing for a while.
	Stalled bool `json:"stalled,omitempty"`
}

// JobDetail is GET /api/jobs/{id}: the record plus its files.
type JobDetail struct {
	Job
	Prompt       string                `json:"prompt"`
	FinalPrompt  string                `json:"final_prompt"`
	SystemPrompt string                `json:"system_prompt"`
	ResultText   string                `json:"result_text"`
	Files        []harness.ChangedFile `json:"files,omitempty"`
	CanPR        bool                  `json:"can_pr"`
}

// Pipeline statuses.
const (
	PipelineRunning   = "en_curso"
	PipelineDone      = "hecho"
	PipelineFailed    = "fallido"
	PipelineCancelled = "cancelado"
)

// Pipeline is one run of a team template: its steps are jobs.
type Pipeline struct {
	ID            string            `json:"id"`
	Template      string            `json:"template"`
	Title         string            `json:"title"`
	ProjectID     string            `json:"project_id"`
	Branch        string            `json:"branch"`
	Task          string            `json:"task"`
	Participants  map[string]string `json:"participants"` // role -> agent id
	Status        string            `json:"status"`
	Iteration     int               `json:"iteration"`
	MaxIterations int               `json:"max_iterations"`
	Steps         []PipelineStep    `json:"steps"`
	Source        Source            `json:"source"`
	// Priority is given to every step's job. Additive to the 1.0.0
	// contract.
	Priority int        `json:"priority"`
	Outcome  string     `json:"outcome,omitempty"`
	Score    *int       `json:"score,omitempty"`
	Created  time.Time  `json:"created"`
	Finished *time.Time `json:"finished,omitempty"`
}

// PipelineStep is one step of a pipeline.
type PipelineStep struct {
	Name    string `json:"name"`
	Role    string `json:"role"`
	AgentID string `json:"agent_id"`
	Kind    string `json:"kind"`
	JobID   string `json:"job_id,omitempty"`
	Status  string `json:"status"`
}

// TemplateRole is a seat of a team template.
type TemplateRole struct {
	Role         string `json:"role"`
	Label        string `json:"label"`
	DefaultAgent string `json:"default_agent"`
}

// Template describes a team template to the browser.
type Template struct {
	ID          string         `json:"id"`
	Name        string         `json:"name"`
	Icon        string         `json:"icon"`
	Description string         `json:"description"`
	Roles       []TemplateRole `json:"roles"`
	// Needs is "", "patch" (a source job with changes) or "pr".
	Needs     string `json:"needs,omitempty"`
	Iterative bool   `json:"iterative"`
}

// Schedule fires a job or a pipeline on the given UTC weekdays and time.
type Schedule struct {
	ID      string         `json:"id"`
	Name    string         `json:"name"`
	Enabled bool           `json:"enabled"`
	Days    []int          `json:"days"` // 0 = domingo ... 6 = sábado
	Time    string         `json:"time"` // "HH:MM" UTC
	Target  ScheduleTarget `json:"target"`
	LastRun *time.Time     `json:"last_run,omitempty"`
	LastRef string         `json:"last_ref,omitempty"`
	NextRun *time.Time     `json:"next_run,omitempty"`
	Created time.Time      `json:"created"`
	// LastSlot is the slot that last fired, and Edited when the schedule
	// was last saved (created, edited, enabled): a slot fires only when
	// it is newer than both. Additive to the 1.0.0 contract.
	LastSlot *time.Time `json:"last_slot,omitempty"`
	Edited   *time.Time `json:"edited,omitempty"`
}

// ScheduleTarget is what a schedule creates.
type ScheduleTarget struct {
	// Type is "job" or "pipeline".
	Type      string `json:"type"`
	ProjectID string `json:"project_id"`
	AgentID   string `json:"agent_id,omitempty"`
	Kind      string `json:"kind,omitempty"`
	Template  string `json:"template,omitempty"`
	Prompt    string `json:"prompt"`
	Branch    string `json:"branch,omitempty"`
}

// Proposal is an improvement waiting for a human: a lesson for a
// project's memory, or a new system prompt for an agent.
type Proposal struct {
	ID         string     `json:"id"`
	Type       string     `json:"type"`
	TargetType string     `json:"target_type"` // "proyecto" | "agente"
	TargetID   string     `json:"target_id"`
	Content    string     `json:"content"`
	Rationale  string     `json:"rationale"`
	SourceJob  string     `json:"source_job,omitempty"`
	Status     string     `json:"status"`
	Created    time.Time  `json:"created"`
	Decided    *time.Time `json:"decided,omitempty"`
}

// Eval is a frozen task of the evaluation bench: an agent's answer to it
// is scored 0-10 by a judge against Criteria.
type Eval struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	ProjectID string    `json:"project_id"`
	Kind      string    `json:"kind"`
	Prompt    string    `json:"prompt"`
	Criteria  string    `json:"criteria"`
	Created   time.Time `json:"created"`
}

// Metric aggregates finished jobs of one agent version (or one model).
type Metric struct {
	AgentID      string     `json:"agent_id,omitempty"`
	Version      int        `json:"version,omitempty"`
	Harness      string     `json:"harness,omitempty"`
	Model        string     `json:"model,omitempty"`
	Jobs         int        `json:"jobs"`
	Succeeded    int        `json:"succeeded"`
	Failed       int        `json:"failed"`
	ThumbsUp     int        `json:"thumbs_up"`
	ThumbsDown   int        `json:"thumbs_down"`
	Approved     int        `json:"approved"` // reviews of its changes that approved them
	Rejected     int        `json:"rejected"`
	EvalRuns     int        `json:"eval_runs"`
	EvalScoreAvg float64    `json:"eval_score_avg"`
	CostUSD      float64    `json:"cost_usd"`
	AvgCostUSD   float64    `json:"avg_cost_usd"`
	AvgSeconds   float64    `json:"avg_seconds"`
	AvgTurns     float64    `json:"avg_turns"`
	LastUsed     *time.Time `json:"last_used,omitempty"`
}

// Metrics is the leaderboard.
type Metrics struct {
	ByAgentVersion []Metric `json:"by_agent_version"`
	ByModel        []Metric `json:"by_model"`
	TotalCostUSD   float64  `json:"total_cost_usd"`
	CostTodayUSD   float64  `json:"cost_today_usd"`
	JobsToday      int      `json:"jobs_today"`
}

// Active is the run in the sandbox right now.
type Active struct {
	JobID    string    `json:"job_id"`
	Task     string    `json:"task"`
	State    string    `json:"state"`
	Activity string    `json:"activity"`
	Since    time.Time `json:"since"`
}

// AXState is what the office knows of the AX lab.
type AXState struct {
	Ready         bool   `json:"ready"`
	ReapNote      string `json:"reap_note,omitempty"`
	Blocking      string `json:"blocking,omitempty"`
	CleanupFailed string `json:"cleanup_failed,omitempty"`
}

// Credentials tells which credentials exist, never their values.
type Credentials struct {
	Claude         bool       `json:"claude"`
	Codex          bool       `json:"codex"`
	CodexSource    string     `json:"codex_source"` // "guardada", "secreto" o ""
	CodexRefreshed *time.Time `json:"codex_refreshed,omitempty"`
	GitHub         bool       `json:"github"`
	GitHubOwners   []string   `json:"github_owners"`
}

// Catalog lists the models and efforts the pinned CLIs know.
type Catalog struct {
	Claude HarnessCatalog `json:"claude"`
	Codex  HarnessCatalog `json:"codex"`
}

// HarnessCatalog is one CLI's catalogue.
type HarnessCatalog struct {
	Version string         `json:"version"`
	Models  []CatalogModel `json:"models"`
	Efforts []string       `json:"efforts"`
	Source  string         `json:"source"`
}

// CatalogModel is one model; Efforts empty means the harness default set.
type CatalogModel struct {
	ID      string   `json:"id"`
	Label   string   `json:"label"`
	Efforts []string `json:"efforts,omitempty"`
	Default string   `json:"default_effort,omitempty"`
}

// Snapshot is GET /api/office: everything the browser needs at once.
type Snapshot struct {
	Version     string      `json:"version"`
	Now         time.Time   `json:"now"`
	Rev         int64       `json:"rev"`
	Settings    Settings    `json:"settings"`
	Catalog     Catalog     `json:"catalog"`
	Agents      []Agent     `json:"agents"`
	Projects    []Project   `json:"projects"`
	Jobs        []Job       `json:"jobs"`
	Queue       []string    `json:"queue"`
	Active      *Active     `json:"active"`
	Pipelines   []Pipeline  `json:"pipelines"`
	Templates   []Template  `json:"templates"`
	Schedules   []Schedule  `json:"schedules"`
	Proposals   []Proposal  `json:"proposals"`
	Evals       []Eval      `json:"evals"`
	Metrics     Metrics     `json:"metrics"`
	AX          AXState     `json:"ax"`
	Credentials Credentials `json:"credentials"`
	Limits      Limits      `json:"limits"`
	Usage       UsageView   `json:"usage"`
	Warnings    []string    `json:"warnings"`
}

// Limits are the reviewed bounds the browser validates against too.
type Limits struct {
	MaxTurns          int      `json:"max_turns"`
	MaxTimeoutMinutes int      `json:"max_timeout_minutes"`
	MaxPromptBytes    int      `json:"max_prompt_bytes"`
	MaxSystemPrompt   int      `json:"max_system_prompt_bytes"`
	MaxQueue          int      `json:"max_queue"`
	RepoHosts         []string `json:"repo_hosts"`
}
