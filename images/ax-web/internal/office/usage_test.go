package office

import (
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
