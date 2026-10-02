package fakeax

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"
	"sync"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// AgentWrapper is argv[0] of the agent; the guest treats such a command as
// the agent unless a script matches it.
const AgentWrapper = "ax-agent"

// outputChunk bounds one output message, well under gRPC's 4 MiB.
const outputChunk = 64 << 10

// Reply scripts how a command behaves.
type Reply struct {
	// Exit is the exit code once the command ends.
	Exit int32
	// Stdout, then Stderr, are sent when its output is streamed, in
	// chunks of at most 64 KiB.
	Stdout []byte
	Stderr []byte
	// WaitStdin keeps a command started with stdin running until its
	// stdin is closed, like `git apply -`.
	WaitStdin bool
	// Hold keeps it running until a signal (or its timeout).
	Hold bool
}

type script struct {
	prefix []string
	reply  Reply
}

// Proc is a process the guest started.
type Proc struct {
	ID      string
	Request *ateenvv1alpha.StartProcessRequest
	Stdin   []byte
	Closed  bool
	Signals []ateenvv1alpha.Signal
}

// Guest is one sandbox's ProcessService. A command whose argv starts with
// a scripted prefix behaves as scripted (the longest prefix wins). Other
// `git` commands exit at once with GitExit (the clone check, `git
// rev-parse`, prints HeadSHA when GitExit is 0); a command run through
// AgentWrapper is the scripted agent (Output, ExitCode, Hold); anything
// else exits 127.
type Guest struct {
	ateenvv1alpha.UnimplementedProcessServiceServer

	mu      sync.Mutex
	Started []*ateenvv1alpha.StartProcessRequest
	Actors  []string
	// Stdin and Closed are the agent's.
	Stdin   []byte
	Closed  bool
	Signals []ateenvv1alpha.Signal
	// Output is the agent's, sent in order once its stream opens.
	Output []*ateenvv1alpha.ProcessOutput
	// ExitCode is used when the agent ends by itself.
	ExitCode int32
	// Hold keeps the agent running until a signal or Exit.
	Hold bool
	// IgnoreTerm makes SIGTERM have no effect.
	IgnoreTerm bool
	// GitExit and GitOutput (stderr) script unscripted git commands;
	// HeadSHA is what `git rev-parse` prints when GitExit is 0.
	GitExit   int32
	GitOutput []byte
	HeadSHA   string
	// Unreachable makes GetProcess fail, as a guest the router cannot reach.
	Unreachable bool
	procs       map[string]*process
	order       []string
	scripts     []script
	agent       string
}

type process struct {
	id        string
	req       *ateenvv1alpha.StartProcessRequest
	exit      chan struct{}
	once      sync.Once
	code      int32
	output    []*ateenvv1alpha.ProcessOutput
	waitStdin bool
	stdinCode int32
	stdin     []byte
	closed    bool
	signals   []ateenvv1alpha.Signal
}

func (p *process) finish(code int32) {
	p.once.Do(func() {
		p.code = code
		close(p.exit)
	})
}

// NewGuest returns a guest whose agent holds until told otherwise.
func NewGuest() *Guest {
	return &Guest{Hold: true, procs: map[string]*process{},
		HeadSHA: "0123456789abcdef0123456789abcdef01234567"}
}

// Script makes every command whose argv starts with prefix behave as r. A
// later script with the same prefix replaces the earlier one.
func (g *Guest) Script(r Reply, prefix ...string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for i, s := range g.scripts {
		if slices.Equal(s.prefix, prefix) {
			g.scripts[i].reply = r
			return
		}
	}
	g.scripts = append(g.scripts, script{prefix: append([]string(nil), prefix...), reply: r})
}

func (g *Guest) match(argv []string) (Reply, bool) {
	best := -1
	var reply Reply
	for _, s := range g.scripts {
		if len(s.prefix) > best && len(s.prefix) <= len(argv) && slices.Equal(argv[:len(s.prefix)], s.prefix) {
			best, reply = len(s.prefix), s.reply
		}
	}
	return reply, best >= 0
}

