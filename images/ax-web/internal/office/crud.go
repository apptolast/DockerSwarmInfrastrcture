package office

import (
	"context"
	"fmt"
	"slices"
	"strings"

	"apptolast.com/ax-web/internal/harness"
	"apptolast.com/ax-web/internal/runs"
)

// AgentInput creates or edits an agent. Nil fields keep their value (or
// take the default on creation); read-only fields a client sends back
// unchanged are ignored.
type AgentInput struct {
	ID              *string   `json:"id,omitempty"`
	Name            *string   `json:"name,omitempty"`
	Role            *string   `json:"role,omitempty"`
	Emoji           *string   `json:"emoji,omitempty"`
	Color           *string   `json:"color,omitempty"`
	Harness         *string   `json:"harness,omitempty"`
	Model           *string   `json:"model,omitempty"`
	FallbackModel   *string   `json:"fallback_model,omitempty"`
	Effort          *string   `json:"effort,omitempty"`
	Mode            *string   `json:"mode,omitempty"`
	MaxTurns        *int      `json:"max_turns,omitempty"`
	TimeoutMinutes  *int      `json:"timeout_minutes,omitempty"`
	SystemPrompt    *string   `json:"system_prompt,omitempty"`
	DisallowedTools *[]string `json:"disallowed_tools,omitempty"`
	Enabled         *bool     `json:"enabled,omitempty"`
	// Note explains a new version in the history.
	Note    *string `json:"note,omitempty"`
	Version any     `json:"version,omitempty"`
	History any     `json:"history,omitempty"`
	Created any     `json:"created,omitempty"`
	Updated any     `json:"updated,omitempty"`
}

func setStr(dst *string, src *string) {
	if src != nil {
		*dst = *src
	}
}

func setInt(dst *int, src *int) {
	if src != nil {
		*dst = *src
	}
}

func (in *AgentInput) apply(a *Agent) {
	setStr(&a.Name, in.Name)
	setStr(&a.Role, in.Role)
	setStr(&a.Emoji, in.Emoji)
	setStr(&a.Color, in.Color)
	setStr(&a.Harness, in.Harness)
	setStr(&a.Model, in.Model)
	setStr(&a.FallbackModel, in.FallbackModel)
	setStr(&a.Effort, in.Effort)
	setStr(&a.Mode, in.Mode)
	setInt(&a.MaxTurns, in.MaxTurns)
	setInt(&a.TimeoutMinutes, in.TimeoutMinutes)
	setStr(&a.SystemPrompt, in.SystemPrompt)
	if in.DisallowedTools != nil {
		a.DisallowedTools = slices.Clone(*in.DisallowedTools)
	}
	if in.Enabled != nil {
		a.Enabled = *in.Enabled
	}
	a.Model, a.FallbackModel, a.Effort = strings.TrimSpace(a.Model), strings.TrimSpace(a.FallbackModel), strings.TrimSpace(a.Effort)
}

func behaviourChanged(a, b *Agent) bool {
	return a.Harness != b.Harness || a.Model != b.Model || a.Effort != b.Effort || a.Mode != b.Mode ||
		a.SystemPrompt != b.SystemPrompt
}

// CreateAgent adds an agent to the team.
func (o *Office) CreateAgent(in AgentInput, clientIP string) (Agent, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	now := o.now()
	a := Agent{Harness: harness.Claude, Mode: harness.ModeRead, MaxTurns: min(40, o.cfg.Limits.MaxTurns),
		TimeoutMinutes: min(30, o.cfg.Limits.MaxTimeoutMinutes), Enabled: true, Version: 1,
		History: []AgentRev{}, DisallowedTools: []string{}, Created: now, Updated: now}
	in.apply(&a)
	if a.Harness == harness.Codex && in.MaxTurns == nil {
		a.MaxTurns = 0
	}
	if err := validateAgent(&a, o.cfg.Limits); err != nil {
		return Agent{}, err
	}
	if in.ID != nil && *in.ID != "" {
		if !ValidID(*in.ID) {
			return Agent{}, fieldErr("id", "El identificador debe ser [a-z][a-z0-9-], de 2 a 32 caracteres")
		}
		if o.agentLocked(*in.ID) != nil {
			return Agent{}, fieldErr("id", "Ya existe un agente con ese identificador")
		}
		a.ID = *in.ID
	} else {
		a.ID = uniqueID(slug(a.Name, "agente"), func(s string) bool { return o.agentLocked(s) != nil })
	}
	o.st.Agents = append(o.st.Agents, a)
	o.invalidateLocked("agents")
	o.saveLocked(true, false)
	o.auditAction("agent.create", clientIP, "agent", a.ID, "harness", a.Harness, "model", a.Model, "mode", a.Mode)
	return cloneAgent(a), nil
}

