package office

import (
	"math"
	"slices"
	"sort"
	"time"
)

type agentKey struct {
	agent   string
	version int
}

type modelKey struct {
	harness string
	model   string
}

type acc struct {
	m        Metric
	seconds  float64
	timed    int
	turns    int
	evalSum  int
	lastUsed time.Time
}

// finiteCost is a job's cost for the sums: a value that is not a finite
// number of at least 0 (it cannot be written as JSON, or makes no sense)
// counts as 0.
func finiteCost(c float64) float64 {
	if math.IsNaN(c) || math.IsInf(c, 0) || c < 0 {
		return 0
	}
	return c
}

func modelOf(j *Job) string {
	switch {
	case j.Agent.Model != "":
		return j.Agent.Model
	case j.Usage.ModelUsed != "":
		return j.Usage.ModelUsed
	}
	return "predeterminado"
}

// computeMetrics aggregates finished jobs per agent version and per
// model, with the verdicts of the reviews of their changes and the
// scores of their evaluations.
func computeMetrics(jobs map[string]*Job, pipelines []Pipeline, now time.Time) Metrics {
	byAgent := map[agentKey]*acc{}
	byModel := map[modelKey]*acc{}
	accs := func(j *Job) (*acc, *acc) {
		ak := agentKey{j.AgentID, j.Agent.Version}
		mk := modelKey{j.Agent.Harness, modelOf(j)}
		a := byAgent[ak]
		if a == nil {
			a = &acc{m: Metric{AgentID: ak.agent, Version: ak.version}}
			byAgent[ak] = a
		}
		m := byModel[mk]
		if m == nil {
			m = &acc{m: Metric{Harness: mk.harness, Model: mk.model}}
			byModel[mk] = m
		}
		return a, m
	}
	out := Metrics{ByAgentVersion: []Metric{}, ByModel: []Metric{}}
	y, mo, d := now.UTC().Date()
	today := time.Date(y, mo, d, 0, 0, 0, 0, time.UTC)
	for _, j := range jobs {
		if !isTerminal(j.Status) {
			continue
		}
		cost := finiteCost(j.Usage.CostUSD)
		out.TotalCostUSD += cost
		if j.Finished != nil && !j.Finished.Before(today) {
			out.CostTodayUSD += cost
			out.JobsToday++
		}
		a, m := accs(j)
		for _, x := range []*acc{a, m} {
			x.m.Jobs++
			switch j.Status {
			case StatusDone:
				x.m.Succeeded++
			case StatusFailed:
				x.m.Failed++
			}
			if j.Rating != nil {
				switch j.Rating.Score {
				case 1:
					x.m.ThumbsUp++
				case -1:
					x.m.ThumbsDown++
				}
			}
			x.m.CostUSD += cost
			x.turns += max(j.Usage.Turns, 0)
			if j.Started != nil && j.Finished != nil && j.Finished.After(*j.Started) {
				x.seconds += j.Finished.Sub(*j.Started).Seconds()
				x.timed++
			}
			if j.Finished != nil && j.Finished.After(x.lastUsed) {
				x.lastUsed = *j.Finished
			}
		}
	}
	// Reviews of changes count for the author of the changes.
	for _, r := range jobs {
		if r.Kind != KindReview || r.Verdict == "" || r.ApplyFrom == "" || !isTerminal(r.Status) {
			continue
		}
		c := jobs[r.ApplyFrom]
		if c == nil || c.Kind != KindChange || !isTerminal(c.Status) {
			continue
		}
		a, m := accs(c)
		for _, x := range []*acc{a, m} {
			if r.Verdict == VerdictApproved {
				x.m.Approved++
			} else {
				x.m.Rejected++
			}
		}
	}
	// Evaluation scores count for the candidate.
	for _, p := range pipelines {
		if p.Template != TplEval || p.Score == nil || len(p.Steps) == 0 {
			continue
		}
		c := jobs[p.Steps[0].JobID]
		if c == nil || !isTerminal(c.Status) {
			continue
		}
		a, m := accs(c)
		for _, x := range []*acc{a, m} {
			x.m.EvalRuns++
			x.evalSum += *p.Score
		}
	}
	finish := func(x *acc) Metric {
		m := x.m
		if m.Jobs > 0 {
			m.AvgCostUSD = m.CostUSD / float64(m.Jobs)
			m.AvgTurns = float64(x.turns) / float64(m.Jobs)
		}
		if x.timed > 0 {
			m.AvgSeconds = x.seconds / float64(x.timed)
		}
		if m.EvalRuns > 0 {
			m.EvalScoreAvg = float64(x.evalSum) / float64(m.EvalRuns)
		}
		if !x.lastUsed.IsZero() {
			t := x.lastUsed
			m.LastUsed = &t
		}
		return m
	}
	for _, x := range byAgent {
		out.ByAgentVersion = append(out.ByAgentVersion, finish(x))
	}
	for _, x := range byModel {
		out.ByModel = append(out.ByModel, finish(x))
	}
	sort.Slice(out.ByAgentVersion, func(i, j int) bool {
		a, b := out.ByAgentVersion[i], out.ByAgentVersion[j]
		if a.AgentID != b.AgentID {
			return a.AgentID < b.AgentID
		}
		return a.Version > b.Version
	})
	sort.Slice(out.ByModel, func(i, j int) bool {
		a, b := out.ByModel[i], out.ByModel[j]
		if a.Harness != b.Harness {
			return a.Harness < b.Harness
		}
		return a.Model < b.Model
	})
	return out
}

// metricsLocked returns the leaderboard, recomputed once per revision
// and per UTC day (the "today" figures change at midnight).
func (o *Office) metricsLocked() Metrics {
	day := o.now().UTC().Format(time.DateOnly)
	if o.metricsRev != o.rev || o.metricsDay != day {
		o.metrics = computeMetrics(o.jobs, o.st.Pipelines, o.now())
		o.metricsRev, o.metricsDay = o.rev, day
	}
	m := o.metrics
	m.ByAgentVersion = slices.Clone(m.ByAgentVersion)
	m.ByModel = slices.Clone(m.ByModel)
	return m
}

// Metrics returns the leaderboard.
func (o *Office) Metrics() Metrics {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.metricsLocked()
}
