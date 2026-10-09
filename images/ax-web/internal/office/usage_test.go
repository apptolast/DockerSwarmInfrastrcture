package office

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

func TestUsageHold(t *testing.T) {
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	window := func(pct float64, resetIn time.Duration) harness.UsageWindow {
		return harness.UsageWindow{Utilization: pct / 100, ResetsAt: now.Add(resetIn)}
	}
	fresh := now.Add(-time.Minute)
	cases := []struct {
		name    string
		w       *harness.UsageWindows
		sampled time.Time
		want    time.Time
		reason  string
	}{
		{name: "no sample holds nothing", w: nil, sampled: fresh},
		{name: "stale sample holds nothing", sampled: now.Add(-16 * time.Minute),
			w: &harness.UsageWindows{FiveHour: window(99, time.Hour), SevenDay: window(1, time.Hour)}},
		{name: "below the gate", sampled: fresh,
			w: &harness.UsageWindows{FiveHour: window(94.9, time.Hour), SevenDay: window(50, time.Hour)}},
		{name: "five-hour window at the gate waits for its reset", sampled: fresh,
			w:      &harness.UsageWindows{FiveHour: window(95, 2*time.Hour), SevenDay: window(10, 72*time.Hour)},
			want:   now.Add(2 * time.Hour),
			reason: "de 5 horas"},
		{name: "both at the gate wait for the later reset", sampled: fresh,
			w:      &harness.UsageWindows{FiveHour: window(97, time.Hour), SevenDay: window(96, 48*time.Hour)},
			want:   now.Add(48 * time.Hour),
			reason: "semanal"},
		{name: "a window whose reset has passed holds nothing", sampled: fresh,
			w: &harness.UsageWindows{FiveHour: window(99, -time.Minute), SevenDay: window(5, 24*time.Hour)}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			until, reason := usageHold(c.w, c.sampled, now)
			if !until.Equal(c.want) {
				t.Fatalf("until = %v, want %v", until, c.want)
			}
			if c.reason == "" {
				if reason != "" {
					t.Fatalf("reason = %q, want none", reason)
				}
				return
			}
			if !strings.Contains(reason, c.reason) || !strings.Contains(reason, "UTC") {
				t.Fatalf("reason = %q, want the %q window and the UTC reset time", reason, c.reason)
			}
		})
	}
}

func TestUsageView(t *testing.T) {
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	window := func(pct float64, resetIn time.Duration) harness.UsageWindow {
		return harness.UsageWindow{Utilization: pct / 100, ResetsAt: now.Add(resetIn)}
	}
	t.Run("no sample shows only the gate", func(t *testing.T) {
		v := usageView(nil, time.Time{}, now)
		if v.FiveHour != nil || v.SevenDay != nil || v.Sampled != nil || v.Stale || v.GatePercent != usageGatePercent {
			t.Errorf("got %+v", v)
		}
	})
	t.Run("a live window shows its percent and its reset", func(t *testing.T) {
		v := usageView(&harness.UsageWindows{FiveHour: window(42, 2*time.Hour), SevenDay: window(18, 72*time.Hour)},
			now.Add(-time.Minute), now)
		if v.FiveHour == nil || v.FiveHour.Percent < 41.9 || v.FiveHour.Percent > 42.1 ||
			!v.FiveHour.ResetsAt.Equal(now.Add(2*time.Hour)) {
			t.Errorf("five-hour: %+v", v.FiveHour)
		}
		if v.SevenDay == nil || v.SevenDay.Percent < 17.9 || v.SevenDay.Percent > 18.1 {
			t.Errorf("seven-day: %+v", v.SevenDay)
		}
		if v.Stale || v.Sampled == nil {
			t.Errorf("a one-minute-old sample is fresh: %+v", v)
		}
	})
	t.Run("a window that already reset is not shown", func(t *testing.T) {
		v := usageView(&harness.UsageWindows{FiveHour: window(99, -time.Minute), SevenDay: window(5, time.Hour)},
			now.Add(-time.Minute), now)
		if v.FiveHour != nil || v.SevenDay == nil {
			t.Errorf("got five-hour %+v and seven-day %+v", v.FiveHour, v.SevenDay)
		}
	})
	t.Run("an old sample is stale but still shown", func(t *testing.T) {
		v := usageView(&harness.UsageWindows{FiveHour: window(60, time.Hour), SevenDay: window(5, time.Hour)},
			now.Add(-20*time.Minute), now)
		if !v.Stale || v.FiveHour == nil {
			t.Errorf("got %+v", v)
		}
	})
}

