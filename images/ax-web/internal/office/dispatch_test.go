package office

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/harness"
	"apptolast.com/ax-web/internal/runs"
)

func TestDispatchPriorityThenAge(t *testing.T) {
	h := newHarness(t)
	low := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "baja", Priority: ptr(0)})
	h.clock.Add(time.Second)
	normal1 := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "normal 1"})
	h.clock.Add(time.Second)
	high := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "alta", Priority: ptr(2)})
	h.clock.Add(time.Second)
	normal2 := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "normal 2"})
	if q := h.o.Snapshot().Queue; strings.Join(q, ",") != strings.Join([]string{high.ID, normal1.ID, normal2.ID, low.ID}, ",") {
		t.Fatalf("queue %v", q)
	}
	var order []string
	for range 4 {
		j := h.runNext(t, exited(0, "ok"))
		order = append(order, j.ID)
	}
	if strings.Join(order, ",") != strings.Join([]string{high.ID, normal1.ID, normal2.ID, low.ID}, ",") {
		t.Fatalf("order %v", order)
	}
}

func TestDispatchWaitsWhenNotFree(t *testing.T) {
	h := newHarness(t)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	h.exec.ready = false
	if h.dispatch(t) != "" {
		t.Fatal("dispatched while not ready")
	}
	h.exec.ready = true
	if _, err := h.o.PauseQueue(true, "x"); err != nil {
		t.Fatal(err)
	}
	if h.dispatch(t) != "" {
		t.Fatal("dispatched while paused")
	}
	h.o.PauseQueue(false, "x")
	// A host task holds the worker: the job stays queued and says why.
	h.exec.setErr(errors.New("x"))
	h.exec.setErr(&busyErr{"tarea-20261002-101010"})
	if h.dispatch(t) != "" {
		t.Fatal("launch counted")
	}
	got, _ := h.o.Job(j.ID)
	if got.Status != StatusQueued || got.Waiting != "El sandbox está ocupado (tarea-20261002-101010)" {
		t.Fatalf("%+v", got)
	}
	// It backs off, then starts once the worker is free.
	h.exec.setErr(nil)
	if h.dispatch(t) != "" {
		t.Fatal("no back-off")
	}
	h.clock.Add(busyBackoff)
	task := h.dispatch(t)
	if task != "web-"+j.ID {
		t.Fatalf("task %q", task)
	}
	got, _ = h.o.Job(j.ID)
	if got.Waiting != "" || got.Status != StatusPreparing || got.Task != task || got.Started == nil {
		t.Fatalf("%+v", got)
	}
	// Another launch error fails the job.
	h.complete(t, task, exited(0, "ok"))
	k := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "otra"})
	h.exec.setErr(errors.New("AX no respondió"))
	h.dispatch(t)
	got, _ = h.o.Job(k.ID)
	if got.Status != StatusFailed || !strings.Contains(got.Message, "AX no respondió") {
		t.Fatalf("%+v", got)
	}
}

type busyErr struct{ task string }

func (e *busyErr) Error() string        { return "el sandbox está ocupado (" + e.task + ")" }
func (e *busyErr) Is(target error) bool { return target == harness.ErrBusy }

