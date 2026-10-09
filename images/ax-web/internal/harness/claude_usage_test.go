package harness

import "testing"

// A rate_limit_event as `claude -p --output-format stream-json` prints it.
const rateLimitLine = `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791546000,` +
	`"rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":0.97,"resetsAt":1791546000},` +
	`"seven_day":{"utilization":0.12,"resetsAt":1792098000}}},"uuid":"u","session_id":"s"}`

func TestClaudeParserKeepsTheLatestWindows(t *testing.T) {
	p := &ClaudeParser{}
	if evs := p.line([]byte(rateLimitLine)); len(evs) != 0 {
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
}

func TestClaudeParserIgnoresAnEventWithoutBothWindows(t *testing.T) {
	p := &ClaudeParser{}
	p.line([]byte(rateLimitLine))
	partial := `{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0.5,"resetsAt":1}}}}`
	p.line([]byte(partial))
	if f := p.Final(); f.Windows == nil || f.Windows.FiveHour.Utilization != 0.97 {
		t.Fatalf("a partial event replaced the windows: %+v", f.Windows)
	}
}
