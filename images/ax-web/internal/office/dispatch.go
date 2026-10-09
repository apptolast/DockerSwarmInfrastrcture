package office

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"

	"apptolast.com/ax-web/internal/harness"
	"apptolast.com/ax-web/internal/runs"
)

var busyTask = regexp.MustCompile(`\b((?:tarea|web)-[A-Za-z0-9._-]+)`)

// waitingMessage explains why a launch was refused as busy.
func waitingMessage(err error, blocking string) string {
	if blocking == "" {
		if m := busyTask.FindStringSubmatch(err.Error()); m != nil {
			blocking = m[1]
		}
	}
	if blocking != "" {
		return "El sandbox está ocupado (" + blocking + ")"
	}
	return "El sandbox está ocupado"
}

// nextQueuedLocked is the queued job to start: highest priority, then
// oldest.
func (o *Office) nextQueuedLocked() *Job {
	var q []*Job
	for _, id := range o.order {
		if j := o.jobs[id]; j.Status == StatusQueued {
			q = append(q, j)
		}
	}
	if len(q) == 0 {
		return nil
	}
	sortQueue(q)
	return q[0]
}

// failJobLocked ends a job that never ran.
func (o *Office) failJobLocked(j *Job, msg string) {
	now := o.now()
	j.Status, j.Message, j.Finished, j.Waiting, j.Activity = StatusFailed, msg, &now, "", ""
	o.touchJobLocked(j.ID)
	o.invalidateLocked("metrics")
	if j.PipelineID != "" {
		o.advancePipelineLocked(j.PipelineID)
	}
	o.saveLocked(true, true)
}

// buildSpecLocked prepares a job's run: the effective settings of its
// agent now, the composed prompt and the patch to apply.
func (o *Office) buildSpecLocked(j *Job) (harness.Spec, error) {
	p := o.projectLocked(j.ProjectID)
	if p == nil {
		return harness.Spec{}, errors.New("El proyecto ya no existe")
	}
	a := o.agentLocked(j.AgentID)
	if a == nil {
		return harness.Spec{}, errors.New("El agente ya no existe")
	}
	eff := effective(a, j.Overrides)
	if err := validateBehaviour("", &eff, o.cfg.Limits, a.DisallowedTools); err != nil {
		return harness.Spec{}, fmt.Errorf("La configuración del agente ya no es válida: %v", err)
	}
	if j.Kind == KindChange && eff.Mode != harness.ModeFull {
		return harness.Spec{}, errors.New("Un trabajo de tipo cambio necesita modo completo")
	}
	j.Agent = eff
	prompt, err := o.store.readJobFile(j.ID, filePrompt, maxInternalPrompt)
	if err != nil || len(prompt) == 0 {
		return harness.Spec{}, errors.New("No se encontró el encargo del trabajo")
	}
	in := promptInput{
		OfficeName: o.st.Settings.OfficeName, JobID: j.ID, AgentName: a.Name, AgentRole: a.Role,
		Persona: a.SystemPrompt, Harness: eff.Harness, Mode: eff.Mode, Project: *p, Branch: j.Branch,
		Kind: j.Kind, Prompt: string(prompt), Memory: o.memoryForPromptLocked(p),
	}
	for _, cid := range j.Context {
		if c := o.contextItemLocked(cid); c != nil {
			in.Context = append(in.Context, *c)
		}
	}
	var patch []byte
	if j.ApplyFrom != "" {
		src := o.jobs[j.ApplyFrom]
		if src == nil {
			return harness.Spec{}, fmt.Errorf("No existe el trabajo %s cuyos cambios había que aplicar", j.ApplyFrom)
		}
		if src.Changes != nil && src.Changes.Files > 0 {
			if src.Changes.PatchTruncated {
				return harness.Spec{}, fmt.Errorf("El parche de %s está truncado y no se puede aplicar", src.ID)
			}
			patch, err = o.store.readJobFile(src.ID, filePatch, harness.MaxPatchBytes)
			if err != nil || len(patch) == 0 {
				return harness.Spec{}, fmt.Errorf("No se encontró el parche de %s", src.ID)
			}
			var files []harness.ChangedFile
			_, _ = o.store.readJobJSON(src.ID, fileChanges, jobFileMax, &files)
			in.Applied = &appliedChanges{JobID: src.ID, Files: files, Patch: patch}
		}
	}
	var ext external
	if ok, _ := o.store.readJobJSON(j.ID, fileExternal, jobFileMax, &ext); ok {
		in.External = &ext
	}
	final := composePrompt(in)
	persona := strings.TrimSpace(a.SystemPrompt)
	if err := o.store.writeJobFile(j.ID, fileFinalPrompt, []byte(final)); err != nil {
		return harness.Spec{}, errors.New("No se pudo guardar el prompt final")
	}
	if err := o.store.writeJobFile(j.ID, fileSystemPrompt, []byte(persona)); err != nil {
		return harness.Spec{}, errors.New("No se pudo guardar el prompt del agente")
	}
	spec := harness.Spec{
		ID: "web-" + j.ID, Repo: p.Repo, Branch: j.Branch, Prompt: final, Harness: eff.Harness,
		Model: eff.Model, FallbackModel: eff.FallbackModel, Effort: eff.Effort, Mode: eff.Mode,
		Timeout: time.Duration(eff.TimeoutMinutes) * time.Minute, ApplyPatch: patch,
		CaptureChanges: j.Kind == KindChange,
	}
	if eff.Harness == harness.Claude {
		spec.AppendSystemPrompt = persona
		spec.MaxTurns = eff.MaxTurns
		if eff.Mode == harness.ModeFull {
			spec.DisallowedTools = slices.Clone(a.DisallowedTools)
		}
	}
	if err := harness.Validate(spec); err != nil {
		return harness.Spec{}, fmt.Errorf("La ejecución no es válida: %v", err)
	}
	return spec, nil
}

