package harness

import "testing"

// rateLimitLine is a rate_limit_event as this sandbox has actually
// observed it: status "allowed", no unifiedWindows (see claude_test.go:20).
const rateLimitLine = `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}`

// TestClaudeParserRealEventCarriesNoWindows documents the actually
// observed shape: a real rate_limit_event, as seen in production, yields
// no reading at all. The gate and the UI must treat this as "sin dato",
// never as 0%.
func TestClaudeParserRealEventCarriesNoWindows(t *testing.T) {
	p := &ClaudeParser{}
	if evs := p.line([]byte(rateLimitLine)); len(evs) != 0 {
		t.Fatalf("rate_limit_event produced %d timeline events, want none", len(evs))
	}
	f := p.Final()
	if f.Windows != nil || f.WindowsFirst != nil {
		t.Fatalf("a real event with no unifiedWindows produced a reading: %+v", f)
	}
}

// syntheticWindowsLine declares the unifiedWindows shape Claude Code's
// binary documents (the field is marked @internal there and is only
// filled in some states); it has not been observed in this office as of
// 2026-10-11 — every real sample seen so far (above) carries none. Kept so
// the parser's handling of that shape, if the CLI ever does emit it, does
// not silently rot.
const syntheticWindowsLine = `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791546000,` +
	`"rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":0.97,"resetsAt":1791546000},` +
	`"seven_day":{"utilization":0.12,"resetsAt":1792098000}}},"uuid":"u","session_id":"s"}`

func TestClaudeParserKeepsTheLatestWindows(t *testing.T) {
	p := &ClaudeParser{}
	if evs := p.line([]byte(syntheticWindowsLine)); len(evs) != 0 {
		t.Fatalf("rate_limit_event produced %d timeline events, want none", len(evs))
	}
	f := p.Final()
	if f.Windows == nil {
		t.Fatal("Final carries no windows")
	}
	if f.Windows.FiveHour.Utilization != 0.97 || f.Windows.FiveHour.ResetsAt.Unix() != 1791546000 {
		t.Fatalf("five_hour = %+v", f.Windows.FiveHour)
	}
	if f.Windows.SevenDay.Utilization != 0.12 || f.Windows.SevenDay.ResetsAt.Unix() != 1792098000 {
		t.Fatalf("seven_day = %+v", f.Windows.SevenDay)
	}
	if f.WindowsFirst == nil || f.WindowsFirst.FiveHour.Utilization != 0.97 {
		t.Fatalf("WindowsFirst = %+v", f.WindowsFirst)
	}
}

func TestClaudeParserIgnoresAnEventWithoutBothWindows(t *testing.T) {
	p := &ClaudeParser{}
	p.line([]byte(syntheticWindowsLine))
	partial := `{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0.5,"resetsAt":1}}}}`
	p.line([]byte(partial))
	if f := p.Final(); f.Windows == nil || f.Windows.FiveHour.Utilization != 0.97 {
		t.Fatalf("a partial event replaced the windows: %+v", f.Windows)
	}
}

// TestClaudeParserFirstAndLastWindowsDiffer: a run whose window reset
// mid-session must keep both readings distinct, each with its own
// ResetsAt — the office's delta needs to tell them apart to refuse a
// diff across a reset (see windowDeltaPct in the office package).
func TestClaudeParserFirstAndLastWindowsDiffer(t *testing.T) {
	first := `{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{` +
		`"five_hour":{"utilization":0.40,"resetsAt":1000},"seven_day":{"utilization":0.10,"resetsAt":2000}}}}`
	last := `{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{` +
		`"five_hour":{"utilization":0.05,"resetsAt":5000},"seven_day":{"utilization":0.11,"resetsAt":2000}}}}`
	p := &ClaudeParser{}
	p.line([]byte(first))
	p.line([]byte(last))
	f := p.Final()
	if f.WindowsFirst == nil || f.WindowsFirst.FiveHour.Utilization != 0.40 || f.WindowsFirst.FiveHour.ResetsAt.Unix() != 1000 {
		t.Fatalf("WindowsFirst = %+v", f.WindowsFirst)
	}
	if f.Windows == nil || f.Windows.FiveHour.Utilization != 0.05 || f.Windows.FiveHour.ResetsAt.Unix() != 5000 {
		t.Fatalf("Windows = %+v", f.Windows)
	}
}

// TestClaudeParserResultEventKeepsWindowsSeenEarlier: in a real session
// the rate_limit_event arrives before the final "result" event
// (claude_test.go's claudeSession fixture has it in that order). The
// result event used to rebuild Final from scratch and silently drop any
// window already observed.
func TestClaudeParserResultEventKeepsWindowsSeenEarlier(t *testing.T) {
	p := &ClaudeParser{}
	p.line([]byte(syntheticWindowsLine))
	p.line([]byte(`{"type":"result","subtype":"success","result":"ok"}`))
	f := p.Final()
	if f.Windows == nil || f.WindowsFirst == nil {
		t.Fatalf("the result event dropped the windows seen earlier: %+v", f)
	}
}
