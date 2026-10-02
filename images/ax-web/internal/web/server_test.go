package web

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/fakeax"
	"apptolast.com/ax-web/internal/harness"
	"apptolast.com/ax-web/internal/office"
	"apptolast.com/ax-web/internal/runs"
)

const (
	origin      = "https://ax.apptolast.com"
	extraOrigin = "https://oficina.apptolast.com"
)

var fakeCredential = strings.Repeat("c", 40)

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

// fakeExec runs nothing: the test drives each launched run's hooks.
type fakeExec struct {
	mu   sync.Mutex
	runs []*fakeRun
}

type fakeRun struct {
	id    string
	hooks harness.Hooks
	done  chan struct{}
	res   harness.Result
}

func (r *fakeRun) ID() string             { return r.id }
func (r *fakeRun) Done() <-chan struct{}  { return r.done }
func (r *fakeRun) Result() harness.Result { return r.res }

func (f *fakeExec) Launch(_ context.Context, spec harness.Spec, _ string, hooks harness.Hooks) (harness.Run, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range f.runs {
		select {
		case <-r.done:
		default:
			return nil, harness.ErrBusy
		}
	}
	r := &fakeRun{id: spec.ID, hooks: hooks, done: make(chan struct{})}
	f.runs = append(f.runs, r)
	return r, nil
}

func (f *fakeExec) ActiveTask() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range f.runs {
		select {
		case <-r.done:
		default:
			return r.id
		}
	}
	return ""
}

func (f *fakeExec) Cancel(id, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range f.runs {
		if r.id == id {
			select {
			case <-r.done:
			default:
				r.res = harness.Result{Outcome: harness.OutcomeCancelled}
				close(r.done)
			}
		}
	}
	return nil
}

func (f *fakeExec) Ready() bool { return true }

func (f *fakeExec) waitRun(t *testing.T, n int) *fakeRun {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		f.mu.Lock()
		if len(f.runs) >= n {
			r := f.runs[n-1]
			f.mu.Unlock()
			return r
		}
		f.mu.Unlock()
		if time.Now().After(deadline) {
			t.Fatal("no run launched")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// tasks is the run manager for the raw AX views, with a settable active
// task.
type tasks struct {
	*runs.Manager
	mu     sync.Mutex
	active string
}

func (t *tasks) ActiveTask() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.active
}

type fixture struct {
	ax     *fakeax.AX
	exec   *fakeExec
	tasks  *tasks
	office *office.Office
	app    *Server
	srv    *httptest.Server
	stop   chan struct{}
	audit  *lockedBuffer
	dir    string
}

var uiFS = fstest.MapFS{
	"index.html":      {Data: []byte(`<!doctype html><link rel="stylesheet" href="{{APP_CSS}}"><script src="{{APP_JS}}" defer></script>`)},
	"css/10-b.css":    {Data: []byte("b{}")},
	"css/00-a.css":    {Data: []byte("a{}")},
	"js/00-core.js":   {Data: []byte("window.Oficina={};")},
	"js/20-views.js":  {Data: []byte("(()=>{})();")},
	"js/notes.txt":    {Data: []byte("ignored")},
	"favicon.svg":     {Data: []byte(`<svg xmlns="http://www.w3.org/2000/svg"/>`)},
	"css/old/x.css":   {Data: []byte("nested{}")},
	"other/readme.md": {Data: []byte("x")},
}

func newFixture(t *testing.T, now time.Time) *fixture {
	t.Helper()
	f := &fixture{ax: fakeax.NewAX(), exec: &fakeExec{}, stop: make(chan struct{}), audit: &lockedBuffer{}, dir: t.TempDir()}
	audit := slog.New(slog.NewJSONHandler(f.audit, nil))
	axConn, stopAX := fakeax.Serve(func(s *grpc.Server) { v1alpha1.RegisterAXServer(s, f.ax) })
	ax := v1alpha1.NewAXClient(axConn)
	window, _ := config.ParseWindow("22:30-00:40")
	opt := runs.DefaultOptions()
	opt.Atespace = "default"
	opt.PollInterval, opt.CallTimeout = 10*time.Millisecond, 2*time.Second
	f.tasks = &tasks{Manager: runs.NewManager(opt, runs.Deps{AX: ax, Audit: audit})}
	tokenDir := filepath.Join(f.dir, "agent")
	os.MkdirAll(tokenDir, 0o700)
	os.WriteFile(filepath.Join(tokenDir, "claude-oauth-token"), []byte(fakeCredential), 0o600)
	clock := func() time.Time { return now }
	o, err := office.New(office.Config{
		StateDir: filepath.Join(f.dir, "state"), ClaudeTokenFile: filepath.Join(tokenDir, "claude-oauth-token"),
		OfficeSecretDir: filepath.Join(f.dir, "office"), CodexAuthKey: "codex-auth-json", GitHubTokenKey: "github-token",
		Projects: []config.Project{{ID: "web", Name: "Web", Repo: "https://github.com/apptolast/web", Branch: "main"}},
		Limits:   office.Limits{MaxTurns: 150, MaxTimeoutMinutes: 90, RepoHosts: []string{"github.com"}},
		MaxQueue: 20, RetentionJobs: 1000, Version: "1.0.0", GitHubAPI: "http://127.0.0.1:1",
	}, f.exec, nil, clock, audit)
	if err != nil {
		t.Fatal(err)
	}
	f.office = o
	ctx, cancel := context.WithCancel(context.Background())
	go o.Run(ctx)
	f.app = &Server{
		AX: ax, Tasks: f.tasks, Office: o, Atespace: "default", Origin: origin, ExtraOrigins: []string{extraOrigin},
		Blackout: window, WatchdogLead: 5 * time.Minute, Certs: &CertWatch{},
		Now: clock, Audit: audit, PingInterval: 50 * time.Millisecond, CallTimeout: 2 * time.Second,
		DeleteWait: 300 * time.Millisecond, DeleteTimeout: 600 * time.Millisecond, Stop: f.stop,
		Static: uiFS, Version: "1.0.0",
	}
	f.srv = httptest.NewUnstartedServer(f.app.Handler())
	// As in production: net/http clears the read deadline once the request
	// is read, so SSE streams outlive ReadTimeout. The test pins that.
	f.srv.Config.ReadTimeout = 300 * time.Millisecond
	f.srv.Start()
	t.Cleanup(func() {
		close(f.stop)
		f.srv.Close()
		cancel()
		f.exec.mu.Lock()
		for _, r := range f.exec.runs {
			select {
			case <-r.done:
			default:
				close(r.done)
			}
		}
		f.exec.mu.Unlock()
		cctx, ccancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer ccancel()
		o.Close(cctx)
		stopAX()
	})
	return f
}

var daytime = time.Date(2026, 9, 26, 10, 0, 0, 0, time.UTC)

// post sends a same-origin JSON POST unless edit changes it.
func (f *fixture) post(t *testing.T, path, body string, edit func(*http.Request)) (*http.Response, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, f.srv.URL+path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", origin)
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	req.Header.Set(CSRFHeader, "1")
	if edit != nil {
		edit(req)
	}
	return do(t, req)
}

func (f *fixture) get(t *testing.T, path string) (*http.Response, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, f.srv.URL+path, nil)
	return do(t, req)
}

