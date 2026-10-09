package office

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

func (h *harnessT) pipeline(t *testing.T, id string) (Pipeline, []Job) {
	t.Helper()
	p, jobs, err := h.o.Pipeline(id)
	if err != nil {
		t.Fatal(err)
	}
	return p, jobs
}

func stepNames(p Pipeline) string {
	var n []string
	for _, s := range p.Steps {
		n = append(n, s.Name)
	}
	return strings.Join(n, ",")
}

// step runs the pipeline's next job and checks which step it was.
func (h *harnessT) step(t *testing.T, name string, res harness.Result) (harness.Spec, Job) {
	t.Helper()
	task := h.dispatch(t)
	if task == "" {
		t.Fatalf("step %s was not queued", name)
	}
	spec := h.exec.last()
	j := h.complete(t, task, res)
	if !strings.HasSuffix(j.Title, " · "+name) {
		t.Fatalf("ran %q, expected step %s", j.Title, name)
	}
	h.clock.Add(time.Second)
	return spec, j
}

func TestTeamApprovedFirstTime(t *testing.T) {
	h := newHarness(t)
	pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web",
		Task: "Añade un test del parser", Priority: ptr(2)}, "x")
	if err != nil {
		t.Fatal(err)
	}
	if pl.Status != PipelineRunning || len(pl.Steps) != 1 || pl.Participants["developer"] != "linus" ||
		pl.MaxIterations != 2 || pl.Branch != "develop" || !strings.HasPrefix(pl.Title, "Equipo: ") {
		t.Fatalf("%+v", pl)
	}
	planSpec, plan := h.step(t, "Plan", exited(0, "1. Escribe el test\n## Resumen\nPlan listo."))
	if planSpec.Mode != harness.ModeRead || plan.Priority != 2 || plan.PipelineID != pl.ID || plan.Step != 0 {
		t.Fatalf("%+v", plan)
	}
	implSpec, impl := h.step(t, "Implementación", withChanges(exited(0, "## Resumen\nHecho."), 1))
	if !strings.Contains(implSpec.Prompt, "### Plan — Ada\n1. Escribe el test") || !implSpec.CaptureChanges {
		t.Fatal(implSpec.Prompt)
	}
	revSpec, rev := h.step(t, "Revisión", exited(0, "Bien.\nVEREDICTO: APROBADO"))
	if len(revSpec.ApplyPatch) == 0 || rev.ApplyFrom != impl.ID || !strings.Contains(revSpec.Prompt, "## Cambios ya presentes") {
		t.Fatalf("%+v", rev)
	}
	p, jobs := h.pipeline(t, pl.ID)
	if p.Status != PipelineDone || p.Outcome != OutcomeApproved || len(jobs) != 3 || p.Finished == nil ||
		p.Steps[2].Status != StatusDone || stepNames(p) != "Plan,Implementación,Revisión" {
		t.Fatalf("%+v", p)
	}
	if h.dispatch(t) != "" {
		t.Fatal("extra step")
	}
}

func TestTeamIterationLimit(t *testing.T) {
	cases := []struct {
		name, last, outcome string
	}{
		{"still changes", "VEREDICTO: CAMBIOS", OutcomePending},
		{"no verdict", "Ni idea.", OutcomeNoVerdict},
		{"approved after fix", "VEREDICTO: APROBADO", OutcomeApproved},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web",
				Task: "Arregla el bug", MaxIterations: ptr(1)}, "x")
			if err != nil {
				t.Fatal(err)
			}
			h.step(t, "Plan", exited(0, "plan"))
			_, impl := h.step(t, "Implementación", withChanges(exited(0, "impl"), 1))
			h.step(t, "Revisión", exited(0, "Falta un caso.\nVEREDICTO: CAMBIOS"))
			fixSpec, fix := h.step(t, "Corrección", withChanges(exited(0, "arreglado"), 2))
			if fix.ApplyFrom != impl.ID || len(fixSpec.ApplyPatch) == 0 || !strings.Contains(fixSpec.Prompt, "Falta un caso.") {
				t.Fatalf("%+v", fix)
			}
			_, rev2 := h.step(t, "Revisión", exited(0, c.last))
			if rev2.ApplyFrom != fix.ID {
				t.Fatalf("second review applies %s", rev2.ApplyFrom)
			}
			p, _ := h.pipeline(t, pl.ID)
			if p.Status != PipelineDone || p.Outcome != c.outcome || p.Iteration != 1 || len(p.Steps) != 5 {
				t.Fatalf("%+v", p)
			}
		})
	}
}