// UpdateAgent edits an agent; a change of harness, model, effort, mode or
// system prompt makes a new version.
func (o *Office) UpdateAgent(id string, in AgentInput, clientIP string) (Agent, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	a := o.agentLocked(id)
	if a == nil {
		return Agent{}, notFound("No existe ese agente")
	}
	if in.ID != nil && *in.ID != "" && *in.ID != id {
		return Agent{}, fieldErr("id", "El identificador de un agente no cambia")
	}
	trial := cloneAgent(*a)
	in.apply(&trial)
	if in.Harness != nil && *in.Harness != a.Harness && in.MaxTurns == nil {
		if trial.Harness == harness.Codex {
			trial.MaxTurns = 0
		} else if trial.MaxTurns == 0 {
			trial.MaxTurns = min(40, o.cfg.Limits.MaxTurns)
		}
	}
	if err := validateAgent(&trial, o.cfg.Limits); err != nil {
		return Agent{}, err
	}
	bumped := behaviourChanged(a, &trial)
	if bumped {
		note := "Editada a mano"
		if in.Note != nil && strings.TrimSpace(*in.Note) != "" {
			n, err := line("note", *in.Note, 200, false)
			if err != nil {
				return Agent{}, err
			}
			note = n
		}
		o.bumpVersionLocked(a, note)
	}
	trial.Version, trial.History, trial.Created, trial.Updated = a.Version, a.History, a.Created, o.now()
	*a = trial
	o.invalidateLocked("agents")
	if bumped {
		o.invalidateLocked("metrics")
	}
	o.saveLocked(true, false)
	o.auditAction("agent.update", clientIP, "agent", id, "version", a.Version, "new_version", bumped)
	return cloneAgent(*a), nil
}

// DeleteAgent removes an agent nothing pending depends on.
func (o *Office) DeleteAgent(id, clientIP string) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	i := slices.IndexFunc(o.st.Agents, func(a Agent) bool { return a.ID == id })
	if i < 0 {
		return notFound("No existe ese agente")
	}
	for _, j := range o.jobs {
		if j.AgentID == id && !isTerminal(j.Status) {
			return conflict("El agente tiene trabajos en cola o en curso")
		}
	}
	for _, p := range o.st.Pipelines {
		if p.Status != PipelineRunning {
			continue
		}
		for _, a := range p.Participants {
			if a == id {
				return conflict("El agente participa en un equipo en curso")
			}
		}
	}
	for _, s := range o.st.Schedules {
		if s.Target.Type == "job" && s.Target.AgentID == id {
			return conflict("Un turno programado usa este agente: «" + s.Name + "»")
		}
	}
	o.st.Agents = slices.Delete(o.st.Agents, i, i+1)
	if o.st.Settings.DefaultAgent == id {
		o.st.Settings.DefaultAgent = ""
		if len(o.st.Agents) > 0 {
			o.st.Settings.DefaultAgent = o.st.Agents[0].ID
		}
		o.invalidateLocked("settings")
	}
	o.invalidateLocked("agents", "metrics")
	o.saveLocked(true, false)
	o.auditAction("agent.delete", clientIP, "agent", id)
	return nil
}