// memoryForPromptLocked lists a project's active lessons, newest first.
func (o *Office) memoryForPromptLocked(p *Project) []string {
	items := slices.Clone(p.Memory)
	sort.SliceStable(items, func(a, b int) bool { return items[a].Created.After(items[b].Created) })
	var out []string
	for _, m := range items {
		if m.Active && len(out) < o.st.Settings.MaxLessonsInPrompt {
			out = append(out, m.Text)
		}
	}
	return out
}

// contextItemLocked is a previous job as context: its result (or
// summary) under its step name.
func (o *Office) contextItemLocked(id string) *contextItem {
	j := o.jobs[id]
	if j == nil {
		return nil
	}
	step := j.Title
	if j.PipelineID != "" {
		if p := o.pipelineLocked(j.PipelineID); p != nil && j.Step >= 0 && j.Step < len(p.Steps) {
			step = p.Steps[j.Step].Name
		}
	}
	res, _ := o.store.readJobFile(id, fileResult, harness.MaxResultText)
	t := strings.TrimSpace(string(res))
	if t == "" {
		t = j.Summary
	}
	if t == "" {
		t = "(sin resultado: " + j.Status + ")"
	}
	return &contextItem{Step: step, Agent: j.Agent.Name, Text: t}
}

// credentialProblemLocked tells why a harness cannot run now: its
// credential is missing or empty. Checked before anything is created in
// AX, at every attempt (a Secret may change while a job waits).
func (o *Office) credentialProblemLocked(h string) string {
	if h == harness.Codex {
		if o.creds.currentCodex() == nil {
			return "Codex no está configurado en la Oficina"
		}
		return ""
	}
	if _, err := o.creds.claude(); err != nil {
		return "Claude no está configurado en la Oficina"
	}
	return ""
}

