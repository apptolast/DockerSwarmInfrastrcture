package office

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"apptolast.com/ax-web/internal/harness"
	"apptolast.com/ax-web/internal/runs"
)

// Template ids.
const (
	TplTeam     = "equipo"
	TplPanel    = "panel"
	TplDebate   = "debate"
	TplRedBlue  = "rojoazul"
	TplEval     = "evaluacion"
	TplImprove  = "mejora"
	pipelineDir = "pipelines"
)

// Pipeline outcomes.
const (
	OutcomeApproved   = "aprobado"
	OutcomePending    = "cambios pendientes"
	OutcomeNoVerdict  = "sin veredicto"
	OutcomeDecided    = "decidido"
	OutcomeNoScore    = "sin puntuación"
	outcomeStepFailed = "un paso falló"
	// A cambio step whose changes a later step cannot apply ends the
	// pipeline instead of reviewing (or judging) an untouched tree.
	OutcomeNoChanges   = "sin cambios aplicables"
	OutcomeTruncated   = "parche truncado"
	OutcomeNoCapture   = "no se pudieron recoger los cambios"
	outcomeNoCandidate = "no se pudo leer el resultado del candidato"
)

// changeRoles are the roles of a template whose steps change the code:
// their agent must be in completo mode.
var changeRoles = map[string][]string{
	TplTeam:    {"developer"},
	TplImprove: {"developer"},
	TplRedBlue: {"blue"},
}

// changesOutcome tells why a finished cambio step cannot be continued by
// a step that applies its changes ("" when it can) and whether that is a
// failure.
func changesOutcome(j *Job) (string, bool) {
	switch {
	case j != nil && hasApplicableChanges(j):
		return "", false
	case j != nil && j.Changes != nil && j.Changes.PatchTruncated:
		return OutcomeTruncated, true
	case j != nil && j.Changes != nil && j.Changes.Error != "":
		return OutcomeNoCapture, true
	}
	return OutcomeNoChanges, false
}

var pipelineIDRe = regexp.MustCompile(`^p-[0-9]{1,12}$`)

var templates = []Template{
	{ID: TplTeam, Name: "Equipo: plan → código → revisión", Icon: "🧑‍🤝‍🧑", Iterative: true,
		Description: "Ada planifica, Linus implementa y Grace revisa; si la revisión pide cambios, Linus corrige y Grace vuelve a revisar hasta el máximo de iteraciones.",
		Roles:       []TemplateRole{{"planner", "Planifica", "ada"}, {"developer", "Implementa", "linus"}, {"reviewer", "Revisa", "grace"}}},
	{ID: TplPanel, Name: "Panel de revisión", Icon: "🔍", Needs: "patch",
		Description: "Tres revisiones independientes (corrección, seguridad y tests) y una síntesis con el veredicto. Puede revisar los cambios de un trabajo, un pull request o el estado actual de la rama.",
		Roles:       []TemplateRole{{"correctness", "Corrección", "grace"}, {"security", "Seguridad", "hedy"}, {"tests", "Tests", "kent"}, {"chair", "Síntesis", "ada"}}},
	{ID: TplDebate, Name: "Debate de diseño", Icon: "💬",
		Description: "Tres propuestas independientes (una de ellas con Codex) y una decisión razonada de quien preside.",
		Roles:       []TemplateRole{{"a", "Propuesta A", "ada"}, {"b", "Propuesta B", "linus"}, {"c", "Propuesta C", "hedy"}, {"chair", "Decide", "grace"}}},
	{ID: TplRedBlue, Name: "Rojo / azul (seguridad)", Icon: "🛡️", Iterative: true,
		Description: "El equipo rojo busca problemas explotables; el azul los corrige y el rojo verifica, hasta que no quede nada o se agoten las iteraciones.",
		Roles:       []TemplateRole{{"red", "Ataca", "hedy"}, {"blue", "Defiende", "linus"}}},
	{ID: TplEval, Name: "Evaluación (banco de pruebas)", Icon: "🧪",
		Description: "Un agente resuelve una tarea fija del banco de pruebas y un juez la puntúa de 0 a 10 con sus criterios. La crea el banco de pruebas.",
		Roles:       []TemplateRole{{"candidate", "Candidato", "linus"}, {"judge", "Juez", "grace"}}},
	{ID: TplImprove, Name: "Auto-mejora de la Oficina", Icon: "♻️", Iterative: true,
		Description: "Como «Equipo», sobre el propio código de la Oficina (images/ax-web del proyecto de la infraestructura).",
		Roles:       []TemplateRole{{"planner", "Planifica", "ada"}, {"developer", "Implementa", "linus"}, {"reviewer", "Revisa", "grace"}}},
}