func do(t *testing.T, req *http.Request) (*http.Response, map[string]any) {
	t.Helper()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body map[string]any
	data, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(data, &body)
	return resp, body
}

func raw(t *testing.T, url string) (*http.Response, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp, string(data)
}

const jobBody = `{"project_id":"web","agent_id":"becario","kind":"pregunta","prompt":"¿Dónde está el README?"}`

func TestCSRF(t *testing.T) {
	f := newFixture(t, daytime)
	f.office.PauseQueue(true, "x")
	cases := map[string]struct {
		edit func(*http.Request)
		code int
	}{
		"cross-site origin": {func(r *http.Request) { r.Header.Set("Origin", "https://evil.example") }, 403},
		"cross-site fetch":  {func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") }, 403},
		"same-site":         {func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "same-site") }, 403},
		"null origin":       {func(r *http.Request) { r.Header.Set("Origin", "null") }, 403},
		"origin with path":  {func(r *http.Request) { r.Header.Set("Origin", extraOrigin+"/x") }, 403},
		"no provenance": {func(r *http.Request) {
			r.Header.Del("Origin")
			r.Header.Del("Sec-Fetch-Site")
		}, 403},
		"no panel header": {func(r *http.Request) { r.Header.Del(CSRFHeader) }, 403},
		"form post": {func(r *http.Request) {
			r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}, 415},
		"text/plain": {func(r *http.Request) { r.Header.Set("Content-Type", "text/plain") }, 415},
	}
	for name, c := range cases {
		resp, _ := f.post(t, "/api/jobs", jobBody, c.edit)
		if resp.StatusCode != c.code {
			t.Errorf("%s: %d", name, resp.StatusCode)
		}
		if resp.Header.Get("Access-Control-Allow-Origin") != "" {
			t.Errorf("%s: CORS header sent", name)
		}
	}
	if q := f.office.Snapshot().Queue; len(q) != 0 {
		t.Fatal("a rejected request created a job")
	}
	// The office's own host name is an accepted origin.
	if resp, body := f.post(t, "/api/jobs", jobBody, func(r *http.Request) { r.Header.Set("Origin", extraOrigin) }); resp.StatusCode != 201 {
		t.Fatalf("extra origin: %d %v", resp.StatusCode, body)
	}
	// Only Sec-Fetch-Site, as a same-origin fetch from an older browser.
	if resp, _ := f.post(t, "/api/jobs", jobBody, func(r *http.Request) { r.Header.Del("Origin") }); resp.StatusCode != 201 {
		t.Fatalf("same-origin without Origin: %d", resp.StatusCode)
	}
	req, _ := http.NewRequest(http.MethodPut, f.srv.URL+"/api/jobs", strings.NewReader("{}"))
	if resp, _ := do(t, req); resp.StatusCode != 405 {
		t.Fatalf("PUT: %d", resp.StatusCode)
	}
	// A valid job padded with JSON whitespace past the cap: only the body
	// limit can refuse it.
	big := strings.TrimSuffix(jobBody, "}") + strings.Repeat(" ", MaxBodyBytes) + "}"
	if resp, body := f.post(t, "/api/jobs", big, nil); resp.StatusCode != 400 || body["error"] != "el cuerpo supera 128 KiB" {
		t.Fatalf("oversized body: %d %v", resp.StatusCode, body)
	}
	if resp, _ := f.post(t, "/api/jobs", `{"project_id":"web","extra":1}`, nil); resp.StatusCode != 400 {
		t.Fatalf("unknown field: %d", resp.StatusCode)
	}
	// The old run endpoints are gone.
	for _, p := range []string{"/api/runs", "/api/runs/x/cancel"} {
		if resp, _ := f.post(t, p, "{}", nil); resp.StatusCode != 404 {
			t.Errorf("%s: %d", p, resp.StatusCode)
		}
	}
	if resp, _ := f.get(t, "/api/runs/current"); resp.StatusCode != 404 {
		t.Errorf("runs/current: %d", resp.StatusCode)
	}
}

