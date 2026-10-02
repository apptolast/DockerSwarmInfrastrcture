package harness

import (
	"errors"
	"slices"
	"strings"
	"testing"
	"time"
)

func claudeSpec() Spec {
	return Spec{
		ID: "web-j20261002-0001", Repo: "https://github.com/apptolast/demo", Branch: "main",
		Prompt: "Explica la arquitectura.\nCon detalle.", Harness: Claude, Model: "opus", Effort: "high",
		Mode: ModeRead, MaxTurns: 40, Timeout: 30 * time.Minute,
	}
}

func TestCommandClaudeRead(t *testing.T) {
	s := claudeSpec()
	s.AppendSystemPrompt = "Eres Ada.\nArquitecta."
	s.DisallowedTools = []string{"WebFetch"} // ignored in read mode
	got, err := Command(s)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"ax-agent", "claude", "-p", "--output-format", "stream-json", "--verbose",
		"--strict-mcp-config", "--max-turns", "40", "--restricted",
		"--model", "opus", "--effort", "high", "--append-system-prompt", "Eres Ada.\nArquitecta."}
	if !slices.Equal(got, want) {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
	if env := ExtraEnv(s); len(env) != 0 {
		t.Fatalf("read mode env %v", env)
	}
	for _, a := range got {
		if strings.Contains(a, "Explica") {
			t.Fatal("the prompt leaked into argv")
		}
	}
}

func TestCommandClaudeFull(t *testing.T) {
	s := claudeSpec()
	s.Mode, s.Model, s.FallbackModel, s.Effort = ModeFull, "claude-opus-5-5[1m]", "sonnet", ""
	s.DisallowedTools = []string{"Bash(git push *)", "WebFetch"}
	got, err := Command(s)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"ax-agent", "claude", "-p", "--output-format", "stream-json", "--verbose",
		"--strict-mcp-config", "--max-turns", "40",
		"--setting-sources", "user", "--permission-mode", "bypassPermissions",
		"--disallowed-tools", "Bash(git push *),WebFetch",
		"--model", "claude-opus-5-5[1m]", "--fallback-model", "sonnet"}
	if !slices.Equal(got, want) {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
	if env := ExtraEnv(s); len(env) != 1 || env["IS_SANDBOX"] != "1" {
		t.Fatalf("full mode env %v", env)
	}
}

func TestCommandCodex(t *testing.T) {
	s := claudeSpec()
	s.Harness, s.Model, s.Effort, s.MaxTurns = Codex, "gpt-6-sol", "ultra", 0
	s.AppendSystemPrompt, s.FallbackModel = "ignorado", "ignorado"
	got, err := Command(s)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"ax-agent", "codex", "exec", "--json", "--color", "never", "--ephemeral",
		"--sandbox", "danger-full-access", "-m", "gpt-6-sol", "-c", `model_reasoning_effort="ultra"`, "-"}
	if !slices.Equal(got, want) {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
	s.Model, s.Effort, s.Mode = "", "", ModeFull
	got, _ = Command(s)
	if !slices.Equal(got, []string{"ax-agent", "codex", "exec", "--json", "--color", "never",
		"--ephemeral", "--sandbox", "danger-full-access", "-"}) {
		t.Fatalf("defaults %q", got)
	}
	if env := ExtraEnv(s); len(env) != 0 {
		t.Fatalf("codex env %v", env)
	}
}