// dispatchOnce starts the next queued job if the sandbox is free. The
// job's spec is prepared once (prompt files written, "envía el encargo"
// logged) and reused while the sandbox stays busy.
func (o *Office) dispatchOnce(ctx context.Context) {
	if !o.exec.Ready() || o.exec.ActiveTask() != "" {
		return
	}
	o.mu.Lock()
	if o.closed || o.st.Settings.QueuePaused || o.launching != "" || o.active != nil || o.now().Before(o.busyUntil) {
		o.mu.Unlock()
		return
	}
	j := o.nextQueuedLocked()
	if j == nil {
		o.mu.Unlock()
		return
	}
	if until, msg := usageHold(o.usage, o.usageAt, o.now()); !until.IsZero() {
		j.Waiting, o.usageWaiting = msg, true
		o.busyUntil = until
		o.mu.Unlock()
		time.AfterFunc(until.Sub(o.now()), o.kick)
		return
	}
	if o.usageWaiting {
		j.Waiting, o.usageWaiting = "", false
	}
	id := j.ID
	pr := o.prepared
	fresh := pr == nil || pr.jobID != id
	if fresh {
		o.prepared = nil
		spec, err := o.buildSpecLocked(j)
		if err != nil {
			o.failJobLocked(j, err.Error())
			o.mu.Unlock()
			o.kick()
			return
		}
		pr = &preparedRun{jobID: id, spec: spec, seq: lastSeq(o.store, id)}
		o.prepared = pr
	}
	spec := pr.spec
	if msg := o.credentialProblemLocked(spec.Harness); msg != "" {
		o.prepared = nil
		o.failJobLocked(j, msg)
		o.mu.Unlock()
		o.kick()
		return
	}
	ar := &activeRun{jobID: id, task: spec.ID, since: o.now(), lastEvent: o.now(), seq: pr.seq}
	ar.log, _ = o.store.openEventLog(id)
	o.launching, o.cancelReq = id, false
	o.active = ar
	if fresh {
		o.appendEventLocked(ar, harness.Event{Kind: harness.EventSystem, Text: fmt.Sprintf(
			"La Oficina envía el encargo al sandbox: %s, modelo %s, esfuerzo %s, modo %s.",
			spec.Harness, orDefault(spec.Model), orDefault(spec.Effort), spec.Mode)})
		pr.seq = ar.seq
		o.refreshJobBytesLocked(id)
	}
	o.mu.Unlock()

	hooks := harness.Hooks{
		OnState: func(s string) { o.onState(id, s) },
		OnEvent: func(ev harness.Event) { o.onEvent(id, ev) },
	}
	lctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), launchTimeout)
	run, err := o.exec.Launch(lctx, spec, "oficina", hooks)
	cancel()

	o.mu.Lock()
	o.launching = ""
	j = o.jobs[id]
	if err != nil {
		o.active = nil
		ar.log.close()
		// ErrBlackout (a "not now" of the run manager) matches ErrNotReady.
		if errors.Is(err, harness.ErrBusy) || errors.Is(err, harness.ErrNotReady) {
			blocking := o.axState.Blocking
			o.busyUntil = o.now().Add(busyBackoff)
			if j != nil {
				if j.Status != StatusQueued {
					j.Status, j.Activity = StatusQueued, ""
					o.mirrorStepLocked(j)
				}
				switch {
				case errors.Is(err, runs.ErrBlackout):
					j.Waiting = "Esperando a que termine la ventana del Observatorio"
				case errors.Is(err, harness.ErrBusy):
					j.Waiting = waitingMessage(err, blocking)
				default:
					j.Waiting = "El ejecutor aún no está listo o AX no responde; se reintentará"
				}
				o.touchJobLocked(id)
				if o.cancelReq {
					now := o.now()
					j.Status, j.Finished, j.Message, j.Waiting = StatusCancelled, &now, "Cancelado antes de empezar", ""
					if o.prepared != nil && o.prepared.jobID == id {
						o.prepared = nil
					}
					if j.PipelineID != "" {
						o.advancePipelineLocked(j.PipelineID)
					}
					o.saveLocked(true, true)
				}
			}
			o.mu.Unlock()
			if errors.Is(err, harness.ErrBusy) {
				o.refreshProbe(ctx, true)
			}
			return
		}
		o.prepared = nil
		if j != nil {
			o.failJobLocked(j, "No se pudo lanzar: "+err.Error())
		}
		o.mu.Unlock()
		o.kick()
		return
	}
	o.prepared = nil
	ar.run = run
	if t := run.ID(); t != "" {
		ar.task = t
	}
	if j != nil {
		now := o.now()
		j.Task, j.Started, j.Waiting = ar.task, &now, ""
		if j.Status == StatusQueued {
			j.Status = StatusPreparing
			o.mirrorStepLocked(j)
		}
		o.touchJobLocked(id)
		o.saveLocked(false, true)
	}
	cancelNow := o.cancelReq
	o.cancelReq = false
	o.finalizers.Add(1)
	o.mu.Unlock()
	o.auditAction("job.launch", "oficina", "job", id, "task", ar.task, "harness", spec.Harness,
		"model", spec.Model, "mode", spec.Mode)
	if cancelNow {
		_ = o.exec.Cancel(ar.task, "cancelado desde la Oficina")
	}
	go func() {
		defer o.finalizers.Done()
		<-run.Done()
		o.finalize(id, run.Result())
	}()
}

