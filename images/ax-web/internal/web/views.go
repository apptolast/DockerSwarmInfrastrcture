package web

import (
	"net/url"
	"time"

	v1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Hidden replaces every value the browser must not see.
const Hidden = "[oculto]"

// The views copy an allowlist of fields; the raw AX objects are never
// serialized. Environment values (ax-tarea puts the agent credential in
// spec.env) and workspace URLs' userinfo never leave the panel.

// EnvView keeps only the variable's name.
type EnvView struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// ResourcesView is recorded, not enforced (google/ax#369).
type ResourcesView struct {
	RequestsCPU    string `json:"requests_cpu,omitempty"`
	RequestsMemory string `json:"requests_memory,omitempty"`
	LimitsCPU      string `json:"limits_cpu,omitempty"`
	LimitsMemory   string `json:"limits_memory,omitempty"`
}

// ConditionView is one task condition.
type ConditionView struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
	Changed string `json:"changed,omitempty"`
}

// WorkspaceRefView is one binding of a task.
type WorkspaceRefView struct {
	Name string `json:"name"`
	Path string `json:"path,omitempty"`
}

// TaskView is a task as the panel shows it.
type TaskView struct {
	Name       string             `json:"name"`
	Created    string             `json:"created,omitempty"`
	Phase      string             `json:"phase"`
	Actor      string             `json:"actor,omitempty"`
	Image      string             `json:"image,omitempty"`
	Suspend    bool               `json:"suspend"`
	Debug      bool               `json:"debug"`
	Env        []EnvView          `json:"env"`
	Resources  *ResourcesView     `json:"resources,omitempty"`
	Workspaces []WorkspaceRefView `json:"workspaces"`
	Gateway    string             `json:"gateway,omitempty"`
	Conditions []ConditionView    `json:"conditions"`
	PanelRun   bool               `json:"panel_run"`
	CanSuspend bool               `json:"can_suspend"`
	CanResume  bool               `json:"can_resume"`
}

func stamp(ts *timestamppb.Timestamp) string {
	if ts == nil {
		return ""
	}
	return ts.AsTime().UTC().Format(time.RFC3339)
}

// NewTaskView copies the allowed fields of t.
func NewTaskView(t *v1alpha1.Task) TaskView {
	spec, st := t.GetSpec(), t.GetStatus()
	v := TaskView{
		Name:       t.GetMetadata().GetName(),
		Created:    stamp(t.GetMetadata().GetCreationTimestamp()),
		Phase:      st.GetPhase(),
		Actor:      st.GetActor(),
		Image:      spec.GetImage(),
		Suspend:    spec.GetSuspend(),
		Debug:      spec.GetDebug(),
		Env:        []EnvView{},
		Workspaces: []WorkspaceRefView{},
		Gateway:    spec.GetGateway().GetName(),
		Conditions: []ConditionView{},
	}
	for _, e := range spec.GetEnv() {
		v.Env = append(v.Env, EnvView{Name: e.GetName(), Value: Hidden})
	}
	if r := spec.GetResources(); r != nil {
		v.Resources = &ResourcesView{
			RequestsCPU: r.GetRequests().GetCpu(), RequestsMemory: r.GetRequests().GetMemory(),
			LimitsCPU: r.GetLimits().GetCpu(), LimitsMemory: r.GetLimits().GetMemory(),
		}
	}
	for _, w := range spec.GetWorkspaces() {
		v.Workspaces = append(v.Workspaces, WorkspaceRefView{Name: w.GetName(), Path: w.GetPath()})
	}
	for _, c := range st.GetConditions() {
		v.Conditions = append(v.Conditions, ConditionView{
			Type: c.GetType(), Status: c.GetStatus(), Reason: c.GetReason(),
			Message: c.GetMessage(), Changed: stamp(c.GetLastTransitionTime()),
		})
	}
	return v
}

// GitView is one repository of a workspace.
type GitView struct {
	Name   string `json:"name,omitempty"`
	Repo   string `json:"repo"`
	Branch string `json:"branch,omitempty"`
	Dir    string `json:"dir,omitempty"`
}

// WorkspaceView is a workspace, read-only.
type WorkspaceView struct {
	Name       string    `json:"name"`
	Created    string    `json:"created,omitempty"`
	Git        []GitView `json:"git"`
	MCPServers int       `json:"mcp_servers"`
}

// NewWorkspaceView copies the allowed fields of w.
func NewWorkspaceView(w *v1alpha1.Workspace) WorkspaceView {
	v := WorkspaceView{
		Name:    w.GetMetadata().GetName(),
		Created: stamp(w.GetMetadata().GetCreationTimestamp()),
		Git:     []GitView{},
	}
	for _, g := range w.GetSpec().GetGit() {
		v.Git = append(v.Git, GitView{
			Name: g.GetName(), Repo: SanitizeURL(g.GetRepo()), Branch: g.GetBranch(), Dir: g.GetDir(),
		})
	}
	v.MCPServers = len(w.GetSpec().GetMcp().GetServers())
	return v
}

// SanitizeURL drops userinfo, query and fragment, where a credential could
// hide.
func SanitizeURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Opaque != "" {
		return Hidden
	}
	u.User, u.RawQuery, u.ForceQuery, u.Fragment, u.RawFragment = nil, "", false, "", ""
	return u.String()
}

// ListenerView and HostView describe a gateway.
type ListenerView struct {
	Name     string `json:"name"`
	Port     int32  `json:"port"`
	Protocol string `json:"protocol"`
}

// HostView is one egress rule.
type HostView struct {
	Host string `json:"host"`
	Port int32  `json:"port"`
}

// GatewayView is a gateway, read-only.
type GatewayView struct {
	Name      string         `json:"name"`
	Created   string         `json:"created,omitempty"`
	Listeners []ListenerView `json:"listeners"`
	Egress    []HostView     `json:"egress"`
}

// NewGatewayView copies the allowed fields of g.
func NewGatewayView(g *v1alpha1.Gateway) GatewayView {
	v := GatewayView{
		Name:      g.GetMetadata().GetName(),
		Created:   stamp(g.GetMetadata().GetCreationTimestamp()),
		Listeners: []ListenerView{},
		Egress:    []HostView{},
	}
	for _, l := range g.GetSpec().GetListeners() {
		v.Listeners = append(v.Listeners, ListenerView{Name: l.GetName(), Port: l.GetPort(), Protocol: l.GetProtocol()})
	}
	for _, h := range g.GetSpec().GetEgress().GetAllowlist().GetHosts() {
		v.Egress = append(v.Egress, HostView{Host: h.GetHost(), Port: h.GetPort()})
	}
	return v
}