// Exit ends the agent with code.
func (g *Guest) Exit(code int32) {
	g.mu.Lock()
	p := g.procs[g.agent]
	g.mu.Unlock()
	if p != nil {
		p.finish(code)
	}
}

// Snapshot copies what the guest received: every start request, the actor
// headers, the agent's stdin and whether it was closed, and the signals.
func (g *Guest) Snapshot() (started []*ateenvv1alpha.StartProcessRequest, actors []string,
	stdin []byte, closed bool, signals []ateenvv1alpha.Signal) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]*ateenvv1alpha.StartProcessRequest(nil), g.Started...),
		append([]string(nil), g.Actors...), append([]byte(nil), g.Stdin...), g.Closed,
		append([]ateenvv1alpha.Signal(nil), g.Signals...)
}

// Processes copies every process the guest started, in order.
func (g *Guest) Processes() []Proc {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]Proc, 0, len(g.order))
	for _, id := range g.order {
		p := g.procs[id]
		out = append(out, Proc{ID: id, Request: proto.Clone(p.req).(*ateenvv1alpha.StartProcessRequest),
			Stdin: append([]byte(nil), p.stdin...), Closed: p.closed,
			Signals: append([]ateenvv1alpha.Signal(nil), p.signals...)})
	}
	return out
}

// Find returns the processes whose argv starts with prefix.
func (g *Guest) Find(prefix ...string) []Proc {
	var out []Proc
	for _, p := range g.Processes() {
		if argv := p.Request.GetCommand(); len(argv) >= len(prefix) && slices.Equal(argv[:len(prefix)], prefix) {
			out = append(out, p)
		}
	}
	return out
}

// Agent is the agent's StartProcess request, or nil.
func (g *Guest) Agent() *ateenvv1alpha.StartProcessRequest {
	g.mu.Lock()
	defer g.mu.Unlock()
	if p := g.procs[g.agent]; p != nil {
		return proto.Clone(p.req).(*ateenvv1alpha.StartProcessRequest)
	}
	return nil
}

func (g *Guest) actor(ctx context.Context) {
	md, _ := metadata.FromIncomingContext(ctx)
	g.mu.Lock()
	g.Actors = append(g.Actors, md.Get("ate-target-actor")...)
	g.mu.Unlock()
}

// chunks splits stdout then stderr into output messages.
func chunks(stdout, stderr []byte) []*ateenvv1alpha.ProcessOutput {
	var out []*ateenvv1alpha.ProcessOutput
	for len(stdout) > 0 {
		n := min(len(stdout), outputChunk)
		out = append(out, &ateenvv1alpha.ProcessOutput{Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: stdout[:n]}})
		stdout = stdout[n:]
	}
	for len(stderr) > 0 {
		n := min(len(stderr), outputChunk)
		out = append(out, &ateenvv1alpha.ProcessOutput{Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: stderr[:n]}})
		stderr = stderr[n:]
	}
	return out
}

