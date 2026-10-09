package office

import (
	"context"
	"strings"
	"testing"

	"apptolast.com/ax-web/internal/harness"
)

func TestSlugAndUniqueID(t *testing.T) {
	cases := map[string]string{
		"Ada Lovelace":          "ada-lovelace",
		"  Señora Peña  ":       "senora-pena",
		"Revisión de código!!":  "revision-de-codigo",
		"123":                   "agente-123",
		"é":                     "agente",
		"":                      "agente",
		strings.Repeat("a", 50): strings.Repeat("a", 28),
	}
	for in, want := range cases {
		got := slug(in, "agente")
		if got != want || !ValidID(got) {
			t.Errorf("slug(%q) = %q, want %q (valid %v)", in, got, want, ValidID(got))
		}
	}
	taken := map[string]bool{"ada": true, "ada-2": true}
	if got := uniqueID("ada", func(s string) bool { return taken[s] }); got != "ada-3" {
		t.Fatal(got)
	}
}

func TestValidateAgent(t *testing.T) {
	lim := Limits{MaxTurns: 150, MaxTimeoutMinutes: 90}
	base := func() Agent {
		return Agent{Name: "Nueva", Role: "Prueba", Color: "#112233", Harness: harness.Claude, Model: "opus",
			Effort: "high", Mode: harness.ModeRead, MaxTurns: 10, TimeoutMinutes: 10}
	}
	cases := []struct {
		name  string
		edit  func(*Agent)
		field string
	}{
		{"ok", func(*Agent) {}, ""},
		{"empty name", func(a *Agent) { a.Name = " " }, "name"},
		{"long name", func(a *Agent) { a.Name = strings.Repeat("ñ", 61) }, "name"},
		{"newline in role", func(a *Agent) { a.Role = "a\nb" }, "role"},
		{"big emoji", func(a *Agent) { a.Emoji = strings.Repeat("🙂", 5) }, "emoji"},
		{"bad color", func(a *Agent) { a.Color = "red" }, "color"},
		{"harness", func(a *Agent) { a.Harness = "gemini" }, "harness"},
		{"mode", func(a *Agent) { a.Mode = "root" }, "mode"},
		{"model", func(a *Agent) { a.Model = "-x" }, "model"},
		{"free model ok", func(a *Agent) { a.Model = "claude-opus-6[1m]" }, ""},
		{"effort", func(a *Agent) { a.Effort = "ultra" }, "effort"},
		{"codex disabled", func(a *Agent) { a.Harness, a.Model, a.Effort, a.MaxTurns = harness.Codex, "gpt-6-sol", "ultra", 0 }, "harness"},
		{"advisor ok", func(a *Agent) { a.Model, a.Advisor = "haiku", "opus" }, ""},
		{"advisor equals model", func(a *Agent) { a.Model, a.Advisor = "opus", "opus" }, "advisor"},
		{"advisor malformed", func(a *Agent) { a.Advisor = "-x" }, "advisor"},
		{"turns zero claude", func(a *Agent) { a.MaxTurns = 0 }, "max_turns"},
		{"turns over", func(a *Agent) { a.MaxTurns = 151 }, "max_turns"},
		{"timeout", func(a *Agent) { a.TimeoutMinutes = 4 }, "timeout_minutes"},
		{"timeout over", func(a *Agent) { a.TimeoutMinutes = 91 }, "timeout_minutes"},
		{"prompt", func(a *Agent) { a.SystemPrompt = strings.Repeat("x", 16<<10+1) }, "system_prompt"},
		{"tools", func(a *Agent) { a.DisallowedTools = []string{"Bash(rm:*)", "1bad"} }, "disallowed_tools"},
		{"tools ok", func(a *Agent) { a.DisallowedTools = []string{"Bash(git push:*)", "WebFetch"} }, ""},
	}
	for _, c := range cases {
		a := base()
		c.edit(&a)
		err := validateAgent(&a, lim)
		var fe *FieldError
		if c.field == "" {
			if err != nil {
				t.Errorf("%s: %v", c.name, err)
			}
			continue
		}
		if !asField(err, &fe) || fe.Field != c.field {
			t.Errorf("%s: %v", c.name, err)
		}
	}
}

func TestEffectiveOverrides(t *testing.T) {
	a := &Agent{Harness: harness.Claude, Model: "opus", Effort: "xhigh", Mode: harness.ModeRead, MaxTurns: 40, TimeoutMinutes: 30}
	e := effective(a, &Overrides{Harness: harness.Codex})
	if e.Harness != harness.Codex || e.Model != "" || e.Effort != "" || e.MaxTurns != 0 || e.Mode != harness.ModeRead {
		t.Fatalf("%+v", e)
	}
	e = effective(a, &Overrides{Model: "haiku", Mode: harness.ModeFull, MaxTurns: 5, TimeoutMinutes: 6})
	if e.Model != "haiku" || e.Effort != "xhigh" || e.Mode != harness.ModeFull || e.MaxTurns != 5 || e.TimeoutMinutes != 6 {
		t.Fatalf("%+v", e)
	}
	c := &Agent{Harness: harness.Codex, Model: "gpt-6-sol", Effort: "ultra", Mode: harness.ModeFull, TimeoutMinutes: 30}
	if e := effective(c, &Overrides{Model: "gpt-5.5"}); e.Effort != "" {
		t.Fatalf("unsupported effort kept: %+v", e)
	}
}