// Templates lists the team templates.
func Templates() []Template {
	out := make([]Template, len(templates))
	for i, t := range templates {
		t.Roles = slices.Clone(t.Roles)
		out[i] = t
	}
	return out
}

func templateByID(id string) *Template {
	for i := range templates {
		if templates[i].ID == id {
			return &templates[i]
		}
	}
	return nil
}

// ImproveTask is the task the UI suggests for the "mejora" template.
const ImproveTask = "Mejora la Oficina de agentes (images/ax-web): busca un fallo, una carencia de usabilidad o de pruebas, y arréglala con un cambio pequeño, probado y documentado."

// PipelineRequest is POST /api/pipelines.
type PipelineRequest struct {
	Template      string            `json:"template"`
	ProjectID     string            `json:"project_id"`
	Task          string            `json:"task"`
	Branch        string            `json:"branch,omitempty"`
	Title         string            `json:"title,omitempty"`
	Participants  map[string]string `json:"participants,omitempty"`
	MaxIterations *int              `json:"max_iterations,omitempty"`
	Source        *Source           `json:"source,omitempty"`
	Priority      *int              `json:"priority,omitempty"`
}

func (s *store) pipelineExternalPath(id string) string {
	return filepath.Join(s.dir, pipelineDir, id+".external.json")
}

func (s *store) writePipelineExternal(id string, e *external) error {
	if !pipelineIDRe.MatchString(id) {
		return fmt.Errorf("identificador de equipo no válido")
	}
	if err := os.MkdirAll(filepath.Join(s.dir, pipelineDir), 0o700); err != nil {
		return err
	}
	data, err := jsonMarshal(e)
	if err != nil {
		return err
	}
	return writeAtomic(s.pipelineExternalPath(id), data, false)
}

func (s *store) readPipelineExternal(id string) *external {
	if !pipelineIDRe.MatchString(id) {
		return nil
	}
	data, err := readBounded(s.pipelineExternalPath(id), jobFileMax)
	if err != nil {
		return nil
	}
	var e external
	if jsonUnmarshal(data, &e) != nil {
		return nil
	}
	return &e
}