func TestSpecFromAgentAndJob(t *testing.T) {
	h := newHarness(t)
	if _, err := h.o.UpdateAgent("linus", AgentInput{DisallowedTools: &[]string{"Bash(git push:*)"}}, "x"); err != nil {
		t.Fatal(err)
	}
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Arregla el README",
		Overrides: &Overrides{Model: "sonnet", Effort: "max", TimeoutMinutes: 20}})
	task := h.dispatch(t)
	spec := h.exec.last()
	if spec.ID != "web-"+j.ID || spec.Repo != "https://github.com/apptolast/web" || spec.Branch != "develop" ||
		spec.Harness != harness.Claude || spec.Model != "sonnet" || spec.Effort != "max" || spec.Mode != harness.ModeFull ||
		spec.MaxTurns != 120 || spec.Timeout != 20*time.Minute || !spec.CaptureChanges || spec.ApplyPatch != nil ||
		len(spec.DisallowedTools) != 1 || !strings.HasPrefix(spec.AppendSystemPrompt, "Eres Linus") {
		t.Fatalf("%+v", spec)
	}
	if !strings.Contains(spec.Prompt, "## Encargo\nArregla el README") {
		t.Fatal(spec.Prompt)
	}
	d, _ := h.o.JobDetail(j.ID)
	if d.FinalPrompt != spec.Prompt || d.SystemPrompt != spec.AppendSystemPrompt || d.Prompt != "Arregla el README" {
		t.Fatal("files not stored")
	}
	h.complete(t, task, exited(0, "ok"))
	// A codex job without a codex credential fails before launching.
	c := h.job(t, JobRequest{ProjectID: "web", AgentID: "guido", Kind: KindAsk, Prompt: "¿Qué hace main.go?"})
	if h.dispatch(t) != "" {
		t.Fatal("codex launched without credential")
	}
	got, _ := h.o.Job(c.ID)
	if got.Status != StatusFailed || got.Message != "Codex no está configurado en la Oficina" {
		t.Fatalf("%+v", got)
	}
	// With one, the persona goes into the prompt and nothing to argv.
	writeCodex(t, filepath.Join(h.cfg.OfficeSecretDir, h.cfg.CodexAuthKey), "2026-10-01T10:00:00Z", "r1")
	c = h.job(t, JobRequest{ProjectID: "web", AgentID: "guido", Kind: KindAsk, Prompt: "¿Qué hace main.go?"})
	task = h.dispatch(t)
	spec = h.exec.last()
	if spec.Harness != harness.Codex || spec.MaxTurns != 0 || spec.AppendSystemPrompt != "" ||
		!strings.Contains(spec.Prompt, "## Tu papel\nEres Guido") || spec.CaptureChanges {
		t.Fatalf("%+v", spec)
	}
	h.complete(t, task, exited(0, "ok"))
}