func orDefault(s string) string {
	if s == "" {
		return "predeterminado"
	}
	return s
}

// lastSeq is the highest Seq already stored for a job.
func lastSeq(s *store, id string) int64 {
	evs, _ := s.readEvents(id)
	var n int64
	for _, ev := range evs {
		n = max(n, ev.Seq)
	}
	return n
}

// stateStatus maps a run state to a job status.
func stateStatus(s string) string {
	switch s {
	case harness.StatePreparing, harness.StateWaiting:
		return StatusPreparing
	case harness.StateRunning, harness.StateCancelling:
		return StatusRunning
	case harness.StateCleaning, harness.StateCleanupFailed:
		return StatusCleaning
	}
	return ""
}

func (o *Office) onState(id, state string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	j := o.jobs[id]
	if j == nil || isTerminal(j.Status) {
		return
	}
	if state == harness.StateCleanupFailed && o.active != nil && o.active.jobID == id {
		o.axState.CleanupFailed = o.active.task
	} else if state == harness.StateFinished || state == harness.StateCleaning {
		o.axState.CleanupFailed = ""
	}
	st := stateStatus(state)
	if st == "" || st == j.Status {
		o.rev++
		o.deltaDirty = true
		return
	}
	j.Status = st
	if st == StatusRunning && j.Started == nil {
		now := o.now()
		j.Started = &now
	}
	o.touchJobLocked(id)
	o.mirrorStepLocked(j)
	o.saveLocked(false, true)
}

func (o *Office) onEvent(id string, ev harness.Event) {
	o.mu.Lock()
	defer o.mu.Unlock()
	a := o.active
	if a == nil || a.jobID != id {
		return
	}
	o.appendEventLocked(a, ev)
}

// appendEventLocked numbers an event, keeps it (ring and file), updates
// the job's activity and sends it to the job's watchers.
func (o *Office) appendEventLocked(a *activeRun, ev harness.Event) {
	a.seq++
	ev.Seq = a.seq
	if ev.Time.IsZero() {
		ev.Time = o.now()
	}
	if len(a.ring) >= maxRingEvents {
		a.ring = slices.Delete(a.ring, 0, len(a.ring)-maxRingEvents+1)
	}
	a.ring = append(a.ring, ev)
	a.log.append(ev)
	a.lastEvent = o.now()
	if j := o.jobs[a.jobID]; j != nil && !isTerminal(j.Status) {
		changed := false
		if j.Stalled {
			j.Stalled, changed = false, true
		}
		if act := activityOf(ev); act != "" && act != j.Activity {
			j.Activity, changed = act, true
		}
		if changed {
			o.touchJobLocked(j.ID)
		}
	}
	if data, err := json.Marshal(ev); err == nil {
		o.hub.publish(Message{Event: "log", Seq: ev.Seq, Data: data}, a.jobID)
	}
}

