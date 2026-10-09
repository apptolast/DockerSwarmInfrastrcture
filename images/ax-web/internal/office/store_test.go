package office

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/harness"
)

func TestWriteAtomicKeepsBackup(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "office.json")
	if err := writeAtomic(p, []byte(`{"v":1}`), true); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(p + ".bak"); !os.IsNotExist(err) {
		t.Fatal("a first write made a backup of nothing")
	}
	if err := writeAtomic(p, []byte(`{"v":2}`), true); err != nil {
		t.Fatal(err)
	}
	cur, _ := os.ReadFile(p)
	bak, _ := os.ReadFile(p + ".bak")
	if string(cur) != `{"v":2}` || string(bak) != `{"v":1}` {
		t.Fatalf("cur %s bak %s", cur, bak)
	}
	info, _ := os.Stat(p)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", info.Mode())
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp") {
			t.Fatalf("temporary file left: %s", e.Name())
		}
	}
}

func TestLoadFallbacks(t *testing.T) {
	now := func() time.Time { return t0 }
	good := `{"schema":1,"settings":{"office_name":"X"},"agents":[],"seq":{}}`
	cases := []struct {
		name, main, bak string
		found           bool
		warn            string
		officeName      string
		corrupt         int
	}{
		{"fresh", "", "", false, "", "", 0},
		{"main ok", good, "", true, "", "X", 0},
		{"main corrupt, bak ok", "{nope", good, true, "dañado", "X", 1},
		{"main missing, bak ok", "", good, true, "Faltaba", "X", 0},
		{"both corrupt", "{nope", "[]", false, "empezó de cero", "", 2},
		{"schema zero is corrupt", `{"settings":{}}`, good, true, "dañado", "X", 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			s, err := openStore(dir, now)
			if err != nil {
				t.Fatal(err)
			}
			if c.main != "" {
				os.WriteFile(filepath.Join(dir, officeFileName), []byte(c.main), 0o600)
			}
			if c.bak != "" {
				os.WriteFile(filepath.Join(dir, officeFileName+".bak"), []byte(c.bak), 0o600)
			}
			of, found, warn, err := s.loadOffice()
			if err != nil {
				t.Fatal(err)
			}
			if found != c.found || !strings.Contains(warn, c.warn) || (c.warn == "" && warn != "") {
				t.Fatalf("found %v warn %q", found, warn)
			}
			if found && of.Settings.OfficeName != c.officeName {
				t.Fatalf("settings %+v", of.Settings)
			}
			matches, _ := filepath.Glob(filepath.Join(dir, "*.corrupt-*"))
			if len(matches) != c.corrupt {
				t.Fatalf("corrupt files %v", matches)
			}
		})
	}
}

func TestUnknownSchemaRefusesToStart(t *testing.T) {
	cfg := testConfig(t)
	os.MkdirAll(cfg.StateDir, 0o700)
	os.WriteFile(filepath.Join(cfg.StateDir, officeFileName), []byte(`{"schema":2}`), 0o600)
	if _, err := New(cfg, newFakeExec(), nil, func() time.Time { return t0 }, nil); err == nil ||
		!strings.Contains(err.Error(), "esquema 2") {
		t.Fatalf("err %v", err)
	}
}

func TestCorruptStateStartsFreshWithWarning(t *testing.T) {
	cfg := testConfig(t)
	os.MkdirAll(cfg.StateDir, 0o700)
	os.WriteFile(filepath.Join(cfg.StateDir, jobsFileName), []byte(`garbage`), 0o600)
	o, err := New(cfg, newFakeExec(), nil, func() time.Time { return t0 }, nil)
	if err != nil {
		t.Fatal(err)
	}
	s := o.Snapshot()
	if len(s.Warnings) != 1 || !strings.Contains(s.Warnings[0], "jobs.json") || len(s.Agents) != 9 {
		t.Fatalf("warnings %v agents %d", s.Warnings, len(s.Agents))
	}
	// The fresh state was written.
	if _, err := os.Stat(filepath.Join(cfg.StateDir, jobsFileName)); err != nil {
		t.Fatal(err)
	}
}