func TestSecurityHeadersAndAssets(t *testing.T) {
	f := newFixture(t, daytime)
	resp, index := raw(t, f.srv.URL+"/")
	if resp.Header.Get("Cache-Control") != "no-store" || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/html") {
		t.Fatalf("%v", resp.Header)
	}
	m := regexp.MustCompile(`href="/assets/(app\.[0-9a-f]{12}\.css)".*src="/assets/(app\.[0-9a-f]{12}\.js)"`).FindStringSubmatch(index)
	if m == nil || strings.Contains(index, "{{") {
		t.Fatal(index)
	}
	resp, css := raw(t, f.srv.URL+"/assets/"+m[1])
	if resp.StatusCode != 200 || css != "a{}\nb{}" || resp.Header.Get("Cache-Control") != AssetCache ||
		!strings.HasPrefix(resp.Header.Get("Content-Type"), "text/css") {
		t.Fatalf("%d %q %v", resp.StatusCode, css, resp.Header)
	}
	resp, js := raw(t, f.srv.URL+"/assets/"+m[2])
	if js != "window.Oficina={};\n(()=>{})();" || resp.Header.Get("Cache-Control") != AssetCache ||
		!strings.HasPrefix(resp.Header.Get("Content-Type"), "text/javascript") {
		t.Fatalf("%q %v", js, resp.Header)
	}
	if m[1] != hashName("app", "css", []byte("a{}\nb{}")) {
		t.Fatal("hash is not of the content")
	}
	for _, p := range []string{"/assets/app.000000000000.css", "/assets/" + m[1] + "x", "/app.js", "/assets/../index.html"} {
		if resp, _ := raw(t, f.srv.URL+p); resp.StatusCode != 404 {
			t.Errorf("%s: %d", p, resp.StatusCode)
		}
	}
	if resp, svg := raw(t, f.srv.URL+"/favicon.svg"); resp.StatusCode != 200 || resp.Header.Get("Content-Type") != "image/svg+xml" ||
		!strings.Contains(svg, "<svg") {
		t.Fatalf("%d %v", resp.StatusCode, resp.Header)
	}
	for _, p := range []string{"/", "/assets/" + m[2], "/api/office", "/api/tasks", "/nope"} {
		resp, _ := f.get(t, p)
		h := resp.Header
		if h.Get("Content-Security-Policy") != CSP || h.Get("X-Content-Type-Options") != "nosniff" ||
			h.Get("X-Frame-Options") != "DENY" || h.Get("Referrer-Policy") != "no-referrer" {
			t.Errorf("%s: %v", p, h)
		}
	}
	if resp, _ := f.get(t, "/healthz"); resp.StatusCode != 200 {
		t.Fatal(resp.StatusCode)
	}
}

func TestAssetsToleratePartialBundles(t *testing.T) {
	b := buildBundle(fstest.MapFS{"index.html": {Data: []byte("<p>{{APP_CSS}} {{APP_JS}}</p>")}})
	if b.err != nil || !bytes.Contains(b.index, []byte("/assets/app."+b.cssName[4:])) || len(b.css) != 0 || b.favicon != nil {
		t.Fatalf("%+v", b)
	}
	if b := buildBundle(fstest.MapFS{}); b.err == nil {
		t.Fatal("no index.html accepted")
	}
	srv := httptest.NewServer((&Server{Static: fstest.MapFS{}}).Handler())
	defer srv.Close()
	if resp, _ := raw(t, srv.URL+"/"); resp.StatusCode != 500 {
		t.Fatalf("missing UI: %d", resp.StatusCode)
	}
	// The embedded UI builds, whatever the UI agent has put in it so far.
	if b := buildBundle(StaticFS()); b.err != nil {
		t.Fatal(b.err)
	}
}

// The embedded UI has no inline code and no HTML sinks fed with data.
func TestStaticHasNoInlineCodeOrHTMLSinks(t *testing.T) {
	ui := StaticFS()
	inlineScript := regexp.MustCompile(`(?i)<script(?:\s[^>]*)?>\s*[^<\s]`)
	handler := regexp.MustCompile(`(?i)\son[a-z]+\s*=`)
	sinks := regexp.MustCompile(`\.(innerHTML|outerHTML)\s*\+?=\s*[^'"\x60\s]|insertAdjacentHTML|document\.write|\beval\(|new Function`)
	_ = fs.WalkDir(ui, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, _ := fs.ReadFile(ui, p)
		switch {
		case strings.HasSuffix(p, ".html"):
			if inlineScript.Match(data) || bytes.Contains(data, []byte("style=")) || handler.Match(data) ||
				bytes.Contains(data, []byte("<style")) {
				t.Errorf("%s has inline code", p)
			}
		case strings.HasSuffix(p, ".js"):
			if loc := sinks.FindIndex(data); loc != nil {
				t.Errorf("%s uses an HTML sink: %q", p, data[loc[0]:min(loc[1]+20, len(data))])
			}
		}
		return nil
	})
}

