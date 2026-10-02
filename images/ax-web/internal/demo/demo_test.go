package demo

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"apptolast.com/ax-web/internal/harness"
)

func TestCheckLoopback(t *testing.T) {
	for addr, ok := range map[string]bool{
		"127.0.0.1:8090": true, "127.1.2.3:1": true, "[::1]:8090": true, "localhost:8090": true,
		":8090": false, "0.0.0.0:8090": false, "[::]:8090": false, "192.168.1.10:8090": false,
		"example.com:80": false, "127.0.0.1": false, "": false,
	} {
		if err := CheckLoopback(addr); (err == nil) != ok {
			t.Errorf("%q: %v", addr, err)
		}
	}
	if err := Run(context.Background(), Options{Listen: "0.0.0.0:0", StateDir: t.TempDir(), Speed: 1}); err == nil ||
		!strings.Contains(err.Error(), "loopback") {
		t.Fatalf("non-loopback accepted: %v", err)
	}
	if err := Run(context.Background(), Options{Listen: "127.0.0.1:0", StateDir: t.TempDir(), Speed: 0}); err == nil {
		t.Fatal("speed 0 accepted")
	}
}

type client struct {
	t    *testing.T
	base string
}

func (c client) get(path string, v any) int {
	c.t.Helper()
	resp, err := http.Get(c.base + path)
	if err != nil {
		c.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if v != nil {
		_ = json.Unmarshal(data, v)
	}
	return resp.StatusCode
}

func (c client) post(path, body string, v any) int {
	c.t.Helper()
	req, _ := http.NewRequest(http.MethodPost, c.base+path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", c.base)
	req.Header.Set("X-AX-Web", "1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if v != nil {
		_ = json.Unmarshal(data, v)
	}
	return resp.StatusCode
}

func (c client) waitJob(id string) map[string]any {
	c.t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		var j map[string]any
		c.get("/api/jobs/"+id, &j)
		switch j["status"] {
		case "hecho", "fallido", "cancelado":
			return j
		}
		if time.Now().After(deadline) {
			c.t.Fatalf("job %s: %v", id, j["status"])
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestDemoServesTheOffice(t *testing.T) {
	addr := make(chan string, 1)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	state := t.TempDir()
	go func() {
		done <- Run(ctx, Options{Listen: "127.0.0.1:0", StateDir: state, Speed: 200, Version: "1.0.0",
			Listening: func(a string) { addr <- a }})
	}()
	var base string
	select {
	case a := <-addr:
		base = "http://" + a
	case err := <-done:
		t.Fatal(err)
	case <-time.After(10 * time.Second):
		t.Fatal("demo did not start")
	}
	c := client{t: t, base: base}
	var snap struct {
		Version     string           `json:"version"`
		Agents      []map[string]any `json:"agents"`
		Projects    []map[string]any `json:"projects"`
		Credentials map[string]any   `json:"credentials"`
		AX          map[string]any   `json:"ax"`
	}
	if code := c.get("/api/office", &snap); code != 200 || snap.Version != "1.0.0" || len(snap.Agents) != 9 ||
		len(snap.Projects) != len(Projects) || snap.AX["ready"] != true {
		t.Fatalf("%d %+v", code, snap)
	}
	if snap.Credentials["claude"] != true || snap.Credentials["codex"] != true || snap.Credentials["github"] != false {
		t.Fatalf("%v", snap.Credentials)
	}
	if code := c.get("/", nil); code != 200 {
		t.Fatalf("index: %d", code)
	}
	var tasks map[string]any
	if code := c.get("/api/tasks", &tasks); code != 200 || !strings.Contains(mustJSON(tasks), "ejemplo-manual") {
		t.Fatalf("%d %v", code, tasks)
	}
	// A change job: a patch and the file's contents.
	var job map[string]any
	if code := c.post("/api/jobs", `{"project_id":"dockerswarm-infra","agent_id":"linus","kind":"cambio","prompt":"Mejora el README"}`, &job); code != 201 {
		t.Fatalf("%d %v", code, job)
	}
	j := c.waitJob(job["id"].(string))
	ch, _ := j["changes"].(map[string]any)
	if j["status"] != "hecho" || ch == nil || ch["files"] != float64(1) || ch["contents_complete"] != true || j["summary"] == "" {
		t.Fatalf("%v", j)
	}
	resp, err := http.Get(base + "/api/jobs/" + job["id"].(string) + "/patch")
	if err != nil || resp.StatusCode != 200 {
		t.Fatal(err)
	}
	resp.Body.Close()
	var events map[string]any
	c.get("/api/jobs/"+job["id"].(string)+"/events", &events)
	if evs, _ := events["events"].([]any); len(evs) < 8 {
		t.Fatalf("%d events", len(evs))
	}
	// A Codex job runs with the fake Codex credential.
	if code := c.post("/api/jobs", `{"project_id":"ax","agent_id":"guido","kind":"pregunta","prompt":"¿Qué hace el reaper?"}`, &job); code != 201 {
		t.Fatal(code)
	}
	if j := c.waitJob(job["id"].(string)); j["status"] != "hecho" || j["agent"].(map[string]any)["harness"] != harness.Codex {
		t.Fatalf("%v", j)
	}
	// A team runs every step to an outcome.
	var pl map[string]any
	if code := c.post("/api/pipelines", `{"template":"equipo","project_id":"ax","task":"Documenta el reaper"}`, &pl); code != 201 {
		t.Fatalf("%d %v", code, pl)
	}
	deadline := time.Now().Add(60 * time.Second)
	for {
		var got map[string]any
		c.get("/api/pipelines/"+pl["id"].(string), &got)
		p := got["pipeline"].(map[string]any)
		if p["status"] != "en_curso" {
			if p["status"] != "hecho" || p["outcome"] == "" || len(p["steps"].([]any)) < 3 {
				t.Fatalf("%v", p)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("pipeline stuck: %v", p)
		}
		time.Sleep(50 * time.Millisecond)
	}
	// What a PR of the change job would publish, and the fake GitHub's
	// repositories to import.
	var pv map[string]any
	if code := c.get("/api/jobs/"+j["id"].(string)+"/github-preview?target=pr", &pv); code != 200 || pv["title"] == "" ||
		!strings.Contains(pv["body"].(string), "Generado por la Oficina de agentes") {
		t.Fatalf("%d %v", code, pv)
	}
	var repos struct {
		Owner         string           `json:"owner"`
		Authenticated bool             `json:"authenticated"`
		Repos         []map[string]any `json:"repos"`
	}
	if code := c.get("/api/github/repos?owner=apptolast", &repos); code != 200 || repos.Owner != "apptolast" || repos.Authenticated ||
		len(repos.Repos) < 5 || repos.Repos[0]["html_url"] != "https://github.com/apptolast/DockerSwarmInfrastrcture" {
		t.Fatalf("%d %+v", code, repos)
	}
	var imported map[string]any
	r := repos.Repos[2]
	if code := c.post("/api/projects", `{"name":"`+r["name"].(string)+`","repo":"`+r["html_url"].(string)+`","branch":"","description":"`+
		r["description"].(string)+`","service":"","url":"","notes":""}`, &imported); code != 201 || imported["branch"] != "main" {
		t.Fatalf("%d %v", code, imported)
	}
	// An issue job without a GitHub token still runs, with a note.
	if code := c.post("/api/jobs", `{"project_id":"ax","agent_id":"becario","kind":"pregunta","prompt":"Resume la issue #3","title":"Issue #3","source":{"type":"issue","number":3}}`, &job); code != 201 {
		t.Fatalf("%d %v", code, job)
	}
	var detail map[string]any
	c.waitJob(job["id"].(string))
	if c.get("/api/jobs/"+job["id"].(string), &detail); !strings.Contains(detail["final_prompt"].(string), "no incluye la issue #3") {
		t.Fatalf("%v", detail["final_prompt"])
	}
	var ok map[string]any
	if code := c.post("/api/credentials/codex/forget", `{}`, &ok); code != 200 || ok["ok"] != true {
		t.Fatalf("%d %v", code, ok)
	}
	// The coach answers with a proposal.
	if code := c.post("/api/agents/kent/coach", `{}`, &job); code != 201 {
		t.Fatal(code)
	}
	c.waitJob(job["id"].(string))
	var after struct {
		Proposals []map[string]any `json:"proposals"`
	}
	c.get("/api/office", &after)
	found := false
	for _, p := range after.Proposals {
		if p["type"] == "prompt" && p["target_id"] == "kent" {
			found = true
		}
	}
	if !found {
		t.Fatalf("%v", after.Proposals)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("demo did not stop")
	}
}

func mustJSON(v any) string {
	data, _ := json.Marshal(v)
	return string(data)
}

func TestExecutorCancelAndBusy(t *testing.T) {
	e := NewExecutor(0.5, NewAX(time.Now()), time.Now)
	spec := harness.Spec{ID: "web-j1", Repo: "https://github.com/a/b", Branch: "main", Prompt: "hola",
		Harness: harness.Claude, Mode: harness.ModeRead, MaxTurns: 5, Timeout: 10 * time.Minute}
	states := make(chan string, 16)
	r, err := e.Launch(context.Background(), spec, "x", harness.Hooks{OnState: func(s string) { states <- s }})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.Launch(context.Background(), spec, "x", harness.Hooks{}); err == nil {
		t.Fatal("second launch accepted")
	}
	if e.ActiveTask() != "web-j1" || e.ax.ActiveTask() != "web-j1" {
		t.Fatal("not active")
	}
	if <-states != harness.StatePreparing {
		t.Fatal("no preparing state")
	}
	e.Cancel("web-j1", "x")
	select {
	case <-r.Done():
	case <-time.After(10 * time.Second):
		t.Fatal("cancel did not end the run")
	}
	if r.Result().Outcome != harness.OutcomeCancelled || e.ActiveTask() != "" {
		t.Fatalf("%+v", r.Result())
	}
	bad := spec
	bad.ID = "x"
	if _, err := e.Launch(context.Background(), bad, "x", harness.Hooks{}); err == nil {
		t.Fatal("invalid spec accepted")
	}
	e.Shutdown(context.Background())
	if e.Ready() {
		t.Fatal("ready after shutdown")
	}
}