// ProjectInput creates or edits a project (nil fields keep their value).
type ProjectInput struct {
	ID          *string `json:"id,omitempty"`
	Name        *string `json:"name,omitempty"`
	Repo        *string `json:"repo,omitempty"`
	Branch      *string `json:"branch,omitempty"`
	Description *string `json:"description,omitempty"`
	Service     *string `json:"service,omitempty"`
	URL         *string `json:"url,omitempty"`
	Notes       *string `json:"notes,omitempty"`
	Archived    *bool   `json:"archived,omitempty"`
	Seeded      any     `json:"seeded,omitempty"`
	Memory      any     `json:"memory,omitempty"`
	Created     any     `json:"created,omitempty"`
}

func (in *ProjectInput) apply(p *Project) {
	setStr(&p.Name, in.Name)
	setStr(&p.Repo, in.Repo)
	setStr(&p.Branch, in.Branch)
	setStr(&p.Description, in.Description)
	setStr(&p.Service, in.Service)
	setStr(&p.URL, in.URL)
	setStr(&p.Notes, in.Notes)
	if in.Archived != nil {
		p.Archived = *in.Archived
	}
}

// CreateProject adds a project of the owner's (not seeded).
func (o *Office) CreateProject(in ProjectInput, clientIP string) (Project, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	p := Project{Memory: []MemoryItem{}, Created: o.now()}
	in.apply(&p)
	if err := validateProject(&p, o.cfg.Limits); err != nil {
		return Project{}, err
	}
	if in.ID != nil && *in.ID != "" {
		if !ValidID(*in.ID) {
			return Project{}, fieldErr("id", "El identificador debe ser [a-z][a-z0-9-], de 2 a 32 caracteres")
		}
		if o.projectLocked(*in.ID) != nil {
			return Project{}, fieldErr("id", "Ya existe un proyecto con ese identificador")
		}
		p.ID = *in.ID
	} else {
		p.ID = uniqueID(slug(p.Name, "proyecto"), func(s string) bool { return o.projectLocked(s) != nil })
	}
	o.st.Projects = append(o.st.Projects, p)
	o.invalidateLocked("projects")
	o.saveLocked(true, false)
	o.auditAction("project.create", clientIP, "project", p.ID, "repo", p.Repo)
	return cloneProject(p), nil
}

// UpdateProject edits a project. The reviewed fields of a seeded one
// (name, repo, branch, description, service, URL) come from the
// configuration and cannot change here.
func (o *Office) UpdateProject(id string, in ProjectInput, clientIP string) (Project, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	p := o.projectLocked(id)
	if p == nil {
		return Project{}, notFound("No existe ese proyecto")
	}
	if in.ID != nil && *in.ID != "" && *in.ID != id {
		return Project{}, fieldErr("id", "El identificador de un proyecto no cambia")
	}
	trial := cloneProject(*p)
	in.apply(&trial)
	if err := validateProject(&trial, o.cfg.Limits); err != nil {
		return Project{}, err
	}
	if p.Seeded {
		for field, same := range map[string]bool{
			"name": trial.Name == p.Name, "repo": trial.Repo == p.Repo, "branch": trial.Branch == p.Branch,
			"description": trial.Description == p.Description, "service": trial.Service == p.Service, "url": trial.URL == p.URL,
		} {
			if !same {
				return Project{}, fieldErr(field, "Este proyecto viene de la configuración revisada: cámbialo en config/ax-lab.yml")
			}
		}
	}
	if trial.Archived && o.st.Settings.RetroProject == id {
		return Project{}, conflict("Es el proyecto de retrospectivas: elige otro en los ajustes antes de archivarlo")
	}
	*p = trial
	o.invalidateLocked("projects")
	o.saveLocked(true, false)
	o.auditAction("project.update", clientIP, "project", id, "notes_bytes", len(p.Notes), "archived", p.Archived)
	return cloneProject(*p), nil
}