func TestPipelineStepFailureAndCancel(t *testing.T) {
	h := newHarness(t)
	pl, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "x"}, "x")
	h.step(t, "Plan", exited(2, "error"))
	p, _ := h.pipeline(t, pl.ID)
	if p.Status != PipelineFailed || !strings.Contains(p.Outcome, "Plan") {
		t.Fatalf("%+v", p)
	}
	// Cancelling a pipeline cancels its queued step.
	pl2, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplDebate, ProjectID: "web", Task: "y"}, "x")
	if err := h.o.CancelPipeline(pl2.ID, "x"); err != nil {
		t.Fatal(err)
	}
	p2, jobs := h.pipeline(t, pl2.ID)
	if p2.Status != PipelineCancelled || jobs[0].Status != StatusCancelled {
		t.Fatalf("%+v %+v", p2, jobs[0])
	}
	if err := h.o.CancelPipeline(pl2.ID, "x"); IsStatus(err) != 409 {
		t.Fatal(err)
	}
	// Cancelling its running step cancels the pipeline.
	pl3, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "z"}, "x")
	task := h.dispatch(t)
	if err := h.o.CancelJob(strings.TrimPrefix(task, "web-"), "x"); err != nil {
		t.Fatal(err)
	}
	h.complete(t, task, harness.Result{Outcome: harness.OutcomeCancelled})
	if p3, _ := h.pipeline(t, pl3.ID); p3.Status != PipelineCancelled {
		t.Fatalf("%+v", p3)
	}
}

func TestPipelineValidation(t *testing.T) {
	h := newHarness(t)
	ok := PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "x"}
	cases := map[string]func(*PipelineRequest){
		"template":       func(r *PipelineRequest) { r.Template = TplEval },
		"project_id":     func(r *PipelineRequest) { r.ProjectID = "nope" },
		"task":           func(r *PipelineRequest) { r.Task = "" },
		"max_iterations": func(r *PipelineRequest) { r.MaxIterations = ptr(11) },
		"participants":   func(r *PipelineRequest) { r.Participants = map[string]string{"intruso": "ada"} },
		"source":         func(r *PipelineRequest) { r.Source = &Source{Type: "job", JobID: "x"} },
	}
	for field, edit := range cases {
		r := ok
		edit(&r)
		_, err := h.o.CreatePipeline(context.Background(), r, "x")
		var fe *FieldError
		if !asField(err, &fe) || fe.Field != field {
			t.Errorf("%s: %v", field, err)
		}
	}
	r := ok
	r.Participants = map[string]string{"developer": "nadie"}
	if _, err := h.o.CreatePipeline(context.Background(), r, "x"); err == nil {
		t.Fatal("unknown participant accepted")
	}
	// mejora runs on the retro project unless told otherwise.
	pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplImprove, Task: ImproveTask}, "x")
	if err != nil || pl.ProjectID != "dockerswarm-infra" || pl.Branch != "main" {
		t.Fatalf("%+v %v", pl, err)
	}
}