func TestOfficeSnapshotAndExport(t *testing.T) {
	f := newFixture(t, daytime)
	f.app.Certs.Server = daytime.Add(24 * time.Hour)
	resp, body := f.get(t, "/api/office")
	if resp.StatusCode != 200 || body["version"] != "1.0.0" || len(body["agents"].([]any)) != 9 ||
		len(body["templates"].([]any)) != 6 || body["catalog"] == nil || body["limits"] == nil {
		t.Fatalf("%d %v", resp.StatusCode, body["version"])
	}
	if w := body["warnings"].([]any); len(w) != 1 || !strings.Contains(w[0].(string), "certificado del panel") {
		t.Fatalf("warnings %v", w)
	}
	creds := body["credentials"].(map[string]any)
	if creds["claude"] != true || creds["codex"] != false || creds["github"] != false {
		t.Fatalf("%v", creds)
	}
	if strings.Contains(fmt.Sprint(body), fakeCredential) {
		t.Fatal("a credential reached the snapshot")
	}
	resp, data := raw(t, f.srv.URL+"/api/export")
	if resp.Header.Get("Content-Disposition") != `attachment; filename="oficina-2026-09-26.json"` ||
		!strings.Contains(data, `"agents"`) || strings.Contains(data, fakeCredential) {
		t.Fatalf("%v", resp.Header)
	}
}

