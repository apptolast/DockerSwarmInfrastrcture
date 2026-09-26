package web

import (
	"io"
	"net/http"
	"time"
)

// HealthHandler serves the kubelet probes on a plain port that the Service
// does not expose. Readiness deliberately ignores ax-server: if it followed
// ax-server, the panel's endpoint would vanish (externalTrafficPolicy:
// Local drops the traffic) exactly when the owner needs to see and cancel
// what is running.
func HealthHandler() http.Handler {
	mux := http.NewServeMux()
	ok := func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok\n") }
	mux.HandleFunc("GET /healthz", ok)
	mux.HandleFunc("GET /readyz", ok)
	return mux
}

// HealthServer serves HealthHandler on addr. Any pod can reach this plain
// port, so every connection is bounded: net/http clears the read deadline
// between requests when IdleTimeout and ReadTimeout are both zero, and an
// idle keep-alive would then hold its goroutine and buffers for ever.
func HealthServer(addr string) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           HealthHandler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    4 << 10,
	}
}