func (g *Guest) StartProcess(ctx context.Context, req *ateenvv1alpha.StartProcessRequest) (*ateenvv1alpha.Process, error) {
	g.actor(ctx)
	g.mu.Lock()
	defer g.mu.Unlock()
	g.Started = append(g.Started, proto.Clone(req).(*ateenvv1alpha.StartProcessRequest))
	pid := fmt.Sprintf("p%d", len(g.Started))
	p := &process{id: pid, req: proto.Clone(req).(*ateenvv1alpha.StartProcessRequest), exit: make(chan struct{})}
	g.procs[pid] = p
	g.order = append(g.order, pid)
	// Like the guest, SIGKILL at the requested timeout.
	if d := req.GetTimeout().AsDuration(); d > 0 {
		time.AfterFunc(d, func() { p.finish(137) })
	}
	argv := req.GetCommand()
	switch reply, scripted := g.match(argv); {
	case scripted:
		p.output = chunks(reply.Stdout, reply.Stderr)
		switch {
		case reply.Hold:
		case reply.WaitStdin && req.GetStdin():
			p.waitStdin, p.stdinCode = true, reply.Exit
		default:
			p.finish(reply.Exit)
		}
	case len(argv) > 0 && argv[0] == "git":
		var stdout []byte
		if g.GitExit == 0 && slices.Contains(argv, "rev-parse") {
			stdout = []byte(g.HeadSHA + "\n")
		}
		p.output = chunks(stdout, g.GitOutput)
		p.finish(g.GitExit)
	case len(argv) > 0 && argv[0] == AgentWrapper:
		g.agent = pid
		p.output = g.Output
		if !g.Hold {
			p.finish(g.ExitCode)
		}
	default:
		p.output = chunks(nil, []byte(argv0(argv)+": not found\n"))
		p.finish(127)
	}
	return &ateenvv1alpha.Process{ProcessId: pid, Command: argv, State: ateenvv1alpha.ProcessState_PROCESS_STATE_RUNNING}, nil
}

func argv0(argv []string) string {
	if len(argv) == 0 {
		return ""
	}
	return argv[0]
}

func (g *Guest) WriteProcessInput(st grpc.ClientStreamingServer[ateenvv1alpha.WriteProcessInputRequest, ateenvv1alpha.WriteProcessInputResponse]) error {
	g.actor(st.Context())
	var n int64
	for {
		req, err := st.Recv()
		if errors.Is(err, io.EOF) {
			return st.SendAndClose(&ateenvv1alpha.WriteProcessInputResponse{BytesWritten: n})
		}
		if err != nil {
			return err
		}
		g.mu.Lock()
		p, ok := g.procs[req.GetProcessId()]
		switch {
		case !ok:
			g.mu.Unlock()
			return status.Errorf(codes.NotFound, "process %q", req.GetProcessId())
		case !p.req.GetStdin():
			g.mu.Unlock()
			return status.Error(codes.FailedPrecondition, "the process was started without stdin")
		}
		p.stdin = append(p.stdin, req.GetData()...)
		p.closed = p.closed || req.GetClose()
		if req.GetProcessId() == g.agent {
			g.Stdin = append(g.Stdin, req.GetData()...)
			g.Closed = g.Closed || req.GetClose()
		}
		if p.closed && p.waitStdin {
			p.finish(p.stdinCode)
		}
		g.mu.Unlock()
		n += int64(len(req.GetData()))
	}
}

func (g *Guest) process(pid string) (*process, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	p, ok := g.procs[pid]
	if !ok {
		return nil, status.Errorf(codes.NotFound, "process %q", pid)
	}
	return p, nil
}

func (g *Guest) StreamProcessOutput(req *ateenvv1alpha.StreamProcessOutputRequest, st grpc.ServerStreamingServer[ateenvv1alpha.ProcessOutput]) error {
	g.actor(st.Context())
	p, err := g.process(req.GetProcessId())
	if err != nil {
		return err
	}
	var outOff, errOff int64
	for _, o := range p.output {
		d := o.GetStdout()
		off := &outOff
		want := req.GetStdoutOffset()
		if d == nil {
			d, off, want = o.GetStderr(), &errOff, req.GetStderrOffset()
		}
		start := *off
		*off += int64(len(d))
		if *off <= want {
			continue
		}
		if start < want {
			d = d[want-start:]
		}
		msg := &ateenvv1alpha.ProcessOutput{}
		if off == &outOff {
			msg.Output = &ateenvv1alpha.ProcessOutput_Stdout{Stdout: d}
		} else {
			msg.Output = &ateenvv1alpha.ProcessOutput_Stderr{Stderr: d}
		}
		if err := st.Send(msg); err != nil {
			return err
		}
	}
	select {
	case <-st.Context().Done():
		return st.Context().Err()
	case <-p.exit:
		return st.Send(&ateenvv1alpha.ProcessOutput{Output: &ateenvv1alpha.ProcessOutput_Exit{
			Exit: &ateenvv1alpha.Process{ProcessId: req.GetProcessId(),
				State: ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED, ExitCode: p.code},
		}})
	}
}