func TestPanelFromJob(t *testing.T) {
	h := newHarness(t)
	h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Cambia algo"})
	src := h.runNext(t, withChanges(exited(0, "ok"), 1))
	pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplPanel, ProjectID: "web",
		Task: "¿Está bien?", Source: &Source{Type: "job", JobID: src.ID}}, "x")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"Corrección", "Seguridad", "Tests"} {
		spec, j := h.step(t, name, exited(0, name+": bien.\nVEREDICTO: APROBADO"))
		if j.ApplyFrom != src.ID || len(spec.ApplyPatch) == 0 || !strings.Contains(spec.Prompt, "Tu lente:") {
			t.Fatalf("%s: %+v", name, j)
		}
	}
	spec, syn := h.step(t, "Síntesis", exited(0, "VEREDICTO: CAMBIOS"))
	if len(syn.Context) != 3 || !strings.Contains(spec.Prompt, "### Seguridad — Hedy") {
		t.Fatalf("%+v", syn)
	}
	p, _ := h.pipeline(t, pl.ID)
	if p.Status != PipelineDone || p.Outcome != VerdictChanges {
		t.Fatalf("%+v", p)
	}
	// A source without changes is refused.
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	ask := h.runNext(t, exited(0, "ok"))
	if _, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplPanel, ProjectID: "web",
		Task: "x", Source: &Source{Type: "job", JobID: ask.ID}}, "x"); IsStatus(err) != 400 {
		t.Fatalf("%v", err)
	}
}

func TestDebate(t *testing.T) {
	h := newHarness(t)
	writeCodex(t, h.cfg.OfficeSecretDir+"/"+h.cfg.CodexAuthKey, "2026-10-01T10:00:00Z", "r1")
	pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplDebate, ProjectID: "web",
		Task: "¿Cola en memoria o en disco?"}, "x")
	if err != nil {
		t.Fatal(err)
	}
	h.step(t, "Propuesta A", exited(0, "A"))
	h.step(t, "Propuesta B", exited(0, "B"))
	spec, _ := h.step(t, "Propuesta C", exited(0, "C"))
	if spec.Harness != harness.Claude {
		t.Fatalf("C is %s", spec.Harness)
	}
	spec, _ = h.step(t, "Decisión", exited(0, "Gana B"))
	if !strings.Contains(spec.Prompt, "### Propuesta C — Hedy\nC") || spec.Mode != harness.ModeRead {
		t.Fatal(spec.Prompt)
	}
	p, _ := h.pipeline(t, pl.ID)
	if p.Status != PipelineDone || p.Outcome != OutcomeDecided {
		t.Fatalf("%+v", p)
	}
}

func TestRedBlue(t *testing.T) {
	h := newHarness(t)
	pl, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplRedBlue, ProjectID: "web",
		Task: "Revisa la autenticación"}, "x")
	h.step(t, "Ataque", exited(0, "Inyección en login.\nVEREDICTO: CAMBIOS"))
	_, d1 := h.step(t, "Defensa", withChanges(exited(0, "arreglado"), 1))
	_, v1 := h.step(t, "Verificación", exited(0, "Queda otra.\nVEREDICTO: CAMBIOS"))
	if v1.ApplyFrom != d1.ID {
		t.Fatalf("%+v", v1)
	}
	_, d2 := h.step(t, "Defensa", withChanges(exited(0, "arreglado del todo"), 2))
	if d2.ApplyFrom != d1.ID {
		t.Fatalf("second defence applies %s", d2.ApplyFrom)
	}
	h.step(t, "Verificación", exited(0, "VEREDICTO: APROBADO"))
	p, _ := h.pipeline(t, pl.ID)
	if p.Status != PipelineDone || p.Outcome != OutcomeApproved || p.Iteration != 2 ||
		stepNames(p) != "Ataque,Defensa,Verificación,Defensa,Verificación" {
		t.Fatalf("%+v", p)
	}
	// Nothing found: done after the attack.
	pl2, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplRedBlue, ProjectID: "web",
		Task: "Revisa", MaxIterations: ptr(1)}, "x")
	h.step(t, "Ataque", exited(0, "VEREDICTO: APROBADO"))
	if p2, _ := h.pipeline(t, pl2.ID); p2.Outcome != OutcomeApproved || len(p2.Steps) != 1 {
		t.Fatalf("%+v", p2)
	}
}