// DeleteProject removes an owner's project nothing depends on.
func (o *Office) DeleteProject(id, clientIP string) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	i := slices.IndexFunc(o.st.Projects, func(p Project) bool { return p.ID == id })
	if i < 0 {
		return notFound("No existe ese proyecto")
	}
	if o.st.Projects[i].Seeded {
		return conflict("Los proyectos de la configuración solo se archivan")
	}
	if o.st.Settings.RetroProject == id {
		return conflict("Es el proyecto de retrospectivas: elige otro en los ajustes")
	}
	for _, j := range o.jobs {
		if j.ProjectID == id && !isTerminal(j.Status) {
			return conflict("El proyecto tiene trabajos en cola o en curso")
		}
	}
	for _, s := range o.st.Schedules {
		if s.Target.ProjectID == id {
			return conflict("Un turno programado usa este proyecto: «" + s.Name + "»")
		}
	}
	for _, e := range o.st.Evals {
		if e.ProjectID == id {
			return conflict("Una evaluación del banco usa este proyecto: «" + e.Name + "»")
		}
	}
	o.st.Projects = slices.Delete(o.st.Projects, i, i+1)
	o.invalidateLocked("projects")
	o.saveLocked(true, false)
	o.auditAction("project.delete", clientIP, "project", id)
	return nil
}

// MemoryRequest is POST /api/projects/{id}/memory.
type MemoryRequest struct {
	Action string `json:"action"`
	ID     string `json:"id,omitempty"`
	Text   string `json:"text,omitempty"`
}

// MemoryAction adds, edits, archives, restores or deletes a lesson.
func (o *Office) MemoryAction(projectID string, req MemoryRequest, clientIP string) (Project, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	p := o.projectLocked(projectID)
	if p == nil {
		return Project{}, notFound("No existe ese proyecto")
	}
	idx := slices.IndexFunc(p.Memory, func(m MemoryItem) bool { return m.ID == req.ID })
	if req.Action != "add" && idx < 0 {
		return Project{}, notFound("No existe esa lección")
	}
	switch req.Action {
	case "add":
		t, err := validateMemoryText(req.Text)
		if err != nil {
			return Project{}, err
		}
		if len(p.Memory) >= MaxMemoryItems {
			return Project{}, conflict(fmt.Sprintf("La memoria del proyecto está llena (%d)", MaxMemoryItems))
		}
		p.Memory = append(p.Memory, MemoryItem{ID: fmt.Sprintf("m-%d", o.nextSeq("memory")), Text: t,
			Active: true, Created: o.now()})
	case "edit":
		t, err := validateMemoryText(req.Text)
		if err != nil {
			return Project{}, err
		}
		p.Memory[idx].Text = t
	case "archive":
		p.Memory[idx].Active = false
	case "restore":
		p.Memory[idx].Active = true
	case "delete":
		p.Memory = slices.Delete(p.Memory, idx, idx+1)
	default:
		return Project{}, fieldErr("action", "La acción debe ser add, edit, archive, restore o delete")
	}
	o.invalidateLocked("projects")
	o.saveLocked(true, false)
	o.auditAction("project.memory", clientIP, "project", projectID, "memory_action", req.Action, "item", req.ID,
		"text_bytes", len(req.Text))
	return cloneProject(*p), nil
}

// GitHub lists a project's open issues and pull requests, when the
// office has a token for its owner.
func (o *Office) GitHub(ctx context.Context, projectID string) (GitHubView, error) {
	o.mu.Lock()
	p := o.projectLocked(projectID)
	repo := ""
	if p != nil {
		repo = p.Repo
	}
	o.mu.Unlock()
	if p == nil {
		return GitHubView{}, notFound("No existe ese proyecto")
	}
	view := GitHubView{Issues: []Issue{}, Pulls: []Pull{}}
	if !o.gh.enabled(repo) {
		return view, nil
	}
	view.Enabled = true
	issues, err := o.gh.Issues(ctx, repo)
	if err != nil {
		return GitHubView{}, err
	}
	pulls, err := o.gh.Pulls(ctx, repo)
	if err != nil {
		return GitHubView{}, err
	}
	view.Issues, view.Pulls = issues, pulls
	return view, nil
}

