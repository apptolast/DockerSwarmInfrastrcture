package office

import "apptolast.com/ax-web/internal/harness"

// routingNote is the history note of an agent moved to the cheaper routing.
const routingNote = "Enrutado de modelos: ejecutor Haiku o Sonnet y consejero Opus"

// legacyRouting is the model and effort the seeds before 1.0.3 gave each
// worker (Opus for the planner, the developer, the reviewer, the security
// reviewer and the coach).
var legacyRouting = map[string]struct{ model, effort string }{
	"ada": {"opus", "high"}, "linus": {"opus", "high"}, "grace": {"opus", "xhigh"},
	"kent": {"sonnet", "high"}, "hedy": {"opus", "high"}, "margaret": {"sonnet", "medium"},
	"becario": {"haiku", "low"}, "coach": {"opus", "high"},
}

// seedFor is the current seed of the agent with that id, or nil.
func seedFor(id string) *seedAgent {
	for i := range seedAgents {
		if seedAgents[i].id == id {
			return &seedAgents[i]
		}
	}
	return nil
}

// migrateRoutingLocked moves a team installed by an earlier version to the
// current routing, once and without any marker: an agent is moved only
// while it is still exactly as the old seed left it (version 1, no history,
// the old model and effort, no advisor), and moving it makes it version 2,
// so it never matches again. An agent the owner edited keeps what they set.
// Codex is off in the Oficina, so a Codex agent is switched off instead of
// left to fail every job. It returns how many agents changed.
func (o *Office) migrateRoutingLocked() int {
	moved := 0
	for i := range o.st.Agents {
		a := &o.st.Agents[i]
		if a.Harness == harness.Codex {
			if a.Enabled {
				a.Enabled, a.Updated = false, o.now()
				moved++
			}
			continue
		}
		old, ok := legacyRouting[a.ID]
		if !ok || a.Harness != harness.Claude || a.Version != 1 || len(a.History) != 0 ||
			a.Model != old.model || a.Effort != old.effort || a.Advisor != "" {
			continue
		}
		s := seedFor(a.ID)
		if s == nil || !o.asSeeded(a, s) {
			continue
		}
		if s.model == a.Model && s.effort == a.Effort && seedAdvisor[a.ID] == "" {
			continue
		}
		trial := cloneAgent(*a)
		trial.Model, trial.Effort, trial.Advisor = s.model, s.effort, seedAdvisor[a.ID]
		if err := validateAgent(&trial, o.cfg.Limits); err != nil {
			o.warnings = append(o.warnings, "No se pudo enrutar al agente "+a.ID+": "+err.Error())
			continue
		}
		if behaviourChanged(a, &trial) {
			o.bumpVersionLocked(a, routingNote)
		}
		a.Model, a.Effort, a.Advisor, a.Updated = trial.Model, trial.Effort, trial.Advisor, o.now()
		moved++
	}
	if moved > 0 {
		o.audit.Info("audit", "action", "office.migrate.routing", "agents", moved)
	}
	return moved
}

// asSeeded says whether the fields an edit does not version still match the
// seed: a fallback model, the mode, the turns, the time and the forbidden
// tools. Editing any of them leaves the agent at version 1, but it is no
// longer exactly as the old seed left it.
func (o *Office) asSeeded(a *Agent, s *seedAgent) bool {
	want := Agent{MaxTurns: s.turns, TimeoutMinutes: s.timeout}
	clampAgent(&want, o.cfg.Limits)
	return a.FallbackModel == "" && len(a.DisallowedTools) == 0 && a.Mode == s.mode &&
		a.MaxTurns == want.MaxTurns && a.TimeoutMinutes == want.TimeoutMinutes
}