func TestSnapshotCarriesTheUsageOfTheLastRun(t *testing.T) {
	h := newHarness(t)
	if v := h.o.Snapshot().Usage; v.FiveHour != nil || v.GatePercent != usageGatePercent {
		t.Fatalf("before any run: %+v", v)
	}
	h.o.mu.Lock()
	h.o.usage = &harness.UsageWindows{
		FiveHour: harness.UsageWindow{Utilization: 0.5, ResetsAt: t0.Add(time.Hour)},
		SevenDay: harness.UsageWindow{Utilization: 0.1, ResetsAt: t0.Add(24 * time.Hour)},
	}
	h.o.usageAt = t0
	h.o.mu.Unlock()
	v := h.o.Snapshot().Usage
	if v.FiveHour == nil || v.FiveHour.Percent < 49.9 || v.FiveHour.Percent > 50.1 || v.SevenDay == nil {
		t.Errorf("after a sample: %+v", v)
	}
}

// The numbers come from the agent's stream: nonsense must neither break the
// snapshot nor hold the queue.
func TestHostileUsageWindowsAreIgnored(t *testing.T) {
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	good := harness.UsageWindow{Utilization: 0.97, ResetsAt: now.Add(time.Hour)}
	ms := time.UnixMilli(1791546000000).UTC() // a reset sent in milliseconds: the year 58000
	cases := map[string]harness.UsageWindow{
		"NaN":            {Utilization: math.NaN(), ResetsAt: now.Add(time.Hour)},
		"+Inf":           {Utilization: math.Inf(1), ResetsAt: now.Add(time.Hour)},
		"huge":           {Utilization: 1e307, ResetsAt: now.Add(time.Hour)},
		"negative":       {Utilization: -0.5, ResetsAt: now.Add(time.Hour)},
		"reset in ms":    {Utilization: 0.99, ResetsAt: ms},
		"reset too far":  {Utilization: 0.99, ResetsAt: now.Add(9 * 24 * time.Hour)},
		"reset was zero": {Utilization: 0.99},
	}
	for name, bad := range cases {
		t.Run(name, func(t *testing.T) {
			w := &harness.UsageWindows{FiveHour: bad, SevenDay: good}
			v := usageView(w, now.Add(-time.Minute), now)
			if v.FiveHour != nil {
				t.Errorf("the bad window was kept: %+v", v.FiveHour)
			}
			if v.SevenDay == nil {
				t.Error("the good window was lost with the bad one")
			}
			if _, err := json.Marshal(Snapshot{Usage: v}); err != nil {
				t.Fatalf("the snapshot no longer marshals: %v", err)
			}
			until, msg := usageHold(&harness.UsageWindows{FiveHour: bad}, now.Add(-time.Minute), now)
			if !until.IsZero() || msg != "" {
				t.Errorf("a bad window held the queue until %v: %q", until, msg)
			}
		})
	}
}

func TestUsageOverOneHundredPercentIsDrawnAsFull(t *testing.T) {
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	v := usageView(&harness.UsageWindows{FiveHour: harness.UsageWindow{Utilization: 1.04, ResetsAt: now.Add(time.Hour)}},
		now, now)
	if v.FiveHour == nil || v.FiveHour.Percent != 100 {
		t.Errorf("got %+v, want a bar at 100", v.FiveHour)
	}
}

// A finished run that reports windows reaches the snapshot and tells the
// browser to reload, through the real finalize path.
func TestFinishedRunPublishesItsUsageWindows(t *testing.T) {
	h := newHarness(t)
	sub := h.o.hub.subscribe("")
	h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "uso"})
	res := exited(0, "ok")
	res.Windows = &harness.UsageWindows{
		FiveHour: harness.UsageWindow{Utilization: 0.61, ResetsAt: t0.Add(2 * time.Hour)},
		SevenDay: harness.UsageWindow{Utilization: 0.12, ResetsAt: t0.Add(48 * time.Hour)},
	}
	h.runNext(t, res)
	v := h.o.Snapshot().Usage
	if v.FiveHour == nil || v.FiveHour.Percent < 60.9 || v.FiveHour.Percent > 61.1 || v.SevenDay == nil {
		t.Fatalf("snapshot usage after the run: %+v", v)
	}
	h.o.flushDelta()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case m := <-sub.C:
			var d Delta
			if m.Event == "office" && json.Unmarshal(m.Data, &d) == nil {
				for _, k := range d.Invalidate {
					if k == "usage" {
						return
					}
				}
			}
		case <-deadline:
			t.Fatal("no delta told the browser to reload the usage")
		}
	}
}
