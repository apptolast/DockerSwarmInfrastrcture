package office

import (
	"testing"

	"apptolast.com/ax-web/internal/harness"
)

// asLegacy rewrites the harness's team as a 1.0.1 install left it: the old
// models, version 1, no history and no advisor.
func asLegacy(t *testing.T, h *harnessT) {
	t.Helper()
	h.o.mu.Lock()
	defer h.o.mu.Unlock()
	for i := range h.o.st.Agents {
		a := &h.o.st.Agents[i]
		if old, ok := legacyRouting[a.ID]; ok {
			a.Model, a.Effort, a.Advisor, a.Version, a.History = old.model, old.effort, "", 1, []AgentRev{}
		}
		if a.Harness == harness.Codex {
			a.Enabled = true // an old install seeded the Codex agent switched on
		}
	}
	h.o.saveLocked(true, false)
}

func teamOf(o *Office) map[string]Agent {
	o.mu.Lock()
	defer o.mu.Unlock()
	m := map[string]Agent{}
	for _, a := range o.st.Agents {
		m[a.ID] = cloneAgent(a)
	}
	return m
}

func TestRoutingMovesUntouchedLegacyAgents(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	team := teamOf(h.reopen(t))
	for id, old := range legacyRouting {
		s := seedFor(id)
		a := team[id]
		if a.Model != s.model || a.Effort != s.effort || a.Advisor != seedAdvisor[id] {
			t.Errorf("%s: got %s/%s advisor %q, want %s/%s advisor %q", id, a.Model, a.Effort, a.Advisor,
				s.model, s.effort, seedAdvisor[id])
		}
		if old.model == s.model && old.effort == s.effort {
			if a.Version != 1 || len(a.History) != 0 {
				t.Errorf("%s: already routed, but it became version %d with %d revisions", id, a.Version, len(a.History))
			}
			continue
		}
		if a.Version != 2 || len(a.History) != 1 {
			t.Fatalf("%s: version %d with %d revisions, want 2 and 1", id, a.Version, len(a.History))
		}
		rev := a.History[0]
		if rev.Version != 1 || rev.Model != old.model || rev.Effort != old.effort || rev.Note != routingNote {
			t.Errorf("%s: revision %+v does not keep the old behaviour", id, rev)
		}
	}
}

func TestRoutingKeepsWhatTheOwnerEdited(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	h.o.mu.Lock()
	for i := range h.o.st.Agents {
		a := &h.o.st.Agents[i]
		switch a.ID {
		case "ada": // edited by hand: version 2
			a.Version, a.History = 2, []AgentRev{{Version: 1, Model: "sonnet", Note: "Editada a mano"}}
		case "linus": // a custom model, still version 1
			a.Model = "sonnet"
		case "grace": // an advisor the owner chose
			a.Advisor = "opus"
		}
	}
	h.o.saveLocked(true, false)
	h.o.mu.Unlock()
	team := teamOf(h.reopen(t))
	if a := team["ada"]; a.Model != "opus" || a.Version != 2 || len(a.History) != 1 {
		t.Errorf("ada was changed: %+v", a)
	}
	if a := team["linus"]; a.Model != "sonnet" || a.Effort != "high" || a.Advisor != "" || a.Version != 1 {
		t.Errorf("linus was changed: %+v", a)
	}
	if a := team["grace"]; a.Model != "opus" || a.Effort != "xhigh" || a.Version != 1 {
		t.Errorf("grace was changed: %+v", a)
	}
	if a := team["hedy"]; a.Model != "sonnet" || a.Version != 2 {
		t.Errorf("hedy, untouched, was not moved: %+v", a)
	}
}

func TestRoutingRunsOnceAndPersists(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	first := teamOf(h.reopen(t))
	second := teamOf(h.reopen(t))
	for id, a := range first {
		b := second[id]
		if a.Model != b.Model || a.Effort != b.Effort || a.Advisor != b.Advisor || a.Version != b.Version ||
			len(a.History) != len(b.History) || a.Enabled != b.Enabled {
			t.Errorf("%s changed on the second start: %+v then %+v", id, a, b)
		}
	}
}

func TestRoutingSwitchesCodexOff(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	team := teamOf(h.reopen(t))
	g := team["guido"]
	if g.Harness != harness.Codex || g.Enabled {
		t.Errorf("guido: harness %q enabled %v, want codex and disabled", g.Harness, g.Enabled)
	}
	for id, a := range team {
		if id != "guido" && !a.Enabled {
			t.Errorf("%s was switched off", id)
		}
	}
}

func TestRoutingLeavesAFreshInstallAlone(t *testing.T) {
	h := newHarness(t)
	fresh := teamOf(h.reopen(t))
	for _, s := range seedAgents {
		a := fresh[s.id]
		if s.harness == harness.Codex {
			continue
		}
		if a.Model != s.model || a.Version != 1 || len(a.History) != 0 {
			t.Errorf("%s: %+v, want the seed untouched", s.id, a)
		}
	}
}
