package office

import (
	"fmt"
	"math"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

// usageGatePercent is where new jobs stop starting. While a subscription
// window is at or above it, no queued job begins and the queue waits until
// every such window has reset.
const usageGatePercent = 95

// maxWindowReset bounds how far ahead a window may say it resets: the
// longest one is a week.
const maxWindowReset = 8 * 24 * time.Hour

// validWindow says whether a reported window can be trusted: a finite
// utilization (a fraction, allowed a little over 1) and a reset that is still
// ahead but within a week. The numbers come from the agent's stream, so a
// nonsense one must neither hold the queue for ever nor break the snapshot.
func validWindow(w harness.UsageWindow, now time.Time) bool {
	u := w.Utilization
	return !math.IsNaN(u) && !math.IsInf(u, 0) && u >= 0 && u <= 10 &&
		w.ResetsAt.After(now) && !w.ResetsAt.After(now.Add(maxWindowReset))
}

// usageFreshness is how old a reported sample may be and still count. An
// older sample says nothing about the windows now, so it holds nothing: the
// next job reports a fresh one.
const usageFreshness = 15 * time.Minute

// UsageBar is one subscription window as the header shows it.
type UsageBar struct {
	Percent  float64   `json:"percent"`
	ResetsAt time.Time `json:"resets_at"`
}

// UsageView is the usage of the subscription windows in the snapshot. A
// window is absent when none was reported or it has reset since.
type UsageView struct {
	FiveHour    *UsageBar  `json:"five_hour,omitempty"`
	SevenDay    *UsageBar  `json:"seven_day,omitempty"`
	Sampled     *time.Time `json:"sampled,omitempty"`
	GatePercent int        `json:"gate_percent"`
	Stale       bool       `json:"stale"`
}

// usageView is what the header shows of the last reported windows.
func usageView(w *harness.UsageWindows, sampled, now time.Time) UsageView {
	v := UsageView{GatePercent: usageGatePercent}
	if w == nil {
		return v
	}
	v.Sampled = &sampled
	v.Stale = now.Sub(sampled) > usageFreshness
	bar := func(win harness.UsageWindow) *UsageBar {
		if !validWindow(win, now) {
			return nil
		}
		return &UsageBar{Percent: math.Min(win.Utilization*100, 100), ResetsAt: win.ResetsAt}
	}
	v.FiveHour, v.SevenDay = bar(w.FiveHour), bar(w.SevenDay)
	return v
}

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
		if !validWindow(c.win, now) || at < usageGatePercent {
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
