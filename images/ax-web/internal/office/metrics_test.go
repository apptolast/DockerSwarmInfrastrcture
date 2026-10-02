package office

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

func TestComputeMetrics(t *testing.T) {
	now := at("2026-10-02T12:00:00Z")
	tm := func(s string) *time.Time { v := at(s); return &v }
	snap := func(v int, model string) AgentSnapshot {
		return AgentSnapshot{Name: "Linus", Version: v, Harness: harness.Claude, Model: model}
	}
	jobs := map[string]*Job{
		"c1": {ID: "c1", AgentID: "linus", Agent: snap(1, "opus"), Kind: KindChange, Status: StatusDone,
			Started: tm("2026-10-01T10:00:00Z"), Finished: tm("2026-10-01T10:01:40Z"),
			Usage: harness.Usage{CostUSD: 1, Turns: 10}, Rating: &Rating{Score: 1}},
		"c2": {ID: "c2", AgentID: "linus", Agent: snap(1, "opus"), Kind: KindChange, Status: StatusFailed,
			Started: tm("2026-10-02T10:00:00Z"), Finished: tm("2026-10-02T10:00:20Z"),
			Usage: harness.Usage{CostUSD: 0.5, Turns: 2}, Rating: &Rating{Score: -1}},
		"c3": {ID: "c3", AgentID: "linus", Agent: snap(2, "sonnet"), Kind: KindChange, Status: StatusDone,
			Started: tm("2026-10-02T11:00:00Z"), Finished: tm("2026-10-02T11:00:10Z"), Usage: harness.Usage{CostUSD: 2}},
		"r1": {ID: "r1", AgentID: "grace", Agent: AgentSnapshot{Version: 1, Harness: harness.Claude, Model: "opus"},
			Kind: KindReview, Status: StatusDone, ApplyFrom: "c1", Verdict: VerdictApproved, Finished: tm("2026-10-02T11:30:00Z")},
		"r2": {ID: "r2", AgentID: "grace", Agent: AgentSnapshot{Version: 1, Harness: harness.Claude, Model: "opus"},
			Kind: KindReview, Status: StatusDone, ApplyFrom: "c3", Verdict: VerdictChanges, Finished: tm("2026-10-02T11:40:00Z")},
		"q": {ID: "q", AgentID: "linus", Agent: snap(2, "sonnet"), Kind: KindAsk, Status: StatusQueued},
		"g1": {ID: "g1", AgentID: "guido", Agent: AgentSnapshot{Version: 1, Harness: harness.Codex}, Kind: KindAsk, Status: StatusDone,
			Usage: harness.Usage{ModelUsed: ""}, Finished: tm("2026-10-02T09:00:00Z")},
	}
	score := 7
	pipelines := []Pipeline{{Template: TplEval, Score: &score, Steps: []PipelineStep{{JobID: "c3"}, {JobID: "x"}}}}
	m := computeMetrics(jobs, pipelines, now)
	find := func(agent string, v int) Metric {
		for _, x := range m.ByAgentVersion {
			if x.AgentID == agent && x.Version == v {
				return x
			}
		}
		t.Fatalf("no metric %s v%d", agent, v)
		return Metric{}
	}
	l1 := find("linus", 1)
	if l1.Jobs != 2 || l1.Succeeded != 1 || l1.Failed != 1 || l1.ThumbsUp != 1 || l1.ThumbsDown != 1 ||
		l1.Approved != 1 || l1.Rejected != 0 || l1.CostUSD != 1.5 || l1.AvgCostUSD != 0.75 || l1.AvgSeconds != 60 ||
		l1.AvgTurns != 6 || l1.LastUsed == nil || !l1.LastUsed.Equal(at("2026-10-02T10:00:20Z")) {
		t.Fatalf("%+v", l1)
	}
	l2 := find("linus", 2)
	if l2.Jobs != 1 || l2.Rejected != 1 || l2.EvalRuns != 1 || l2.EvalScoreAvg != 7 {
		t.Fatalf("%+v", l2)
	}
	if m.ByAgentVersion[2].AgentID != "linus" || m.ByAgentVersion[2].Version != 2 {
		t.Fatalf("order %+v", m.ByAgentVersion)
	}
	var codex *Metric
	for i := range m.ByModel {
		if m.ByModel[i].Harness == harness.Codex {
			codex = &m.ByModel[i]
		}
	}
	if codex == nil || codex.Model != "predeterminado" || codex.Jobs != 1 {
		t.Fatalf("%+v", m.ByModel)
	}
	if m.TotalCostUSD != 3.5 || m.CostTodayUSD != 2.5 || m.JobsToday != 5 {
		t.Fatalf("totals %+v", m)
	}
}

