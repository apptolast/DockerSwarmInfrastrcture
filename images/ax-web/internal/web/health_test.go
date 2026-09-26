package web

import (
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHealth(t *testing.T) {
	h := httptest.NewServer(HealthHandler())
	defer h.Close()
	code := func(p string) int {
		resp, err := http.Get(h.URL + p)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code("/healthz") != 200 || code("/readyz") != 200 {
		t.Fatal("not healthy")
	}
	if code("/api/tasks") != 404 || code("/") != 404 {
		t.Fatal("the probe port serves the application")
	}
}

// The probe port is reachable from any pod: an idle keep-alive or a slow
// or oversized request must not hold a goroutine and its buffers for ever.
func TestHealthServerBoundsConnections(t *testing.T) {
	srv := HealthServer("127.0.0.1:0")
	if srv.ReadHeaderTimeout <= 0 || srv.ReadTimeout <= 0 || srv.WriteTimeout <= 0 ||
		srv.IdleTimeout <= 0 || srv.IdleTimeout > 30*time.Second || srv.MaxHeaderBytes != 4<<10 {
		t.Fatalf("unbounded probe server: %+v", srv)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = srv.Serve(ln) }()
	defer srv.Close()
	resp, err := http.Get("http://" + ln.Addr().String() + "/healthz")
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("healthz: %v", err)
	}
	resp.Body.Close()
	req, _ := http.NewRequest(http.MethodGet, "http://"+ln.Addr().String()+"/healthz", nil)
	req.Header.Set("X-Pad", strings.Repeat("a", 16<<10))
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusRequestHeaderFieldsTooLarge {
		t.Fatalf("oversized headers: %d", resp.StatusCode)
	}
}
