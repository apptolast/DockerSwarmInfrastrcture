package office

import (
	"fmt"
	"slices"
	"strings"

	"apptolast.com/ax-web/internal/harness"
)

// Bounds of the improvement loop.
const (
	coachJobs        = 15
	coachSummaryLen  = 600
	maxEvalsPerRun   = 50
	coachAgentID     = "coach"
	judgeAgentID     = "grace"
	maxCoachPromptKB = 12
)

// applyLessonsLocked turns a finished job's lessons into proposals or
// memory, following Settings.AutoLessons; repeated texts are skipped.
func (o *Office) applyLessonsLocked(j *Job) {
	policy := o.st.Settings.AutoLessons
	if policy == LessonsOff || len(j.Lessons) == 0 {
		return
	}
	p := o.projectLocked(j.ProjectID)
	if p == nil {
		return
	}
	known := func(t string) bool {
		for _, m := range p.Memory {
			if m.Text == t {
				return true
			}
		}
		for _, pr := range o.st.Proposals {
			if pr.Type == ProposalLesson && pr.TargetID == p.ID && pr.Content == t &&
				(pr.Status == ProposalPending || pr.Status == ProposalApproved) {
				return true
			}
		}
		return false
	}
	pending := 0
	for _, pr := range o.st.Proposals {
		if pr.Type == ProposalLesson && pr.Status == ProposalPending {
			pending++
		}
	}
	for _, l := range j.Lessons {
		t, err := validateMemoryText(stripInvisible(l))
		if err != nil || known(t) {
			continue
		}
		if policy == LessonsPropose && pending >= maxPendingLessons {
			o.warnOnce(fmt.Sprintf("Hay %d lecciones propuestas sin decidir: no se proponen más hasta que decidas algunas.",
				maxPendingLessons))
			break
		}
		if policy == LessonsApprove {
			if len(p.Memory) >= MaxMemoryItems {
				continue
			}
			p.Memory = append(p.Memory, MemoryItem{ID: fmt.Sprintf("m-%d", o.nextSeq("memory")), Text: t,
				SourceJob: j.ID, Active: true, Created: o.now()})
			o.invalidateLocked("projects")
			continue
		}
		o.st.Proposals = append(o.st.Proposals, Proposal{
			ID: fmt.Sprintf("pr-%d", o.nextSeq("proposal")), Type: ProposalLesson, TargetType: "proyecto",
			TargetID: p.ID, Content: t, Rationale: fmt.Sprintf("Lección del trabajo %s (%s)", j.ID, j.Agent.Name),
			SourceJob: j.ID, Status: ProposalPending, Created: o.now(),
		})
		pending++
		o.invalidateLocked("proposals")
	}
	o.pruneProposalsLocked()
}

// pruneProposalsLocked keeps every pending proposal and the newest
// decided ones.
func (o *Office) pruneProposalsLocked() {
	decided := 0
	for _, p := range o.st.Proposals {
		if p.Status != ProposalPending {
			decided++
		}
	}
	excess := decided - keepDecided
	if excess <= 0 {
		return
	}
	kept := o.st.Proposals[:0]
	for _, p := range o.st.Proposals {
		if excess > 0 && p.Status != ProposalPending {
			excess--
			continue
		}
		kept = append(kept, p)
	}
	o.st.Proposals = kept
}

// bumpVersionLocked archives an agent's behaviour in History and starts
// a new version.
func (o *Office) bumpVersionLocked(a *Agent, note string) {
	a.History = append(a.History, AgentRev{Version: a.Version, Harness: a.Harness, Model: a.Model,
		Effort: a.Effort, Mode: a.Mode, SystemPrompt: a.SystemPrompt, Note: note, At: o.now()})
	if len(a.History) > MaxAgentHistory {
		a.History = slices.Delete(a.History, 0, len(a.History)-MaxAgentHistory)
	}
	a.Version++
	a.Updated = o.now()
}

