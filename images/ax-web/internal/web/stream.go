package web

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"apptolast.com/ax-web/internal/office"
)

// maxBatch bounds the messages written before one flush.
const maxBatch = 64

// stream is the browser's only event stream (SSE): "hello" with the
// state's revision, then the coalesced "office" deltas and, with ?job=,
// that job's stored events followed by its live ones as "log" (each with
// its sequence as id, so a reconnecting EventSource resumes after the
// last one). A comment every PingInterval keeps proxies from closing it;
// it ends at StreamMax or shutdown and EventSource reconnects.
func (s *Server) stream(w http.ResponseWriter, r *http.Request) {
	job := r.URL.Query().Get("job")
	if job != "" {
		if !office.ValidJobID(job) {
			writeError(w, http.StatusBadRequest, "identificador no válido")
			return
		}
		if _, err := s.Office.Job(job); err != nil {
			s.fail(w, err)
			return
		}
	}
	var after int64
	if last := r.Header.Get("Last-Event-ID"); last != "" && job != "" {
		if n, err := strconv.ParseInt(last, 10, 64); err == nil && n > 0 {
			after = n
		}
	}
	sub := s.Office.Subscribe(job)
	defer s.Office.Unsubscribe(sub)

	rc := http.NewResponseController(w)
	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	var buf bytes.Buffer
	buf.WriteString("retry: 5000\n\n")
	hello, _ := json.Marshal(map[string]int64{"rev": s.Office.Rev()})
	fmt.Fprintf(&buf, "event: hello\ndata: %s\n\n", hello)
	last := after
	if job != "" {
		evs, err := s.Office.TailEvents(job, after, StreamReplay)
		if err == nil {
			for _, ev := range evs {
				data, _ := json.Marshal(ev)
				fmt.Fprintf(&buf, "id: %d\nevent: log\ndata: %s\n\n", ev.Seq, data)
				last = max(last, ev.Seq)
			}
		}
	}
	if _, err := w.Write(buf.Bytes()); err != nil || rc.Flush() != nil {
		return
	}
	ping := s.PingInterval
	if ping <= 0 {
		ping = 15 * time.Second
	}
	limit := s.StreamMax
	if limit <= 0 {
		limit = DefaultStreamMax
	}
	pings := time.NewTicker(ping)
	defer pings.Stop()
	end := time.NewTimer(limit)
	defer end.Stop()
	write := func(m office.Message) {
		if m.Event == "log" {
			if m.Seq <= last {
				return
			}
			last = m.Seq
			fmt.Fprintf(&buf, "id: %d\nevent: log\ndata: %s\n\n", m.Seq, m.Data)
			return
		}
		fmt.Fprintf(&buf, "event: %s\ndata: %s\n\n", m.Event, m.Data)
	}
	for {
		buf.Reset()
		select {
		case m, ok := <-sub.C:
			if !ok {
				return
			}
			write(m)
			for range maxBatch {
				select {
				case m, ok = <-sub.C:
				default:
					ok = false
					m = office.Message{}
				}
				if !ok {
					break
				}
				write(m)
			}
		case <-pings.C:
			buf.WriteString(": ping\n\n")
		case <-end.C:
			return
		case <-r.Context().Done():
			return
		case <-s.Stop:
			return
		}
		if buf.Len() == 0 {
			continue
		}
		if _, err := w.Write(buf.Bytes()); err != nil || rc.Flush() != nil {
			return
		}
	}
}
