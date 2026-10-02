package office

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestHubFiltersAndDropsSlowSubscribers(t *testing.T) {
	h := newHub()
	all := h.subscribe("")
	watcher := h.subscribe("j1")
	slow := h.subscribe("j2")
	h.publish(Message{Event: "log", Seq: 1}, "j1")
	if m := <-watcher.C; m.Seq != 1 {
		t.Fatal(m)
	}
	select {
	case m := <-all.C:
		t.Fatalf("unwatched job reached %+v", m)
	default:
	}
	for i := range SubscriberBuffer + 1 {
		h.publish(Message{Event: "office", Seq: int64(i)}, "")
		<-all.C
		<-watcher.C
	}
	// slow never read: it was dropped and its channel closed.
	n := 0
	for range slow.C {
		n++
	}
	if n != SubscriberBuffer || h.count() != 2 {
		t.Fatalf("slow got %d, subscribers %d", n, h.count())
	}
	h.unsubscribe(all)
	h.unsubscribe(all)
	h.close()
	if _, ok := <-watcher.C; ok {
		t.Fatal("open after close")
	}
	if _, ok := <-h.subscribe("").C; ok {
		t.Fatal("subscribed after close")
	}
}

func TestDeltaCoalescing(t *testing.T) {
	h := newHarness(t)
	sub := h.o.Subscribe("")
	defer h.o.Unsubscribe(sub)
	h.o.flushDelta()
	drain := func() []Delta {
		var out []Delta
		for {
			select {
			case m := <-sub.C:
				var d Delta
				json.Unmarshal(m.Data, &d)
				out = append(out, d)
			case <-time.After(50 * time.Millisecond):
				return out
			}
		}
	}
	drain()
	a := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "uno"})
	b := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "dos"})
	h.o.UpdateSettings(SettingsInput{OfficeName: ptr("Otra oficina")}, "x")
	h.o.flushDelta()
	h.o.flushDelta()
	ds := drain()
	if len(ds) != 1 || len(ds[0].Jobs) != 2 || ds[0].Counts.Queued != 2 || len(ds[0].Queue) != 2 ||
		!strings.Contains(strings.Join(ds[0].Invalidate, ","), "settings") || ds[0].Rev != h.o.Rev() {
		t.Fatalf("%+v", ds)
	}
	if err := h.o.CancelJob(a.ID, "x"); err != nil {
		t.Fatal(err)
	}
	h.o.DeleteJob(a.ID, "x")
	h.o.flushDelta()
	ds = drain()
	if len(ds) != 1 || len(ds[0].Removed) != 1 || ds[0].Removed[0] != a.ID || ds[0].Queue[0] != b.ID {
		t.Fatalf("%+v", ds)
	}
}
