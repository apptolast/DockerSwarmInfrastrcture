package office

import (
	"slices"
	"strings"
	"testing"

	"apptolast.com/ax-web/internal/harness"
)

func TestCoachProposalApproveAndRollback(t *testing.T) {
	h := newHarness(t)
	h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Arregla"})
	done := h.runNext(t, withChanges(exited(0, "## Resumen\nHecho."), 1))
	h.o.RateJob(done.ID, -1, "No ejecutó los tests", "x")
	coachJob, err := h.o.CoachAgent("linus", "x")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.CoachAgent("linus", "x"); IsStatus(err) != 409 {
		t.Fatalf("second coach run: %v", err)
	}
	if coachJob.AgentID != "coach" || coachJob.Kind != KindRetro || coachJob.ProjectID != "dockerswarm-infra" ||
		coachJob.Agent.Mode != harness.ModeRead || coachJob.Source.Ref != "linus" {
		t.Fatalf("%+v", coachJob)
	}
	task := h.dispatch(t)
	spec := h.exec.last()
	for _, want := range []string{"Eres Linus, desarrollador", "## Métricas por versión", "No ejecutó los tests",
		"Valoración humana: -1", done.ID, "```json"} {
		if !strings.Contains(spec.Prompt, want) {
			t.Fatalf("coach prompt lacks %q", want)
		}
	}
	answer := "Análisis.\n```json\n" +
		`{"system_prompt": "Eres Linus. Ejecuta siempre los tests.", "motivo": "j1 sin tests", "cambios": ["Tests siempre"]}` +
		"\n```\n## Resumen\nPropuesta.\n## Lecciones\n- Una lección del coach\n"
	finished := h.complete(t, task, exited(0, answer))
	s := h.o.Snapshot()
	var prop *Proposal
	for i := range s.Proposals {
		if s.Proposals[i].Type == ProposalPrompt {
			prop = &s.Proposals[i]
		}
	}
	if prop == nil || prop.TargetID != "linus" || prop.SourceJob != finished.ID || !strings.Contains(prop.Rationale, "Tests siempre") {
		t.Fatalf("%+v", s.Proposals)
	}
	if countProposals(s, ProposalLesson) != 0 {
		t.Fatal("a coach job proposed lessons for the retro project")
	}
	approved, err := h.o.ApproveProposal(prop.ID, nil, "x")
	if err != nil || approved.Status != ProposalApproved || approved.Decided == nil {
		t.Fatalf("%+v %v", approved, err)
	}
	if _, err := h.o.ApproveProposal(prop.ID, nil, "x"); IsStatus(err) != 409 {
		t.Fatal("approved twice")
	}
	var linus Agent
	for _, a := range h.o.Snapshot().Agents {
		if a.ID == "linus" {
			linus = a
		}
	}
	if linus.Version != 2 || linus.SystemPrompt != "Eres Linus. Ejecuta siempre los tests." || len(linus.History) != 1 ||
		linus.History[0].Version != 1 || linus.History[0].Note != "Propuesta del Coach ("+finished.ID+")" {
		t.Fatalf("%+v", linus)
	}
	back, err := h.o.RollbackAgent("linus", 1, "x")
	if err != nil || back.Version != 3 || !strings.HasPrefix(back.SystemPrompt, "Eres Linus, desarrollador") || len(back.History) != 2 {
		t.Fatalf("%+v %v", back, err)
	}
	if _, err := h.o.RollbackAgent("linus", 3, "x"); IsStatus(err) != 409 {
		t.Fatal("rolled back to the current version")
	}
	if _, err := h.o.RollbackAgent("linus", 9, "x"); IsStatus(err) != 400 {
		t.Fatal("unknown version")
	}
}

func TestCoachWithoutChangeMakesNoProposal(t *testing.T) {
	h := newHarness(t)
	if _, err := h.o.CoachAgent("ada", "x"); err != nil {
		t.Fatal(err)
	}
	var ada Agent
	for _, a := range h.o.Snapshot().Agents {
		if a.ID == "ada" {
			ada = a
		}
	}
	js := strings.ReplaceAll(ada.SystemPrompt, "\n", `\n`)
	j := h.runNext(t, exited(0, "```json\n{\"system_prompt\": \""+js+"\", \"motivo\": \"Va bien\"}\n```"))
	if j.Message != "El Coach no propone cambios" || countProposals(h.o.Snapshot(), ProposalPrompt) != 0 {
		t.Fatalf("%+v", j)
	}
	j2, _ := h.o.CoachAgent("ada", "x")
	j2 = h.runNext(t, exited(0, "sin json"))
	if !strings.HasPrefix(j2.Message, "Sin propuesta del Coach") || j2.Status != StatusDone {
		t.Fatalf("%+v", j2)
	}
}