func TestEventLogTruncates(t *testing.T) {
	s, _ := openStore(t.TempDir(), func() time.Time { return t0 })
	l, err := s.openEventLog("j1")
	if err != nil {
		t.Fatal(err)
	}
	big := strings.Repeat("x", 3000)
	n := 0
	for !l.truncated {
		n++
		l.append(harness.Event{Seq: int64(n), Kind: harness.EventText, Text: big})
	}
	l.append(harness.Event{Seq: int64(n + 1), Kind: harness.EventText, Text: "después"})
	l.close()
	info, _ := os.Stat(filepath.Join(s.jobDir("j1"), fileEvents))
	if info.Size() > maxEventsBytes+512 {
		t.Fatalf("size %d", info.Size())
	}
	evs, err := s.readEvents("j1")
	if err != nil {
		t.Fatal(err)
	}
	last := evs[len(evs)-1]
	if last.Text != "registro truncado" || last.Kind != harness.EventSystem {
		t.Fatalf("last %+v", last)
	}
	// Reopening a full log keeps it closed.
	l2, _ := s.openEventLog("j1")
	if !l2.truncated {
		t.Fatal("reopened log accepts more")
	}
	l2.close()
	if _, err := s.openEventLog("../x"); err == nil {
		t.Fatal("bad id accepted")
	}
}

func TestSeedAndReconcileProjects(t *testing.T) {
	h := newHarness(t)
	s := h.o.Snapshot()
	if len(s.Agents) != 9 || s.Settings.DefaultAgent != "linus" || s.Settings.RetroProject != "dockerswarm-infra" ||
		s.Settings.AutoLessons != LessonsPropose || s.Settings.MaxLessonsInPrompt != 12 || s.Settings.MaxIterations != 2 {
		t.Fatalf("seed %+v", s.Settings)
	}
	for _, a := range s.Agents {
		lines := strings.Count(a.SystemPrompt, "\n") + 1
		// Codex does not run in the Oficina, so its agent is seeded off.
		if a.Enabled != (a.Harness != harness.Codex) || a.Version != 1 || lines < 8 || lines > 25 {
			t.Errorf("agent %s: enabled %v version %d prompt lines %d", a.ID, a.Enabled, a.Version, lines)
		}
	}
	if len(s.Projects) != 2 || !s.Projects[1].Seeded || s.Projects[1].Branch != "develop" {
		t.Fatalf("projects %+v", s.Projects)
	}
	// Notes and memory survive a configuration change; a project leaving
	// the configuration becomes deletable.
	if _, err := h.o.UpdateProject("web", ProjectInput{Notes: ptr("usa pnpm")}, "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.MemoryAction("web", MemoryRequest{Action: "add", Text: "Los tests van con pnpm test"}, "x"); err != nil {
		t.Fatal(err)
	}
	h.cfg.Projects[1].Name = "Web pública"
	h.cfg.Projects[1].Branch = "main"
	h.cfg.Projects = h.cfg.Projects[1:]
	o2 := h.reopen(t)
	s2 := o2.Snapshot()
	var web, infra *Project
	for i := range s2.Projects {
		switch s2.Projects[i].ID {
		case "web":
			web = &s2.Projects[i]
		case "dockerswarm-infra":
			infra = &s2.Projects[i]
		}
	}
	if web == nil || web.Name != "Web pública" || web.Branch != "main" || web.Notes != "usa pnpm" || len(web.Memory) != 1 {
		t.Fatalf("web %+v", web)
	}
	if infra == nil || infra.Seeded {
		t.Fatalf("infra %+v", infra)
	}
	if err := o2.DeleteProject("web", "x"); IsStatus(err) != 409 {
		t.Fatalf("seeded delete: %v", err)
	}
}

func ptr[T any](v T) *T { return &v }

