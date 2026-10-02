// Package fakeax is an in-memory AX server and guest ProcessService for
// tests, served over bufconn. It is imported only by _test files.
package fakeax

import (
	"context"
	"net"
	"sort"
	"sync"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

// Target is the address to dial with the option Listen returns.
const Target = "passthrough:///bufnet"

// Listen starts the services on an in-memory listener and returns the dial
// option that reaches it plus a stop function.
func Listen(register func(*grpc.Server)) (grpc.DialOption, func()) {
	ln := bufconn.Listen(1 << 20)
	s := grpc.NewServer()
	register(s)
	go func() { _ = s.Serve(ln) }()
	return grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
		return ln.DialContext(ctx)
	}), s.Stop
}

// Serve is Listen plus a client connection.
func Serve(register func(*grpc.Server)) (*grpc.ClientConn, func()) {
	dial, stop := Listen(register)
	conn, err := grpc.NewClient(Target, dial, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		panic(err)
	}
	return conn, func() {
		conn.Close()
		stop()
	}
}

// AX is a minimal AX control plane.
type AX struct {
	v1alpha1.UnimplementedAXServer

	mu         sync.Mutex
	tasks      map[string]*v1alpha1.Task
	workspaces map[string]*v1alpha1.Workspace
	gateways   []*v1alpha1.Gateway
	gets       map[string]int

	// ReadyAfter is how many GetTask calls a new task needs before it
	// reports Ready=True with Actor.
	ReadyAfter int
	NeverReady bool
	// FailTasks makes every task report phase Failed, as AX does when the
	// actor cannot start.
	FailTasks bool
	// StuckDeletes leaves deleted tasks Terminating forever.
	StuckDeletes bool
	// ListError makes ListTasks fail, as an unreachable ax-server.
	ListError error
	Actor     string
	// Created records every UpdateTask and UpdateWorkspace request.
	CreatedTasks      []*v1alpha1.Task
	CreatedWorkspaces []*v1alpha1.Workspace
	Deleted           []string
	Suspended         []string
	Resumed           []string
}

// NewAX returns an empty control plane.
func NewAX() *AX {
	return &AX{
		tasks: map[string]*v1alpha1.Task{}, workspaces: map[string]*v1alpha1.Workspace{},
		gets: map[string]int{}, Actor: "actor-1",
	}
}

// AddTask stores t as is.
func (a *AX) AddTask(t *v1alpha1.Task) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.tasks[t.GetMetadata().GetName()] = t
	a.gets[t.GetMetadata().GetName()] = -1 << 30 // never promoted
}

// AddWorkspace stores w as is.
func (a *AX) AddWorkspace(w *v1alpha1.Workspace) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.workspaces[w.GetMetadata().GetName()] = w
}

// AddGateway stores g.
func (a *AX) AddGateway(g *v1alpha1.Gateway) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.gateways = append(a.gateways, g)
}

// Has reports whether a task or workspace exists.
func (a *AX) Has(task, workspace string) (bool, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	_, t := a.tasks[task]
	_, w := a.workspaces[workspace]
	return t, w
}

// SetListError changes ListError under the lock.
func (a *AX) SetListError(err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.ListError = err
}

// SetStuck changes StuckDeletes under the lock.
func (a *AX) SetStuck(stuck bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.StuckDeletes = stuck
}

// Snapshot copies the recorded requests under the lock.
func (a *AX) Snapshot() (tasks []*v1alpha1.Task, workspaces []*v1alpha1.Workspace, deleted []string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]*v1alpha1.Task(nil), a.CreatedTasks...),
		append([]*v1alpha1.Workspace(nil), a.CreatedWorkspaces...),
		append([]string(nil), a.Deleted...)
}

func notFound(kind, name string) error {
	return status.Errorf(codes.NotFound, "%s %q not found", kind, name)
}

func (a *AX) GetTask(_ context.Context, req *v1alpha1.GetTaskRequest) (*v1alpha1.Task, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.tasks[req.GetName()]
	if !ok {
		return nil, notFound("task", req.GetName())
	}
	if t.GetStatus().GetPhase() == v1alpha1.PhaseTerminating {
		if !a.StuckDeletes {
			delete(a.tasks, req.GetName())
			return nil, notFound("task", req.GetName())
		}
		return proto.Clone(t).(*v1alpha1.Task), nil
	}
	a.gets[req.GetName()]++
	if a.FailTasks {
		t.Status = &v1alpha1.TaskStatus{Phase: "Failed", Conditions: []*v1alpha1.Condition{
			{Type: "Ready", Status: "False"},
		}}
		return proto.Clone(t).(*v1alpha1.Task), nil
	}
	if !a.NeverReady && a.gets[req.GetName()] > a.ReadyAfter {
		t.Status = &v1alpha1.TaskStatus{Phase: "Running", Actor: a.Actor, Conditions: []*v1alpha1.Condition{
			{Type: "WorkspaceReady", Status: "True"}, {Type: "Ready", Status: "True"},
		}}
	}
	return proto.Clone(t).(*v1alpha1.Task), nil
}