// The Codex catalogue is the "list" entries of `codex debug models` in the
// pinned image (cli/codex-models.json, 2026-10-02): slug, efforts, default.
func TestCatalogMatchesCodexModels(t *testing.T) {
	want := []string{
		"gpt-6-astra:low,medium,high,xhigh,max,ultra:low",
		"gpt-6-sol:low,medium,high,xhigh,max,ultra:medium",
		"gpt-6-luna:low,medium,high,xhigh,max:medium",
		"gpt-5.6-sol:low,medium,high,xhigh,max,ultra:low",
		"gpt-5.6-terra:low,medium,high,xhigh,max,ultra:medium",
		"gpt-5.6-luna:low,medium,high,xhigh,max:medium",
		"gpt-5.5:low,medium,high,xhigh:medium",
	}
	var got []string
	for _, m := range CatalogInfo().Codex.Models {
		got = append(got, m.ID+":"+strings.Join(m.Efforts, ",")+":"+m.Default)
	}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("got\n%s\nwant\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestCatalogEfforts(t *testing.T) {
	if e := effortsFor(harness.Codex, "gpt-5.5"); strings.Join(e, ",") != "low,medium,high,xhigh" {
		t.Fatal(e)
	}
	if e := effortsFor(harness.Codex, "otro-modelo"); len(e) != 6 {
		t.Fatal(e)
	}
	if e := effortsFor(harness.Claude, "opus"); len(e) != 5 {
		t.Fatal(e)
	}
	c := CatalogInfo()
	if c.Claude.Version != "2.1.274" || c.Codex.Version != "0.156.1" || len(c.Claude.Models) != 8 || len(c.Codex.Models) != 7 {
		t.Fatalf("%+v", c)
	}
}

// The leaderboard is recomputed when the UTC day changes even if nothing
// else did, and a cost that is not a finite number never reaches a sum.
func TestMetricsDayAndNonFiniteCosts(t *testing.T) {
	h := newHarness(t)
	h.clock.Set(at("2026-10-02T23:50:00Z"))
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "x"})
	h.runNext(t, exited(0, "ok"))
	if m := h.o.Metrics(); m.JobsToday != 1 || m.CostTodayUSD != 0.5 {
		t.Fatalf("%+v", m)
	}
	h.clock.Set(at("2026-10-03T00:05:00Z"))
	if m := h.o.Metrics(); m.JobsToday != 0 || m.CostTodayUSD != 0 || m.TotalCostUSD != 0.5 {
		t.Fatalf("after midnight %+v", m)
	}
	jobs := map[string]*Job{
		"a": {ID: "a", AgentID: "x", Status: StatusDone, Usage: harness.Usage{CostUSD: math.NaN(), Turns: -3}},
		"b": {ID: "b", AgentID: "x", Status: StatusDone, Usage: harness.Usage{CostUSD: math.Inf(1)}},
		"c": {ID: "c", AgentID: "x", Status: StatusDone, Usage: harness.Usage{CostUSD: 1.25, Turns: 4}},
	}
	m := computeMetrics(jobs, nil, at("2026-10-02T12:00:00Z"))
	if m.TotalCostUSD != 1.25 || m.ByAgentVersion[0].AvgTurns != 4.0/3 {
		t.Fatalf("%+v", m)
	}
	if _, err := json.Marshal(m); err != nil {
		t.Fatal(err)
	}
}