// ApproveProposal applies a pending proposal, with content replacing its
// text when given.
func (o *Office) ApproveProposal(id string, content *string, clientIP string) (Proposal, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	p := o.proposalLocked(id)
	if p == nil {
		return Proposal{}, notFound("No existe esa propuesta")
	}
	if p.Status != ProposalPending {
		return Proposal{}, conflict("La propuesta ya se decidió")
	}
	body := p.Content
	if content != nil {
		body = *content
	}
	switch p.Type {
	case ProposalLesson:
		pj := o.projectLocked(p.TargetID)
		if pj == nil {
			return Proposal{}, conflict("El proyecto de la lección ya no existe")
		}
		t, err := validateMemoryText(body)
		if err != nil {
			return Proposal{}, &FieldError{Field: "content", Message: err.(*FieldError).Message}
		}
		if len(pj.Memory) >= MaxMemoryItems {
			return Proposal{}, conflict(fmt.Sprintf("La memoria del proyecto está llena (%d)", MaxMemoryItems))
		}
		pj.Memory = append(pj.Memory, MemoryItem{ID: fmt.Sprintf("m-%d", o.nextSeq("memory")), Text: t,
			SourceJob: p.SourceJob, Active: true, Created: o.now()})
		p.Content = t
		o.invalidateLocked("projects")
	case ProposalPrompt:
		a := o.agentLocked(p.TargetID)
		if a == nil {
			return Proposal{}, conflict("El agente de la propuesta ya no existe")
		}
		t, err := text("content", strings.TrimSpace(body), MaxSystemPromptBytes, true)
		if err != nil {
			return Proposal{}, err
		}
		o.bumpVersionLocked(a, "Propuesta del Coach ("+p.SourceJob+")")
		a.SystemPrompt = t
		p.Content = t
		o.invalidateLocked("agents")
	default:
		return Proposal{}, conflict("Tipo de propuesta desconocido")
	}
	now := o.now()
	p.Status, p.Decided = ProposalApproved, &now
	out := *p
	o.invalidateLocked("proposals", "metrics")
	o.pruneProposalsLocked()
	o.saveLocked(true, false)
	o.auditAction("proposal.approve", clientIP, "proposal", id, "type", out.Type, "target", out.TargetID,
		"edited", content != nil)
	return out, nil
}

// RejectProposal discards a pending proposal.
func (o *Office) RejectProposal(id, clientIP string) (Proposal, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	p := o.proposalLocked(id)
	if p == nil {
		return Proposal{}, notFound("No existe esa propuesta")
	}
	if p.Status != ProposalPending {
		return Proposal{}, conflict("La propuesta ya se decidió")
	}
	now := o.now()
	p.Status, p.Decided = ProposalRejected, &now
	out := *p
	o.invalidateLocked("proposals")
	o.pruneProposalsLocked()
	o.saveLocked(true, false)
	o.auditAction("proposal.reject", clientIP, "proposal", id)
	return out, nil
}