func TestValidateRejects(t *testing.T) {
	cases := []struct {
		name, field string
		edit        func(*Spec)
	}{
		{"id prefix", "id", func(s *Spec) { s.ID = "tarea-1" }},
		{"id upper", "id", func(s *Spec) { s.ID = "web-ABC" }},
		{"id long", "id", func(s *Spec) { s.ID = "web-" + strings.Repeat("a", 60) }},
		{"id dash", "id", func(s *Spec) { s.ID = "web--x" }},
		{"harness", "harness", func(s *Spec) { s.Harness = "gemini" }},
		{"mode", "mode", func(s *Spec) { s.Mode = "root" }},
		{"model option", "model", func(s *Spec) { s.Model = "--dangerously-skip-permissions" }},
		{"model space", "model", func(s *Spec) { s.Model = "opus x" }},
		{"model newline", "model", func(s *Spec) { s.Model = "opus\n" }},
		{"model long", "model", func(s *Spec) { s.Model = strings.Repeat("m", 129) }},
		{"fallback", "fallback_model", func(s *Spec) { s.FallbackModel = "-x" }},
		{"claude ultra", "effort", func(s *Spec) { s.Effort = "ultra" }},
		{"effort", "effort", func(s *Spec) { s.Effort = "extreme" }},
		{"turns 0", "max_turns", func(s *Spec) { s.MaxTurns = 0 }},
		{"turns 501", "max_turns", func(s *Spec) { s.MaxTurns = 501 }},
		{"timeout short", "timeout", func(s *Spec) { s.Timeout = 59 * time.Second }},
		{"timeout long", "timeout", func(s *Spec) { s.Timeout = 3*time.Hour + time.Second }},
		{"empty prompt", "prompt", func(s *Spec) { s.Prompt = " \n\t" }},
		{"big prompt", "prompt", func(s *Spec) { s.Prompt = strings.Repeat("a", MaxPromptBytes+1) }},
		{"nul prompt", "prompt", func(s *Spec) { s.Prompt = "a\x00b" }},
		{"bad utf8", "prompt", func(s *Spec) { s.Prompt = "a\xffb" }},
		{"big persona", "system_prompt", func(s *Spec) { s.AppendSystemPrompt = strings.Repeat("a", MaxSystemPromptBytes+1) }},
		{"nul persona", "system_prompt", func(s *Spec) { s.AppendSystemPrompt = "a\x00" }},
		{"many tools", "disallowed_tools", func(s *Spec) { s.DisallowedTools = make([]string, 21) }},
		{"tool newline", "disallowed_tools", func(s *Spec) { s.DisallowedTools = []string{"Bash\nx"} }},
		{"tool option", "disallowed_tools", func(s *Spec) { s.DisallowedTools = []string{"--model"} }},
		{"tool comma", "disallowed_tools", func(s *Spec) { s.DisallowedTools = []string{"Bash,Edit"} }},
	}
	for _, c := range cases {
		s := claudeSpec()
		c.edit(&s)
		err := Validate(s)
		var se *SpecError
		if !errors.As(err, &se) || se.Field != c.field {
			t.Errorf("%s: got %v, want a %s error", c.name, err, c.field)
		}
		if _, err := Command(s); err == nil {
			t.Errorf("%s: Command accepted it", c.name)
		}
	}
}

func TestValidateAccepts(t *testing.T) {
	ok := []func(*Spec){
		func(s *Spec) {},
		func(s *Spec) { s.Model, s.Effort = "", "" },
		func(s *Spec) { s.Model = "claude-haiku-4-5-20251001" },
		func(s *Spec) { s.Model = "us.anthropic/claude:x_1.2" },
		func(s *Spec) { s.Harness, s.Effort, s.MaxTurns = Codex, "ultra", 0 },
		func(s *Spec) { s.Harness, s.Model = Codex, "gpt-5.6-terra" },
		func(s *Spec) { s.MaxTurns, s.Timeout = 500, 3*time.Hour },
		func(s *Spec) { s.MaxTurns, s.Timeout = 1, time.Minute },
		func(s *Spec) { s.ID = "web-" + strings.Repeat("a", 59) },
		func(s *Spec) { s.Prompt = strings.Repeat("ñ", MaxPromptBytes/2) },
		func(s *Spec) { s.DisallowedTools = []string{"Bash(rm -rf *)", "mcp__x__y", "Edit"} },
	}
	for i, edit := range ok {
		s := claudeSpec()
		edit(&s)
		if err := Validate(s); err != nil {
			t.Errorf("case %d: %v", i, err)
		}
	}
}