// CreatePipeline starts a team on a task.
func (o *Office) CreatePipeline(ctx context.Context, req PipelineRequest, clientIP string) (Pipeline, error) {
	tpl := templateByID(req.Template)
	if tpl == nil || tpl.ID == TplEval {
		return Pipeline{}, fieldErr("template", "Plantilla de equipo no válida")
	}
	src := Source{Type: "manual"}
	if req.Source != nil && req.Source.Type != "" && req.Source.Type != "manual" {
		switch req.Source.Type {
		case "issue", "pr":
			s, err := checkSource(req.Source)
			if err != nil {
				return Pipeline{}, err
			}
			src = s
		case "job":
			if tpl.ID != TplPanel {
				return Pipeline{}, fieldErr("source", "Solo el panel de revisión parte de un trabajo")
			}
			src = Source{Type: "job", JobID: req.Source.JobID}
		default:
			return Pipeline{}, fieldErr("source", "El origen debe ser manual, issue, pr o job")
		}
	}
	o.mu.Lock()
	projectID := req.ProjectID
	if projectID == "" && tpl.ID == TplImprove {
		projectID = o.st.Settings.RetroProject
	}
	p := o.projectLocked(projectID)
	repo := ""
	if p != nil {
		repo = p.Repo
	}
	o.mu.Unlock()
	if p == nil {
		return Pipeline{}, fieldErr("project_id", "No existe ese proyecto")
	}
	var ext *external
	if src.Type == "issue" || src.Type == "pr" {
		var err error
		if ext, err = o.externalFor(ctx, repo, &src); err != nil {
			return Pipeline{}, err
		}
	}

	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed {
		return Pipeline{}, ErrClosed
	}
	if p = o.projectLocked(projectID); p == nil || p.Archived {
		return Pipeline{}, fieldErr("project_id", "El proyecto no existe o está archivado")
	}
	task, err := text("task", req.Task, MaxJobPromptBytes, true)
	if err != nil {
		return Pipeline{}, err
	}
	title, err := line("title", req.Title, MaxTitleRunes, false)
	if err != nil {
		return Pipeline{}, err
	}
	if title == "" {
		title = truncRunes(strings.SplitN(tpl.Name, ":", 2)[0]+": "+deriveTitle(task), MaxTitleRunes)
	}
	branch := strings.TrimSpace(req.Branch)
	if branch == "" {
		branch = p.Branch
	}
	if err := runs.ValidateBranch(branch); err != nil {
		return Pipeline{}, runsError("branch", err)
	}
	maxIt := o.st.Settings.MaxIterations
	if req.MaxIterations != nil {
		maxIt = *req.MaxIterations
	}
	if maxIt < 1 || maxIt > MaxIterationsLimit {
		return Pipeline{}, fieldErr("max_iterations", fmt.Sprintf("Las iteraciones van de 1 a %d", MaxIterationsLimit))
	}
	prio := defaultPriority
	if req.Priority != nil {
		prio = *req.Priority
	}
	if prio < 0 || prio > MaxPriority {
		return Pipeline{}, fieldErr("priority", "La prioridad va de 0 (baja) a 2 (alta)")
	}
	parts, err := o.participantsLocked(tpl, req.Participants)
	if err != nil {
		return Pipeline{}, err
	}
	if src.Type == "job" {
		sj := o.jobs[src.JobID]
		if sj == nil || sj.ProjectID != p.ID || sj.Status != StatusDone || !hasApplicableChanges(sj) {
			return Pipeline{}, fieldErr("source", "El trabajo de origen debe ser un cambio terminado de este proyecto, con un parche aplicable")
		}
	}
	pl := Pipeline{
		ID: fmt.Sprintf("p-%d", o.nextSeq("pipeline")), Template: tpl.ID, Title: title, ProjectID: p.ID,
		Branch: branch, Task: task, Participants: parts, Status: PipelineRunning, MaxIterations: maxIt,
		Steps: []PipelineStep{}, Source: src, Priority: prio, Created: o.now(),
	}
	return o.startPipelineLocked(pl, ext, clientIP)
}

func (o *Office) startPipelineLocked(pl Pipeline, ext *external, clientIP string) (Pipeline, error) {
	if ext != nil {
		if err := o.store.writePipelineExternal(pl.ID, ext); err != nil {
			return Pipeline{}, &Error{Status: 500, Message: "No se pudieron guardar los datos externos"}
		}
	}
	o.st.Pipelines = append(o.st.Pipelines, pl)
	if err := o.nextStepLocked(&o.st.Pipelines[len(o.st.Pipelines)-1]); err != nil {
		o.st.Pipelines = o.st.Pipelines[:len(o.st.Pipelines)-1]
		_ = os.Remove(o.store.pipelineExternalPath(pl.ID))
		return Pipeline{}, err
	}
	o.touchPipelineLocked(pl.ID)
	o.prunePipelinesLocked()
	o.saveLocked(true, true)
	o.auditAction("pipeline.create", clientIP, append([]any{"pipeline", pl.ID, "template", pl.Template,
		"project", pl.ProjectID}, promptAttrs(pl.Task)...)...)
	return clonePipeline(*o.pipelineLocked(pl.ID)), nil
}

// participantsLocked fills the roles with the requested or default
// agents, which must exist and be enabled.
func (o *Office) participantsLocked(tpl *Template, req map[string]string) (map[string]string, error) {
	out := map[string]string{}
	for role := range req {
		if !slices.ContainsFunc(tpl.Roles, func(r TemplateRole) bool { return r.Role == role }) {
			return nil, fieldErr("participants", "Rol desconocido: "+truncRunes(role, 40))
		}
	}
	for _, r := range tpl.Roles {
		id := r.DefaultAgent
		if v := strings.TrimSpace(req[r.Role]); v != "" {
			id = v
		}
		a := o.agentLocked(id)
		if a == nil || !a.Enabled {
			return nil, fieldErr("participants", fmt.Sprintf("El agente de «%s» no existe o está desactivado", r.Label))
		}
		if slices.Contains(changeRoles[tpl.ID], r.Role) && a.Mode != harness.ModeFull {
			return nil, fieldErr("participants", fmt.Sprintf("«%s» cambia el código y %s está en modo lectura: elige un agente en modo completo",
				r.Label, a.Name))
		}
		out[r.Role] = id
	}
	return out, nil
}