// GitHubRepos lists an owner's repositories on GitHub, for importing them
// as projects (POST /api/projects, one by one). AX clones anonymously, so
// only public ones can run; the browser warns about private ones.
func (o *Office) GitHubRepos(ctx context.Context, owner string) (GitHubRepos, error) {
	return o.gh.Repos(ctx, strings.TrimSpace(owner))
}

// SettingsInput edits the settings (nil fields keep their value).
type SettingsInput struct {
	OfficeName         *string `json:"office_name,omitempty"`
	DefaultAgent       *string `json:"default_agent,omitempty"`
	AutoLessons        *string `json:"auto_lessons,omitempty"`
	MaxLessonsInPrompt *int    `json:"max_lessons_in_prompt,omitempty"`
	QueuePaused        *bool   `json:"queue_paused,omitempty"`
	RetroProject       *string `json:"retro_project,omitempty"`
	MaxIterations      *int    `json:"max_iterations,omitempty"`
}

// UpdateSettings validates and applies new settings.
func (o *Office) UpdateSettings(in SettingsInput, clientIP string) (Settings, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	s := o.st.Settings
	setStr(&s.OfficeName, in.OfficeName)
	setStr(&s.DefaultAgent, in.DefaultAgent)
	setStr(&s.AutoLessons, in.AutoLessons)
	setInt(&s.MaxLessonsInPrompt, in.MaxLessonsInPrompt)
	setStr(&s.RetroProject, in.RetroProject)
	setInt(&s.MaxIterations, in.MaxIterations)
	if in.QueuePaused != nil {
		s.QueuePaused = *in.QueuePaused
	}
	var err error
	if s.OfficeName, err = line("office_name", s.OfficeName, MaxNameRunes, true); err != nil {
		return Settings{}, err
	}
	if s.DefaultAgent != "" && o.agentLocked(s.DefaultAgent) == nil {
		return Settings{}, fieldErr("default_agent", "No existe ese agente")
	}
	if s.AutoLessons != LessonsPropose && s.AutoLessons != LessonsApprove && s.AutoLessons != LessonsOff {
		return Settings{}, fieldErr("auto_lessons", "Debe ser proponer, aprobar u off")
	}
	if s.MaxLessonsInPrompt < 0 || s.MaxLessonsInPrompt > MaxLessonsLimit {
		return Settings{}, fieldErr("max_lessons_in_prompt", fmt.Sprintf("Va de 0 a %d", MaxLessonsLimit))
	}
	if p := o.projectLocked(s.RetroProject); p == nil || p.Archived {
		return Settings{}, fieldErr("retro_project", "No existe ese proyecto o está archivado")
	}
	if s.MaxIterations < 1 || s.MaxIterations > MaxIterationsLimit {
		return Settings{}, fieldErr("max_iterations", fmt.Sprintf("Las iteraciones van de 1 a %d", MaxIterationsLimit))
	}
	o.st.Settings = s
	o.invalidateLocked("settings")
	o.saveLocked(true, false)
	o.auditAction("settings.update", clientIP, "queue_paused", s.QueuePaused, "auto_lessons", s.AutoLessons)
	o.kick()
	return s, nil
}

// PauseQueue stops or resumes the dispatcher.
func (o *Office) PauseQueue(paused bool, clientIP string) (Settings, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.st.Settings.QueuePaused = paused
	o.invalidateLocked("settings")
	o.saveLocked(true, false)
	o.auditAction("queue.pause", clientIP, "paused", paused)
	o.kick()
	return o.st.Settings, nil
}

