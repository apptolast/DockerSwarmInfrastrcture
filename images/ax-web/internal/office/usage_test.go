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