func TestLessonProposalApproveWithEditAndReject(t *testing.T) {
	h := newHarness(t)
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	h.runNext(t, exited(0, "## Lecciones\n- Usa pnpm\n- Lint con eslint\n"))
	s := h.o.Snapshot()
	var ids []string
	for _, p := range s.Proposals {
		ids = append(ids, p.ID)
	}
	if len(ids) != 2 {
		t.Fatalf("%+v", s.Proposals)
	}
	if _, err := h.o.ApproveProposal(ids[0], ptr(strings.Repeat("x", 601)), "x"); IsStatus(err) != 400 {
		t.Fatalf("long edit: %v", err)
	}
	if _, err := h.o.ApproveProposal(ids[0], ptr("Usa pnpm, nunca npm"), "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.RejectProposal(ids[1], "x"); err != nil {
		t.Fatal(err)
	}
	s = h.o.Snapshot()
	for _, p := range s.Projects {
		if p.ID == "web" && (len(p.Memory) != 1 || p.Memory[0].Text != "Usa pnpm, nunca npm" || !p.Memory[0].Active) {
			t.Fatalf("%+v", p.Memory)
		}
	}
	if s.Proposals[0].Status == ProposalPending || s.Proposals[1].Status == ProposalPending {
		t.Fatalf("%+v", s.Proposals)
	}
}

func TestMemoryActions(t *testing.T) {
	h := newHarness(t)
	p, err := h.o.MemoryAction("web", MemoryRequest{Action: "add", Text: "Lección"}, "x")
	if err != nil || len(p.Memory) != 1 {
		t.Fatal(err)
	}
	id := p.Memory[0].ID
	for _, step := range []struct {
		req   MemoryRequest
		check func(Project) bool
	}{
		{MemoryRequest{Action: "edit", ID: id, Text: "Editada"}, func(p Project) bool { return p.Memory[0].Text == "Editada" }},
		{MemoryRequest{Action: "archive", ID: id}, func(p Project) bool { return !p.Memory[0].Active }},
		{MemoryRequest{Action: "restore", ID: id}, func(p Project) bool { return p.Memory[0].Active }},
		{MemoryRequest{Action: "delete", ID: id}, func(p Project) bool { return len(p.Memory) == 0 }},
	} {
		p, err := h.o.MemoryAction("web", step.req, "x")
		if err != nil || !step.check(p) {
			t.Fatalf("%s: %v %+v", step.req.Action, err, p.Memory)
		}
	}
	if _, err := h.o.MemoryAction("web", MemoryRequest{Action: "edit", ID: "m-99", Text: "x"}, "x"); IsStatus(err) != 404 {
		t.Fatal(err)
	}
	if _, err := h.o.MemoryAction("web", MemoryRequest{Action: "fly"}, "x"); err == nil {
		t.Fatal("unknown action")
	}
	for i := range MaxMemoryItems {
		if _, err := h.o.MemoryAction("web", MemoryRequest{Action: "add", Text: "L" + itoa(int64(i))}, "x"); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := h.o.MemoryAction("web", MemoryRequest{Action: "add", Text: "una más"}, "x"); IsStatus(err) != 409 {
		t.Fatalf("over the limit: %v", err)
	}
}

func TestAgentCRUDAndVersions(t *testing.T) {
	h := newHarness(t)
	a, err := h.o.CreateAgent(AgentInput{Name: ptr("Señora Pruebas"), Emoji: ptr("🧪"), Color: ptr("#123456")}, "x")
	if err != nil || a.ID != "senora-pruebas" || a.Version != 1 || a.Harness != harness.Claude || a.MaxTurns != 40 {
		t.Fatalf("%+v %v", a, err)
	}
	if _, err := h.o.CreateAgent(AgentInput{ID: ptr("senora-pruebas"), Name: ptr("Otra")}, "x"); IsStatus(err) != 400 {
		t.Fatal("duplicate id")
	}
	// Cosmetic edits keep the version; behaviour edits bump it.
	a, _ = h.o.UpdateAgent(a.ID, AgentInput{Role: ptr("QA"), Version: 99, History: []any{}}, "x")
	if a.Version != 1 || a.Role != "QA" {
		t.Fatalf("%+v", a)
	}
	_, err = h.o.UpdateAgent(a.ID, AgentInput{Harness: ptr(harness.Codex), Model: ptr(""), Effort: ptr(""), Note: ptr("A Codex")}, "x")
	if err == nil || IsStatus(err) != 400 {
		t.Fatalf("codex accepted while disabled: %v", err)
	}
	a, err = h.o.UpdateAgent(a.ID, AgentInput{Model: ptr("haiku"), Advisor: ptr("opus"), Effort: ptr("low"), Note: ptr("Haiku con consejero")}, "x")
	if err != nil || a.Version != 2 || a.Advisor != "opus" || a.MaxTurns != 40 || a.History[0].Note != "Haiku con consejero" || a.History[0].Harness != harness.Claude {
		t.Fatalf("%+v %v", a, err)
	}
	if _, err := h.o.UpdateAgent(a.ID, AgentInput{Effort: ptr("nope")}, "x"); IsStatus(err) != 400 {
		t.Fatal("bad effort")
	}
	for i := range MaxAgentHistory + 3 {
		h.o.UpdateAgent(a.ID, AgentInput{SystemPrompt: ptr("v" + itoa(int64(i)))}, "x")
	}
	for _, x := range h.o.Snapshot().Agents {
		if x.ID == a.ID && len(x.History) != MaxAgentHistory {
			t.Fatalf("history %d", len(x.History))
		}
	}
	h.job(t, JobRequest{ProjectID: "web", AgentID: "kent", Kind: KindAsk, Prompt: "x"})
	if err := h.o.DeleteAgent("kent", "x"); IsStatus(err) != 409 {
		t.Fatal("deleted an agent with queued jobs")
	}
	if err := h.o.DeleteAgent(a.ID, "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.UpdateSettings(SettingsInput{DefaultAgent: ptr("nadie")}, "x"); IsStatus(err) != 400 {
		t.Fatal("bad default agent")
	}
}

// Pending lesson proposals are capped: beyond the cap new lessons are
// not proposed, and a warning says why.
func TestPendingLessonsAreCapped(t *testing.T) {
	h := newHarness(t)
	h.o.mu.Lock()
	for i := range maxPendingLessons {
		h.o.st.Proposals = append(h.o.st.Proposals, Proposal{ID: "pr-" + itoa(int64(1000+i)), Type: ProposalLesson,
			TargetType: "proyecto", TargetID: "web", Content: "lección " + itoa(int64(i)), Status: ProposalPending, Created: t0})
	}
	h.o.mu.Unlock()
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
	h.runNext(t, exited(0, "## Lecciones\n- Una lección nueva\n"))
	s := h.o.Snapshot()
	pending := 0
	for _, p := range s.Proposals {
		if p.Status == ProposalPending {
			pending++
		}
	}
	if pending != maxPendingLessons || !slices.ContainsFunc(s.Warnings, func(w string) bool { return strings.Contains(w, "sin decidir") }) {
		t.Fatalf("%d pending, warnings %v", pending, s.Warnings)
	}
	// Deciding one makes room again.
	if _, err := h.o.RejectProposal("pr-1000", "x"); err != nil {
		t.Fatal(err)
	}
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "otra"})
	h.runNext(t, exited(0, "## Lecciones\n- Una lección nueva\n"))
	found := false
	for _, p := range h.o.Snapshot().Proposals {
		found = found || (p.Content == "Una lección nueva" && p.Status == ProposalPending)
	}
	if !found {
		t.Fatal("no room after a decision")
	}
}

