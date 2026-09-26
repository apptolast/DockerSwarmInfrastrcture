package web

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"

	"apptolast.com/ax-web/internal/config"
	"apptolast.com/ax-web/internal/fakeax"
	"apptolast.com/ax-web/internal/guest"
	"apptolast.com/ax-web/internal/runs"
)

const origin = "https://ax.apptolast.com"

var fakeCredential = strings.Repeat("c", 40)

type fixture struct {
	ax    *fakeax.AX
	guest *fakeax.Guest
	m     *runs.Manager
	srv   *httptest.Server
	stop  chan struct{}
	audit *lockedBuffer
}

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

func newFixture(t *testing.T, now time.Time) *fixture {
	t.Helper()
	f := &fixture{ax: fakeax.NewAX(), guest: fakeax.NewGuest(), stop: make(chan struct{}), audit: &lockedBuffer{}}
	audit := slog.New(slog.NewJSONHandler(f.audit, nil))
	axConn, stopAX := fakeax.Serve(func(s *grpc.Server) { v1alpha1.RegisterAXServer(s, f.ax) })
	guestDial, stopGuest := fakeax.Listen(func(s *grpc.Server) { ateenvv1alpha.RegisterProcessServiceServer(s, f.guest) })
	window, _ := config.ParseWindow("22:30-00:40")
	opt := runs.DefaultOptions()
	opt.Atespace, opt.AgentImage, opt.Blackout = "default", "r/a@sha256:"+strings.Repeat("ab", 32), window
	opt.WatchdogLead = 5 * time.Minute
	opt.PollInterval, opt.ReadyTimeout = 10*time.Millisecond, 2*time.Second
	opt.CleanupTimeout, opt.CleanupRetry, opt.KillGrace = 500*time.Millisecond, 50*time.Millisecond, 100*time.Millisecond
	ax := v1alpha1.NewAXClient(axConn)
	clock := func() time.Time { return now }
	f.m = runs.NewManager(opt, runs.Deps{
		AX: ax,
		DialGuest: func(atespace, actor string) (runs.ProcessClient, error) {
			return guest.Dial(fakeax.Target, atespace, actor, guestDial)
		},
		ReadToken: func() (string, error) { return fakeCredential, nil },
		Now:       clock,
		Audit:     audit,
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	f.m.Reap(ctx)
	app := &Server{
		AX: ax, Runs: f.m, Atespace: "default", Origin: origin,
		Limits:   runs.Limits{RepoHosts: []string{"github.com"}, MaxTurns: 50, MaxTimeout: 45 * time.Minute},
		Blackout: window, WatchdogLead: 5 * time.Minute, Certs: &CertWatch{},
		Now: clock, Audit: audit, PingInterval: 50 * time.Millisecond,
		CallTimeout: 2 * time.Second, DeleteWait: 300 * time.Millisecond,
		DeleteTimeout: 600 * time.Millisecond, Stop: f.stop,
	}
	f.srv = httptest.NewUnstartedServer(app.Handler())
	// As in production: net/http clears the read deadline once the request
	// is read, so SSE streams outlive ReadTimeout. The test pins that.
	f.srv.Config.ReadTimeout = 300 * time.Millisecond
	f.srv.Start()
	t.Cleanup(func() {
		close(f.stop)
		f.srv.Close()
		sctx, scancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer scancel()
		f.m.Shutdown(sctx)
		stopAX()
		stopGuest()
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

const runBody = `{"repo":"https://github.com/a/b","branch":"main","prompt":"hola","agent":"claude","turns":3,"timeout_minutes":5,"cpu":"1","memory":"1Gi"}`

func TestCSRF(t *testing.T) {
	f := newFixture(t, daytime)
	cases := map[string]struct {
		edit func(*http.Request)
		code int
	}{
		"cross-site origin": {func(r *http.Request) { r.Header.Set("Origin", "https://evil.example") }, 403},
		"cross-site fetch":  {func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") }, 403},
		"same-site":         {func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "same-site") }, 403},
		"null origin":       {func(r *http.Request) { r.Header.Set("Origin", "null") }, 403},
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
		resp, _ := f.post(t, "/api/runs", runBody, c.edit)
		if resp.StatusCode != c.code {
			t.Errorf("%s: %d", name, resp.StatusCode)
		}
		if resp.Header.Get("Access-Control-Allow-Origin") != "" {
			t.Errorf("%s: CORS header sent", name)
		}
	}
	if f.m.ActiveTask() != "" {
		t.Fatal("a rejected request started a run")
	}
	// Only Sec-Fetch-Site, as a same-origin fetch from an older browser.
	resp, _ := f.post(t, "/api/tasks/x/suspend", "{}", func(r *http.Request) { r.Header.Del("Origin") })
	if resp.StatusCode != 404 {
		t.Fatalf("same-origin without Origin: %d", resp.StatusCode)
	}
	req, _ := http.NewRequest(http.MethodPut, f.srv.URL+"/api/runs", strings.NewReader("{}"))
	if resp, _ := do(t, req); resp.StatusCode != 405 {
		t.Fatalf("PUT: %d", resp.StatusCode)
	}
	// A valid run padded with JSON whitespace past the cap: only the body
	// limit can refuse it.
	big := strings.TrimSuffix(runBody, "}") + strings.Repeat(" ", MaxBodyBytes) + "}"
	if resp, body := f.post(t, "/api/runs", big, nil); resp.StatusCode != 400 ||
		body["error"] != "el cuerpo supera 32 KiB" {
		t.Fatalf("oversized body: %d %v", resp.StatusCode, body)
	}
	if resp, _ := f.post(t, "/api/runs", `{"repo":"x","extra":1}`, nil); resp.StatusCode != 400 {
		t.Fatalf("unknown field: %d", resp.StatusCode)
	}
}

func TestSecurityHeaders(t *testing.T) {
	f := newFixture(t, daytime)
	for _, p := range []string{"/", "/app.js", "/app.css", "/api/tasks", "/nope"} {
		resp, _ := f.get(t, p)
		h := resp.Header
		if h.Get("Content-Security-Policy") != CSP || h.Get("X-Content-Type-Options") != "nosniff" ||
			h.Get("Cache-Control") != "no-store" || h.Get("X-Frame-Options") != "DENY" {
			t.Errorf("%s: %v", p, h)
		}
	}
	resp, _ := f.get(t, "/app.js")
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/javascript") {
		t.Fatal(resp.Header.Get("Content-Type"))
	}
	if resp, _ := f.get(t, "/healthz"); resp.StatusCode != 200 {
		t.Fatal(resp.StatusCode)
	}
}

func TestStaticHasNoInlineCodeOrHTMLSinks(t *testing.T) {
	html, _ := static.ReadFile("static/index.html")
	js, _ := static.ReadFile("static/app.js")
	if strings.Contains(string(html), "<script>") || strings.Contains(string(html), "style=") ||
		strings.Contains(string(html), " on") {
		t.Fatal("inline code in index.html")
	}
	for _, sink := range []string{"innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"} {
		if strings.Contains(string(js), sink) {
			t.Fatalf("app.js uses %s", sink)
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
		resp, err := http.Get(f.srv.URL + p)
		if err != nil {
			t.Fatal(err)
		}
		data, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 || strings.Contains(string(data), fakeCredential) {
			t.Fatalf("%s leaks: %s", p, data)
		}
		if p == "/api/tasks/tarea-1" && !strings.Contains(string(data), `"CLAUDE_CODE_OAUTH_TOKEN","value":"[oculto]"`) {
			t.Fatalf("no redacted env: %s", data)
		}
		if p == "/api/workspaces" && !strings.Contains(string(data), `"https://github.com/a/b"`) {
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
}

func startRun(t *testing.T, f *fixture) string {
	t.Helper()
	resp, body := f.post(t, "/api/runs", runBody, nil)
	if resp.StatusCode != 202 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	return body["id"].(string)
}

func waitRunning(t *testing.T, f *fixture, id string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for f.m.Get(id).View().State != runs.StateRunning {
		if time.Now().After(deadline) {
			t.Fatal("not running")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestSuspendDisabledDuringPanelRun(t *testing.T) {
	f := newFixture(t, daytime)
	id := startRun(t, f)
	waitRunning(t, f, id)
	resp, _ := f.post(t, "/api/tasks/"+id+"/suspend", "{}", nil)
	if resp.StatusCode != 409 || len(f.ax.Suspended) != 0 {
		t.Fatalf("suspend during run: %d", resp.StatusCode)
	}
	_, body := f.get(t, "/api/tasks/"+id)
	if body["panel_run"] != true || body["can_suspend"] != false {
		t.Fatalf("%v", body)
	}
	if resp, _ := f.post(t, "/api/tasks/"+id+"/delete", `{"confirm":"`+id+`"}`, nil); resp.StatusCode != 409 {
		t.Fatalf("delete during run: %d", resp.StatusCode)
	}
	if resp, _ := f.post(t, "/api/runs", runBody, nil); resp.StatusCode != 409 {
		t.Fatalf("second run: %d", resp.StatusCode)
	}
	if resp, _ := f.post(t, "/api/runs/"+id+"/cancel", "{}", nil); resp.StatusCode != 202 {
		t.Fatalf("cancel: %d", resp.StatusCode)
	}
	<-f.m.Get(id).Done()
	// Another task can be suspended.
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "otra"}})
	if resp, _ := f.post(t, "/api/tasks/otra/suspend", "{}", nil); resp.StatusCode != 200 {
		t.Fatalf("suspend: %d", resp.StatusCode)
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
}

func TestResumeRefusedInBlackout(t *testing.T) {
	f := newFixture(t, time.Date(2026, 9, 26, 23, 0, 0, 0, time.UTC))
	f.ax.AddTask(&v1alpha1.Task{Metadata: &v1alpha1.ObjectMeta{Name: "otra"}, Spec: &v1alpha1.TaskSpec{Suspend: true}})
	if resp, _ := f.post(t, "/api/tasks/otra/resume", "{}", nil); resp.StatusCode != 409 || len(f.ax.Resumed) != 0 {
		t.Fatalf("resume in blackout: %d", resp.StatusCode)
	}
	if resp, body := f.post(t, "/api/runs", runBody, nil); resp.StatusCode != 409 {
		t.Fatalf("run in blackout: %d %v", resp.StatusCode, body)
	}
	_, body := f.get(t, "/api/status")
	if body["in_blackout"] != true {
		t.Fatalf("%v", body)
	}
}

func TestRunValidationErrorsNameTheField(t *testing.T) {
	f := newFixture(t, daytime)
	resp, body := f.post(t, "/api/runs", `{"repo":"https://x@github.com/a/b","prompt":"x"}`, nil)
	if resp.StatusCode != 400 || body["field"] != "repo" {
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

func openEvents(t *testing.T, f *fixture, id, last string) (sse, func()) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.srv.URL+"/api/runs/"+id+"/events", nil)
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

func TestEventsStreamReplayAndHeartbeat(t *testing.T) {
	f := newFixture(t, daytime)
	f.guest.Output = []*ateenvv1alpha.ProcessOutput{
		{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte("uno\n")}},
		{Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: []byte("<b>dos</b>\n")}},
	}
	id := startRun(t, f)
	begin := time.Now()
	s, closeStream := openEvents(t, f, id, "")
	if l := s.line(t); l != "retry: 5000" || time.Since(begin) > time.Second {
		t.Fatalf("first line %q after %s", l, time.Since(begin))
	}
	if l := s.line(t); l != ": ok" {
		t.Fatal(l)
	}
	waitRunning(t, f, id)
	// The agent's output arrives as JSON text, never markup.
	var ids []string
	for len(ids) < 10 {
		l := s.until(t, "id: ")
		data := s.until(t, "data: ")
		ids = append(ids, strings.TrimPrefix(l, "id: "))
		if strings.Contains(data, "dos") {
			// Escaped for good measure; the page renders it as text anyway.
			if !strings.Contains(data, `"s":"err"`) || strings.Contains(data, "<") {
				t.Fatal(data)
			}
			break
		}
	}
	// Heartbeats keep flowing past the server's read timeout.
	for i := 0; i < 10; i++ {
		if s.until(t, ": ping") != ": ping" {
			t.Fatal("no heartbeat")
		}
	}
	if time.Since(begin) < 400*time.Millisecond {
		t.Fatal("the stream did not outlive the read timeout")
	}
	closeStream()

	// Resume after the chunk before the last: only the last comes back.
	s2, close2 := openEvents(t, f, id, ids[len(ids)-2])
	defer close2()
	s2.until(t, "id: "+ids[len(ids)-1])
	if d := s2.until(t, "data: "); !strings.Contains(d, "dos") || strings.Contains(d, "uno") {
		t.Fatal(d)
	}
	if resp, _ := f.post(t, "/api/runs/"+id+"/cancel", "{}", nil); resp.StatusCode != 202 {
		t.Fatal(resp.StatusCode)
	}
	s2.until(t, "event: state")
	if d := s2.until(t, "data: "); !strings.Contains(d, `"state":`) {
		t.Fatal(d)
	}
	s2.until(t, "event: end")
	if d := s2.until(t, "data: "); !strings.Contains(d, `"state":"finished"`) {
		t.Fatal(d)
	}
	// Reconnecting after the end gets 204, so EventSource stops.
	end := f.m.Get(id).Output.End()
	req, _ := http.NewRequest(http.MethodGet, f.srv.URL+"/api/runs/"+id+"/events", nil)
	req.Header.Set("Last-Event-ID", strconv.FormatInt(end, 10))
	if resp, _ := do(t, req); resp.StatusCode != 204 {
		t.Fatalf("finished run: %d", resp.StatusCode)
	}
	if resp, _ := f.get(t, "/api/runs/web-nope/events"); resp.StatusCode != 404 {
		t.Fatal(resp.StatusCode)
	}
}

// The audit names the address Traefik appended: the last X-Forwarded-For
// entry, never one the client wrote before it.
func TestAuditUsesTheLastForwardedFor(t *testing.T) {
	f := newFixture(t, daytime)
	resp, body := f.post(t, "/api/runs", runBody, func(r *http.Request) {
		r.Header.Set("X-Forwarded-For", "198.51.100.9, 203.0.113.7")
	})
	if resp.StatusCode != 202 {
		t.Fatalf("%d %v", resp.StatusCode, body)
	}
	audit := f.audit.String()
	if !strings.Contains(audit, `"action":"run.start"`) || !strings.Contains(audit, `"client_ip":"203.0.113.7"`) ||
		strings.Contains(audit, "198.51.100.9") {
		t.Fatalf("audit %s", audit)
	}
}