func TestJobLifecycleEndpoints(t *testing.T) {
	f := newFixture(t, daytime)
	resp, body := f.post(t, "/api/jobs", `{"project_id":"web","agent_id":"ada","kind":"cambio","prompt":"x"}`, nil)
	if resp.StatusCode != 400 || body["field"] != "kind" || body["error"] != "Un trabajo de tipo cambio necesita modo completo" {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	resp, body = f.post(t, "/api/jobs", `{"project_id":"web","agent_id":"linus","kind":"cambio","prompt":"Arregla el README",
		"priority":2,"overrides":{"effort":"max"}}`, nil)
	if resp.StatusCode != 201 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	id := body["id"].(string)
	run := f.exec.waitRun(t, 1)
	run.hooks.OnState(harness.StateRunning)
	run.hooks.OnEvent(harness.Event{Kind: harness.EventText, Text: "<b>hola</b>"})
	_, body = f.get(t, "/api/jobs/"+id)
	if body["status"] != "en_curso" || !strings.Contains(body["final_prompt"].(string), "## Encargo\nArregla el README") ||
		body["can_pr"] != false || body["prompt"] != "Arregla el README" {
		t.Fatalf("%v", body)
	}
	_, body = f.get(t, "/api/jobs/"+id+"/events?after=0")
	if evs := body["events"].([]any); len(evs) != 2 || body["more"] != false {
		t.Fatalf("%v", body)
	}
	if resp, _ := f.get(t, "/api/jobs/"+id+"/events?after=-1"); resp.StatusCode != 400 {
		t.Fatal(resp.StatusCode)
	}
	if resp, _ := f.get(t, "/api/jobs/"+id+"/patch"); resp.StatusCode != 404 {
		t.Fatal("patch before the end")
	}
	zero := 0
	run.res = harness.Result{Outcome: harness.OutcomeExited, ExitCode: &zero, ResultText: "## Resumen\nHecho.",
		Changes: &harness.Changes{BaseSHA: strings.Repeat("a", 40), Patch: []byte("diff --git a/R b/R\n+<script>\n"),
			Files: []harness.ChangedFile{{Path: "R", Status: "M", Additions: 1}}}}
	close(run.done)
	waitStatus(t, f, id, "hecho")
	resp, patch := raw(t, f.srv.URL+"/api/jobs/"+id+"/patch")
	if resp.StatusCode != 200 || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/plain") ||
		resp.Header.Get("Content-Disposition") != "" || !strings.Contains(patch, "+<script>") {
		t.Fatalf("%d %v", resp.StatusCode, resp.Header)
	}
	resp, _ = raw(t, f.srv.URL+"/api/jobs/"+id+"/patch?download=1")
	if resp.Header.Get("Content-Disposition") != `attachment; filename="`+id+`.patch"` {
		t.Fatal(resp.Header)
	}
	for path, want := range map[string]int{
		"/api/jobs/" + id + "/rate":     200,
		"/api/jobs/" + id + "/priority": 409,
		"/api/jobs/" + id + "/cancel":   409,
		"/api/jobs/" + id + "/pr":       409,
		"/api/jobs/" + id + "/comment":  409,
	} {
		b := `{}`
		switch {
		case strings.HasSuffix(path, "rate"):
			b = `{"score":1,"note":"bien"}`
		case strings.HasSuffix(path, "priority"):
			b = `{"priority":0}`
		case strings.HasSuffix(path, "comment"):
			b = `{"number":3}`
		}
		if resp, body := f.post(t, path, b, nil); resp.StatusCode != want {
			t.Errorf("%s: %d %v", path, resp.StatusCode, body)
		}
	}
	resp, body = f.post(t, "/api/jobs/"+id+"/followup", `{"agent_id":"grace","kind":"revision","prompt":"Revisa"}`, nil)
	if resp.StatusCode != 201 || body["apply_from"] != id {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	follow := body["id"].(string)
	resp, body = f.post(t, "/api/jobs/"+id+"/retry", "", nil)
	if resp.StatusCode != 201 || body["kind"] != "cambio" {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	retry := body["id"].(string)
	if resp, _ := f.post(t, "/api/jobs/"+retry+"/cancel", "{}", nil); resp.StatusCode != 202 {
		t.Fatal(resp.StatusCode)
	}
	if resp, _ := f.post(t, "/api/jobs/"+id+"/delete", "{}", nil); resp.StatusCode != 409 {
		t.Fatal("deleted a job its follow-up needs")
	}
	f.post(t, "/api/jobs/"+follow+"/cancel", "{}", nil)
	waitStatus(t, f, follow, "cancelado")
	if resp, body := f.post(t, "/api/jobs/"+id+"/delete", "{}", nil); resp.StatusCode != 200 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	if resp, _ := f.get(t, "/api/jobs/"+id); resp.StatusCode != 404 {
		t.Fatal(resp.StatusCode)
	}
	if resp, _ := f.get(t, "/api/jobs/..%2Fx"); resp.StatusCode != 400 {
		t.Fatal(resp.StatusCode)
	}
	if a := f.audit.String(); strings.Contains(a, "Arregla el README") || !strings.Contains(a, `"prompt_sha256"`) {
		t.Fatalf("audit %s", a)
	}
}

func waitStatus(t *testing.T, f *fixture, id, status string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		j, err := f.office.Job(id)
		if err == nil && j.Status == status {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("job %s: %s", id, j.Status)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestCatalogueEndpoints(t *testing.T) {
	f := newFixture(t, daytime)
	f.office.PauseQueue(true, "x")
	steps := []struct {
		path, body string
		code       int
		check      func(map[string]any) bool
	}{
		{"/api/agents", `{"name":"Nueva","emoji":"🧪","color":"#123456","harness":"claude","model":"sonnet","mode":"lectura","max_turns":5,"timeout_minutes":10}`, 201,
			func(b map[string]any) bool { return b["id"] == "nueva" && b["version"] == float64(1) }},
		{"/api/agents/nueva", `{"system_prompt":"Eres Nueva.","note":"primera"}`, 200,
			func(b map[string]any) bool { return b["version"] == float64(2) }},
		{"/api/agents/nueva/rollback", `{"version":1}`, 200, func(b map[string]any) bool { return b["version"] == float64(3) }},
		{"/api/agents/nueva", `{"max_turns":999}`, 400, func(b map[string]any) bool { return b["field"] == "max_turns" }},
		{"/api/agents/nueva/coach", `{}`, 201, func(b map[string]any) bool { return b["agent_id"] == "coach" }},
		{"/api/agents/nadie/delete", `{}`, 404, nil},
		{"/api/agents/NADIE/delete", `{}`, 400, nil},
		{"/api/projects", `{"name":"Otro","repo":"https://github.com/apptolast/otro"}`, 201,
			func(b map[string]any) bool { return b["id"] == "otro" && b["branch"] == "main" }},
		{"/api/projects/otro", `{"notes":"Usa make"}`, 200, func(b map[string]any) bool { return b["notes"] == "Usa make" }},
		{"/api/projects/otro/memory", `{"action":"add","text":"Lección"}`, 200,
			func(b map[string]any) bool { return len(b["memory"].([]any)) == 1 }},
		{"/api/projects/web/delete", `{}`, 409, func(b map[string]any) bool {
			return b["error"] == "Los proyectos de la configuración solo se archivan"
		}},
		{"/api/projects/otro/delete", `{}`, 200, nil},
		{"/api/pipelines", `{"template":"equipo","project_id":"web","task":"Añade tests"}`, 201,
			func(b map[string]any) bool { return b["id"] == "p-1" && len(b["steps"].([]any)) == 1 }},
		{"/api/pipelines/p-1/cancel", `{}`, 202, nil},
		{"/api/schedules", `{"name":"Diario","days":[1,2,3],"time":"07:00","target":{"type":"job","project_id":"web","agent_id":"becario","kind":"pregunta","prompt":"Resume los cambios"}}`, 201,
			func(b map[string]any) bool { return b["id"] == "diario" && b["enabled"] == true }},
		{"/api/schedules", `{"id":"diario","name":"Diario","enabled":false,"days":[1],"time":"07:30","target":{"type":"job","project_id":"web","agent_id":"becario","kind":"pregunta","prompt":"Resume"}}`, 200,
			func(b map[string]any) bool { return b["enabled"] == false && b["time"] == "07:30" }},
		{"/api/schedules/diario/run", `{}`, 200, func(b map[string]any) bool { return b["last_ref"] != "" }},
		{"/api/schedules/diario/delete", `{}`, 200, nil},
		{"/api/proposals/pr-9/approve", `{"content":"x"}`, 404, nil},
		{"/api/proposals/9/reject", `{}`, 400, nil},
		{"/api/evals", `{"name":"README","project_id":"web","kind":"pregunta","prompt":"¿Qué hace?","criteria":"Cita ficheros"}`, 201,
			func(b map[string]any) bool { return b["id"] == "readme" }},
		{"/api/evals/run", `{"agent_id":"becario","eval_ids":["readme"]}`, 201,
			func(b map[string]any) bool { return len(b["pipelines"].([]any)) == 1 }},
		{"/api/evals/readme/delete", `{}`, 200, nil},
		{"/api/settings", `{"office_name":"Mi oficina","auto_lessons":"aprobar"}`, 200,
			func(b map[string]any) bool { return b["office_name"] == "Mi oficina" }},
		{"/api/settings", `{"auto_lessons":"siempre"}`, 400, func(b map[string]any) bool { return b["field"] == "auto_lessons" }},
		{"/api/queue/pause", `{}`, 400, nil},
		{"/api/queue/pause", `{"paused":false}`, 200, func(b map[string]any) bool { return b["queue_paused"] == false }},
	}
	for _, s := range steps {
		resp, body := f.post(t, s.path, s.body, nil)
		if resp.StatusCode != s.code || (s.check != nil && !s.check(body)) {
			t.Errorf("%s %s: %d %v", s.path, s.body, resp.StatusCode, body)
		}
	}
	resp, body := f.get(t, "/api/pipelines/p-1")
	if resp.StatusCode != 200 || body["pipeline"].(map[string]any)["status"] != "cancelado" || len(body["jobs"].([]any)) != 1 {
		t.Fatalf("%v", body)
	}
	resp, body = f.get(t, "/api/projects/web/github")
	if resp.StatusCode != 200 || body["enabled"] != false || len(body["issues"].([]any)) != 0 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
}

type sse struct {
	r *bufio.Reader
}

func (s sse) line(t *testing.T) string {
	t.Helper()
	l, err := s.r.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSuffix(l, "\n")
}

// until reads lines until one has prefix and returns it.
func (s sse) until(t *testing.T, prefix string) string {
	t.Helper()
	for {
		if l := s.line(t); strings.HasPrefix(l, prefix) {
			return l
		}
	}
}

func openStream(t *testing.T, f *fixture, query, last string) (sse, func()) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.srv.URL+"/api/stream"+query, nil)
	if last != "" {
		req.Header.Set("Last-Event-ID", last)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("%d %v", resp.StatusCode, resp.Header)
	}
	return sse{bufio.NewReader(resp.Body)}, func() { cancel(); resp.Body.Close() }
}

func TestStream(t *testing.T) {
	f := newFixture(t, daytime)
	begin := time.Now()
	s, closeStream := openStream(t, f, "", "")
	if l := s.line(t); l != "retry: 5000" || time.Since(begin) > time.Second {
		t.Fatalf("first line %q after %s", l, time.Since(begin))
	}
	s.until(t, "event: hello")
	if d := s.line(t); !strings.HasPrefix(d, `data: {"rev":`) {
		t.Fatal(d)
	}
	_, body := f.post(t, "/api/jobs", jobBody, nil)
	id := body["id"].(string)
	s.until(t, "event: office")
	if d := s.until(t, "data: "); !strings.Contains(d, id) || !strings.Contains(d, `"counts"`) {
		t.Fatal(d)
	}
	// Heartbeats keep flowing past the server's read timeout.
	for range 8 {
		s.until(t, ": ping")
	}
	if time.Since(begin) < 400*time.Millisecond {
		t.Fatal("the stream did not outlive the read timeout")
	}
	closeStream()

	run := f.exec.waitRun(t, 1)
	for i := range 3 {
		run.hooks.OnEvent(harness.Event{Kind: harness.EventText, Text: fmt.Sprintf("<b>línea %d</b>", i)})
	}
	js, closeJob := openStream(t, f, "?job="+id, "")
	var ids []string
	for len(ids) < 4 {
		ids = append(ids, strings.TrimPrefix(js.until(t, "id: "), "id: "))
		if ev := js.line(t); ev != "event: log" {
			t.Fatal(ev)
		}
		d := js.line(t)
		if strings.Contains(d, "<b>") && !strings.Contains(d, `<b>`) {
			t.Fatal(d)
		}
	}
	run.hooks.OnEvent(harness.Event{Kind: harness.EventTool, Tool: "Bash", Input: "ls"})
	if l := js.until(t, "id: "); l != "id: 5" {
		t.Fatal(l)
	}
	if d := js.until(t, "data: "); !strings.Contains(d, `"tool":"Bash"`) {
		t.Fatal(d)
	}
	closeJob()
	// A reconnect resumes after Last-Event-ID.
	rs, closeR := openStream(t, f, "?job="+id, "4")
	if l := rs.until(t, "id: "); l != "id: 5" {
		t.Fatal(l)
	}
	closeR()
	for q, code := range map[string]int{"?job=NOPE": 400, "?job=j000000-000000-0000": 404} {
		if resp, _ := f.get(t, "/api/stream"+q); resp.StatusCode != code {
			t.Errorf("%s: %d", q, resp.StatusCode)
		}
	}
	// Streams end at StreamMax. A second server gets its own copy of the
	// settings: the first one's stream handlers may still be reading them.
	short := *f.app
	short.StreamMax = 100 * time.Millisecond
	srv2 := httptest.NewServer(short.Handler())
	defer srv2.Close()
	ms, closeM := openStream(t, &fixture{srv: srv2}, "", "")
	defer closeM()
	for {
		if _, err := ms.r.ReadString('\n'); err != nil {
			break
		}
	}
}

func TestTaskViewsRedact(t *testing.T) {
	f := newFixture(t, daytime)
	f.ax.AddTask(&v1alpha1.Task{
		Metadata: &v1alpha1.ObjectMeta{Name: "tarea-1"},
		Spec: &v1alpha1.TaskSpec{
			Command: []string{"sh", "-c", "echo " + fakeCredential},
			Env: []*v1alpha1.EnvVar{{Name: "CLAUDE_CODE_OAUTH_TOKEN", Value: fakeCredential},
				{Name: "AX_TASK_YAML", Value: fakeCredential}},
		},
	})
	f.ax.AddWorkspace(&v1alpha1.Workspace{
		Metadata: &v1alpha1.ObjectMeta{Name: "ws-tarea-1"},
		Spec: &v1alpha1.WorkspaceSpec{Git: []*v1alpha1.GitRepo{
			{Repo: "https://user:" + fakeCredential + "@github.com/a/b?x=" + fakeCredential + "#" + fakeCredential},
		}},
	})
	for _, p := range []string{"/api/tasks", "/api/tasks/tarea-1", "/api/workspaces"} {
		resp, data := raw(t, f.srv.URL+p)
		if resp.StatusCode != 200 || strings.Contains(data, fakeCredential) {
			t.Fatalf("%s leaks: %s", p, data)
		}
		if p == "/api/tasks/tarea-1" && !strings.Contains(data, `"CLAUDE_CODE_OAUTH_TOKEN","value":"[oculto]"`) {
			t.Fatalf("no redacted env: %s", data)
		}
		if p == "/api/workspaces" && !strings.Contains(data, `"https://github.com/a/b"`) {
			t.Fatalf("repo: %s", data)
		}
	}
	if resp, _ := f.get(t, "/api/tasks/..%2Fx"); resp.StatusCode != 400 {
		t.Fatalf("bad name: %d", resp.StatusCode)
	}
	if resp, _ := f.get(t, "/api/tasks/nope"); resp.StatusCode != 404 {
		t.Fatalf("missing: %d", resp.StatusCode)
	}
}

func TestGatewaysReadOnly(t *testing.T) {
	f := newFixture(t, daytime)
	f.ax.AddGateway(&v1alpha1.Gateway{Metadata: &v1alpha1.ObjectMeta{Name: "gw"}, Spec: &v1alpha1.GatewaySpec{
		Listeners: []*v1alpha1.Listener{{Name: "http", Port: 80, Protocol: "HTTP"}},
		Egress:    &v1alpha1.EgressConfig{Allowlist: &v1alpha1.EgressAllowlist{Hosts: []*v1alpha1.HostRule{{Host: "api.github.com", Port: 443}}}},
	}})
	resp, body := f.get(t, "/api/gateways")
	gws := body["gateways"].([]any)
	if resp.StatusCode != 200 || len(gws) != 1 || gws[0].(map[string]any)["name"] != "gw" {
		t.Fatalf("%v", body)
	}
	if resp, _ := f.post(t, "/api/gateways", "{}", nil); resp.StatusCode != 405 {
		t.Fatalf("POST gateways: %d", resp.StatusCode)
	}
}

func TestDeleteNeedsExactName(t *testing.T) {
	f := newFixture(t, daytime)
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "tarea-2"}})
	f.ax.AddWorkspace(&v1alpha1.Workspace{Metadata: &v1alpha1.ObjectMeta{Name: "ws-tarea-2"}})
	if resp, _ := f.post(t, "/api/tasks/tarea-2/delete", `{"confirm":"tarea-"}`, nil); resp.StatusCode != 400 {
		t.Fatalf("wrong confirmation: %d", resp.StatusCode)
	}
	if resp, _ := f.post(t, "/api/tasks/tarea-2/delete", `{}`, nil); resp.StatusCode != 400 {
		t.Fatalf("no confirmation: %d", resp.StatusCode)
	}
	if hasTask, _ := f.ax.Has("tarea-2", ""); !hasTask {
		t.Fatal("deleted without confirmation")
	}
	resp, body := f.post(t, "/api/tasks/tarea-2/delete", `{"confirm":"tarea-2"}`, nil)
	if resp.StatusCode != 200 || body["deleted"] != true {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	if hasTask, hasWS := f.ax.Has("tarea-2", "ws-tarea-2"); hasTask || hasWS {
		t.Fatal("still there")
	}
	// A task stuck Terminating: the answer says so instead of waiting past
	// Traefik's response header timeout.
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "otra"}})
	f.ax.SetStuck(true)
	resp, body = f.post(t, "/api/tasks/otra/delete", `{"confirm":"otra"}`, nil)
	if resp.StatusCode != 202 || body["deleted"] != false {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	// The run in progress is cancelled from the office, not deleted here.
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "web-j1"}})
	f.tasks.mu.Lock()
	f.tasks.active = "web-j1"
	f.tasks.mu.Unlock()
	if resp, _ := f.post(t, "/api/tasks/web-j1/delete", `{"confirm":"web-j1"}`, nil); resp.StatusCode != 409 {
		t.Fatalf("delete during run: %d", resp.StatusCode)
	}
	if resp, _ := f.post(t, "/api/tasks/web-j1/suspend", "{}", nil); resp.StatusCode != 409 || len(f.ax.Suspended) != 0 {
		t.Fatalf("suspend during run: %d", resp.StatusCode)
	}
	if _, body := f.get(t, "/api/tasks/web-j1"); body["panel_run"] != true || body["can_suspend"] != false {
		t.Fatalf("%v", body)
	}
}

