package demo

import (
	"context"
	"sort"
	"sync"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// AX is an in-memory AX control plane for the raw AX views of the demo.
// It implements v1alpha1.AXClient directly, without gRPC.
type AX struct {
	mu         sync.Mutex
	tasks      map[string]*v1alpha1.Task
	workspaces map[string]*v1alpha1.Workspace
	gateways   []*v1alpha1.Gateway
	active     func() string
}

var _ v1alpha1.AXClient = (*AX)(nil)

func meta(name string, at time.Time) *v1alpha1.ObjectMeta {
	return &v1alpha1.ObjectMeta{Name: name, CreationTimestamp: timestamppb.New(at)}
}

// NewAX returns a control plane with an example task, workspace and
// gateway.
func NewAX(now time.Time) *AX {
	a := &AX{tasks: map[string]*v1alpha1.Task{}, workspaces: map[string]*v1alpha1.Workspace{}}
	a.tasks["ejemplo-manual"] = &v1alpha1.Task{
		Metadata: meta("ejemplo-manual", now.Add(-2*time.Hour)),
		Spec: &v1alpha1.TaskSpec{Image: "localhost:5001/ax-agents@sha256:" + zeros, Suspend: true,
			Workspaces: []*v1alpha1.WorkspaceRef{{Name: "ws-ejemplo-manual", Path: "/workspace"}}},
		Status: &v1alpha1.TaskStatus{Phase: "Suspended", Conditions: []*v1alpha1.Condition{
			{Type: "Ready", Status: "False", Reason: "Suspended", Message: "Tarea de ejemplo de la demo"}}},
	}
	a.workspaces["ws-ejemplo-manual"] = &v1alpha1.Workspace{
		Metadata: meta("ws-ejemplo-manual", now.Add(-2*time.Hour)),
		Spec: &v1alpha1.WorkspaceSpec{Git: []*v1alpha1.GitRepo{
			{Name: "origin", Repo: "https://github.com/google/ax", Branch: "main", Dir: "repo"}}},
	}
	a.gateways = []*v1alpha1.Gateway{{
		Metadata: meta("egress", now.Add(-24*time.Hour)),
		Spec: &v1alpha1.GatewaySpec{
			Listeners: []*v1alpha1.Listener{{Name: "https", Port: 443, Protocol: "HTTPS"}},
			Egress: &v1alpha1.EgressConfig{Allowlist: &v1alpha1.EgressAllowlist{Hosts: []*v1alpha1.HostRule{
				{Host: "api.anthropic.com", Port: 443}, {Host: "github.com", Port: 443}}}},
		},
	}}
	return a
}

const zeros = "0000000000000000000000000000000000000000000000000000000000000000"

func notFound() error { return status.Error(codes.NotFound, "no existe") }

func unimplemented() error { return status.Error(codes.Unimplemented, "no disponible en la demo") }

// addRun records a simulated run's task and workspace.
func (a *AX) addRun(task, repo, branch string, at time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.tasks[task] = &v1alpha1.Task{
		Metadata: meta(task, at),
		Spec: &v1alpha1.TaskSpec{Image: "localhost:5001/ax-agents@sha256:" + zeros, Debug: true,
			Workspaces: []*v1alpha1.WorkspaceRef{{Name: "ws-" + task, Path: "/workspace"}}},
		Status: &v1alpha1.TaskStatus{Phase: "Running", Actor: "actor-demo", Conditions: []*v1alpha1.Condition{
			{Type: "WorkspaceReady", Status: "True"}, {Type: "Ready", Status: "True"}}},
	}
	a.workspaces["ws-"+task] = &v1alpha1.Workspace{
		Metadata: meta("ws-"+task, at),
		Spec:     &v1alpha1.WorkspaceSpec{Git: []*v1alpha1.GitRepo{{Name: "origin", Repo: repo, Branch: branch, Dir: "repo"}}},
	}
}

func (a *AX) removeRun(task string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.tasks, task)
	delete(a.workspaces, "ws-"+task)
}

// ActiveTask is the demo executor's run in progress (web.TaskRunner).
func (a *AX) ActiveTask() string {
	if a.active == nil {
		return ""
	}
	return a.active()
}

// DeleteAndWait deletes at once (web.TaskRunner).
func (a *AX) DeleteAndWait(task, workspace string, _ time.Duration) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.tasks, task)
	delete(a.workspaces, workspace)
	return nil
}

func (a *AX) GetTask(_ context.Context, in *v1alpha1.GetTaskRequest, _ ...grpc.CallOption) (*v1alpha1.Task, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.tasks[in.GetName()]
	if !ok {
		return nil, notFound()
	}
	return proto.Clone(t).(*v1alpha1.Task), nil
}

func (a *AX) ListTasks(_ context.Context, in *v1alpha1.ListTasksRequest, _ ...grpc.CallOption) (*v1alpha1.ListTasksResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	names := make([]string, 0, len(a.tasks))
	for n := range a.tasks {
		names = append(names, n)
	}
	sort.Strings(names)
	limit, off := int(in.GetLimit()), int(in.GetOffset())
	if limit <= 0 {
		limit = 50
	}
	out := &v1alpha1.ListTasksResponse{}
	for i := off; i < len(names) && i < off+limit; i++ {
		out.Tasks = append(out.Tasks, proto.Clone(a.tasks[names[i]]).(*v1alpha1.Task))
	}
	return out, nil
}