func (g *Guest) SignalProcess(ctx context.Context, req *ateenvv1alpha.SignalProcessRequest) (*ateenvv1alpha.Process, error) {
	g.actor(ctx)
	p, err := g.process(req.GetProcessId())
	if err != nil {
		return nil, err
	}
	g.mu.Lock()
	if req.GetProcessId() == g.agent {
		g.Signals = append(g.Signals, req.GetSignal())
	}
	p.signals = append(p.signals, req.GetSignal())
	ignore := g.IgnoreTerm && req.GetProcessId() == g.agent
	g.mu.Unlock()
	switch {
	case req.GetSignal() == ateenvv1alpha.Signal_SIGNAL_KILL:
		p.finish(137)
	case req.GetSignal() == ateenvv1alpha.Signal_SIGNAL_TERM && !ignore:
		p.finish(143)
	}
	return &ateenvv1alpha.Process{ProcessId: req.GetProcessId()}, nil
}

func (g *Guest) GetProcess(ctx context.Context, req *ateenvv1alpha.GetProcessRequest) (*ateenvv1alpha.Process, error) {
	g.actor(ctx)
	g.mu.Lock()
	unreachable := g.Unreachable
	g.mu.Unlock()
	if unreachable {
		return nil, status.Error(codes.Unavailable, "guest unreachable")
	}
	p, err := g.process(req.GetProcessId())
	if err != nil {
		return nil, err
	}
	select {
	case <-p.exit:
		return &ateenvv1alpha.Process{ProcessId: req.GetProcessId(),
			State: ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED, ExitCode: p.code}, nil
	default:
		return &ateenvv1alpha.Process{ProcessId: req.GetProcessId(),
			State: ateenvv1alpha.ProcessState_PROCESS_STATE_RUNNING}, nil
	}
}

// SetUnreachable switches GetProcess failures on or off.
func (g *Guest) SetUnreachable(v bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.Unreachable = v
}

// TarFile is one entry of a tar stream as `git archive --format=tar`
// writes it.
type TarFile struct {
	Name string
	Body string
	// Mode defaults to 0664 (git archive's default umask); 0775 marks an
	// executable.
	Mode int64
	// Link makes the entry a symbolic link to Link.
	Link string
	// Dir makes it a directory, as git archive writes parent directories
	// and submodules.
	Dir bool
}

// Tar builds a tar stream of files, to script `git archive`.
func Tar(files ...TarFile) []byte {
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	mtime := time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)
	for _, f := range files {
		h := &tar.Header{Name: f.Name, Mode: f.Mode, ModTime: mtime, Format: tar.FormatPAX}
		switch {
		case f.Dir:
			h.Typeflag, h.Name = tar.TypeDir, strings.TrimSuffix(f.Name, "/")+"/"
			if h.Mode == 0 {
				h.Mode = 0o775
			}
		case f.Link != "":
			h.Typeflag, h.Linkname, h.Mode = tar.TypeSymlink, f.Link, 0o777
		default:
			h.Typeflag, h.Size = tar.TypeReg, int64(len(f.Body))
			if h.Mode == 0 {
				h.Mode = 0o664
			}
		}
		if err := w.WriteHeader(h); err != nil {
			panic(err)
		}
		if h.Typeflag == tar.TypeReg {
			if _, err := io.WriteString(w, f.Body); err != nil {
				panic(err)
			}
		}
	}
	if err := w.Close(); err != nil {
		panic(err)
	}
	return buf.Bytes()
}