func TestSuspendRefusedWhenTheTaskHoldsACredential(t *testing.T) {
	f := newFixture(t, daytime)
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "tarea-3"}})
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "con-env"},
		Spec: &v1alpha1.TaskSpec{Env: []*v1alpha1.EnvVar{{Name: "X", Value: "y"}}}})
	for _, n := range []string{"tarea-3", "con-env"} {
		if resp, _ := f.post(t, "/api/tasks/"+n+"/suspend", "{}", nil); resp.StatusCode != 409 {
			t.Errorf("%s: %d", n, resp.StatusCode)
		}
		if _, body := f.get(t, "/api/tasks/"+n); body["can_suspend"] != false {
			t.Errorf("%s: %v", n, body)
		}
	}
	if len(f.ax.Suspended) != 0 {
		t.Fatal("suspended")
	}
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "otra"}})
	if resp, _ := f.post(t, "/api/tasks/otra/suspend", "", nil); resp.StatusCode != 200 {
		t.Fatalf("suspend: %d", resp.StatusCode)
	}
}

func TestResumeRefusedInBlackout(t *testing.T) {
	f := newFixture(t, time.Date(2026, 9, 26, 23, 0, 0, 0, time.UTC))
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "otra"}, Spec: &v1alpha1.TaskSpec{Suspend: true}})
	if resp, _ := f.post(t, "/api/tasks/otra/resume", "{}", nil); resp.StatusCode != 409 || len(f.ax.Resumed) != 0 {
		t.Fatalf("resume in blackout: %d", resp.StatusCode)
	}
	_, body := f.get(t, "/api/status")
	if body["in_blackout"] != true || body["version"] != "1.0.0" {
		t.Fatalf("%v", body)
	}
}