func TestEvaluationPipeline(t *testing.T) {
	h := newHarness(t)
	e, err := h.o.SaveEval(EvalInput{Name: "README claro", ProjectID: "web", Kind: KindChange,
		Prompt: "Mejora el README", Criteria: "Debe explicar la instalación"}, "x")
	if err != nil || e.ID != "readme-claro" {
		t.Fatalf("%+v %v", e, err)
	}
	pls, err := h.o.RunEvals("linus", []string{e.ID}, "x")
	if err != nil || len(pls) != 1 || pls[0].Template != TplEval || pls[0].Participants["judge"] != "grace" {
		t.Fatalf("%+v %v", pls, err)
	}
	_, cand := h.step(t, "Candidato", withChanges(exited(0, "He mejorado el README."), 1))
	spec, judge := h.step(t, "Juez", exited(0, "Bien.\nPUNTUACION: 8"))
	if judge.Kind != KindJudge || judge.ApplyFrom != cand.ID || !strings.Contains(spec.Prompt, "Debe explicar la instalación") ||
		!strings.Contains(spec.Prompt, "He mejorado el README.") || judge.Score == nil || *judge.Score != 8 {
		t.Fatalf("%+v\n%s", judge, spec.Prompt)
	}
	p, _ := h.pipeline(t, pls[0].ID)
	if p.Status != PipelineDone || p.Score == nil || *p.Score != 8 || p.Outcome != "puntuación 8/10" {
		t.Fatalf("%+v", p)
	}
	m := h.o.Metrics()
	found := false
	for _, mm := range m.ByAgentVersion {
		if mm.AgentID == "linus" && mm.EvalRuns == 1 && mm.EvalScoreAvg == 8 {
			found = true
		}
	}
	if !found {
		t.Fatalf("%+v", m.ByAgentVersion)
	}
	if _, err := h.o.RunEvals("linus", nil, "x"); err == nil {
		t.Fatal("empty run accepted")
	}
	if err := h.o.DeleteEval(e.ID, "x"); err != nil {
		t.Fatal(err)
	}
}

// A cambio step without applicable changes ends the pipeline with an
// explicit outcome instead of reviewing an untouched tree; a capture
// error alone does not spoil a complete patch.
func TestTeamStopsWithoutApplicableChanges(t *testing.T) {
	cases := []struct {
		name    string
		res     harness.Result
		outcome string
		status  string
	}{
		{"no changes", exited(0, "No hacía falta cambiar nada."), OutcomeNoChanges, PipelineDone},
		{"empty change list", func() harness.Result {
			r := withChanges(exited(0, "nada"), 0)
			r.Changes.Patch = nil
			return r
		}(), OutcomeNoChanges, PipelineDone},
		{"truncated patch", func() harness.Result {
			r := withChanges(exited(0, "enorme"), 3)
			r.Changes.PatchTruncated = true
			return r
		}(), OutcomeTruncated, PipelineFailed},
		{"capture failed", func() harness.Result {
			r := withChanges(exited(0, "x"), 0)
			r.Changes.Patch, r.Changes.Error = nil, "git add falló"
			return r
		}(), OutcomeNoCapture, PipelineFailed},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "x"}, "x")
			if err != nil {
				t.Fatal(err)
			}
			h.step(t, "Plan", exited(0, "plan"))
			h.step(t, "Implementación", c.res)
			p, _ := h.pipeline(t, pl.ID)
			if p.Status != c.status || p.Outcome != c.outcome || stepNames(p) != "Plan,Implementación" {
				t.Fatalf("%+v", p)
			}
			if h.dispatch(t) != "" {
				t.Fatal("a review of nothing was queued")
			}
		})
	}
	// The contents could not be collected, but the patch is whole: the
	// review applies it.
	h := newHarness(t)
	h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "x"}, "x")
	h.step(t, "Plan", exited(0, "plan"))
	res := withChanges(exited(0, "hecho"), 1)
	res.Changes.Error, res.Changes.ContentsComplete = "git archive falló", false
	_, impl := h.step(t, "Implementación", res)
	if !hasApplicableChanges(&impl) || impl.Changes.ContentsComplete {
		t.Fatalf("%+v", impl.Changes)
	}
	spec, rev := h.step(t, "Revisión", exited(0, "VEREDICTO: APROBADO"))
	if rev.ApplyFrom != impl.ID || len(spec.ApplyPatch) == 0 {
		t.Fatalf("%+v", rev)
	}
}