func TestEventsActivityAndFinalize(t *testing.T) {
	h := newHarness(t)
	sub := h.o.Subscribe("")
	defer h.o.Unsubscribe(sub)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Arregla el README"})
	watch := h.o.Subscribe(j.ID)
	defer h.o.Unsubscribe(watch)
	task := h.dispatch(t)
	r := h.exec.run(task)
	r.hooks.OnState(harness.StateRunning)
	r.hooks.OnEvent(harness.Event{Kind: harness.EventInit, Model: "claude-opus-5-5"})
	r.hooks.OnEvent(harness.Event{Kind: harness.EventTool, Tool: "Bash", Input: "npm test"})
	got, _ := h.o.Job(j.ID)
	if got.Status != StatusRunning || got.Activity != "Bash: npm test" {
		t.Fatalf("%+v", got)
	}
	r.hooks.OnEvent(harness.Event{Kind: harness.EventTool, Tool: "Edit", Input: "README.md"})
	if got, _ = h.o.Job(j.ID); got.Activity != "Editando README.md" {
		t.Fatal(got.Activity)
	}
	// The watcher got the log events, numbered after the office's own.
	var seqs []int64
	for len(seqs) < 4 {
		select {
		case m := <-watch.C:
			if m.Event == "log" {
				seqs = append(seqs, m.Seq)
			}
		case <-time.After(time.Second):
			t.Fatalf("log events: %v", seqs)
		}
	}
	if seqs[0] != 1 || seqs[3] != 4 {
		t.Fatalf("seqs %v", seqs)
	}
	// Stalled after 15 minutes of silence, cleared by the next event.
	h.clock.Add(stallAfter)
	h.o.checkStalled()
	if got, _ = h.o.Job(j.ID); !got.Stalled {
		t.Fatal("not stalled")
	}
	r.hooks.OnEvent(harness.Event{Kind: harness.EventText, Text: "Sigo aquí"})
	if got, _ = h.o.Job(j.ID); got.Stalled {
		t.Fatal("still stalled")
	}
	result := sampleResult + "\n"
	done := h.complete(t, task, withChanges(exited(0, result), 2))
	if done.Status != StatusDone || done.Summary == "" || len(done.Lessons) != 3 || done.Verdict != "" ||
		done.Changes == nil || done.Changes.Files != 2 || done.Changes.Additions != 2 || !done.Changes.ContentsComplete ||
		done.Usage.CostUSD != 0.5 || done.Finished == nil || done.Activity != "" {
		t.Fatalf("%+v", done)
	}
	patch, err := h.o.Patch(j.ID)
	if err != nil || !strings.HasPrefix(string(patch), "diff --git") {
		t.Fatalf("%s %v", patch, err)
	}
	var contents []contentEntry
	data, _ := os.ReadFile(filepath.Join(h.o.store.jobDir(j.ID), fileContents))
	if json.Unmarshal(data, &contents) != nil || len(contents) != 2 || string(contents[0].Data) != "hola\nadiós\n" {
		t.Fatalf("contents %s", data)
	}
	evs, more, err := h.o.Events(j.ID, 0, 1000)
	if err != nil || more || len(evs) != 6 || evs[len(evs)-1].Text != "Trabajo terminado: hecho" {
		t.Fatalf("%d %v %+v", len(evs), err, evs)
	}
	page, more, _ := h.o.Events(j.ID, 2, 2)
	if !more || len(page) != 2 || page[0].Seq != 3 {
		t.Fatalf("page %+v", page)
	}
	tail, _ := h.o.TailEvents(j.ID, 0, 2)
	if len(tail) != 2 || tail[1].Seq != 6 {
		t.Fatalf("tail %+v", tail)
	}
	// Lessons became proposals (policy "proponer"), once each.
	s := h.o.Snapshot()
	if n := countProposals(s, ProposalLesson); n != 3 {
		t.Fatalf("proposals %d", n)
	}
	k := h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Otra vez"})
	h.runNext(t, exited(0, result))
	if n := countProposals(h.o.Snapshot(), ProposalLesson); n != 3 {
		t.Fatalf("duplicated proposals: %d (%s)", n, k.ID)
	}
	// The coalesced delta names the changed jobs.
	h.o.flushDelta()
	var delta Delta
	for {
		select {
		case m := <-sub.C:
			if m.Event != "office" {
				continue
			}
			if err := json.Unmarshal(m.Data, &delta); err != nil {
				t.Fatal(err)
			}
		case <-time.After(time.Second):
			t.Fatal("no delta")
		}
		break
	}
	if delta.Rev == 0 || len(delta.Jobs) == 0 || delta.Counts.PendingProposals != 3 ||
		!strings.Contains(strings.Join(delta.Invalidate, ","), "proposals") {
		t.Fatalf("%+v", delta)
	}
}

func countProposals(s Snapshot, typ string) int {
	n := 0
	for _, p := range s.Proposals {
		if p.Type == typ && p.Status == ProposalPending {
			n++
		}
	}
	return n
}

func TestLessonPolicies(t *testing.T) {
	for _, policy := range []string{LessonsApprove, LessonsOff} {
		t.Run(policy, func(t *testing.T) {
			h := newHarness(t)
			if _, err := h.o.UpdateSettings(SettingsInput{AutoLessons: ptr(policy)}, "x"); err != nil {
				t.Fatal(err)
			}
			h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
			h.runNext(t, exited(0, sampleResult))
			s := h.o.Snapshot()
			mem := 0
			for _, p := range s.Projects {
				if p.ID == "web" {
					mem = len(p.Memory)
				}
			}
			if countProposals(s, ProposalLesson) != 0 || (policy == LessonsApprove) != (mem == 3) {
				t.Fatalf("memory %d", mem)
			}
			// Approved lessons reach the next prompt, newest first.
			h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "otra"})
			h.dispatch(t)
			has := strings.Contains(h.exec.last().Prompt, "## Memoria del proyecto (lecciones aprobadas)\n- ")
			if has != (policy == LessonsApprove) {
				t.Fatal(h.exec.last().Prompt)
			}
		})
	}
}

