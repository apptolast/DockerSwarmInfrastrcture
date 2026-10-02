package office

import (
	"context"
	"testing"
	"time"
)

func at(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestScheduleSlots(t *testing.T) {
	// 2026-10-02 is a Friday (5).
	cases := []struct {
		name       string
		days       []int
		time, now  string
		last, next string
	}{
		{"today, passed", []int{5}, "09:00", "2026-10-02T10:00:00Z", "2026-10-02T09:00:00Z", "2026-10-09T09:00:00Z"},
		{"today, exactly now", []int{5}, "10:00", "2026-10-02T10:00:00Z", "2026-10-02T10:00:00Z", "2026-10-09T10:00:00Z"},
		{"today, later", []int{5}, "23:30", "2026-10-02T10:00:00Z", "2026-09-25T23:30:00Z", "2026-10-02T23:30:00Z"},
		{"around midnight", []int{4, 5}, "23:50", "2026-10-02T00:05:00Z", "2026-10-01T23:50:00Z", "2026-10-02T23:50:00Z"},
		{"week wrap", []int{0}, "00:10", "2026-10-03T23:59:00Z", "2026-09-27T00:10:00Z", "2026-10-04T00:10:00Z"},
		{"sunday after midnight", []int{6}, "23:30", "2026-10-04T00:15:00Z", "2026-10-03T23:30:00Z", "2026-10-10T23:30:00Z"},
		{"new year", []int{4}, "23:00", "2027-01-01T00:30:00Z", "2026-12-31T23:00:00Z", "2027-01-07T23:00:00Z"},
	}
	for _, c := range cases {
		s := Schedule{Days: c.days, Time: c.time, Enabled: true}
		last, ok := lastSlot(s, at(c.now))
		if !ok || !last.Equal(at(c.last)) {
			t.Errorf("%s: last %v", c.name, last)
		}
		next, ok := nextSlot(s, at(c.now))
		if !ok || !next.Equal(at(c.next)) {
			t.Errorf("%s: next %v", c.name, next)
		}
	}
}

func TestDueSlot(t *testing.T) {
	created := at("2026-09-01T00:00:00Z")
	s := Schedule{Days: []int{4, 5}, Time: "23:50", Enabled: true, Created: created}
	if _, due := dueSlot(s, at("2026-10-02T00:30:00Z")); !due {
		t.Fatal("slot 40 minutes ago not due")
	}
	if _, due := dueSlot(s, at("2026-10-02T00:51:00Z")); due {
		t.Fatal("slot more than an hour ago due")
	}
	last := at("2026-10-01T23:50:30Z")
	s.LastRun = &last
	if _, due := dueSlot(s, at("2026-10-02T00:30:00Z")); due {
		t.Fatal("fired twice for one slot")
	}
	if _, due := dueSlot(s, at("2026-10-02T23:50:00Z")); !due {
		t.Fatal("the next day's slot not due")
	}
	s.Enabled = false
	if _, due := dueSlot(s, at("2026-10-02T23:50:00Z")); due {
		t.Fatal("disabled schedule due")
	}
	// A schedule created after a slot does not fire for it.
	n := Schedule{Days: []int{5}, Time: "09:30", Enabled: true, Created: at("2026-10-02T09:45:00Z")}
	if _, due := dueSlot(n, at("2026-10-02T10:00:00Z")); due {
		t.Fatal("fired for a slot before its creation")
	}
}

func TestScheduleTickFiresOnce(t *testing.T) {
	h := newHarness(t)
	h.clock.Set(at("2026-10-01T12:00:00Z"))
	s, err := h.o.SaveSchedule(ScheduleInput{Name: "Revisión nocturna", Days: []int{4, 5}, Time: "23:50",
		Target: ScheduleTarget{Type: "job", ProjectID: "web", AgentID: "grace", Kind: KindReview, Prompt: "Revisa main"}}, "x")
	if err != nil || s.ID != "revision-nocturna" || !s.Enabled {
		t.Fatalf("%+v %v", s, err)
	}
	if snap := h.o.Snapshot(); snap.Schedules[0].NextRun == nil || !snap.Schedules[0].NextRun.Equal(at("2026-10-01T23:50:00Z")) {
		t.Fatalf("%+v", snap.Schedules[0])
	}
	h.o.scheduleTick()
	if len(h.o.Snapshot().Queue) != 0 {
		t.Fatal("fired early")
	}
	h.clock.Set(at("2026-10-02T00:10:00Z"))
	h.o.scheduleTick()
	h.o.scheduleTick()
	q := h.o.Snapshot().Queue
	if len(q) != 1 {
		t.Fatalf("queue %v", q)
	}
	j, _ := h.o.Job(q[0])
	if j.Source.Type != "schedule" || j.Source.Ref != s.ID || j.Kind != KindReview {
		t.Fatalf("%+v", j)
	}
	// The next slot is skipped while the previous job is still queued,
	// and fires once it finished (within the hour).
	h.clock.Set(at("2026-10-02T23:51:00Z"))
	h.o.scheduleTick()
	if len(h.o.Snapshot().Queue) != 1 {
		t.Fatal("fired while the previous job was queued")
	}
	h.runNext(t, exited(0, "VEREDICTO: APROBADO"))
	h.o.scheduleTick()
	if len(h.o.Snapshot().Queue) != 1 {
		t.Fatal("did not fire after the previous job ended")
	}
	// Ejecutar ahora: refused while busy, then fires a pipeline target.
	if _, err := h.o.RunSchedule(context.Background(), s.ID, "x"); IsStatus(err) != 409 {
		t.Fatalf("run now while busy: %v", err)
	}
	s2, err := h.o.SaveSchedule(ScheduleInput{Name: "Equipo semanal", Days: []int{1}, Time: "08:00",
		Target: ScheduleTarget{Type: "pipeline", ProjectID: "web", Template: TplTeam, Prompt: "Mejora los tests"}}, "x")
	if err != nil {
		t.Fatal(err)
	}
	ran, err := h.o.RunSchedule(context.Background(), s2.ID, "x")
	if err != nil || ran.LastRef == "" || ran.LastRun == nil {
		t.Fatalf("%+v %v", ran, err)
	}
	p, _, err := h.o.Pipeline(ran.LastRef)
	if err != nil || p.Source.Type != "schedule" || p.Template != TplTeam {
		t.Fatalf("%+v %v", p, err)
	}
	// Updates keep the id and history; validation names fields.
	upd, err := h.o.SaveSchedule(ScheduleInput{ID: s2.ID, Name: "Equipo semanal", Enabled: ptr(false), Days: []int{1, 3},
		Time: "08:30", Target: s2.Target}, "x")
	if err != nil || upd.ID != s2.ID || upd.Enabled || upd.LastRef != ran.LastRef {
		t.Fatalf("%+v %v", upd, err)
	}
	for field, in := range map[string]ScheduleInput{
		"target.agent_id": {Name: "x", Days: []int{1}, Time: "08:00", Target: ScheduleTarget{Type: "job", ProjectID: "web", AgentID: "nadie", Kind: KindAsk, Prompt: "x"}},
		"target.kind":     {Name: "x", Days: []int{1}, Time: "08:00", Target: ScheduleTarget{Type: "job", ProjectID: "web", AgentID: "ada", Kind: KindChange, Prompt: "x"}},
		"target.template": {Name: "x", Days: []int{1}, Time: "08:00", Target: ScheduleTarget{Type: "pipeline", ProjectID: "web", Template: TplEval, Prompt: "x"}},
		"target.type":     {Name: "x", Days: []int{1}, Time: "08:00", Target: ScheduleTarget{Type: "otro", ProjectID: "web", Prompt: "x"}},
		"days":            {Name: "x", Time: "08:00", Target: ScheduleTarget{Type: "job", ProjectID: "web", AgentID: "ada", Kind: KindAsk, Prompt: "x"}},
	} {
		var fe *FieldError
		if _, err := h.o.SaveSchedule(in, "x"); !asField(err, &fe) || fe.Field != field {
			t.Errorf("%s: %v", field, err)
		}
	}
	if err := h.o.DeleteSchedule(s2.ID, "x"); err != nil {
		t.Fatal(err)
	}
	if err := h.o.DeleteSchedule(s2.ID, "x"); IsStatus(err) != 404 {
		t.Fatal(err)
	}
}

// A slot fires once: never again once recorded as fired, and never when
// it had already passed when the schedule was last saved (edited or
// enabled).
func TestDueSlotAfterEditsAndFires(t *testing.T) {
	created := at("2026-09-01T00:00:00Z")
	fired := at("2026-10-01T23:50:00Z")
	s := Schedule{Days: []int{4, 5}, Time: "23:50", Enabled: true, Created: created, LastSlot: &fired}
	if _, due := dueSlot(s, at("2026-10-02T00:30:00Z")); due {
		t.Fatal("the slot that fired fired again")
	}
	if slot, due := dueSlot(s, at("2026-10-02T23:55:00Z")); !due || !slot.Equal(at("2026-10-02T23:50:00Z")) {
		t.Fatal("the next slot did not fire")
	}
	edited := at("2026-10-02T00:20:00Z")
	e := Schedule{Days: []int{4, 5}, Time: "23:50", Enabled: true, Created: created, Edited: &edited}
	if _, due := dueSlot(e, at("2026-10-02T00:30:00Z")); due {
		t.Fatal("a slot before the last edit fired")
	}
	exactly := at("2026-10-01T23:50:00Z")
	e.Edited = &exactly
	if _, due := dueSlot(e, at("2026-10-02T00:30:00Z")); due {
		t.Fatal("a slot at the very time of the edit fired")
	}
}

func TestEnablingAScheduleDoesNotFireAPassedSlot(t *testing.T) {
	h := newHarness(t)
	h.clock.Set(at("2026-10-02T08:00:00Z"))
	in := ScheduleInput{Name: "Diario", Enabled: ptr(false), Days: []int{0, 1, 2, 3, 4, 5, 6}, Time: "09:00",
		Target: ScheduleTarget{Type: "job", ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "¿Qué hay nuevo?"}}
	s, err := h.o.SaveSchedule(in, "x")
	if err != nil {
		t.Fatal(err)
	}
	// Enabled 10 minutes after today's slot: it waits for tomorrow's.
	h.clock.Set(at("2026-10-02T09:10:00Z"))
	in.ID, in.Enabled = s.ID, ptr(true)
	if s, err = h.o.SaveSchedule(in, "x"); err != nil || s.Edited == nil || !s.Edited.Equal(at("2026-10-02T09:10:00Z")) {
		t.Fatalf("%+v %v", s, err)
	}
	h.o.scheduleTick()
	if len(h.o.Snapshot().Queue) != 0 {
		t.Fatal("fired for a slot before it was enabled")
	}
	h.clock.Set(at("2026-10-03T09:00:30Z"))
	h.o.scheduleTick()
	snap := h.o.Snapshot()
	if len(snap.Queue) != 1 || snap.Schedules[0].LastSlot == nil || !snap.Schedules[0].LastSlot.Equal(at("2026-10-03T09:00:00Z")) {
		t.Fatalf("%+v", snap.Schedules[0])
	}
	// Saving it again keeps the record of the slot that fired.
	if s, err = h.o.SaveSchedule(in, "x"); err != nil || s.LastSlot == nil {
		t.Fatalf("%+v %v", s, err)
	}
}
