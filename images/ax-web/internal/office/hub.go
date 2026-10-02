package office

import (
	"sync"
)

// SubscriberBuffer is each subscriber's queue; one that falls this far
// behind is dropped (its stream ends and the browser reconnects).
const SubscriberBuffer = 256

// Message is one server-sent event for a subscriber: "office" deltas for
// everyone, "log" events for those watching that job.
type Message struct {
	Event string
	Seq   int64
	Data  []byte
}

// Subscription receives Messages on C until it is closed: by
// Unsubscribe, by the hub when it falls behind, or at shutdown.
type Subscription struct {
	C   <-chan Message
	ch  chan Message
	job string
}

// Job is the job this subscriber watches, or "".
func (s *Subscription) Job() string { return s.job }

type hub struct {
	mu     sync.Mutex
	subs   map[*Subscription]struct{}
	closed bool
}

func newHub() *hub { return &hub{subs: map[*Subscription]struct{}{}} }

func (h *hub) subscribe(job string) *Subscription {
	ch := make(chan Message, SubscriberBuffer)
	s := &Subscription{C: ch, ch: ch, job: job}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		close(ch)
		return s
	}
	h.subs[s] = struct{}{}
	return s
}

func (h *hub) unsubscribe(s *Subscription) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.subs[s]; ok {
		delete(h.subs, s)
		close(s.ch)
	}
}

// publish sends m to every subscriber (job == "") or to those watching
// job, never blocking: a full subscriber is dropped.
func (h *hub) publish(m Message, job string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for s := range h.subs {
		if job != "" && s.job != job {
			continue
		}
		select {
		case s.ch <- m:
		default:
			delete(h.subs, s)
			close(s.ch)
		}
	}
}

func (h *hub) count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.subs)
}

func (h *hub) close() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.closed = true
	for s := range h.subs {
		delete(h.subs, s)
		close(s.ch)
	}
}