func TestCancelJobs(t *testing.T) {
	h := newHarness(t)
	a := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "uno"})
	b := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "dos"})
	task := h.dispatch(t)
	if err := h.o.CancelJob(b.ID, "203.0.113.9"); err != nil {
		t.Fatal(err)
	}
	if got, _ := h.o.Job(b.ID); got.Status != StatusCancelled {
		t.Fatalf("%+v", got)
	}
	if err := h.o.CancelJob(a.ID, "203.0.113.9"); err != nil {
		t.Fatal(err)
	}
	if len(h.exec.cancelled) != 1 || h.exec.cancelled[0] != task {
		t.Fatalf("%v", h.exec.cancelled)
	}
	got := h.complete(t, task, harness.Result{Outcome: harness.OutcomeCancelled, Message: "cancelada"})
	if got.Status != StatusCancelled {
		t.Fatalf("%+v", got)
	}
	if err := h.o.CancelJob(a.ID, "x"); IsStatus(err) != 409 {
		t.Fatalf("cancel finished: %v", err)
	}
	if !strings.Contains(h.audit.String(), `"client_ip":"203.0.113.9"`) {
		t.Fatal("cancel not audited")
	}
}

func TestRetryRateFollowUpDelete(t *testing.T) {
	h := newHarness(t)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Arregla",
		Overrides: &Overrides{Effort: "low"}})
	if _, err := h.o.RetryJob(j.ID, "x"); IsStatus(err) != 409 {
		t.Fatal("retried a queued job")
	}
	if _, err := h.o.SetPriority(j.ID, 2, "x"); err != nil {
		t.Fatal(err)
	}
	failed := h.runNext(t, exited(1, "falló"))
	if failed.Status != StatusFailed {
		t.Fatalf("%+v", failed)
	}
	if _, err := h.o.SetPriority(j.ID, 0, "x"); IsStatus(err) != 409 {
		t.Fatal("priority of a finished job")
	}
	r, err := h.o.RetryJob(j.ID, "x")
	if err != nil || r.Overrides == nil || r.Overrides.Effort != "low" || r.Priority != 2 || r.Kind != KindChange {
		t.Fatalf("%+v %v", r, err)
	}
	done := h.runNext(t, withChanges(exited(0, "## Resumen\nHecho."), 1))
	if _, err := h.o.RateJob(done.ID, 2, "", "x"); err == nil {
		t.Fatal("score 2 accepted")
	}
	rated, err := h.o.RateJob(done.ID, 1, "Muy bien", "x")
	if err != nil || rated.Rating == nil || rated.Rating.Score != 1 {
		t.Fatalf("%+v %v", rated, err)
	}
	f, err := h.o.FollowUp(done.ID, FollowUpRequest{AgentID: "grace", Kind: KindReview, Prompt: "Revisa esto"}, "x")
	if err != nil || f.ApplyFrom != done.ID || len(f.Context) != 1 || f.Source.Type != "job" {
		t.Fatalf("%+v %v", f, err)
	}
	// The source is needed until the follow-up ran.
	if err := h.o.DeleteJob(done.ID, "x"); IsStatus(err) != 409 {
		t.Fatalf("deleted a needed job: %v", err)
	}
	task := h.dispatch(t)
	spec := h.exec.last()
	if string(spec.ApplyPatch) == "" || !strings.Contains(spec.Prompt, "## Cambios ya presentes en el árbol") ||
		!strings.Contains(spec.Prompt, "## Contexto de pasos anteriores\n### ") {
		t.Fatalf("%+v", spec)
	}
	h.complete(t, task, exited(0, "VEREDICTO: APROBADO"))
	if err := h.o.DeleteJob(done.ID, "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.Job(done.ID); IsStatus(err) != 404 {
		t.Fatal("still there")
	}
	if m := h.o.Metrics(); len(m.ByAgentVersion) == 0 {
		t.Fatal("no metrics")
	}
}