// stepPlan is the next step a template wants.
type stepPlan struct {
	Name, Role, Kind string
	Prompt           string
	ApplyFrom        string
	Context          []string
}

func taskBlock(p *Pipeline) string { return "Tarea del equipo:\n" + p.Task + "\n\n" }

var panelLenses = map[string]string{
	"correctness": "corrección: lógica, casos límite, errores no tratados, concurrencia y compatibilidad",
	"security":    "seguridad: entradas no confiables, secretos, permisos, inyección y despliegue",
	"tests":       "tests: qué cubren, qué falta, si fallarían sin el cambio y si son deterministas",
}

// stepJobs maps step name prefixes to their jobs, in order.
func (o *Office) stepJobLocked(p *Pipeline, i int) *Job {
	if i < 0 || i >= len(p.Steps) {
		return nil
	}
	return o.jobs[p.Steps[i].JobID]
}

// lastStep finds the last step whose name is one of names.
func lastStep(p *Pipeline, names ...string) int {
	for i := len(p.Steps) - 1; i >= 0; i-- {
		if slices.Contains(names, p.Steps[i].Name) {
			return i
		}
	}
	return -1
}

// planNext decides a pipeline's next step from its finished ones; nil
// with an outcome means the pipeline is done (failed when failed is set).
func (o *Office) planNextLocked(p *Pipeline) (*stepPlan, string, bool) {
	n := len(p.Steps)
	jobOf := func(i int) string {
		if i < 0 || i >= n {
			return ""
		}
		return p.Steps[i].JobID
	}
	verdictOf := func(i int) string {
		if j := o.stepJobLocked(p, i); j != nil {
			return j.Verdict
		}
		return ""
	}
	applyIfChanges := func(i int) string {
		if j := o.stepJobLocked(p, i); j != nil && hasApplicableChanges(j) {
			return j.ID
		}
		return ""
	}
	switch p.Template {
	case TplTeam, TplImprove:
		plan := lastStep(p, "Plan")
		switch {
		case n == 0:
			return &stepPlan{Name: "Plan", Role: "planner", Kind: KindPlan, Prompt: taskBlock(p) +
				"Tu parte: elabora el plan de implementación que seguirá quien implementa y que comprobará quien revisa. " +
				"Sé concreto: ficheros, pasos en orden, tests y riesgos."}, "", false
		case p.Steps[n-1].Name == "Plan":
			return &stepPlan{Name: "Implementación", Role: "developer", Kind: KindChange, Context: []string{jobOf(plan)},
				Prompt: taskBlock(p) + "Tu parte: implementa la tarea siguiendo el plan del paso «Plan» (en el contexto). " +
					"Si te apartas del plan, explica por qué."}, "", false
		case p.Steps[n-1].Name == "Implementación" || p.Steps[n-1].Name == "Corrección":
			if out, failed := changesOutcome(o.stepJobLocked(p, n-1)); out != "" {
				return nil, out, failed
			}
			ctx := []string{jobOf(plan)}
			if prev := lastStep(p, "Revisión"); prev >= 0 {
				ctx = append(ctx, jobOf(prev))
			}
			return &stepPlan{Name: "Revisión", Role: "reviewer", Kind: KindReview, ApplyFrom: applyIfChanges(n - 1),
				Context: ctx, Prompt: taskBlock(p) + "Tu parte: revisa los cambios ya aplicados en el árbol (git diff --cached) " +
					"frente a la tarea y al plan (en el contexto): que cumplan la tarea, que sean correctos y que estén probados. " +
					"Si hubo una revisión anterior, comprueba que sus hallazgos se resolvieron."}, "", false
		case p.Steps[n-1].Name == "Revisión":
			switch v := verdictOf(n - 1); {
			case v == VerdictChanges && p.Iteration < p.MaxIterations:
				p.Iteration++
				lastChange := lastStep(p, "Implementación", "Corrección")
				return &stepPlan{Name: "Corrección", Role: "developer", Kind: KindChange, ApplyFrom: applyIfChanges(lastChange),
					Context: []string{jobOf(n - 1)}, Prompt: taskBlock(p) + "Tu parte: el árbol ya contiene la implementación " +
						"anterior y la revisión (en el contexto) pide cambios. Corrige cada hallazgo crítico y alto; " +
						"para el resto, corrige o explica por qué no."}, "", false
			case v == VerdictApproved:
				return nil, OutcomeApproved, false
			case v == VerdictChanges:
				return nil, OutcomePending, false
			default:
				return nil, OutcomeNoVerdict, false
			}
		}
	case TplPanel:
		object := "el estado actual de la rama " + p.Branch
		apply := ""
		switch p.Source.Type {
		case "job":
			object, apply = "los cambios del trabajo "+p.Source.JobID+", ya aplicados en el árbol (git diff --cached)", p.Source.JobID
		case "pr":
			object = fmt.Sprintf("el pull request #%d (su diff está en los datos externos)", p.Source.Number)
		}
		lenses := []struct{ role, name string }{{"correctness", "Corrección"}, {"security", "Seguridad"}, {"tests", "Tests"}}
		if n < len(lenses) {
			l := lenses[n]
			return &stepPlan{Name: l.name, Role: l.role, Kind: KindReview, ApplyFrom: apply,
				Prompt: "Revisión en panel de " + object + ".\n\n" + taskBlock(p) +
					"Tu lente: " + panelLenses[l.role] + ". Céntrate en ella; los otros revisores cubren el resto."}, "", false
		}
		if n == len(lenses) {
			return &stepPlan{Name: "Síntesis", Role: "chair", Kind: KindReview, ApplyFrom: apply,
				Context: []string{jobOf(0), jobOf(1), jobOf(2)},
				Prompt: "Presides el panel de revisión de " + object + ".\n\n" + taskBlock(p) +
					"Tu parte: sintetiza las tres revisiones del contexto: agrupa los hallazgos, quita duplicados, " +
					"resuelve las contradicciones comprobándolas en el código y prioriza. El veredicto final es tuyo."}, "", false
		}
		switch verdictOf(n - 1) {
		case VerdictApproved:
			return nil, OutcomeApproved, false
		case VerdictChanges:
			return nil, VerdictChanges, false
		}
		return nil, OutcomeNoVerdict, false
	case TplDebate:
		names := []struct{ role, name string }{{"a", "Propuesta A"}, {"b", "Propuesta B"}, {"c", "Propuesta C"}}
		if n < len(names) {
			return &stepPlan{Name: names[n].name, Role: names[n].role, Kind: KindPlan,
				Prompt: "Debate de diseño.\n\n" + taskBlock(p) + "Tu parte: propón tu diseño de forma independiente " +
					"(no conoces las otras propuestas): enfoque, ficheros afectados, ventajas, riesgos y cómo verificarlo."}, "", false
		}
		if n == len(names) {
			return &stepPlan{Name: "Decisión", Role: "chair", Kind: KindPlan, Context: []string{jobOf(0), jobOf(1), jobOf(2)},
				Prompt: "Debate de diseño.\n\n" + taskBlock(p) + "Tu parte: compara las tres propuestas del contexto con " +
					"criterios explícitos (simplicidad, riesgo, coste, reversibilidad), comprueba en el código lo que " +
					"afirman y decide. Entrega la decisión razonada y el plan final."}, "", false
		}
		return nil, OutcomeDecided, false
	case TplRedBlue:
		switch {
		case n == 0:
			return &stepPlan{Name: "Ataque", Role: "red", Kind: KindReview, Prompt: "Ejercicio rojo/azul.\n\n" + taskBlock(p) +
				"Tu parte (equipo rojo): busca problemas de seguridad explotables en el alcance de la tarea. Para cada uno: " +
				"fichero:línea, escenario de explotación, gravedad y arreglo. Si encuentras alguno explotable, tu veredicto es CAMBIOS; si no, APROBADO."}, "", false
		case p.Steps[n-1].Name == "Defensa":
			if out, failed := changesOutcome(o.stepJobLocked(p, n-1)); out != "" {
				return nil, out, failed
			}
			return &stepPlan{Name: "Verificación", Role: "red", Kind: KindReview, ApplyFrom: applyIfChanges(n - 1),
				Context: []string{jobOf(lastStep(p, "Ataque", "Verificación")), jobOf(n - 1)},
				Prompt: "Ejercicio rojo/azul.\n\n" + taskBlock(p) + "Tu parte (equipo rojo): las defensas ya están aplicadas en " +
					"el árbol (git diff --cached). Comprueba si cierran los hallazgos del contexto y busca regresiones o " +
					"hallazgos nuevos. CAMBIOS si queda algo explotable; si no, APROBADO."}, "", false
		default: // Ataque or Verificación finished
			switch v := verdictOf(n - 1); {
			case v == VerdictChanges && p.Iteration < p.MaxIterations:
				p.Iteration++
				return &stepPlan{Name: "Defensa", Role: "blue", Kind: KindChange,
					ApplyFrom: applyIfChanges(lastStep(p, "Defensa")), Context: []string{jobOf(n - 1)},
					Prompt: "Ejercicio rojo/azul.\n\n" + taskBlock(p) + "Tu parte (equipo azul): corrige los hallazgos del " +
						"equipo rojo (en el contexto), empezando por los más graves, con cambios mínimos y verificados."}, "", false
			case v == VerdictApproved:
				return nil, OutcomeApproved, false
			case v == VerdictChanges:
				return nil, OutcomePending, false
			default:
				return nil, OutcomeNoVerdict, false
			}
		}
	case TplEval:
		switch n {
		case 0:
			e := o.evalLocked(p.Source.Ref)
			if e == nil {
				return nil, outcomeStepFailed, false
			}
			return &stepPlan{Name: "Candidato", Role: "candidate", Kind: e.Kind, Prompt: e.Prompt}, "", false
		case 1:
			e := o.evalLocked(p.Source.Ref)
			cand := o.stepJobLocked(p, 0)
			if e == nil || cand == nil {
				return nil, outcomeStepFailed, false
			}
			apply := ""
			if cand.Kind == KindChange {
				if out, failed := changesOutcome(cand); out != "" {
					return nil, out, failed
				}
				apply = cand.ID
			}
			// The whole result is read (judgePrompt cuts it); a result that
			// cannot be read fails the evaluation rather than being judged
			// as empty.
			res, err := o.store.readJobFile(cand.ID, fileResult, harness.MaxResultText)
			if err != nil {
				return nil, outcomeNoCandidate, true
			}
			return &stepPlan{Name: "Juez", Role: "judge", Kind: KindJudge, ApplyFrom: apply,
				Prompt: judgePrompt(e.Criteria, string(res), apply != "")}, "", false
		}
		if j := o.stepJobLocked(p, 1); j != nil && j.Score != nil {
			s := *j.Score
			p.Score = &s
			return nil, fmt.Sprintf("puntuación %d/10", s), false
		}
		return nil, OutcomeNoScore, false
	}
	return nil, OutcomeNoVerdict, false
}

