// Package harness is the contract between the office (what to run, and
// what came of it) and the run manager (how a run executes in one AX
// sandbox). It holds the run specification, the structured events an
// agent CLI's output turns into, the result of a run and the captured
// changes, plus the agent command lines and their output parsers. It
// imports nothing of AX: both sides depend on it, never on each other.
package harness

import (
	"context"
	"errors"
	"time"
)

// Agent CLIs a run can drive, both unmodified inside the ax-agents image
// (images/ax-agents/package.json): Claude Code 2.1.274 and Codex 0.156.1.
const (
	Claude = "claude"
	Codex  = "codex"
)

// Tool modes. Read-only ("lectura") analyses and proposes; full
// ("completo") may run commands and edit files inside the gVisor sandbox,
// which is the boundary.
const (
	ModeRead = "lectura"
	ModeFull = "completo"
)

// Errors a launch can fail with before anything is created.
var (
	// ErrBusy: another run, or a host ax-tarea task, holds the only
	// worker. The office keeps the job queued and tries again.
	ErrBusy = errors.New("el sandbox está ocupado")
	// ErrNotReady: the manager is still reaping runs from before a
	// restart, or is shutting down.
	ErrNotReady = errors.New("el ejecutor aún no está listo")
)

// Spec is one validated run. The office builds it; the run manager never
// reinterprets it beyond its own safety checks.
type Spec struct {
	// ID is the AX task name, "web-" plus the job id; the workspace is
	// "ws-" plus ID.
	ID string
	// Repo is a public https://github.com/<owner>/<repo> URL and Branch
	// its branch; both already validated.
	Repo   string
	Branch string
	// Prompt goes to the agent on stdin, never in argv.
	Prompt string
	// Harness is Claude or Codex.
	Harness string
	// Model, FallbackModel (Claude only) and Effort are passed to the CLI
	// when not empty: Claude --model/--fallback-model/--effort, Codex -m
	// and -c model_reasoning_effort.
	Model         string
	FallbackModel string
	Effort        string
	// Mode is ModeRead or ModeFull.
	Mode string
	// AppendSystemPrompt is the persona (Claude --append-system-prompt).
	// The office puts it at the top of the prompt for Codex instead.
	AppendSystemPrompt string
	// MaxTurns caps Claude's turns (--max-turns). Codex has no such flag.
	MaxTurns int
	// DisallowedTools are Claude tool names denied in full mode.
	DisallowedTools []string
	// Timeout bounds the agent process; the guest SIGKILLs it after.
	Timeout time.Duration
	// ApplyPatch, when not empty, is applied to the checkout with
	// `git apply --index` before the agent starts (a follow-up step
	// continues the previous step's changes).
	ApplyPatch []byte
	// CaptureChanges collects the checkout's changes against its base
	// commit after the agent exits.
	CaptureChanges bool
}

// Event kinds, in the order an agent usually emits them.
const (
	EventSystem     = "system"      // the office or the run manager speaking
	EventInit       = "init"        // the agent session started
	EventText       = "text"        // assistant text
	EventThinking   = "thinking"    // reasoning (summarized)
	EventTool       = "tool"        // a tool call (Bash, Edit, Read...)
	EventToolResult = "tool_result" // its outcome
	EventTodo       = "todo"        // a plan / todo list update
	EventUsage      = "usage"       // token counters (Codex turns)
	EventResult     = "result"      // the final result of the session
	EventStderr     = "stderr"      // a line of the agent's stderr
	EventError      = "error"       // an error the agent reported
)

// Event is one line of a run's timeline. It is what the browser renders
// and what the office stores; inputs and outputs are summarized and
// bounded, never a credential.
type Event struct {
	Seq       int64     `json:"seq"`
	Time      time.Time `json:"t"`
	Kind      string    `json:"k"`
	Text      string    `json:"text,omitempty"`
	Tool      string    `json:"tool,omitempty"`
	Input     string    `json:"input,omitempty"`
	OK        *bool     `json:"ok,omitempty"`
	Model     string    `json:"model,omitempty"`
	CostUSD   float64   `json:"cost_usd,omitempty"`
	InTokens  int64     `json:"in,omitempty"`
	OutTokens int64     `json:"out,omitempty"`
	CacheRead int64     `json:"cache_r,omitempty"`
	CacheMade int64     `json:"cache_w,omitempty"`
	Turns     int       `json:"turns,omitempty"`
	Millis    int64     `json:"ms,omitempty"`
}

// Bounds of an event's text fields.
const (
	MaxEventText  = 4 << 10
	MaxEventInput = 600
)

// Outcomes of a run.
const (
	OutcomeExited    = "exited"
	OutcomeCancelled = "cancelled"
	OutcomeTimeout   = "timeout"
	OutcomeFailed    = "failed"
)