// coachPromptLocked is the coach's task about one agent.
func (o *Office) coachPromptLocked(a *Agent) string {
	var b strings.Builder
	fmt.Fprintf(&b, "Revisa al agente %s («%s», id %s) y propón una versión mejorada de su prompt de sistema.\n\n", a.Name, a.Role, a.ID)
	fmt.Fprintf(&b, "## Configuración actual (versión %d)\n- Agente: %s\n- Modelo: %s\n- Esfuerzo: %s\n- Modo: %s\n- Turnos máximos: %d\n- Tiempo máximo: %d min\n\n",
		a.Version, a.Harness, orDefault(a.Model), orDefault(a.Effort), a.Mode, a.MaxTurns, a.TimeoutMinutes)
	f := fence(a.SystemPrompt)
	fmt.Fprintf(&b, "## Prompt de sistema actual\n%stext\n%s\n%s\n\n", f, a.SystemPrompt, f)
	m := o.metricsLocked()
	b.WriteString("## Métricas por versión\n")
	found := false
	for _, mm := range m.ByAgentVersion {
		if mm.AgentID != a.ID {
			continue
		}
		found = true
		fmt.Fprintf(&b, "- v%d: %d trabajos, %d hechos, %d fallidos, 👍 %d / 👎 %d, revisiones de sus cambios: %d aprobadas y %d con cambios, evaluaciones: %d (media %.1f), coste medio %.2f USD, duración media %.0f s, turnos medios %.1f\n",
			mm.Version, mm.Jobs, mm.Succeeded, mm.Failed, mm.ThumbsUp, mm.ThumbsDown, mm.Approved, mm.Rejected,
			mm.EvalRuns, mm.EvalScoreAvg, mm.AvgCostUSD, mm.AvgSeconds, mm.AvgTurns)
	}
	if !found {
		b.WriteString("- (aún sin trabajos terminados)\n")
	}
	b.WriteString("\n## Últimos trabajos terminados\n")
	n := 0
	for i := len(o.order) - 1; i >= 0 && n < coachJobs; i-- {
		j := o.jobs[o.order[i]]
		if j.AgentID != a.ID || !isTerminal(j.Status) {
			continue
		}
		n++
		fmt.Fprintf(&b, "### %s · %s (%s, v%d)\n- Estado: %s", j.ID, j.Title, j.Kind, j.Agent.Version, j.Status)
		if j.Verdict != "" {
			fmt.Fprintf(&b, "; su veredicto: %s", j.Verdict)
		}
		if j.Message != "" {
			fmt.Fprintf(&b, "; mensaje: %s", truncRunes(j.Message, 200))
		}
		fmt.Fprintf(&b, "\n- Coste: %.2f USD, turnos: %d\n", j.Usage.CostUSD, j.Usage.Turns)
		for _, r := range o.jobs {
			if r.ApplyFrom == j.ID && r.Kind == KindReview && r.Verdict != "" {
				fmt.Fprintf(&b, "- Revisión de sus cambios (%s, %s): %s\n", r.ID, r.Agent.Name, r.Verdict)
			}
		}
		if j.Rating != nil {
			fmt.Fprintf(&b, "- Valoración humana: %+d", j.Rating.Score)
			if j.Rating.Note != "" {
				fmt.Fprintf(&b, " — «%s»", truncRunes(j.Rating.Note, 300))
			}
			b.WriteString("\n")
		}
		if j.Summary != "" {
			fmt.Fprintf(&b, "- Resumen: %s\n", truncRunes(strings.Join(strings.Fields(j.Summary), " "), coachSummaryLen))
		}
	}
	if n == 0 {
		b.WriteString("(ninguno todavía: propón solo mejoras claras de redacción, o deja el prompt igual)\n")
	}
	fmt.Fprintf(&b, "\nEl prompt propuesto debe ir en español y no superar %d KiB.", maxCoachPromptKB)
	return b.String()
}

// CoachAgent asks the coach to propose a better persona for an agent.
func (o *Office) CoachAgent(agentID, clientIP string) (Job, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	a := o.agentLocked(agentID)
	if a == nil {
		return Job{}, notFound("No existe ese agente")
	}
	coach := o.agentLocked(coachAgentID)
	if coach == nil || !coach.Enabled {
		return Job{}, conflict("El Coach no existe o está desactivado")
	}
	if o.projectLocked(o.st.Settings.RetroProject) == nil {
		return Job{}, conflict("Falta el proyecto de retrospectivas en los ajustes")
	}
	for _, j := range o.jobs {
		if j.Kind == KindRetro && j.Source.Ref == agentID && !isTerminal(j.Status) {
			return Job{}, conflict("Ya hay una revisión del Coach en marcha para " + a.Name)
		}
	}
	j, err := o.enqueueLocked(newJob{
		ProjectID: o.st.Settings.RetroProject, AgentID: coachAgentID, Kind: KindRetro,
		Prompt: o.coachPromptLocked(a), Title: "Coach · mejora de " + a.Name, Priority: 1,
		Overrides: &Overrides{Mode: harness.ModeRead}, Source: Source{Type: "coach", Ref: agentID},
		internal: true,
	})
	if err != nil {
		return Job{}, err
	}
	o.saveLocked(false, true)
	o.auditAction("agent.coach", clientIP, "agent", agentID, "job", j.ID)
	return *j, nil
}

