// Package forward is ax-web-edge: a plain TCP pipe from Traefik's overlay
// to the panel's NodePort on the kind network. It never terminates TLS, so
// the mTLS session runs end to end between Traefik and the panel.
package forward

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"time"
)

// Forwarder copies bytes between each accepted connection and a fresh
// connection to Target.
type Forwarder struct {
	// Target is host:port, resolved again on every connection through
	// Docker's DNS, so a recreated node keeps working.
	Target string
	// Allow lists the networks a peer may connect from (Traefik's overlay).
	// Any other peer, such as a sandbox on the kind bridge, is closed before
	// it takes a slot. Empty allows every peer.
	Allow []netip.Prefix
	// MaxConns caps concurrent connections; extra ones are closed at once.
	MaxConns int
	// IdleTimeout closes a connection with no bytes in either direction.
	IdleTimeout time.Duration
	// CloseGrace is how long the client may keep sending once the panel has
	// closed its side. Then the connection closes and frees its slot, so a
	// silent client cannot hold one after the panel dropped it.
	CloseGrace time.Duration
	// DialTimeout bounds each connection attempt.
	DialTimeout time.Duration
	// Dial defaults to a net.Dialer; tests replace it.
	Dial func(ctx context.Context, network, address string) (net.Conn, error)
	Log  *slog.Logger
}

// Serve accepts until ctx ends or the listener fails, then waits for the
// open connections.
func (f *Forwarder) Serve(ctx context.Context, ln net.Listener) error {
	dial := f.Dial
	if dial == nil {
		d := &net.Dialer{Timeout: f.DialTimeout}
		dial = d.DialContext
	}
	log := f.Log
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	var refused, full throttle
	slots := make(chan struct{}, f.MaxConns)
	var wg sync.WaitGroup
	stop := context.AfterFunc(ctx, func() { ln.Close() })
	defer stop()
	for {
		c, err := ln.Accept()
		if err != nil {
			wg.Wait()
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if !f.allowed(c.RemoteAddr()) {
			refused.warn(log, "peer outside the allowed networks refused", "peer", c.RemoteAddr().String())
			c.Close()
			continue
		}
		select {
		case slots <- struct{}{}:
		default:
			full.warn(log, "connection limit reached", "max", f.MaxConns)
			c.Close()
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-slots }()
			f.handle(ctx, c, dial, log)
		}()
	}
}

func (f *Forwarder) allowed(addr net.Addr) bool {
	if len(f.Allow) == 0 {
		return true
	}
	tcp, ok := addr.(*net.TCPAddr)
	if !ok {
		return false
	}
	ip := tcp.AddrPort().Addr().Unmap()
	for _, p := range f.Allow {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}

func (f *Forwarder) handle(ctx context.Context, client net.Conn, dial func(context.Context, string, string) (net.Conn, error), log *slog.Logger) {
	defer client.Close()
	dctx, cancel := context.WithTimeout(ctx, f.DialTimeout)
	// IPv4 only: the kind network's node address, never an AAAA surprise.
	upstream, err := dial(dctx, "tcp4", f.Target)
	cancel()
	if err != nil {
		log.Warn("dialing the target failed", "target", f.Target, "error", err)
		return
	}
	defer upstream.Close()

	var last atomic.Int64
	last.Store(time.Now().UnixNano())
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		pipe(client, upstream, &last)
		// The panel is done with this connection: the client gets
		// CloseGrace to finish sending, then its pending read fails and
		// the slot is freed even if it never sends or closes.
		_ = client.SetReadDeadline(time.Now().Add(f.CloseGrace))
	}()
	go func() {
		defer wg.Done()
		pipe(upstream, client, &last)
	}()
	go func() {
		wg.Wait()
		close(done)
	}()
	tick := time.NewTicker(f.IdleTimeout / 4)
	defer tick.Stop()
	for {
		select {
		case <-done:
			return
		case <-ctx.Done():
			return
		case <-tick.C:
			if time.Since(time.Unix(0, last.Load())) >= f.IdleTimeout {
				return
			}
		}
	}
}

type closeWriter interface{ CloseWrite() error }

// pipe copies src to dst and half-closes dst at EOF, so either side can
// finish sending while the other still answers.
func pipe(dst, src net.Conn, last *atomic.Int64) {
	buf := make([]byte, 32<<10)
	for {
		n, err := src.Read(buf)
		if n > 0 {
			last.Store(time.Now().UnixNano())
			if _, werr := dst.Write(buf[:n]); werr != nil {
				src.Close()
				return
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				if cw, ok := dst.(closeWriter); ok {
					_ = cw.CloseWrite()
					return
				}
			}
			dst.Close()
			return
		}
	}
}

// throttle logs the first event and then at most one line a minute with
// the count since, so a flood of refused peers cannot flood the log.
type throttle struct {
	mu      sync.Mutex
	last    time.Time
	skipped int
}

func (t *throttle) warn(log *slog.Logger, msg string, args ...any) {
	t.mu.Lock()
	now := time.Now()
	if !t.last.IsZero() && now.Sub(t.last) < time.Minute {
		t.skipped++
		t.mu.Unlock()
		return
	}
	skipped := t.skipped
	t.last, t.skipped = now, 0
	t.mu.Unlock()
	log.Warn(msg, append(args, "suppressed", skipped)...)
}