// nextStepLocked queues the pipeline's next step, or finishes it.
func (o *Office) nextStepLocked(p *Pipeline) error {
	plan, outcome, failed := o.planNextLocked(p)
	if plan == nil {
		status := PipelineDone
		if failed || outcome == outcomeStepFailed {
			status = PipelineFailed
		}
		o.finishPipelineLocked(p, status, outcome)
		return nil
	}
	agentID := p.Participants[plan.Role]
	var ext *external
	if p.Source.Type == "issue" || p.Source.Type == "pr" {
		ext = o.store.readPipelineExternal(p.ID)
	}
	var ctx []string
	for _, c := range plan.Context {
		if c != "" {
			ctx = append(ctx, c)
		}
	}
	j, err := o.enqueueLocked(newJob{
		ProjectID: p.ProjectID, AgentID: agentID, Kind: plan.Kind, Prompt: plan.Prompt,
		Title: truncRunes(p.Title+" · "+plan.Name, MaxTitleRunes), Branch: p.Branch, Priority: p.Priority,
		Source: Source{Type: "pipeline", Ref: p.ID}, PipelineID: p.ID, Step: len(p.Steps),
		ApplyFrom: plan.ApplyFrom, Context: ctx, External: ext,
		internal:     plan.Kind == KindJudge || plan.Kind == KindRetro,
		continuation: len(p.Steps) > 0,
	})
	if err != nil {
		if len(p.Steps) == 0 {
			return err
		}
		msg := err.Error()
		var fe *FieldError
		if asField(err, &fe) {
			msg = fe.Message
		}
		o.finishPipelineLocked(p, PipelineFailed, "No se pudo crear el paso «"+plan.Name+"»: "+msg)
		return nil
	}
	p.Steps = append(p.Steps, PipelineStep{Name: plan.Name, Role: plan.Role, AgentID: agentID, Kind: plan.Kind,
		JobID: j.ID, Status: j.Status})
	o.touchPipelineLocked(p.ID)
	return nil
}