func (a *AX) UpdateTask(context.Context, *v1alpha1.UpdateTaskRequest, ...grpc.CallOption) (*v1alpha1.Task, error) {
	return nil, unimplemented()
}

func (a *AX) DeleteTask(_ context.Context, in *v1alpha1.DeleteTaskRequest, _ ...grpc.CallOption) (*v1alpha1.DeleteTaskResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, ok := a.tasks[in.GetName()]; !ok {
		return nil, notFound()
	}
	delete(a.tasks, in.GetName())
	return &v1alpha1.DeleteTaskResponse{}, nil
}

func (a *AX) setSuspend(name string, suspend bool) (*v1alpha1.Task, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t, ok := a.tasks[name]
	if !ok {
		return nil, notFound()
	}
	t.Spec.Suspend = suspend
	t.Status.Phase = "Running"
	if suspend {
		t.Status.Phase = "Suspended"
	}
	return proto.Clone(t).(*v1alpha1.Task), nil
}

func (a *AX) SuspendTask(_ context.Context, in *v1alpha1.SuspendTaskRequest, _ ...grpc.CallOption) (*v1alpha1.Task, error) {
	return a.setSuspend(in.GetName(), true)
}

func (a *AX) ResumeTask(_ context.Context, in *v1alpha1.ResumeTaskRequest, _ ...grpc.CallOption) (*v1alpha1.Task, error) {
	return a.setSuspend(in.GetName(), false)
}

func (a *AX) WatchTask(context.Context, *v1alpha1.WatchTaskRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[v1alpha1.WatchTaskResponse], error) {
	return nil, unimplemented()
}

func (a *AX) GetGateway(_ context.Context, in *v1alpha1.GetGatewayRequest, _ ...grpc.CallOption) (*v1alpha1.Gateway, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, g := range a.gateways {
		if g.GetMetadata().GetName() == in.GetName() {
			return proto.Clone(g).(*v1alpha1.Gateway), nil
		}
	}
	return nil, notFound()
}

func (a *AX) ListGateways(context.Context, *v1alpha1.ListGatewaysRequest, ...grpc.CallOption) (*v1alpha1.ListGatewaysResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := &v1alpha1.ListGatewaysResponse{}
	for _, g := range a.gateways {
		out.Gateways = append(out.Gateways, proto.Clone(g).(*v1alpha1.Gateway))
	}
	return out, nil
}

func (a *AX) UpdateGateway(context.Context, *v1alpha1.UpdateGatewayRequest, ...grpc.CallOption) (*v1alpha1.Gateway, error) {
	return nil, unimplemented()
}

func (a *AX) DeleteGateway(context.Context, *v1alpha1.DeleteGatewayRequest, ...grpc.CallOption) (*v1alpha1.DeleteGatewayResponse, error) {
	return nil, unimplemented()
}

func (a *AX) GetWorkspace(_ context.Context, in *v1alpha1.GetWorkspaceRequest, _ ...grpc.CallOption) (*v1alpha1.Workspace, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	w, ok := a.workspaces[in.GetName()]
	if !ok {
		return nil, notFound()
	}
	return proto.Clone(w).(*v1alpha1.Workspace), nil
}

func (a *AX) ListWorkspaces(context.Context, *v1alpha1.ListWorkspacesRequest, ...grpc.CallOption) (*v1alpha1.ListWorkspacesResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	names := make([]string, 0, len(a.workspaces))
	for n := range a.workspaces {
		names = append(names, n)
	}
	sort.Strings(names)
	out := &v1alpha1.ListWorkspacesResponse{}
	for _, n := range names {
		out.Workspaces = append(out.Workspaces, proto.Clone(a.workspaces[n]).(*v1alpha1.Workspace))
	}
	return out, nil
}

func (a *AX) UpdateWorkspace(context.Context, *v1alpha1.UpdateWorkspaceRequest, ...grpc.CallOption) (*v1alpha1.Workspace, error) {
	return nil, unimplemented()
}

func (a *AX) DeleteWorkspace(_ context.Context, in *v1alpha1.DeleteWorkspaceRequest, _ ...grpc.CallOption) (*v1alpha1.DeleteWorkspaceResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.workspaces, in.GetName())
	return &v1alpha1.DeleteWorkspaceResponse{}, nil
}

func (a *AX) GetModel(context.Context, *v1alpha1.GetModelRequest, ...grpc.CallOption) (*v1alpha1.Model, error) {
	return nil, unimplemented()
}

func (a *AX) ListModels(context.Context, *v1alpha1.ListModelsRequest, ...grpc.CallOption) (*v1alpha1.ListModelsResponse, error) {
	return nil, unimplemented()
}

func (a *AX) UpdateModel(context.Context, *v1alpha1.UpdateModelRequest, ...grpc.CallOption) (*v1alpha1.Model, error) {
	return nil, unimplemented()
}

func (a *AX) DeleteModel(context.Context, *v1alpha1.DeleteModelRequest, ...grpc.CallOption) (*v1alpha1.DeleteModelResponse, error) {
	return nil, unimplemented()
}