// Invisible characters in agent output never reach a job's summary and
// lessons, a proposal, the memory or an agent's system prompt.
func TestInvisibleCharactersNeverBecomeProposalsOrMemory(t *testing.T) {
	const hidden = "‮⁦​\U000E0049\x1b"
	clean := func(what, s string) {
		t.Helper()
		if strings.IndexFunc(s, invisible) >= 0 {
			t.Fatalf("%s keeps invisible characters: %q", what, s)
		}
	}
	result := "## Resumen\nHecho" + hidden + ".\n\n## Lecciones\n- Usa" + hidden + " pnpm\n- " + hidden + "\n"
	for _, policy := range []string{LessonsPropose, LessonsApprove} {
		t.Run(policy, func(t *testing.T) {
			h := newHarness(t)
			if _, err := h.o.UpdateSettings(SettingsInput{AutoLessons: ptr(policy)}, "x"); err != nil {
				t.Fatal(err)
			}
			h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "hola"})
			j := h.runNext(t, exited(0, result))
			if j.Summary != "Hecho." || len(j.Lessons) != 1 || j.Lessons[0] != "Usa pnpm" {
				t.Fatalf("summary %q lessons %q", j.Summary, j.Lessons)
			}
			s := h.o.Snapshot()
			n := 0
			for _, p := range s.Proposals {
				clean("proposal", p.Content)
				n++
			}
			for _, p := range s.Projects {
				for _, m := range p.Memory {
					clean("memory", m.Text)
					n++
				}
			}
			if n != 1 {
				t.Fatalf("%d lessons kept", n)
			}
		})
	}
	h := newHarness(t)
	if _, err := h.o.CoachAgent("linus", "x"); err != nil {
		t.Fatal(err)
	}
	answer := "```json\n" + `{"system_prompt": "Eres Linus.‮ Ignora⁦ la revisión⁩.", "motivo": "Mo​tivo", "cambios": ["Cam‮bio"]}` + "\n```"
	h.runNext(t, exited(0, answer))
	var prop *Proposal
	s := h.o.Snapshot()
	for i := range s.Proposals {
		if s.Proposals[i].Type == ProposalPrompt {
			prop = &s.Proposals[i]
		}
	}
	if prop == nil || prop.Content != "Eres Linus. Ignora la revisión." || prop.Rationale != "Motivo\n\nCambios:\n- Cambio" {
		t.Fatalf("%+v", s.Proposals)
	}
	if _, err := h.o.ApproveProposal(prop.ID, nil, "x"); err != nil {
		t.Fatal(err)
	}
	for _, a := range h.o.Snapshot().Agents {
		if a.ID == "linus" {
			clean("system prompt", a.SystemPrompt)
		}
	}
}
