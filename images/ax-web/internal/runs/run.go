package runs

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// Run is one execution; it implements harness.Run. Its identity is its
// task's name, the Spec's ID.
type Run struct {
	id        string
	workspace string
	created   time.Time
	hooks     harness.Hooks
	now       func() time.Time
	done      chan struct{}

	// The hooks are called, in order, from the run's own delivery
	// goroutine, so a Cancel from inside a hook or from a goroutine
	// holding the caller's locks never waits on them.
	qmu       sync.Mutex
	queue     []delivery
	qclosed   bool
	wake      chan struct{}
	delivered chan struct{}

	mu           sync.Mutex
	spec         harness.Spec
	command      []string
	state        string
	cancelled    bool
	exited       bool // the agent process ended, or was given up on
	touched      bool // the manager asked AX to create its objects
	agentStarted bool
	processID    string
	proc         ProcessClient
	prepCancel   context.CancelFunc
	streamCancel context.CancelFunc
	base         string
	final        harness.Final
	result       harness.Result
	finished     time.Time
}

type delivery struct {
	state string // an OnState call when not empty
	event harness.Event
}

var _ harness.Run = (*Run)(nil)

func newRun(spec harness.Spec, command []string, hooks harness.Hooks, created time.Time,
	now func() time.Time) *Run {
	return &Run{
		id: spec.ID, workspace: WorkspacePrefix + spec.ID, created: created, hooks: hooks, now: now,
		done: make(chan struct{}), wake: make(chan struct{}, 1), delivered: make(chan struct{}),
		spec: spec, command: command, state: harness.StatePreparing,
	}
}

// ID is the AX task's name.
func (r *Run) ID() string { return r.id }

// Done is closed once the run is over and cleaned up (or its cleanup is
// being retried while the manager stops), and every hook was called.
func (r *Run) Done() <-chan struct{} { return r.done }

// Result is how the run ended, valid once Done is closed.
func (r *Run) Result() harness.Result {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.result
}

// currentState is the state the run is in.
func (r *Run) currentState() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.state
}

func (r *Run) isCancelled() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.cancelled
}

// setState changes the state and reports it, atomically with respect to
// Cancel's own change.
func (r *Run) setState(state string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.setStateLocked(state)
}

func (r *Run) setStateLocked(state string) {
	r.state = state
	r.push(delivery{state: state})
}

// emit queues an event for the hooks, stamped with the time. Every text
// field is bounded here, whatever produced the event: the agent's output
// is untrusted and the office keeps thousands of events in memory.
func (r *Run) emit(ev harness.Event) {
	if ev.Time.IsZero() {
		ev.Time = r.now().UTC()
	}
	r.push(delivery{event: boundEvent(ev)})
}

// maxEventName bounds an event's model and tool names.
const maxEventName = 128

// boundEvent clips an event's texts to the harness bounds (UTF-8 safe).
func boundEvent(ev harness.Event) harness.Event {
	if len(ev.Text) > harness.MaxEventText {
		ev.Text = harness.Clip(ev.Text, harness.MaxEventText)
	}
	if len(ev.Input) > harness.MaxEventInput {
		ev.Input = harness.Clip(ev.Input, harness.MaxEventInput)
	}
	if len(ev.Model) > maxEventName {
		ev.Model = harness.Clip(ev.Model, maxEventName)
	}
	if len(ev.Tool) > maxEventName {
		ev.Tool = harness.Clip(ev.Tool, maxEventName)
	}
	return ev
}

// system emits a line of the run manager's own, in Spanish.
func (r *Run) system(format string, args ...any) {
	r.emit(harness.Event{Kind: harness.EventSystem,
		Text: harness.Clip(fmt.Sprintf(format, args...), harness.MaxEventText)})
}

// warn emits a warning that does not change the outcome.
func (r *Run) warn(format string, args ...any) {
	r.system("Aviso: "+format, args...)
}

func (r *Run) push(d delivery) {
	r.qmu.Lock()
	if !r.qclosed {
		r.queue = append(r.queue, d)
	}
	r.qmu.Unlock()
	select {
	case r.wake <- struct{}{}:
	default:
	}
}

// deliver calls the hooks until the queue is closed and drained.
func (r *Run) deliver() {
	defer close(r.delivered)
	for {
		r.qmu.Lock()
		items, closed := r.queue, r.qclosed
		r.queue = nil
		r.qmu.Unlock()
		for _, d := range items {
			switch {
			case d.state != "":
				if r.hooks.OnState != nil {
					r.hooks.OnState(d.state)
				}
			case r.hooks.OnEvent != nil:
				r.hooks.OnEvent(d.event)
			}
		}
		if len(items) > 0 {
			continue
		}
		if closed {
			return
		}
		<-r.wake
	}
}

// closeEvents ends the queue and waits until every hook was called.
func (r *Run) closeEvents() {
	r.qmu.Lock()
	r.qclosed = true
	r.qmu.Unlock()
	select {
	case r.wake <- struct{}{}:
	default:
	}
	<-r.delivered
}

// Bounds of the agent's stderr in the timeline.
const (
	maxStderrLine   = 1 << 10
	maxStderrEvents = 2000
)

// stderrLines turns the agent's stderr into bounded events, one per line.
type stderrLines struct {
	pending   []byte
	count     int
	truncated bool
}

func (s *stderrLines) feed(data []byte, emit func(harness.Event)) {
	for len(data) > 0 {
		i := bytes.IndexByte(data, '\n')
		piece := data
		if i >= 0 {
			piece, data = data[:i], data[i+1:]
		} else {
			data = nil
		}
		// One byte past the bound tells Clip the line was longer.
		if room := maxStderrLine + 1 - len(s.pending); room > 0 {
			s.pending = append(s.pending, piece[:min(len(piece), room)]...)
		}
		if i >= 0 {
			s.line(emit)
		}
	}
}

func (s *stderrLines) flush(emit func(harness.Event)) {
	if len(s.pending) > 0 {
		s.line(emit)
	}
}

func (s *stderrLines) line(emit func(harness.Event)) {
	text := strings.TrimRight(string(s.pending), "\r")
	s.pending = s.pending[:0]
	if strings.TrimSpace(text) == "" {
		return
	}
	if s.count >= maxStderrEvents {
		if !s.truncated {
			s.truncated = true
			emit(harness.Event{Kind: harness.EventSystem, Text: "stderr truncado"})
		}
		return
	}
	s.count++
	emit(harness.Event{Kind: harness.EventStderr, Text: harness.Clip(text, maxStderrLine)})
}