// activityOf is the one line the office view shows about an event.
func activityOf(ev harness.Event) string {
	short := func(s string) string { return truncRunes(strings.Join(strings.Fields(s), " "), 80) }
	switch ev.Kind {
	case harness.EventInit:
		return "Sesión iniciada"
	case harness.EventTool:
		switch ev.Tool {
		case "Edit", "Write", "MultiEdit", "NotebookEdit":
			return short("Editando " + ev.Input)
		case "Read":
			return short("Leyendo " + ev.Input)
		case "Grep", "Glob":
			return short("Buscando " + ev.Input)
		case "WebSearch", "WebFetch":
			return short("Consultando " + ev.Input)
		case "TodoWrite":
			return "Planificando"
		}
		if ev.Input != "" {
			return short(ev.Tool + ": " + ev.Input)
		}
		return short(ev.Tool)
	case harness.EventText:
		return short(ev.Text)
	case harness.EventThinking:
		return "Pensando…"
	case harness.EventTodo:
		return "Actualizando su plan"
	case harness.EventResult:
		return "Terminando…"
	case harness.EventError:
		return short("Error: " + ev.Text)
	case harness.EventSystem:
		return short(ev.Text)
	}
	return ""
}

// checkStalled flags the active job when it printed nothing for a while.
func (o *Office) checkStalled() {
	o.mu.Lock()
	defer o.mu.Unlock()
	a := o.active
	if a == nil {
		return
	}
	j := o.jobs[a.jobID]
	if j != nil && j.Status == StatusRunning && !j.Stalled && o.now().Sub(a.lastEvent) >= stallAfter {
		j.Stalled = true
		o.touchJobLocked(j.ID)
	}
}

// changeSummary condenses captured changes for the job record.
func changeSummary(c *harness.Changes) *ChangeSummary {
	s := &ChangeSummary{Files: len(c.Files), PatchTruncated: c.PatchTruncated,
		ContentsComplete: c.ContentsComplete && c.Error == "", BaseSHA: c.BaseSHA, Error: c.Error}
	for _, f := range c.Files {
		s.Additions += f.Additions
		s.Deletions += f.Deletions
	}
	return s
}