// coachProposalLocked turns a coach job's answer into a prompt proposal.
func (o *Office) coachProposalLocked(j *Job, result string) error {
	cp, err := ParseCoach(result)
	if err != nil {
		return err
	}
	a := o.agentLocked(j.Source.Ref)
	if a == nil {
		return fmt.Errorf("el agente %s ya no existe", j.Source.Ref)
	}
	if strings.TrimSpace(a.SystemPrompt) == cp.SystemPrompt {
		j.Message = "El Coach no propone cambios"
		return nil
	}
	rationale := cp.Motivo
	if len(cp.Cambios) > 0 {
		rationale += "\n\nCambios:\n- " + strings.Join(cp.Cambios, "\n- ")
	}
	o.st.Proposals = append(o.st.Proposals, Proposal{
		ID: fmt.Sprintf("pr-%d", o.nextSeq("proposal")), Type: ProposalPrompt, TargetType: "agente",
		TargetID: a.ID, Content: cp.SystemPrompt, Rationale: strings.TrimSpace(rationale), SourceJob: j.ID,
		Status: ProposalPending, Created: o.now(),
	})
	o.invalidateLocked("proposals")
	return nil
}

// RollbackAgent restores a previous version of an agent as a new
// version.
func (o *Office) RollbackAgent(agentID string, version int, clientIP string) (Agent, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	a := o.agentLocked(agentID)
	if a == nil {
		return Agent{}, notFound("No existe ese agente")
	}
	if version == a.Version {
		return Agent{}, conflict("Esa ya es la versión actual")
	}
	i := slices.IndexFunc(a.History, func(r AgentRev) bool { return r.Version == version })
	if i < 0 {
		return Agent{}, fieldErr("version", "No existe esa versión en el historial")
	}
	rev := a.History[i]
	trial := *a
	trial.Harness, trial.Model, trial.Effort, trial.Mode, trial.SystemPrompt = rev.Harness, rev.Model, rev.Effort, rev.Mode, rev.SystemPrompt
	if trial.Harness != a.Harness {
		trial.FallbackModel = ""
		if trial.Harness == harness.Codex {
			trial.MaxTurns = 0
		} else if trial.MaxTurns == 0 {
			trial.MaxTurns = min(40, o.cfg.Limits.MaxTurns)
		}
	}
	if err := validateAgent(&trial, o.cfg.Limits); err != nil {
		return Agent{}, err
	}
	o.bumpVersionLocked(a, fmt.Sprintf("Sustituida al restaurar la versión %d", version))
	a.Harness, a.Model, a.Effort, a.Mode, a.SystemPrompt = trial.Harness, trial.Model, trial.Effort, trial.Mode, trial.SystemPrompt
	a.FallbackModel, a.MaxTurns = trial.FallbackModel, trial.MaxTurns
	o.invalidateLocked("agents", "metrics")
	o.saveLocked(true, false)
	o.auditAction("agent.rollback", clientIP, "agent", agentID, "version", version, "new_version", a.Version)
	return cloneAgent(*a), nil
}

// EvalInput is POST /api/evals (an update when ID is given).
type EvalInput struct {
	ID        string `json:"id,omitempty"`
	Name      string `json:"name"`
	ProjectID string `json:"project_id"`
	Kind      string `json:"kind"`
	Prompt    string `json:"prompt"`
	Criteria  string `json:"criteria"`
	Created   any    `json:"created,omitempty"`
}