func TestRedBlueStopsWithoutApplicableChanges(t *testing.T) {
	h := newHarness(t)
	pl, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplRedBlue, ProjectID: "web", Task: "x"}, "x")
	h.step(t, "Ataque", exited(0, "Inyección.\nVEREDICTO: CAMBIOS"))
	h.step(t, "Defensa", exited(0, "No he cambiado nada."))
	if p, _ := h.pipeline(t, pl.ID); p.Outcome != OutcomeNoChanges || len(p.Steps) != 2 {
		t.Fatalf("%+v", p)
	}
}

// An evaluation of a cambio task whose candidate changed nothing is not
// judged; a long result reaches the judge cut, not lost.
func TestEvaluationJudgeInput(t *testing.T) {
	h := newHarness(t)
	e, _ := h.o.SaveEval(EvalInput{Name: "Cambio", ProjectID: "web", Kind: KindChange, Prompt: "Cambia", Criteria: "c"}, "x")
	pls, err := h.o.RunEvals("linus", []string{e.ID}, "x")
	if err != nil {
		t.Fatal(err)
	}
	h.step(t, "Candidato", exited(0, "No he cambiado nada."))
	if p, _ := h.pipeline(t, pls[0].ID); p.Status != PipelineDone || p.Outcome != OutcomeNoChanges || len(p.Steps) != 1 {
		t.Fatalf("%+v", p)
	}
	// A question whose answer is far over 40 KiB.
	q, _ := h.o.SaveEval(EvalInput{Name: "Pregunta", ProjectID: "web", Kind: KindAsk, Prompt: "Explica", Criteria: "c"}, "x")
	pls, _ = h.o.RunEvals("linus", []string{q.ID}, "x")
	long := "INICIO " + strings.Repeat("palabra ", 10<<10)
	h.step(t, "Candidato", exited(0, long))
	spec, _ := h.step(t, "Juez", exited(0, "PUNTUACION: 6"))
	if !strings.Contains(spec.Prompt, "INICIO palabra") || !strings.Contains(spec.Prompt, "[… recortado …]") ||
		strings.Contains(spec.Prompt, "el candidato no dio respuesta") {
		t.Fatalf("%d bytes", len(spec.Prompt))
	}
	// A read-only agent cannot sit a cambio evaluation.
	var fe *FieldError
	if _, err := h.o.RunEvals("ada", []string{e.ID}, "x"); !asField(err, &fe) || fe.Field != "agent_id" {
		t.Fatalf("%v", err)
	}
}

// The candidate's result could not be read: the evaluation fails.
func TestEvaluationFailsWhenTheResultIsUnreadable(t *testing.T) {
	h := newHarness(t)
	q, _ := h.o.SaveEval(EvalInput{Name: "Pregunta", ProjectID: "web", Kind: KindAsk, Prompt: "Explica", Criteria: "c"}, "x")
	pls, _ := h.o.RunEvals("linus", []string{q.ID}, "x")
	task := h.dispatch(t)
	id := strings.TrimPrefix(task, "web-")
	// result.md is a directory: written atomically over, it cannot be,
	// and reading it fails.
	if err := os.MkdirAll(filepath.Join(h.o.store.jobDir(id), fileResult, "x"), 0o700); err != nil {
		t.Fatal(err)
	}
	j := h.complete(t, task, exited(0, "respuesta"))
	p, _ := h.pipeline(t, pls[0].ID)
	if p.Status != PipelineFailed || p.Outcome != outcomeNoCandidate || len(p.Steps) != 1 {
		t.Fatalf("%+v", p)
	}
	// The write failure itself is reported on the job and in the warnings.
	if !strings.Contains(j.Message, "Aviso: no se pudieron guardar en el disco result.md") ||
		!slices.ContainsFunc(h.o.Snapshot().Warnings, func(w string) bool { return strings.Contains(w, "No se pudieron guardar ficheros") }) {
		t.Fatalf("%q %v", j.Message, h.o.Snapshot().Warnings)
	}
}