// The audit names the address Traefik appended: the last X-Forwarded-For
// entry, never one the client wrote before it.
func TestAuditUsesTheLastForwardedFor(t *testing.T) {
	f := newFixture(t, daytime)
	resp, body := f.post(t, "/api/jobs", jobBody, func(r *http.Request) {
		r.Header.Set("X-Forwarded-For", "198.51.100.9, 203.0.113.7")
	})
	if resp.StatusCode != 201 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	audit := f.audit.String()
	if !strings.Contains(audit, `"action":"job.create"`) || !strings.Contains(audit, `"client_ip":"203.0.113.7"`) ||
		strings.Contains(audit, "198.51.100.9") {
		t.Fatalf("audit %s", audit)
	}
}

// A value that cannot be JSON is a 500 with an error, never an empty 200.
func TestWriteJSONRefusesWhatItCannotEncode(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusOK, map[string]float64{"cost": math.NaN()})
	if rec.Code != 500 || !strings.Contains(rec.Body.String(), `"error":"no se pudo serializar la respuesta"`) {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder()
	writeJSON(rec, http.StatusCreated, map[string]int{"n": 1})
	if rec.Code != 201 || rec.Body.String() != "{\"n\":1}\n" || rec.Header().Get("Content-Type") != "application/json; charset=utf-8" {
		t.Fatalf("%d %q", rec.Code, rec.Body.String())
	}
}