// SaveEval creates or updates an evaluation of the bench.
func (o *Office) SaveEval(in EvalInput, clientIP string) (Eval, error) {
	name, err := line("name", in.Name, MaxNameRunes, true)
	if err != nil {
		return Eval{}, err
	}
	if !validKind(in.Kind) {
		return Eval{}, fieldErr("kind", "Tipo de trabajo no válido")
	}
	prompt, err := text("prompt", in.Prompt, MaxJobPromptBytes, true)
	if err != nil {
		return Eval{}, err
	}
	criteria, err := text("criteria", in.Criteria, MaxCriteriaBytes, true)
	if err != nil {
		return Eval{}, err
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.projectLocked(in.ProjectID) == nil {
		return Eval{}, fieldErr("project_id", "No existe ese proyecto")
	}
	var e *Eval
	if in.ID != "" {
		if e = o.evalLocked(in.ID); e == nil {
			return Eval{}, notFound("No existe esa evaluación")
		}
	} else {
		id := uniqueID(slug(name, "eval"), func(s string) bool { return o.evalLocked(s) != nil })
		o.st.Evals = append(o.st.Evals, Eval{ID: id, Created: o.now()})
		e = &o.st.Evals[len(o.st.Evals)-1]
	}
	e.Name, e.ProjectID, e.Kind, e.Prompt, e.Criteria = name, in.ProjectID, in.Kind, prompt, criteria
	out := *e
	o.invalidateLocked("evals")
	o.saveLocked(true, false)
	o.auditAction("eval.save", clientIP, append([]any{"eval", out.ID}, promptAttrs(prompt)...)...)
	return out, nil
}

// DeleteEval removes an evaluation of the bench.
func (o *Office) DeleteEval(id, clientIP string) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	i := slices.IndexFunc(o.st.Evals, func(e Eval) bool { return e.ID == id })
	if i < 0 {
		return notFound("No existe esa evaluación")
	}
	o.st.Evals = slices.Delete(o.st.Evals, i, i+1)
	o.invalidateLocked("evals")
	o.saveLocked(true, false)
	o.auditAction("eval.delete", clientIP, "eval", id)
	return nil
}

// RunEvals starts one evaluation pipeline per eval for an agent.
func (o *Office) RunEvals(agentID string, evalIDs []string, clientIP string) ([]Pipeline, error) {
	if len(evalIDs) == 0 || len(evalIDs) > maxEvalsPerRun {
		return nil, fieldErr("eval_ids", fmt.Sprintf("Elige de 1 a %d evaluaciones", maxEvalsPerRun))
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	a := o.agentLocked(agentID)
	if a == nil || !a.Enabled {
		return nil, fieldErr("agent_id", "El agente no existe o está desactivado")
	}
	judge := o.agentLocked(judgeAgentID)
	if judge == nil || !judge.Enabled {
		return nil, conflict("El juez (Grace) no existe o está desactivado")
	}
	var evals []Eval
	for _, id := range evalIDs {
		e := o.evalLocked(id)
		if e == nil {
			return nil, fieldErr("eval_ids", "No existe la evaluación "+truncRunes(id, 40))
		}
		if e.Kind == KindChange && a.Mode != harness.ModeFull {
			return nil, fieldErr("agent_id", fmt.Sprintf("La evaluación «%s» pide cambios y %s está en modo lectura: necesita modo completo",
				e.Name, a.Name))
		}
		evals = append(evals, *e)
	}
	if o.queuedCountLocked()+len(evals) > o.cfg.MaxQueue {
		return nil, conflict(fmt.Sprintf("La cola está llena (%d)", o.cfg.MaxQueue))
	}
	out := []Pipeline{}
	for _, e := range evals {
		pl := Pipeline{
			ID: fmt.Sprintf("p-%d", o.nextSeq("pipeline")), Template: TplEval,
			Title:     truncRunes("Evaluación: "+e.Name+" · "+a.Name, MaxTitleRunes),
			ProjectID: e.ProjectID, Task: e.Prompt, Participants: map[string]string{"candidate": a.ID, "judge": judgeAgentID},
			Status: PipelineRunning, MaxIterations: 1, Steps: []PipelineStep{}, Source: Source{Type: "eval", Ref: e.ID},
			Priority: 0, Created: o.now(),
		}
		p := o.projectLocked(e.ProjectID)
		if p == nil {
			return out, conflict("El proyecto de la evaluación " + e.Name + " ya no existe")
		}
		pl.Branch = p.Branch
		created, err := o.startPipelineLocked(pl, nil, clientIP)
		if err != nil {
			return out, err
		}
		out = append(out, created)
	}
	return out, nil
}
