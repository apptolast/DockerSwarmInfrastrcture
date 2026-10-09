package harness

import (
	"slices"
	"testing"
)

// The result line of `claude -p --output-format json` for a run whose
// executor consulted an Opus advisor.
const resultWithAdvisor = `{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.2577,` +
	`"num_turns":8,"duration_ms":1000,"result":"PASS","modelUsage":{` +
	`"claude-haiku-5-5":{"costUSD":0.018},"claude-opus-5-5":{"costUSD":0.2398}}}`

func TestClaudeResultReportsEachModelsCost(t *testing.T) {
	p := &ClaudeParser{}
	p.line([]byte(resultWithAdvisor))
	f := p.Final()
	if f.ModelCost["claude-haiku-5-5"] != 0.018 || f.ModelCost["claude-opus-5-5"] != 0.2398 {
		t.Fatalf("ModelCost = %v", f.ModelCost)
	}
}

func TestClaudeResultDropsInvalidCosts(t *testing.T) {
	p := &ClaudeParser{}
	p.line([]byte(`{"type":"result","subtype":"success","modelUsage":{"x":{"costUSD":-1},"y":{"costUSD":0.5}}}`))
	f := p.Final()
	if _, bad := f.ModelCost["x"]; bad || f.ModelCost["y"] != 0.5 {
		t.Fatalf("ModelCost = %v", f.ModelCost)
	}
}

func TestAdvisorReachesTheClaudeCommand(t *testing.T) {
	s := claudeSpec()
	s.Model, s.Advisor = "haiku", "opus"
	got, err := Command(s)
	if err != nil {
		t.Fatal(err)
	}
	i := slices.Index(got, "--advisor")
	if i < 0 || i+1 >= len(got) || got[i+1] != "opus" {
		t.Fatalf("argv has no --advisor opus: %v", got)
	}
}

func TestValidateAdvisor(t *testing.T) {
	s := claudeSpec()
	s.Model, s.Advisor = "haiku", "opus"
	if err := Validate(s); err != nil {
		t.Fatal(err)
	}
	s.Advisor = "haiku"
	if err := Validate(s); err == nil {
		t.Fatal("an advisor equal to the executor was accepted")
	}
	s.Advisor, s.Harness, s.Model, s.Effort, s.MaxTurns = "opus", Codex, "gpt-6-sol", "high", 0
	if err := Validate(s); err == nil {
		t.Fatal("an advisor on Codex was accepted")
	}
}