func TestGitHubPreviewForgetAndReposEndpoints(t *testing.T) {
	f := newFixture(t, daytime)
	resp, body := f.post(t, "/api/jobs", `{"project_id":"web","agent_id":"becario","kind":"pregunta","prompt":"hola"}`, nil)
	if resp.StatusCode != 201 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	id := body["id"].(string)
	if resp, body := f.get(t, "/api/jobs/"+id+"/github-preview?target=comment"); resp.StatusCode != 409 || body["error"] == "" {
		t.Fatalf("unfinished: %d %v", resp.StatusCode, body)
	}
	run := f.exec.waitRun(t, 1)
	zero := 0
	run.res = harness.Result{Outcome: harness.OutcomeExited, ExitCode: &zero, ResultText: "## Resumen\nEstá en README.md; pregunta a @ana."}
	close(run.done)
	waitStatus(t, f, id, "hecho")
	resp, body = f.get(t, "/api/jobs/"+id+"/github-preview?target=comment")
	if resp.StatusCode != 200 || body["title"] != "" || !strings.Contains(body["body"].(string), "pregunta a `@ana`.") {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	if resp, body := f.get(t, "/api/jobs/"+id+"/github-preview?target=pr"); resp.StatusCode != 409 {
		t.Fatalf("pr of a question: %d %v", resp.StatusCode, body)
	}
	if resp, body := f.get(t, "/api/jobs/"+id+"/github-preview"); resp.StatusCode != 400 || body["field"] != "target" {
		t.Fatalf("no target: %d %v", resp.StatusCode, body)
	}
	if resp, _ := f.get(t, "/api/jobs/j000000-000000-0000/github-preview?target=pr"); resp.StatusCode != 404 {
		t.Fatal(resp.StatusCode)
	}
	// Edited bodies are bounded and name their field; unknown fields are refused.
	if resp, body := f.post(t, "/api/jobs/"+id+"/comment", `{"number":3,"body":"`+strings.Repeat("b", 60001)+`"}`, nil); resp.StatusCode != 400 || body["field"] != "body" {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	if resp, _ := f.post(t, "/api/jobs/"+id+"/pr", `{"title":"x","otra":1}`, nil); resp.StatusCode != 400 {
		t.Fatal(resp.StatusCode)
	}
	// Forgetting the stored Codex credential: CSRF rules apply.
	if resp, _ := f.post(t, "/api/credentials/codex/forget", `{}`, func(r *http.Request) { r.Header.Del(CSRFHeader) }); resp.StatusCode != 403 {
		t.Fatal(resp.StatusCode)
	}
	if resp, body := f.post(t, "/api/credentials/codex/forget", ``, nil); resp.StatusCode != 200 || body["ok"] != true {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	if !strings.Contains(f.audit.String(), `"action":"codex.auth.forget"`) {
		t.Fatal("not audited")
	}
	// Repositories of an owner: the owner is validated; GitHub is not
	// reachable in this fixture.
	if resp, body := f.get(t, "/api/github/repos?owner=a/b"); resp.StatusCode != 400 || body["field"] != "owner" {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	if resp, body := f.get(t, "/api/github/repos?owner=apptolast"); resp.StatusCode != 502 || body["error"] == "" {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
}