func TestRecoveryAfterRestart(t *testing.T) {
	h := newHarness(t)
	pl, err := h.o.CreatePipeline(t.Context(), PipelineRequest{Template: TplTeam, ProjectID: "web", Task: "Añade un test"}, "x")
	if err != nil {
		t.Fatal(err)
	}
	task := h.dispatch(t)
	if task == "" {
		t.Fatal("not dispatched")
	}
	h.exec.run(task).hooks.OnState(harness.StateRunning)
	queued := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "¿Dónde está el README?"})
	// The panel dies here: a new office on the same state.
	o2 := h.reopen(t)
	j, _ := o2.Job(strings.TrimPrefix(task, "web-"))
	if j.Status != StatusFailed || j.Message != "Interrumpido por un reinicio del panel" {
		t.Fatalf("job %+v", j)
	}
	p, _, _ := o2.Pipeline(pl.ID)
	if p.Status != PipelineFailed {
		t.Fatalf("pipeline %+v", p)
	}
	if q, _ := o2.Job(queued.ID); q.Status != StatusQueued {
		t.Fatalf("queued job %+v", q)
	}
}

func TestRetentionDeletesOldestFinished(t *testing.T) {
	h := newHarness(t, func(c *Config) { c.RetentionJobs = 3 })
	var ids []string
	for i := range 4 {
		j := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "pregunta " + string(rune('a'+i))})
		ids = append(ids, j.ID)
		h.clock.Add(time.Second)
		h.runNext(t, exited(0, "respuesta"))
	}
	if _, err := h.o.Job(ids[0]); IsStatus(err) != 404 {
		t.Fatalf("oldest kept: %v", err)
	}
	if _, err := os.Stat(h.o.store.jobDir(ids[0])); !os.IsNotExist(err) {
		t.Fatal("its directory remains")
	}
	for _, id := range ids[1:] {
		if _, err := h.o.Job(id); err != nil {
			t.Fatal(err)
		}
	}
	var jf jobsFile
	data, _ := os.ReadFile(filepath.Join(h.cfg.StateDir, jobsFileName))
	json.Unmarshal(data, &jf)
	if len(jf.Jobs) != 3 {
		t.Fatalf("stored %d", len(jf.Jobs))
	}
}

// A state file that cannot be read (as opposed to one that is not valid
// JSON) stops the start-up and is left exactly where it was.
func TestUnreadableStateRefusesToStart(t *testing.T) {
	for name, setup := range map[string]func(dir string){
		"not a file": func(dir string) { os.MkdirAll(filepath.Join(dir, officeFileName), 0o700) },
		"too big": func(dir string) {
			f, _ := os.Create(filepath.Join(dir, jobsFileName))
			f.Truncate(fileMaxBytes + 1)
			f.Close()
		},
		"backup unreadable": func(dir string) {
			os.WriteFile(filepath.Join(dir, officeFileName), []byte("{dañado"), 0o600)
			os.MkdirAll(filepath.Join(dir, officeFileName+".bak"), 0o700)
		},
	} {
		t.Run(name, func(t *testing.T) {
			cfg := testConfig(t)
			if _, err := openStore(cfg.StateDir, func() time.Time { return t0 }); err != nil {
				t.Fatal(err)
			}
			setup(cfg.StateDir)
			before, _ := os.ReadDir(cfg.StateDir)
			_, err := New(cfg, newFakeExec(), nil, func() time.Time { return t0 }, nil)
			if err == nil || !strings.Contains(err.Error(), "no se pudo leer") {
				t.Fatalf("err %v", err)
			}
			after, _ := os.ReadDir(cfg.StateDir)
			if len(after) != len(before) {
				t.Fatalf("files changed: %v -> %v", before, after)
			}
			if m, _ := filepath.Glob(filepath.Join(cfg.StateDir, "*.corrupt-*")); len(m) != 0 {
				t.Fatalf("set aside %v", m)
			}
		})
	}
}

func TestSaveRefusesFilesTooBigToLoad(t *testing.T) {
	s, _ := openStore(t.TempDir(), func() time.Time { return t0 })
	big := strings.Repeat("x", fileMaxBytes)
	if err := s.saveOffice(&officeFile{Projects: []Project{{Notes: big}}}); err != errTooBig {
		t.Fatalf("%v", err)
	}
	if _, err := os.Stat(s.path(officeFileName)); !os.IsNotExist(err) {
		t.Fatal("written anyway")
	}
}