func TestCreateJobValidation(t *testing.T) {
	h := newHarness(t, func(c *Config) { c.MaxQueue = 3 })
	ok := JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "Arregla el README"}
	cases := []struct {
		name  string
		edit  func(*JobRequest)
		field string
	}{
		{"project", func(r *JobRequest) { r.ProjectID = "nope" }, "project_id"},
		{"agent", func(r *JobRequest) { r.AgentID = "nope" }, "agent_id"},
		{"kind", func(r *JobRequest) { r.Kind = KindRetro }, "kind"},
		{"empty prompt", func(r *JobRequest) { r.Prompt = "  \n" }, "prompt"},
		{"big prompt", func(r *JobRequest) { r.Prompt = strings.Repeat("x", 32<<10+1) }, "prompt"},
		{"title", func(r *JobRequest) { r.Title = strings.Repeat("t", 121) }, "title"},
		{"branch", func(r *JobRequest) { r.Branch = "a..b" }, "branch"},
		{"priority", func(r *JobRequest) { r.Priority = ptr(3) }, "priority"},
		{"change needs full mode", func(r *JobRequest) { r.AgentID = "ada" }, "kind"},
		{"change in read override", func(r *JobRequest) { r.Overrides = &Overrides{Mode: harness.ModeRead} }, "kind"},
		{"override turns", func(r *JobRequest) { r.Overrides = &Overrides{MaxTurns: 999} }, "overrides.max_turns"},
		{"override effort", func(r *JobRequest) { r.Overrides = &Overrides{Effort: "ultra"} }, "overrides.effort"},
		{"source", func(r *JobRequest) { r.Source = &Source{Type: "job"} }, "source"},
	}
	for _, c := range cases {
		r := ok
		c.edit(&r)
		_, err := h.o.CreateJob(context.Background(), r, "x")
		var fe *FieldError
		if !asField(err, &fe) || fe.Field != c.field {
			t.Errorf("%s: %v", c.name, err)
		}
	}
	j := h.job(t, ok)
	if j.Title != "Arregla el README" || j.Branch != "develop" || j.Priority != 1 || j.Agent.Version != 1 ||
		j.Status != StatusQueued || j.Source.Type != "manual" || !ValidJobID(j.ID) {
		t.Fatalf("%+v", j)
	}
	h.job(t, ok)
	h.job(t, ok)
	_, err := h.o.CreateJob(context.Background(), ok, "x")
	if IsStatus(err) != 409 || err.Error() != "La cola está llena (3)" {
		t.Fatalf("full queue: %v", err)
	}
	// The audit has the prompt's size and digest, never its text.
	if a := h.audit.String(); !strings.Contains(a, `"prompt_sha256"`) || strings.Contains(a, "Arregla el README") {
		t.Fatalf("audit %s", a)
	}
	// An archived project takes no new jobs.
	h.o.UpdateProject("dockerswarm-infra", ProjectInput{}, "x")
	if _, err := h.o.UpdateProject("dockerswarm-infra", ProjectInput{Archived: ptr(true)}, "x"); IsStatus(err) != 409 {
		t.Fatalf("archiving the retro project: %v", err)
	}
}

func TestValidateSchedule(t *testing.T) {
	s := Schedule{Name: "Noche", Days: []int{5, 1, 1}, Time: "23:30"}
	if err := validateSchedule(&s); err != nil || len(s.Days) != 2 || s.Days[0] != 1 {
		t.Fatalf("%v %v", err, s.Days)
	}
	for _, bad := range []Schedule{
		{Name: "", Days: []int{1}, Time: "10:00"},
		{Name: "x", Days: nil, Time: "10:00"},
		{Name: "x", Days: []int{7}, Time: "10:00"},
		{Name: "x", Days: []int{1}, Time: "24:00"},
		{Name: "x", Days: []int{1}, Time: "9:00"},
	} {
		if err := validateSchedule(&bad); err == nil {
			t.Errorf("%+v accepted", bad)
		}
	}
}

func TestValidateProject(t *testing.T) {
	lim := Limits{RepoHosts: []string{"github.com"}}
	p := Project{Name: "X", Repo: "https://github.com/a/b"}
	if err := validateProject(&p, lim); err != nil || p.Branch != "main" {
		t.Fatalf("%v %+v", err, p)
	}
	for field, edit := range map[string]func(*Project){
		"repo":    func(p *Project) { p.Repo = "https://gitlab.com/a/b" },
		"branch":  func(p *Project) { p.Branch = "-x" },
		"service": func(p *Project) { p.Service = "Web App" },
		"url":     func(p *Project) { p.URL = "http://x.com" },
		"notes":   func(p *Project) { p.Notes = strings.Repeat("n", 8<<10+1) },
	} {
		q := Project{Name: "X", Repo: "https://github.com/a/b"}
		edit(&q)
		var fe *FieldError
		if err := validateProject(&q, lim); !asField(err, &fe) || fe.Field != field {
			t.Errorf("%s: %v", field, err)
		}
	}
}