// ScheduleInput creates (or, with ID, updates) a schedule.
type ScheduleInput struct {
	ID       string         `json:"id,omitempty"`
	Name     string         `json:"name"`
	Enabled  *bool          `json:"enabled,omitempty"`
	Days     []int          `json:"days"`
	Time     string         `json:"time"`
	Target   ScheduleTarget `json:"target"`
	LastRun  any            `json:"last_run,omitempty"`
	LastRef  any            `json:"last_ref,omitempty"`
	NextRun  any            `json:"next_run,omitempty"`
	Created  any            `json:"created,omitempty"`
	LastSlot any            `json:"last_slot,omitempty"`
	Edited   any            `json:"edited,omitempty"`
}

// checkTargetLocked validates what a schedule creates.
func (o *Office) checkTargetLocked(t *ScheduleTarget) error {
	p := o.projectLocked(t.ProjectID)
	if p == nil {
		return fieldErr("target.project_id", "No existe ese proyecto")
	}
	prompt, err := text("target.prompt", t.Prompt, MaxJobPromptBytes, true)
	if err != nil {
		return err
	}
	t.Prompt = prompt
	if t.Branch = strings.TrimSpace(t.Branch); t.Branch != "" {
		if err := runs.ValidateBranch(t.Branch); err != nil {
			return runsError("target.branch", err)
		}
	}
	switch t.Type {
	case "job":
		a := o.agentLocked(t.AgentID)
		if a == nil {
			return fieldErr("target.agent_id", "No existe ese agente")
		}
		if !validKind(t.Kind) {
			return fieldErr("target.kind", "Tipo de trabajo no válido")
		}
		if t.Kind == KindChange && a.Mode != harness.ModeFull {
			return fieldErr("target.kind", "Un trabajo de tipo cambio necesita modo completo")
		}
		t.Template = ""
	case "pipeline":
		if tpl := templateByID(t.Template); tpl == nil || tpl.ID == TplEval {
			return fieldErr("target.template", "Plantilla de equipo no válida")
		}
		t.AgentID, t.Kind = "", ""
	default:
		return fieldErr("target.type", "El destino debe ser job o pipeline")
	}
	return nil
}

// SaveSchedule creates or updates a schedule.
func (o *Office) SaveSchedule(in ScheduleInput, clientIP string) (Schedule, error) {
	s := Schedule{Name: in.Name, Days: slices.Clone(in.Days), Time: in.Time, Target: in.Target, Enabled: true}
	if in.Enabled != nil {
		s.Enabled = *in.Enabled
	}
	if err := validateSchedule(&s); err != nil {
		return Schedule{}, err
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if err := o.checkTargetLocked(&s.Target); err != nil {
		return Schedule{}, err
	}
	// A saved schedule never fires for a slot before it was saved.
	now := o.now()
	s.Edited = &now
	if in.ID != "" {
		cur := o.scheduleLocked(in.ID)
		if cur == nil {
			return Schedule{}, notFound("No existe ese turno")
		}
		s.ID, s.Created, s.LastRun, s.LastRef, s.LastSlot = cur.ID, cur.Created, cur.LastRun, cur.LastRef, cur.LastSlot
		*cur = s
	} else {
		s.ID = uniqueID(slug(s.Name, "turno"), func(x string) bool { return o.scheduleLocked(x) != nil })
		s.Created = now
		o.st.Schedules = append(o.st.Schedules, s)
	}
	o.invalidateLocked("schedules")
	o.saveLocked(true, false)
	o.auditAction("schedule.save", clientIP, append([]any{"schedule", s.ID, "target", s.Target.Type},
		promptAttrs(s.Target.Prompt)...)...)
	out := *o.scheduleLocked(s.ID)
	out.Days = slices.Clone(out.Days)
	return out, nil
}

// DeleteSchedule removes a schedule.
func (o *Office) DeleteSchedule(id, clientIP string) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	i := slices.IndexFunc(o.st.Schedules, func(s Schedule) bool { return s.ID == id })
	if i < 0 {
		return notFound("No existe ese turno")
	}
	o.st.Schedules = slices.Delete(o.st.Schedules, i, i+1)
	o.invalidateLocked("schedules")
	o.saveLocked(true, false)
	o.auditAction("schedule.delete", clientIP, "schedule", id)
	return nil
}