func TestTruncatedPatchCannotContinue(t *testing.T) {
	h := newHarness(t)
	h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Arregla"})
	res := withChanges(exited(0, "ok"), 1)
	res.Changes.PatchTruncated = true
	src := h.runNext(t, res)
	f, err := h.o.FollowUp(src.ID, FollowUpRequest{AgentID: "grace", Kind: KindReview, Prompt: "Revisa"}, "x")
	if err != nil || f.ApplyFrom != "" {
		t.Fatalf("%+v %v", f, err)
	}
}

func TestShutdownWaitsForFinalize(t *testing.T) {
	h := newHarness(t)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	ctx, cancel := context.WithCancel(context.Background())
	go h.o.Run(ctx)
	deadline := time.Now().Add(5 * time.Second)
	for h.exec.count() == 0 {
		if time.Now().After(deadline) {
			t.Fatal("Run did not dispatch")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	go func() {
		time.Sleep(50 * time.Millisecond)
		h.exec.finish("web-"+j.ID, harness.Result{Outcome: harness.OutcomeCancelled})
	}()
	cctx, ccancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer ccancel()
	h.o.Close(cctx)
	if got, _ := h.o.Job(j.ID); got.Status != StatusCancelled {
		t.Fatalf("%+v", got)
	}
	if _, err := h.o.CreateJob(context.Background(), JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk,
		Prompt: "x"}, "x"); err != ErrClosed {
		t.Fatalf("after close: %v", err)
	}
}

// While the sandbox stays busy, the next job is prepared once: its prompt
// files are written and "envía el encargo" is logged a single time,
// until something it was built from changes.
func TestBusyRetriesReuseThePreparedSpec(t *testing.T) {
	h := newHarness(t)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	h.exec.setErr(&busyErr{"tarea-1"})
	sends := func() int {
		evs, _, err := h.o.Events(j.ID, 0, 1000)
		if err != nil {
			t.Fatal(err)
		}
		n := 0
		for _, ev := range evs {
			if strings.HasPrefix(ev.Text, "La Oficina envía el encargo") {
				n++
			}
		}
		return n
	}
	final := filepath.Join(h.o.store.jobDir(j.ID), fileFinalPrompt)
	for i := range 3 {
		h.dispatch(t)
		h.clock.Add(busyBackoff)
		if i == 0 {
			os.WriteFile(final, []byte("marca"), 0o600)
		}
	}
	if data, _ := os.ReadFile(final); string(data) != "marca" || sends() != 1 {
		t.Fatalf("rewritten (%q) or logged %d times", data, sends())
	}
	// Editing the agent prepares the job again, with the new settings.
	if _, err := h.o.UpdateAgent("becario", AgentInput{Effort: ptr("medium")}, "x"); err != nil {
		t.Fatal(err)
	}
	h.exec.setErr(nil)
	task := h.dispatch(t)
	if task == "" || h.exec.last().Effort != "medium" || sends() != 2 {
		t.Fatalf("task %q, effort %q, %d sends", task, h.exec.last().Effort, sends())
	}
	if data, _ := os.ReadFile(final); string(data) == "marca" {
		t.Fatal("not prepared again")
	}
	// The live tail follows the events of the earlier attempt, in order.
	h.exec.run(task).hooks.OnEvent(harness.Event{Kind: harness.EventText, Text: "en marcha"})
	evs, _ := h.o.TailEvents(j.ID, 0, 100)
	if len(evs) != 3 || evs[0].Seq != 1 || evs[2].Text != "en marcha" || evs[2].Seq != 3 {
		t.Fatalf("%+v", evs)
	}
	h.complete(t, task, exited(0, "ok"))
}

// The live ring always provides the tail of the active job, even when
// the stored log stopped at its bound.
func TestActiveEventsPreferTheRing(t *testing.T) {
	h := newHarness(t)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	task := h.dispatch(t)
	run := h.exec.run(task)
	text := strings.Repeat("t", 1500)
	for range maxRingEvents + 100 {
		run.hooks.OnEvent(harness.Event{Kind: harness.EventText, Text: text})
	}
	run.hooks.OnEvent(harness.Event{Kind: harness.EventText, Text: "la última"})
	stored, _ := h.o.store.readEvents(j.ID)
	if last := stored[len(stored)-1]; last.Text != "registro truncado" {
		t.Fatalf("the log did not reach its bound: %d events", len(stored))
	}
	evs, err := h.o.TailEvents(j.ID, 0, 5000)
	if err != nil || evs[len(evs)-1].Text != "la última" {
		t.Fatalf("tail %v", err)
	}
	for i := 1; i < len(evs); i++ {
		if evs[i].Seq != evs[i-1].Seq+1 {
			t.Fatalf("gap or repeat at %d: %d after %d", i, evs[i].Seq, evs[i-1].Seq)
		}
	}
	h.complete(t, task, exited(0, "ok"))
}

// Without a Claude credential nothing is created in AX: the job fails.
func TestClaudeCredentialCheckedBeforeLaunch(t *testing.T) {
	h := newHarness(t)
	os.WriteFile(h.cfg.ClaudeTokenFile, []byte(" \n"), 0o600)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	if h.dispatch(t) != "" || h.exec.count() != 0 {
		t.Fatal("launched without a Claude credential")
	}
	if got, _ := h.o.Job(j.ID); got.Status != StatusFailed || got.Message != "Claude no está configurado en la Oficina" {
		t.Fatalf("%+v", got)
	}
}

// The blackout window is a "not now": the job stays queued.
func TestBlackoutKeepsTheJobQueued(t *testing.T) {
	h := newHarness(t)
	j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	h.exec.setErr(runs.ErrBlackout)
	h.dispatch(t)
	got, _ := h.o.Job(j.ID)
	if got.Status != StatusQueued || got.Waiting != "Esperando a que termine la ventana del Observatorio" {
		t.Fatalf("%+v", got)
	}
}

// A retry that applied another job's changes needs them still.
func TestRetryNeedsTheChangesItApplied(t *testing.T) {
	h := newHarness(t)
	h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "cambia"})
	src := h.runNext(t, withChanges(exited(0, "hecho"), 1))
	f, err := h.o.FollowUp(src.ID, FollowUpRequest{AgentID: "grace", Kind: KindReview, Prompt: "Revisa"}, "x")
	if err != nil || f.ApplyFrom != src.ID {
		t.Fatalf("%+v %v", f, err)
	}
	h.runNext(t, exited(0, "VEREDICTO: APROBADO"))
	if _, err := h.o.RetryJob(f.ID, "x"); err != nil {
		t.Fatal(err)
	}
	h.runNext(t, exited(0, "VEREDICTO: APROBADO"))
	h.o.mu.Lock()
	h.o.jobs[src.ID].Changes.PatchTruncated = true
	h.o.mu.Unlock()
	if _, err := h.o.RetryJob(f.ID, "x"); IsStatus(err) != 409 || !strings.Contains(err.Error(), "ya no se pueden aplicar") {
		t.Fatalf("%v", err)
	}
	h.o.mu.Lock()
	h.o.jobs[src.ID].Changes.PatchTruncated = false
	h.o.mu.Unlock()
	if err := h.o.DeleteJob(src.ID, "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.RetryJob(f.ID, "x"); IsStatus(err) != 409 || !strings.Contains(err.Error(), "Ya no existe el trabajo") {
		t.Fatalf("%v", err)
	}
}