// mirrorStepLocked copies a pipeline job's status to its step, at every
// change of the job's status.
func (o *Office) mirrorStepLocked(j *Job) {
	if j.PipelineID == "" {
		return
	}
	p := o.pipelineLocked(j.PipelineID)
	if p == nil {
		return
	}
	for i := range p.Steps {
		if p.Steps[i].JobID == j.ID && p.Steps[i].Status != j.Status {
			p.Steps[i].Status = j.Status
			o.touchPipelineLocked(p.ID)
		}
	}
}

func (o *Office) finishPipelineLocked(p *Pipeline, status, outcome string) {
	now := o.now()
	p.Status, p.Outcome, p.Finished = status, outcome, &now
	o.touchPipelineLocked(p.ID)
	o.invalidateLocked("metrics")
}

// advancePipelineLocked follows the pipeline's current step: it mirrors
// the step's status and, once the step ended, plans the next one.
func (o *Office) advancePipelineLocked(id string) {
	p := o.pipelineLocked(id)
	if p == nil {
		return
	}
	changed := false
	for i := range p.Steps {
		if j := o.jobs[p.Steps[i].JobID]; j != nil && p.Steps[i].Status != j.Status {
			p.Steps[i].Status = j.Status
			changed = true
		}
	}
	if changed {
		o.touchPipelineLocked(p.ID)
	}
	if p.Status != PipelineRunning {
		return
	}
	if len(p.Steps) == 0 {
		o.finishPipelineLocked(p, PipelineFailed, outcomeStepFailed)
		return
	}
	last := p.Steps[len(p.Steps)-1]
	j := o.jobs[last.JobID]
	switch {
	case j == nil:
		o.finishPipelineLocked(p, PipelineFailed, "Se perdió el trabajo del paso «"+last.Name+"»")
	case !isTerminal(j.Status):
		return
	case j.Status == StatusCancelled:
		o.finishPipelineLocked(p, PipelineCancelled, "cancelado en «"+last.Name+"»")
	case j.Status == StatusFailed:
		o.finishPipelineLocked(p, PipelineFailed, "falló «"+last.Name+"»")
	default:
		_ = o.nextStepLocked(p)
	}
}

