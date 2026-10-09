package office

import (
	"fmt"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// usageGatePercent is where new jobs stop starting. While a subscription
// window is at or above it, no queued job begins and the queue waits until
// every such window has reset.
const usageGatePercent = 95

// usageFreshness is how old a reported sample may be and still count. An
// older sample says nothing about the windows now, so it holds nothing: the
// next job reports a fresh one.
const usageFreshness = 15 * time.Minute

// usageHold returns when the queue may start jobs again and the reason it
// waits, or the zero time when no window is at the gate. A window whose
// reset has passed is empty again, so it holds nothing.
func usageHold(w *harness.UsageWindows, sampled, now time.Time) (time.Time, string) {
	if w == nil || now.Sub(sampled) > usageFreshness {
		return time.Time{}, ""
	}
	var until time.Time
	var name string
	var pct float64
	for _, c := range []struct {
		name string
		win  harness.UsageWindow
	}{{"de 5 horas", w.FiveHour}, {"semanal", w.SevenDay}} {
		at := c.win.Utilization * 100
		if !c.win.ResetsAt.After(now) || at < usageGatePercent {
			continue
		}
		// Every window at the gate must reset, so the latest reset counts.
		if until.IsZero() || c.win.ResetsAt.After(until) {
			until, name, pct = c.win.ResetsAt, c.name, at
		}
	}
	if until.IsZero() {
		return time.Time{}, ""
	}
	return until, fmt.Sprintf("Espera: la ventana %s está al %.0f %%; se reanuda a las %s UTC",
		name, pct, until.UTC().Format("15:04"))
}
