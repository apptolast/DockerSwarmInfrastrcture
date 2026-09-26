package forward

import (
	"context"
	"io"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// echoUntilEOF reads everything, then answers with it after the client's
// half-close.
func echoUntilEOF(t *testing.T) net.Listener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("eco:"), data...))
			}()
		}
	}()
	t.Cleanup(func() { ln.Close() })
	return ln
}

func start(t *testing.T, f *Forwarder) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = f.Serve(ctx, ln); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	return ln.Addr().String()
}

func TestHalfCloseAndReResolve(t *testing.T) {
	backend := echoUntilEOF(t)
	var dials atomic.Int32
	var mu sync.Mutex
	var asked []string
	f := &Forwarder{
		Target: "kind-control-plane:30843", MaxConns: 4, IdleTimeout: time.Minute, DialTimeout: time.Second,
		// Every connection resolves the name again.
		Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
			if network != "tcp4" {
				return nil, net.UnknownNetworkError(network)
			}
			dials.Add(1)
			mu.Lock()
			asked = append(asked, address)
			mu.Unlock()
			var d net.Dialer
			return d.DialContext(ctx, network, backend.Addr().String())
		},
	}
	addr := start(t, f)
	for i := 0; i < 2; i++ {
		c, err := net.Dial("tcp", addr)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := c.Write([]byte("hola")); err != nil {
			t.Fatal(err)
		}
		// The backend answers only after it sees EOF: the half-close must
		// travel through while the reply direction stays open.
		if err := c.(*net.TCPConn).CloseWrite(); err != nil {
			t.Fatal(err)
		}
		_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
		got, err := io.ReadAll(c)
		c.Close()
		if err != nil || string(got) != "eco:hola" {
			t.Fatalf("%q %v", got, err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if dials.Load() != 2 || asked[0] != "kind-control-plane:30843" || asked[1] != asked[0] {
		t.Fatalf("dials %d %v", dials.Load(), asked)
	}
}

func TestConnectionCap(t *testing.T) {
	hold := make(chan struct{})
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { <-hold; c.Close() }()
		}
	}()
	defer close(hold)
	f := &Forwarder{Target: ln.Addr().String(), MaxConns: 1, IdleTimeout: time.Minute, DialTimeout: time.Second}
	addr := start(t, f)
	first, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	time.Sleep(100 * time.Millisecond)
	second, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	_ = second.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := second.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("the connection over the cap was not closed: %v", err)
	}
}

func TestIdleTimeout(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go func() {
		c, err := ln.Accept()
		if err == nil {
			defer c.Close()
			_, _ = io.Copy(io.Discard, c)
		}
	}()
	f := &Forwarder{Target: ln.Addr().String(), MaxConns: 2, IdleTimeout: 200 * time.Millisecond, DialTimeout: time.Second}
	addr := start(t, f)
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	begin := time.Now()
	if _, err := c.Read(make([]byte, 1)); err != io.EOF || time.Since(begin) > 2*time.Second {
		t.Fatalf("idle connection kept: %v after %s", err, time.Since(begin))
	}
}

func TestUnreachableTarget(t *testing.T) {
	f := &Forwarder{Target: "127.0.0.1:1", MaxConns: 2, IdleTimeout: time.Minute, DialTimeout: time.Second}
	addr := start(t, f)
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := c.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("%v", err)
	}
}

// A peer that stays silent after the panel closed its side must not keep
// its slot: once CloseGrace passes the slot serves another connection.
func TestSilentClientFreesItsSlot(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	var accepted atomic.Int32
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			// The panel drops a peer that never completes a handshake.
			accepted.Add(1)
			c.Close()
		}
	}()
	f := &Forwarder{Target: ln.Addr().String(), MaxConns: 1, IdleTimeout: time.Minute,
		CloseGrace: 100 * time.Millisecond, DialTimeout: time.Second}
	addr := start(t, f)
	silent, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer silent.Close()
	// The panel's close reaches the silent peer as EOF.
	_ = silent.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := silent.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("the panel's close did not reach the client: %v", err)
	}
	time.Sleep(400 * time.Millisecond)
	next, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer next.Close()
	deadline := time.Now().Add(2 * time.Second)
	for accepted.Load() < 2 {
		if time.Now().After(deadline) {
			t.Fatalf("the silent client still holds the only slot (%d upstream connections)", accepted.Load())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestAllowList(t *testing.T) {
	backend := echoUntilEOF(t)
	var dials atomic.Int32
	dial := func(ctx context.Context, network, _ string) (net.Conn, error) {
		dials.Add(1)
		var d net.Dialer
		return d.DialContext(ctx, network, backend.Addr().String())
	}
	exchange := func(allow string) (string, error) {
		f := &Forwarder{Target: "kind-control-plane:30843", MaxConns: 1, IdleTimeout: time.Minute,
			DialTimeout: time.Second, Dial: dial, Allow: []netip.Prefix{netip.MustParsePrefix(allow)}}
		c, err := net.Dial("tcp", start(t, f))
		if err != nil {
			return "", err
		}
		defer c.Close()
		_, _ = c.Write([]byte("hola"))
		_ = c.(*net.TCPConn).CloseWrite()
		_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
		got, err := io.ReadAll(c)
		return string(got), err
	}
	// A peer outside the allowed networks never reaches the panel.
	if got, _ := exchange("10.0.0.0/8"); got != "" || dials.Load() != 0 {
		t.Fatalf("a refused peer was forwarded: %q, %d dials", got, dials.Load())
	}
	if got, err := exchange("127.0.0.0/8"); got != "eco:hola" || err != nil || dials.Load() != 1 {
		t.Fatalf("an allowed peer: %q %v, %d dials", got, err, dials.Load())
	}
}