// Pipeline returns a pipeline and its steps' jobs.
func (o *Office) Pipeline(id string) (Pipeline, []Job, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	p := o.pipelineLocked(id)
	if p == nil {
		return Pipeline{}, nil, notFound("No existe ese equipo")
	}
	jobs := []Job{}
	for _, s := range p.Steps {
		if j := o.jobs[s.JobID]; j != nil {
			jobs = append(jobs, *j)
		}
	}
	return clonePipeline(*p), jobs, nil
}

// CancelPipeline stops a running pipeline and its current step.
func (o *Office) CancelPipeline(id, clientIP string) error {
	o.mu.Lock()
	p := o.pipelineLocked(id)
	if p == nil {
		o.mu.Unlock()
		return notFound("No existe ese equipo")
	}
	if p.Status != PipelineRunning {
		o.mu.Unlock()
		return conflict("El equipo ya terminó")
	}
	o.finishPipelineLocked(p, PipelineCancelled, "cancelado por una persona")
	current := ""
	if n := len(p.Steps); n > 0 {
		current = p.Steps[n-1].JobID
	}
	o.saveLocked(true, false)
	o.mu.Unlock()
	o.auditAction("pipeline.cancel", clientIP, "pipeline", id)
	if current != "" {
		if err := o.CancelJob(current, clientIP); err != nil && IsStatus(err) != 409 {
			return err
		}
	}
	return nil
}

// prunePipelinesLocked keeps the newest keepPipelines pipelines (running
// ones always).
func (o *Office) prunePipelinesLocked() {
	excess := len(o.st.Pipelines) - keepPipelines
	if excess <= 0 {
		return
	}
	kept := o.st.Pipelines[:0]
	for _, p := range o.st.Pipelines {
		if excess > 0 && p.Status != PipelineRunning {
			excess--
			_ = os.Remove(o.store.pipelineExternalPath(p.ID))
			continue
		}
		kept = append(kept, p)
	}
	o.st.Pipelines = kept
}