// Beyond MaxJobsBytes the oldest finished jobs nothing needs are deleted,
// as beyond RetentionJobs.
func TestRetentionByBytes(t *testing.T) {
	h := newHarness(t)
	h.job(t, JobRequest{ProjectID: "web", AgentID: "linus", Kind: KindChange, Prompt: "cambia"})
	j1 := h.runNext(t, withChanges(exited(0, "hecho"), 1))
	var ids []string
	for i := range 2 {
		h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "pregunta " + itoa(int64(i))})
		ids = append(ids, h.runNext(t, exited(0, strings.Repeat("r", 2000))).ID)
	}
	j4 := h.job(t, JobRequest{ProjectID: "web", AgentID: "becario", Kind: KindAsk, Prompt: "la última"})
	// A follow-up of j1 waits: j1 is needed and must survive.
	if _, err := h.o.FollowUp(j1.ID, FollowUpRequest{AgentID: "grace", Kind: KindReview, Prompt: "Revisa"}, "x"); err != nil {
		t.Fatal(err)
	}
	h.o.mu.Lock()
	if want := h.o.store.jobDirSize(j1.ID); want == 0 || h.o.jobBytes[j1.ID] != want {
		t.Fatalf("j1 measured %d, is %d", h.o.jobBytes[j1.ID], want)
	}
	h.o.maxJobBytes = h.o.jobBytesTotal - 1
	limit := h.o.maxJobBytes
	h.o.mu.Unlock()
	if j := h.runNext(t, exited(0, "fin")); j.ID != j4.ID {
		t.Fatalf("ran %s", j.ID)
	}
	if _, err := h.o.Job(j1.ID); err != nil {
		t.Fatalf("a needed job was deleted: %v", err)
	}
	if _, err := h.o.Job(ids[0]); IsStatus(err) != 404 {
		t.Fatalf("the oldest unneeded job was kept: %v", err)
	}
	if _, err := h.o.Job(j4.ID); err != nil {
		t.Fatal(err)
	}
	h.o.mu.Lock()
	total := h.o.jobBytesTotal
	var sum int64
	for id := range h.o.jobs {
		sum += h.o.store.jobDirSize(id)
	}
	h.o.mu.Unlock()
	if total > limit || total != sum {
		t.Fatalf("total %d (measured %d), limit %d", total, sum, limit)
	}
}

// A project created by hand keeps its id when the configuration later
// brings one with the same id and another repository.
func TestReconcileLeavesHandMadeProjects(t *testing.T) {
	h := newHarness(t)
	if _, err := h.o.CreateProject(ProjectInput{ID: ptr("docs"), Name: ptr("Mis docs"), Repo: ptr("https://github.com/ana/docs")}, "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.o.CreateProject(ProjectInput{ID: ptr("api"), Name: ptr("API"), Repo: ptr("https://github.com/apptolast/api")}, "x"); err != nil {
		t.Fatal(err)
	}
	h.cfg.Projects = append(h.cfg.Projects,
		config.Project{ID: "docs", Name: "Docs", Repo: "https://github.com/apptolast/docs", Branch: "main"},
		config.Project{ID: "api", Name: "API de AppToLast", Repo: "https://github.com/apptolast/api", Branch: "main"})
	o2 := h.reopen(t)
	s := o2.Snapshot()
	var docs, api *Project
	for i := range s.Projects {
		switch s.Projects[i].ID {
		case "docs":
			docs = &s.Projects[i]
		case "api":
			api = &s.Projects[i]
		}
	}
	if docs == nil || docs.Seeded || docs.Repo != "https://github.com/ana/docs" || docs.Name != "Mis docs" {
		t.Fatalf("docs %+v", docs)
	}
	if !slices.ContainsFunc(s.Warnings, func(w string) bool { return strings.Contains(w, "proyecto docs") }) {
		t.Fatalf("warnings %v", s.Warnings)
	}
	// Same repository: adopted by the configuration.
	if api == nil || !api.Seeded || api.Name != "API de AppToLast" {
		t.Fatalf("api %+v", api)
	}
}