// Run states, as the run manager reports them through Hooks.OnState.
const (
	StatePreparing     = "preparing"
	StateWaiting       = "waiting"
	StateRunning       = "running"
	StateCancelling    = "cancelling"
	StateCleaning      = "cleaning"
	StateCleanupFailed = "cleanup_failed"
	StateFinished      = "finished"
)

// Usage is what a session reports about itself.
type Usage struct {
	ModelUsed  string  `json:"model_used,omitempty"`
	CostUSD    float64 `json:"cost_usd"`
	InTokens   int64   `json:"in_tokens"`
	OutTokens  int64   `json:"out_tokens"`
	CacheRead  int64   `json:"cache_read_tokens"`
	CacheMade  int64   `json:"cache_write_tokens"`
	Turns      int     `json:"turns"`
	DurationMS int64   `json:"duration_ms"`
}

// UsageWindow is one rate-limit window of the subscription as Claude Code
// reports it. Utilization is a fraction (0.01 is 1 %) and ResetsAt is when
// the window resets.
type UsageWindow struct {
	Utilization float64   `json:"utilization"`
	ResetsAt    time.Time `json:"resets_at"`
}

// UsageWindows are the two windows that gate new work: the rolling
// five-hour window and the weekly one.
type UsageWindows struct {
	FiveHour UsageWindow `json:"five_hour"`
	SevenDay UsageWindow `json:"seven_day"`
}

// Result is how a run ended. ResultText is the agent's final answer
// (Claude's result event, Codex's last agent message), bounded by
// MaxResultText.
type Result struct {
	Outcome    string   `json:"outcome"`
	Message    string   `json:"message,omitempty"`
	ExitCode   *int     `json:"exit_code,omitempty"`
	IsError    bool     `json:"is_error,omitempty"`
	ResultText string   `json:"-"`
	Usage      Usage    `json:"usage"`
	Changes    *Changes `json:"-"`
	// Windows are the subscription windows the run last reported, nil
	// when the harness reports none.
	Windows *UsageWindows `json:"-"`
}

// MaxResultText bounds the final answer kept from a run.
const MaxResultText = 256 << 10

// Changes are the checkout's changes against the commit it was cloned at.
type Changes struct {
	BaseSHA string `json:"base_sha"`
	// Patch is `git diff --cached --binary BASE` after `git add -A`,
	// at most MaxPatchBytes; PatchTruncated tells it was cut.
	Patch          []byte        `json:"-"`
	PatchTruncated bool          `json:"patch_truncated,omitempty"`
	Files          []ChangedFile `json:"files"`
	// Contents are the changed files' new contents, for a pull request
	// built through the GitHub API. ContentsComplete is false when they
	// were too many or too large, or something could not be read: then
	// no pull request can be built from them.
	Contents         []FileContent `json:"-"`
	ContentsComplete bool          `json:"contents_complete"`
	// Error tells why the capture failed, if it did.
	Error string `json:"error,omitempty"`
}

// Capture bounds.
const (
	MaxPatchBytes    = 4 << 20
	MaxContentsBytes = 8 << 20
	MaxContentFiles  = 400
)

// ChangedFile is one entry of the change list.
type ChangedFile struct {
	Path      string `json:"path"`
	OldPath   string `json:"old_path,omitempty"`
	Status    string `json:"status"` // A, M, D, R, C, T
	Additions int    `json:"additions"`
	Deletions int    `json:"deletions"`
	Binary    bool   `json:"binary,omitempty"`
}

// FileContent is one changed file as it is now. Mode is a git tree mode:
// "100644", "100755" or "120000" (Data is then the link target).
type FileContent struct {
	Path    string `json:"path"`
	Mode    string `json:"mode,omitempty"`
	Deleted bool   `json:"deleted,omitempty"`
	Data    []byte `json:"data,omitempty"`
}

// Hooks let the office follow a run as it happens. Each may be nil; they
// are called from the run's own goroutine and must not block.
type Hooks struct {
	OnState func(state string)
	OnEvent func(Event)
}

// Run is a launched run.
type Run interface {
	ID() string
	// Done is closed once the run ended and its task and workspace are
	// gone (or their deletion was given up on and is being retried).
	Done() <-chan struct{}
	// Result is valid once Done is closed.
	Result() Result
}

// Executor runs one Spec at a time in AX.
type Executor interface {
	// Launch starts spec, or fails with ErrBusy or ErrNotReady (possibly
	// wrapped) when it cannot right now.
	Launch(ctx context.Context, spec Spec, clientIP string, hooks Hooks) (Run, error)
	// ActiveTask is the AX task of the run in progress, or "".
	ActiveTask() string
	// Cancel stops the run with that ID; it is idempotent.
	Cancel(id, reason string) error
	// Ready is false until the start-up reap succeeded.
	Ready() bool
}
