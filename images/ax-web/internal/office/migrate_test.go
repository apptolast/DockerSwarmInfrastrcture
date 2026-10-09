package office

import (
	"log/slog"
	"strings"
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

// The exact result the owner will see on the live team, written out so a
// change to the tables cannot move it unnoticed.
func TestRoutingResultsAreExactlyTheReviewedOnes(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	team := teamOf(h.reopen(t))
	want := map[string][3]string{
		"ada": {"sonnet", "high", "opus"}, "linus": {"sonnet", "high", "opus"},
		"grace": {"sonnet", "high", "opus"}, "hedy": {"sonnet", "high", "opus"},
		"kent": {"haiku", "medium", "opus"}, "margaret": {"haiku", "medium", "opus"},
		"coach": {"haiku", "medium", "opus"}, "becario": {"haiku", "low", ""},
	}
	for id, w := range want {
		a := team[id]
		if got := [3]string{a.Model, a.Effort, a.Advisor}; got != w {
			t.Errorf("%s: got %v, want %v", id, got, w)
		}
	}
}

// The undo the guide promises: restoring version 1 of a migrated agent must
// work, including for the agents whose old model is the new advisor.
func TestRestoringAMigratedAgentWorks(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	o := h.reopen(t)
	for _, id := range []string{"ada", "linus", "grace", "hedy", "coach", "kent", "margaret"} {
		old := legacyRouting[id]
		a, err := o.RollbackAgent(id, 1, "198.51.100.7")
		if err != nil {
			t.Fatalf("%s: restoring v1: %v", id, err)
		}
		if a.Model != old.model || a.Effort != old.effort || a.Advisor != "" || a.Version != 3 {
			t.Errorf("%s: after restoring v1: %s/%s advisor %q v%d, want %s/%s no advisor v3",
				id, a.Model, a.Effort, a.Advisor, a.Version, old.model, old.effort)
		}
	}
}

// A revision archived before the advisor was kept (nil) restores without
// touching the advisor, and an advisor equal to the restored model is dropped
// instead of failing the restore.
func TestRestoringAnOldRevisionKeepsOrDropsTheAdvisor(t *testing.T) {
	h := newHarness(t)
	h.o.mu.Lock()
	a := h.o.agentLocked("ada")
	a.Version, a.Model, a.Advisor = 2, "sonnet", "opus"
	a.History = []AgentRev{{Version: 1, Harness: harness.Claude, Model: "opus", Effort: "high", Mode: a.Mode,
		SystemPrompt: a.SystemPrompt, Note: "old"}} // no advisor recorded
	h.o.mu.Unlock()
	got, err := h.o.RollbackAgent("ada", 1, "198.51.100.7")
	if err != nil {
		t.Fatalf("restoring an old revision: %v", err)
	}
	if got.Model != "opus" || got.Advisor != "" {
		t.Errorf("model %q advisor %q: the advisor equal to the model must be dropped", got.Model, got.Advisor)
	}
}

// Choosing the advisor's own model as the worker drops the advisor; setting
// both to the same model explicitly is still refused.
func TestChangingTheModelToTheAdvisorDropsTheAdvisor(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	o := h.reopen(t)
	a, err := o.UpdateAgent("ada", AgentInput{Model: ptr("opus")}, "198.51.100.7")
	if err != nil || a.Model != "opus" || a.Advisor != "" {
		t.Fatalf("model to opus: %+v %v", a, err)
	}
	if _, err := o.UpdateAgent("linus", AgentInput{Model: ptr("opus"), Advisor: ptr("opus")}, "198.51.100.7"); err == nil {
		t.Error("an explicit advisor equal to the model was accepted")
	}
}

// Fields an edit changes without bumping the version still make an agent
// "edited by hand", so the migration leaves it alone.
func TestRoutingLeavesAgentsEditedWithoutAVersionBump(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	h.o.mu.Lock()
	for i := range h.o.st.Agents {
		a := &h.o.st.Agents[i]
		switch a.ID {
		case "ada":
			a.FallbackModel = "sonnet"
		case "linus":
			a.MaxTurns++
		case "grace":
			a.TimeoutMinutes++
		case "hedy":
			a.DisallowedTools = []string{"WebFetch"}
		case "coach":
			a.Mode = harness.ModeFull
		}
	}
	h.o.saveLocked(true, false)
	h.o.mu.Unlock()
	team := teamOf(h.reopen(t))
	for _, id := range []string{"ada", "linus", "grace", "hedy", "coach"} {
		a := team[id]
		if a.Version != 1 || a.Model != legacyRouting[id].model || a.Advisor != "" {
			t.Errorf("%s was moved although it was edited: %s v%d advisor %q", id, a.Model, a.Version, a.Advisor)
		}
	}
	if a := team["kent"]; a.Version != 2 {
		t.Errorf("kent, untouched, was not moved: v%d", a.Version)
	}
}

// The migration is written to disk once: a second start finds nothing to do.
func TestRoutingIsPersistedAndLoggedOnce(t *testing.T) {
	h := newHarness(t)
	asLegacy(t, h)
	logs := &syncBuffer{}
	open := func() *Office {
		o, err := New(h.cfg, h.exec, nil, h.clock.Now, slog.New(slog.NewJSONHandler(logs, nil)))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { o.Close(t.Context()) })
		return o
	}
	first := open()
	if n := strings.Count(logs.String(), "office.migrate.routing"); n != 1 {
		t.Fatalf("first start logged the migration %d times, want 1", n)
	}
	a := teamOf(first)["ada"]
	second := open()
	if n := strings.Count(logs.String(), "office.migrate.routing"); n != 1 {
		t.Errorf("a second start migrated again: %d log lines", n)
	}
	b := teamOf(second)["ada"]
	if a.Version != 2 || b.Version != 2 || len(b.History) != 1 {
		t.Errorf("ada after two starts: v%d then v%d with %d revisions, want 2, 2 and 1", a.Version, b.Version, len(b.History))
	}
}