func (a *AX) ListTasks(_ context.Context, req *v1alpha1.ListTasksRequest) (*v1alpha1.ListTasksResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.ListError != nil {
		return nil, a.ListError
	}
	names := make([]string, 0, len(a.tasks))
	for n := range a.tasks {
		names = append(names, n)
	}
	sort.Strings(names)
	limit, off := int(req.GetLimit()), int(req.GetOffset())
	if limit <= 0 {
		limit = 50
	}
	resp := &v1alpha1.ListTasksResponse{}
	for i := off; i < len(names) && i < off+limit; i++ {
		resp.Tasks = append(resp.Tasks, proto.Clone(a.tasks[names[i]]).(*v1alpha1.Task))
	}
	return resp, nil
}

func (a *AX) UpdateTask(_ context.Context, req *v1alpha1.UpdateTaskRequest) (*v1alpha1.Task, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := proto.Clone(req.GetTask()).(*v1alpha1.Task)
	a.CreatedTasks = append(a.CreatedTasks, proto.Clone(t).(*v1alpha1.Task))
	t.Status = &v1alpha1.TaskStatus{Phase: "Pending"}
	a.tasks[t.GetMetadata().GetName()] = t
	a.gets[t.GetMetadata().GetName()] = 0
	return t, nil
}

func (a *AX) DeleteTask(_ context.Context, req *v1alpha1.DeleteTaskRequest) (*v1alpha1.DeleteTaskResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.tasks[req.GetName()]
	if !ok {
		return nil, notFound("task", req.GetName())
	}
	if t.Status == nil {
		t.Status = &v1alpha1.TaskStatus{}
	}
	t.Status.Phase = v1alpha1.PhaseTerminating
	a.Deleted = append(a.Deleted, "task/"+req.GetName())
	return &v1alpha1.DeleteTaskResponse{}, nil
}

func (a *AX) SuspendTask(_ context.Context, req *v1alpha1.SuspendTaskRequest) (*v1alpha1.Task, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.tasks[req.GetName()]
	if !ok {
		return nil, notFound("task", req.GetName())
	}
	if t.Spec == nil {
		t.Spec = &v1alpha1.TaskSpec{}
	}
	t.Spec.Suspend = true
	a.Suspended = append(a.Suspended, req.GetName())
	return proto.Clone(t).(*v1alpha1.Task), nil
}

func (a *AX) ResumeTask(_ context.Context, req *v1alpha1.ResumeTaskRequest) (*v1alpha1.Task, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.tasks[req.GetName()]
	if !ok {
		return nil, notFound("task", req.GetName())
	}
	if t.Spec == nil {
		t.Spec = &v1alpha1.TaskSpec{}
	}
	t.Spec.Suspend = false
	a.Resumed = append(a.Resumed, req.GetName())
	return proto.Clone(t).(*v1alpha1.Task), nil
}

func (a *AX) ListGateways(context.Context, *v1alpha1.ListGatewaysRequest) (*v1alpha1.ListGatewaysResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return &v1alpha1.ListGatewaysResponse{Gateways: a.gateways}, nil
}

func (a *AX) GetWorkspace(_ context.Context, req *v1alpha1.GetWorkspaceRequest) (*v1alpha1.Workspace, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	w, ok := a.workspaces[req.GetName()]
	if !ok {
		return nil, notFound("workspace", req.GetName())
	}
	return proto.Clone(w).(*v1alpha1.Workspace), nil
}

func (a *AX) ListWorkspaces(context.Context, *v1alpha1.ListWorkspacesRequest) (*v1alpha1.ListWorkspacesResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	resp := &v1alpha1.ListWorkspacesResponse{}
	names := make([]string, 0, len(a.workspaces))
	for n := range a.workspaces {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		resp.Workspaces = append(resp.Workspaces, proto.Clone(a.workspaces[n]).(*v1alpha1.Workspace))
	}
	return resp, nil
}

func (a *AX) UpdateWorkspace(_ context.Context, req *v1alpha1.UpdateWorkspaceRequest) (*v1alpha1.Workspace, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	w := proto.Clone(req.GetWorkspace()).(*v1alpha1.Workspace)
	a.CreatedWorkspaces = append(a.CreatedWorkspaces, proto.Clone(w).(*v1alpha1.Workspace))
	a.workspaces[w.GetMetadata().GetName()] = w
	return w, nil
}

func (a *AX) DeleteWorkspace(_ context.Context, req *v1alpha1.DeleteWorkspaceRequest) (*v1alpha1.DeleteWorkspaceResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.StuckDeletes {
		return &v1alpha1.DeleteWorkspaceResponse{}, nil
	}
	delete(a.workspaces, req.GetName())
	a.Deleted = append(a.Deleted, "workspace/"+req.GetName())
	return &v1alpha1.DeleteWorkspaceResponse{}, nil
}