// finalize records how a run ended, applies the lesson policy, moves its
// pipeline on and frees the slot.
func (o *Office) finalize(id string, res harness.Result) {
	status := statusOf(res)
	resultText := res.ResultText
	if len(resultText) > harness.MaxResultText {
		resultText, _ = cutBytes(resultText, harness.MaxResultText)
	}
	// A file that could not be written is never ignored: the job and the
	// office's warnings say so, and a patch that is not on disk is never
	// offered to a later step.
	var unsaved []string
	if resultText != "" {
		if err := o.store.writeJobFile(id, fileResult, []byte(resultText)); err != nil {
			unsaved = append(unsaved, fileResult)
		}
	}
	var changes *ChangeSummary
	if c := res.Changes; c != nil {
		changes = changeSummary(c)
		if len(c.Patch) > 0 {
			if err := o.store.writeJobFile(id, filePatch, c.Patch); err != nil {
				unsaved = append(unsaved, filePatch)
				changes.PatchTruncated = true
				if changes.Error == "" {
					changes.Error = "no se pudo guardar el parche"
				}
			}
		}
		if err := o.store.writeJobJSON(id, fileChanges, c.Files); err != nil {
			unsaved = append(unsaved, fileChanges)
		}
		if c.ContentsComplete && c.Error == "" && len(c.Contents) > 0 {
			entries := make([]contentEntry, 0, len(c.Contents))
			for _, f := range c.Contents {
				entries = append(entries, contentEntry{Path: f.Path, Mode: f.Mode, Deleted: f.Deleted, Data: f.Data})
			}
			if err := o.store.writeJobJSON(id, fileContents, entries); err != nil {
				unsaved = append(unsaved, fileContents)
				changes.ContentsComplete = false
			}
		} else {
			changes.ContentsComplete = false
		}
	}

	o.mu.Lock()
	defer o.kick()
	defer o.mu.Unlock()
	j := o.jobs[id]
	a := o.active
	if a != nil && a.jobID == id {
		o.active = nil
		o.axState.CleanupFailed = ""
	}
	if res.Windows != nil {
		o.usage, o.usageAt = res.Windows, o.now()
	}
	if j == nil {
		if a != nil {
			a.log.close()
		}
		return
	}
	now := o.now()
	j.Status, j.Finished, j.Outcome, j.Message, j.ExitCode = status, &now, res.Outcome, res.Message, res.ExitCode
	j.Usage, j.Changes, j.Activity, j.Stalled, j.Waiting = harness.SanitizeUsage(res.Usage), changes, "", false, ""
	if len(unsaved) > 0 {
		note := "Aviso: no se pudieron guardar en el disco " + strings.Join(unsaved, ", ")
		if j.Message != "" {
			note = j.Message + " " + note
		}
		j.Message = note
		o.audit.Error("office.save", "job", id, "files", strings.Join(unsaved, ","))
		o.warnOnce("No se pudieron guardar ficheros de algunos trabajos (¿volumen lleno?): revisa sus avisos.")
	}
	if j.Usage.ModelUsed == "" && j.Agent.Model != "" {
		j.Usage.ModelUsed = j.Agent.Model
	}
	if resultText != "" {
		j.Summary = ParseSummary(resultText)
		j.Lessons = ParseLessons(resultText)
		switch j.Kind {
		case KindReview:
			j.Verdict = ParseVerdict(resultText)
		case KindJudge:
			j.Score = ParseScore(resultText)
		}
	}
	if status == StatusDone && j.Kind == KindRetro {
		if err := o.coachProposalLocked(j, resultText); err != nil {
			j.Message = "Sin propuesta del Coach: " + err.Error()
		}
	}
	if status == StatusDone && j.Kind != KindRetro && j.Kind != KindJudge {
		o.applyLessonsLocked(j)
	}
	if a != nil && a.jobID == id {
		o.appendEventLocked(a, harness.Event{Kind: harness.EventSystem, Text: "Trabajo terminado: " + statusLabel(status)})
		a.log.close()
	}
	o.touchJobLocked(id)
	o.invalidateLocked("metrics")
	o.refreshJobBytesLocked(id)
	if j.PipelineID != "" {
		o.advancePipelineLocked(j.PipelineID)
	}
	o.retainLocked()
	o.saveLocked(true, true)
	o.auditAction("job.end", "oficina", "job", id, "status", status, "outcome", res.Outcome,
		"cost_usd", j.Usage.CostUSD)
}

func statusLabel(s string) string {
	switch s {
	case StatusDone:
		return "hecho"
	case StatusCancelled:
		return "cancelado"
	}
	return "fallido"
}

// retainLocked deletes the oldest finished jobs that nothing still needs
// while there are more than RetentionJobs or their directories add up to
// more than MaxJobsBytes.
func (o *Office) retainLocked() {
	if o.dropOldestLocked(len(o.order)-o.cfg.RetentionJobs, o.jobBytesTotal-o.maxJobBytes) > 0 {
		o.invalidateLocked("metrics")
	}
}

// dropOldestLocked deletes the oldest finished jobs nothing needs: at
// least n of them and, beyond that, until minFree bytes were freed. It
// returns how many it deleted.
func (o *Office) dropOldestLocked(n int, minFree int64) int {
	if n <= 0 && minFree <= 0 {
		return 0
	}
	refs := o.referencedSetLocked()
	var drop []string
	var freed int64
	for _, id := range o.order {
		if len(drop) >= n && freed >= minFree {
			break
		}
		if j := o.jobs[id]; !isTerminal(j.Status) || refs[id] || (o.active != nil && o.active.jobID == id) {
			continue
		}
		drop = append(drop, id)
		freed += o.jobBytes[id]
	}
	for _, id := range drop {
		o.removeJobLocked(id)
	}
	return len(drop)
}
