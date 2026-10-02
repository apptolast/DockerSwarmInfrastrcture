package runs

import (
	"context"
	"errors"
	"io"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	"google.golang.org/protobuf/types/known/durationpb"
)

// maxInputChunk bounds one WriteProcessInput message.
const maxInputChunk = 256 << 10

// maxDetail bounds the output a failed command's message quotes from.
const maxDetail = 4 << 10

var (
	// errGuestLost: the guest stopped answering while a process ran.
	errGuestLost = errors.New("se perdió la conexión con el sandbox")
	// errStopped: the reader asked to stop (an output bound was reached).
	errStopped = errors.New("lectura detenida")
)

// follow streams a process's output from the start until it exits, and
// returns its exit code. atenet-router's Envoy cuts every stream at its
// route timeout (10 s unless the router runs with a longer
// --route-timeout), so a silent process loses its stream too: after a
// stream ends without an exit message, follow asks the guest directly and,
// while the process runs, follows it again from the offsets already read.
// Once the guest says it exited, one more stream drains what is left.
// Only an unreachable guest counts towards giving up (errGuestLost); ctx
// ending returns ctx.Err(); sink returning false returns errStopped.
func (m *Manager) follow(ctx context.Context, proc ProcessClient, pid string,
	sink func(stdout, stderr []byte) bool) (int, error) {
	var outOff, errOff int64
	failures := 0
	exitSeen := false
	exitCode := 0
	for {
		sctx, scancel := context.WithCancel(ctx)
		st, err := proc.StreamProcessOutput(sctx, &ateenvv1alpha.StreamProcessOutputRequest{
			ProcessId: pid, StdoutOffset: outOff, StderrOffset: errOff, Follow: true,
		})
		for err == nil {
			var msg *ateenvv1alpha.ProcessOutput
			msg, err = st.Recv()
			if err != nil {
				break
			}
			if exit := msg.GetExit(); exit != nil {
				scancel()
				return int(exit.GetExitCode()), nil
			}
			out, errOut := msg.GetStdout(), msg.GetStderr()
			if len(out)+len(errOut) == 0 {
				continue
			}
			outOff += int64(len(out))
			errOff += int64(len(errOut))
			failures = 0
			if !sink(out, errOut) {
				scancel()
				return 0, errStopped
			}
		}
		scancel()
		if ctx.Err() != nil {
			return 0, ctx.Err()
		}
		if exitSeen {
			// Even the drain lost its stream: take the guest's word.
			return exitCode, nil
		}
		cctx, ccancel := context.WithTimeout(ctx, m.opt.CallTimeout)
		p, gerr := proc.GetProcess(cctx, &ateenvv1alpha.GetProcessRequest{ProcessId: pid})
		ccancel()
		switch {
		case gerr == nil && p.GetState() == ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED:
			exitSeen, exitCode = true, int(p.GetExitCode())
			continue
		case gerr == nil:
			failures = 0
		default:
			failures++
		}
		if failures > 5 {
			return 0, errGuestLost
		}
		select {
		case <-ctx.Done():
		case <-time.After(m.opt.PollInterval):
		}
	}
}

// output is what a fixed command printed.
type output struct {
	code   int    // its exit code, -1 when unknown
	stdout []byte // at most the limit asked for
	cut    bool   // stdout passed the limit: the command was killed
	detail []byte // stdout and stderr as they came, at most maxDetail
}

// run starts a fixed argv in the sandbox with no credential, feeds it
// stdin when not nil, and collects its output. Past limit bytes of
// stdout it stops reading and kills the command. An error means the
// command could not be started or followed.
func (m *Manager) run(ctx context.Context, proc ProcessClient, argv []string, stdin []byte,
	limit int, timeout time.Duration) (output, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout+m.opt.CallTimeout)
	defer cancel()
	o := output{code: -1}
	// Not under ctx, as for the agent: once asked, the guest may have
	// started the process, and a cancellation that lands while the call is
	// in flight would lose its id. Ask to completion, then kill it if the
	// run was cancelled meanwhile.
	sctx, scancel := context.WithTimeout(context.Background(), m.opt.CallTimeout)
	p, err := proc.StartProcess(sctx, &ateenvv1alpha.StartProcessRequest{
		Command: argv, Cwd: WorkspacePath, Stdin: stdin != nil, Timeout: durationpb.New(timeout),
	})
	scancel()
	if err != nil {
		return o, err
	}
	pid := p.GetProcessId()
	if err := ctx.Err(); err != nil {
		m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
		return o, err
	}
	if stdin != nil {
		if err := m.writeStdin(ctx, proc, pid, stdin); err != nil {
			m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
			return o, err
		}
	}
	code, err := m.follow(ctx, proc, pid, func(out, errOut []byte) bool {
		for _, d := range [][]byte{out, errOut} {
			if room := maxDetail - len(o.detail); room > 0 {
				o.detail = append(o.detail, d[:min(len(d), room)]...)
			}
		}
		if len(o.stdout)+len(out) > limit {
			o.stdout = append(o.stdout, out[:limit-len(o.stdout)]...)
			o.cut = true
			return false
		}
		o.stdout = append(o.stdout, out...)
		return true
	})
	switch {
	case errors.Is(err, errStopped):
		m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
		return o, nil
	case err != nil:
		m.signal(proc, pid, ateenvv1alpha.Signal_SIGNAL_KILL)
		return o, err
	}
	o.code = code
	return o, nil
}

// writeStdin writes data to a process's stdin in bounded messages and
// closes it, so it never appears in argv or in GetProcess.command.
func (m *Manager) writeStdin(ctx context.Context, proc ProcessClient, pid string, data []byte) error {
	ctx, cancel := context.WithTimeout(ctx, m.opt.CallTimeout)
	defer cancel()
	st, err := proc.WriteProcessInput(ctx)
	if err != nil {
		return err
	}
	for {
		n := min(len(data), maxInputChunk)
		last := n == len(data)
		if err := st.Send(&ateenvv1alpha.WriteProcessInputRequest{
			ProcessId: pid, Data: data[:n], Close: last,
		}); err != nil {
			if errors.Is(err, io.EOF) {
				// The guest ended the stream: its status tells why.
				if _, serr := st.CloseAndRecv(); serr != nil {
					return serr
				}
			}
			return err
		}
		data = data[n:]
		if last {
			break
		}
	}
	_, err = st.CloseAndRecv()
	return err
}

func (m *Manager) signal(proc ProcessClient, pid string, sig ateenvv1alpha.Signal) {
	ctx, cancel := context.WithTimeout(context.Background(), m.opt.CallTimeout)
	defer cancel()
	_, _ = proc.SignalProcess(ctx, &ateenvv1alpha.SignalProcessRequest{ProcessId: pid, Signal: sig})
}