// Roles that change code need an agent in completo mode.
func TestPipelineParticipantModes(t *testing.T) {
	h := newHarness(t)
	for _, req := range []PipelineRequest{
		{Template: TplTeam, ProjectID: "web", Task: "x", Participants: map[string]string{"developer": "ada"}},
		{Template: TplRedBlue, ProjectID: "web", Task: "x", Participants: map[string]string{"blue": "grace"}},
		{Template: TplImprove, Task: "x", Participants: map[string]string{"developer": "becario"}},
	} {
		_, err := h.o.CreatePipeline(context.Background(), req, "x")
		var fe *FieldError
		if !asField(err, &fe) || fe.Field != "participants" || !strings.Contains(fe.Message, "modo completo") {
			t.Errorf("%s: %v", req.Template, err)
		}
	}
	// Read-only agents are fine where nobody changes code.
	if _, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "x",
		Participants: map[string]string{"planner": "becario", "reviewer": "hedy"}}, "x"); err != nil {
		t.Fatal(err)
	}
}

// A pipeline admitted into the queue always gets its next steps, even
// when the queue filled up meanwhile; its first step counts.
func TestPipelineStepsAreExemptFromMaxQueue(t *testing.T) {
	h := newHarness(t, func(c *Config) { c.MaxQueue = 2 })
	pl, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplDebate, ProjectID: "web", Task: "x"}, "x")
	if err != nil {
		t.Fatal(err)
	}
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "lleno", Priority: ptr(0)})
	if _, err := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplDebate, ProjectID: "web", Task: "y"}, "x"); IsStatus(err) != 409 {
		t.Fatalf("a new pipeline in a full queue: %v", err)
	}
	task := h.dispatch(t) // Propuesta A leaves the queue...
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "lleno 2", Priority: ptr(0)})
	if len(h.o.Snapshot().Queue) != 2 {
		t.Fatal("the queue is not full")
	}
	h.complete(t, task, exited(0, "A")) // ...and B joins the full queue.
	p, _ := h.pipeline(t, pl.ID)
	if p.Status != PipelineRunning || stepNames(p) != "Propuesta A,Propuesta B" || len(h.o.Snapshot().Queue) != 3 {
		t.Fatalf("%+v", p)
	}
}

// A step's status follows its job at every change, not only at the end.
func TestPipelineStepStatusMirrorsTheJob(t *testing.T) {
	h := newHarness(t)
	pl, _ := h.o.CreatePipeline(context.Background(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "x"}, "x")
	task := h.dispatch(t)
	if p, _ := h.pipeline(t, pl.ID); p.Steps[0].Status != StatusPreparing {
		t.Fatalf("%+v", p.Steps[0])
	}
	h.exec.run(task).hooks.OnState(harness.StateRunning)
	if p, _ := h.pipeline(t, pl.ID); p.Steps[0].Status != StatusRunning {
		t.Fatalf("%+v", p.Steps[0])
	}
	h.complete(t, task, exited(0, "plan"))
	if p, _ := h.pipeline(t, pl.ID); p.Steps[0].Status != StatusDone || p.Steps[1].Status != StatusQueued {
		t.Fatalf("%+v", p.Steps)
	}
}
