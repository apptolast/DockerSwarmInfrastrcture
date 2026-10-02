package office

import (
	"context"
	"slices"
	"time"
)

// fireWindow is how late a missed slot still fires (e.g. after a
// restart).
const fireWindow = time.Hour

// slotTime parses "HH:MM" (validated) into hours and minutes.
func slotTime(s string) (int, int, bool) {
	if !timeRe.MatchString(s) {
		return 0, 0, false
	}
	return int(s[0]-'0')*10 + int(s[1]-'0'), int(s[3]-'0')*10 + int(s[4]-'0'), true
}

// lastSlot is the most recent day of s.Days at s.Time (UTC) not after
// now.
func lastSlot(s Schedule, now time.Time) (time.Time, bool) {
	h, m, ok := slotTime(s.Time)
	if !ok || len(s.Days) == 0 {
		return time.Time{}, false
	}
	now = now.UTC()
	for d := 0; d <= 7; d++ {
		day := now.AddDate(0, 0, -d)
		slot := time.Date(day.Year(), day.Month(), day.Day(), h, m, 0, 0, time.UTC)
		if slices.Contains(s.Days, int(slot.Weekday())) && !slot.After(now) {
			return slot, true
		}
	}
	return time.Time{}, false
}

// nextSlot is the first day of s.Days at s.Time (UTC) after now.
func nextSlot(s Schedule, now time.Time) (time.Time, bool) {
	h, m, ok := slotTime(s.Time)
	if !ok || len(s.Days) == 0 {
		return time.Time{}, false
	}
	now = now.UTC()
	for d := 0; d <= 7; d++ {
		day := now.AddDate(0, 0, d)
		slot := time.Date(day.Year(), day.Month(), day.Day(), h, m, 0, 0, time.UTC)
		if slices.Contains(s.Days, int(slot.Weekday())) && slot.After(now) {
			return slot, true
		}
	}
	return time.Time{}, false
}

// dueSlot tells whether s should fire now, and for which slot: the last
// slot, at most an hour ago, strictly newer than the slot that last fired
// and than the schedule's last save (creation, edit or enabling: editing
// a schedule never fires a slot that had already passed), and not
// covered by a later run ("Ejecutar ahora").
func dueSlot(s Schedule, now time.Time) (time.Time, bool) {
	if !s.Enabled {
		return time.Time{}, false
	}
	slot, ok := lastSlot(s, now)
	if !ok || now.Sub(slot) > fireWindow {
		return time.Time{}, false
	}
	edited := s.Created
	if s.Edited != nil {
		edited = *s.Edited
	}
	if !slot.After(edited) || (s.LastSlot != nil && !slot.After(*s.LastSlot)) {
		return time.Time{}, false
	}
	if s.LastRun != nil && !s.LastRun.Before(slot) {
		return time.Time{}, false
	}
	return slot, true
}

// busyLocked tells whether the schedule's previous job or pipeline is
// still queued or running.
func (o *Office) scheduleBusyLocked(s *Schedule) bool {
	if s.LastRef == "" {
		return false
	}
	if j := o.jobs[s.LastRef]; j != nil {
		return !isTerminal(j.Status)
	}
	if p := o.pipelineLocked(s.LastRef); p != nil {
		return p.Status == PipelineRunning
	}
	return false
}

// fireLocked creates the schedule's job or pipeline.
func (o *Office) fireLocked(s *Schedule, clientIP string) error {
	src := Source{Type: "schedule", Ref: s.ID}
	var ref string
	switch s.Target.Type {
	case "job":
		j, err := o.enqueueLocked(newJob{
			ProjectID: s.Target.ProjectID, AgentID: s.Target.AgentID, Kind: s.Target.Kind, Prompt: s.Target.Prompt,
			Title: truncRunes(s.Name+" · "+deriveTitle(s.Target.Prompt), MaxTitleRunes), Branch: s.Target.Branch,
			Priority: defaultPriority, Source: src,
		})
		if err != nil {
			return err
		}
		ref = j.ID
	case "pipeline":
		tpl := templateByID(s.Target.Template)
		p := o.projectLocked(s.Target.ProjectID)
		if tpl == nil || tpl.ID == TplEval || p == nil || p.Archived {
			return fieldErr("target", "El destino del turno ya no es válido")
		}
		parts, err := o.participantsLocked(tpl, nil)
		if err != nil {
			return err
		}
		branch := s.Target.Branch
		if branch == "" {
			branch = p.Branch
		}
		pl := Pipeline{
			ID: "p-" + itoa(o.nextSeq("pipeline")), Template: tpl.ID,
			Title:     truncRunes(s.Name+" · "+deriveTitle(s.Target.Prompt), MaxTitleRunes),
			ProjectID: p.ID, Branch: branch, Task: s.Target.Prompt, Participants: parts, Status: PipelineRunning,
			MaxIterations: o.st.Settings.MaxIterations, Steps: []PipelineStep{}, Source: src,
			Priority: defaultPriority, Created: o.now(),
		}
		created, err := o.startPipelineLocked(pl, nil, clientIP)
		if err != nil {
			return err
		}
		ref = created.ID
	default:
		return fieldErr("target", "El destino del turno ya no es válido")
	}
	now := o.now()
	s.LastRun, s.LastRef = &now, ref
	o.invalidateLocked("schedules")
	o.auditAction("schedule.fire", clientIP, "schedule", s.ID, "ref", ref)
	return nil
}

// scheduleTick fires the schedules that are due.
func (o *Office) scheduleTick() {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed {
		return
	}
	now := o.now()
	fired := false
	for i := range o.st.Schedules {
		s := &o.st.Schedules[i]
		slot, due := dueSlot(*s, now)
		if !due || o.scheduleBusyLocked(s) {
			continue
		}
		if err := o.fireLocked(s, "turno"); err != nil {
			// Recorded as run so a broken target does not retry every 30 s.
			s.LastRun, s.LastRef = &now, ""
			o.audit.Warn("audit", "action", "schedule.fire", "schedule", s.ID, "error", err.Error())
			o.warnOnce("El turno «" + s.Name + "» no pudo crear su trabajo: " + err.Error())
		}
		s.LastSlot = &slot
		fired = true
	}
	if fired {
		o.saveLocked(true, true)
	}
}

// RunSchedule fires a schedule now ("Ejecutar ahora").
func (o *Office) RunSchedule(_ context.Context, id, clientIP string) (Schedule, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	s := o.scheduleLocked(id)
	if s == nil {
		return Schedule{}, notFound("No existe ese turno")
	}
	if o.scheduleBusyLocked(s) {
		return Schedule{}, conflict("El trabajo anterior de este turno aún no ha terminado")
	}
	if err := o.fireLocked(s, clientIP); err != nil {
		return Schedule{}, err
	}
	o.saveLocked(true, true)
	out := *s
	out.Days = slices.Clone(out.Days)
	return out, nil
}
