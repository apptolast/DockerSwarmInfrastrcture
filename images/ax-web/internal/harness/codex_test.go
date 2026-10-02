package harness

import (
	"strings"
	"testing"
)

// A codex exec --json session (Codex 0.156.x event shapes).
var codexSession = []string{
	`{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}`,
	`{"type":"turn.started"}`,
	`{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"**Explorando el repositorio**\n\nVoy a listar los ficheros."}}`,
	`{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","aggregated_output":"","exit_code":null,"status":"in_progress"}}`,
	`{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","aggregated_output":"README.md\nmain.go\n","exit_code":0,"status":"completed"}}`,
	`{"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"bash -lc 'go test ./...'","aggregated_output":"","exit_code":null,"status":"in_progress"}}`,
	`{"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"bash -lc 'go test ./...'","aggregated_output":"--- FAIL: TestX","exit_code":1,"status":"failed"}}`,
	`{"type":"item.completed","item":{"id":"item_3","type":"file_change","changes":[{"path":"/workspace/repo/main.go","kind":"update"},{"path":"/workspace/repo/x_test.go","kind":"add"}],"status":"completed"}}`,
	`{"type":"item.completed","item":{"id":"item_4","type":"todo_list","items":[{"text":"Leer el código","completed":true},{"text":"Escribir tests","completed":false}]}}`,
	`{"type":"item.completed","item":{"id":"item_5","type":"mcp_tool_call","server":"docs","tool":"search","arguments":{"q":"x"},"result":null,"error":null,"status":"completed"}}`,
	`{"type":"item.completed","item":{"id":"item_6","type":"web_search","query":"golang archive/tar"}}`,
	`{"type":"error","message":"Reconnecting... 1/5"}`,
	`{"type":"item.completed","item":{"id":"item_7","type":"agent_message","text":"Primer mensaje."}}`,
	`{"type":"item.completed","item":{"id":"item_8","type":"error","message":"aviso no fatal"}}`,
	`{"type":"item.completed","item":{"id":"item_9","type":"image_view","path":"x.png"}}`,
	`{"type":"item.updated","item":{"id":"item_4","type":"todo_list","items":[]}}`,
	`{"type":"item.completed","item":{"id":"item_10","type":"agent_message","text":"## Resumen\nHecho.\n## Lecciones\n- ninguna"}}`,
	`{"type":"turn.completed","usage":{"input_tokens":24763,"cached_input_tokens":24448,"output_tokens":122,"reasoning_output_tokens":64}}`,
}

func TestCodexParserSession(t *testing.T) {
	p := NewParser(Codex)
	evs := feedAll(p, splitEvery(strings.Join(codexSession, "\n"), 29)...) // no final newline
	type want struct{ kind, tool, input, text string }
	wants := []want{
		{EventInit, "", "", "Sesión de Codex iniciada"},
		{EventThinking, "", "", "**Explorando el repositorio**\n\nVoy a listar los ficheros."},
		{EventTool, "Bash", "bash -lc ls", ""},
		{EventToolResult, "", "", "README.md\nmain.go\n"},
		{EventTool, "Bash", "bash -lc 'go test ./...'", ""},
		{EventToolResult, "", "", "--- FAIL: TestX"},
		{EventTool, "Edit", "/workspace/repo/main.go, /workspace/repo/x_test.go", ""},
		{EventTodo, "", "", "[x] Leer el código\n[ ] Escribir tests"},
		{EventTool, "docs/search", "", ""},
		{EventTool, "WebSearch", "golang archive/tar", ""},
		{EventError, "", "", "Reconnecting... 1/5"},
		{EventText, "", "", "Primer mensaje."},
		{EventError, "", "", "aviso no fatal"},
		{EventSystem, "", "", "image_view"},
		{EventText, "", "", "## Resumen\nHecho.\n## Lecciones\n- ninguna"},
		{EventUsage, "", "", ""},
	}
	if len(evs) != len(wants) {
		for _, e := range evs {
			t.Logf("%+v", e)
		}
		t.Fatalf("%d events, want %d", len(evs), len(wants))
	}
	for i, w := range wants {
		e := evs[i]
		if e.Kind != w.kind || e.Tool != w.tool || e.Input != w.input || e.Text != w.text {
			t.Errorf("event %d: %+v, want %+v", i, e, w)
		}
	}
	if ok := evs[3].OK; ok == nil || !*ok {
		t.Error("exit 0 is OK")
	}
	if ok := evs[5].OK; ok == nil || *ok {
		t.Error("exit 1 is not OK")
	}
	u := evs[15]
	if u.InTokens != 24763 || u.CacheRead != 24448 || u.OutTokens != 186 || u.Turns != 1 {
		t.Errorf("usage %+v", u)
	}
	f := p.Final()
	// The error event was followed by a completed turn: not an error.
	if !f.Saw || f.IsError || f.ResultText != "## Resumen\nHecho.\n## Lecciones\n- ninguna" ||
		f.Usage.InTokens != 24763 || f.Usage.OutTokens != 186 || f.Usage.Turns != 1 || f.Usage.CostUSD != 0 {
		t.Errorf("final %+v", f)
	}
}

func TestCodexParserAccumulatesAndFails(t *testing.T) {
	p := &CodexParser{}
	evs := feedAll(p,
		`{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":3}}`+"\n",
		`{"type":"turn.completed","usage":{"input_tokens":5,"cached_input_tokens":1,"output_tokens":2,"reasoning_output_tokens":1}}`+"\n",
		`{"type":"turn.failed","error":{"message":"stream disconnected before completion"}}`+"\n",
	)
	if len(evs) != 3 || evs[1].InTokens != 15 || evs[1].CacheRead != 5 || evs[1].OutTokens != 6 || evs[1].Turns != 2 ||
		evs[2].Kind != EventError || evs[2].Text != "stream disconnected before completion" {
		t.Fatalf("%+v", evs)
	}
	if f := p.Final(); !f.IsError || !f.Saw || f.Usage.Turns != 2 || f.ResultText != "" {
		t.Fatalf("final %+v", f)
	}
	// A lone error event with no completed turn after it is an error.
	q := &CodexParser{}
	feedAll(q, `{"type":"thread.started"}`+"\n"+`{"type":"error","message":"unexpected status 401 Unauthorized"}`+"\n")
	if f := q.Final(); !f.IsError || f.Saw {
		t.Fatalf("final %+v", f)
	}
}

func TestCodexParserTolerance(t *testing.T) {
	p := &CodexParser{}
	evs := feedAll(p,
		"Reading prompt from stdin...\n",
		`{"type":"item.completed","item":{"id":"i","item_type":"agent_message","text":"antiguo"}}`+"\n",
		`{"type":"item.started","item":{"type":"command_execution","command":["bash","-lc","ls"]}}`+"\n",
		`{"type":"item.completed","item":{"type":"command_execution","aggregated_output":5,"exit_code":"x"}}`+"\n",
		`{"type":"item.completed","item":"not an object"}`+"\n",
		`{"type":"item.completed","item":{"type":"file_change","changes":"x"}}`+"\n",
		`{"type":"turn.completed","usage":{"input_tokens":"many"}}`+"\n",
		`{"type":"something.new","payload":[1,2,3]}`+"\n",
		`{"type":"turn.failed"}`+"\n",
		`{broken json`+"\n",
	)
	kinds := []string{EventText, EventText, EventTool, EventToolResult, EventTool, EventUsage, EventError, EventText}
	if len(evs) != len(kinds) {
		for _, e := range evs {
			t.Logf("%+v", e)
		}
		t.Fatalf("%d events", len(evs))
	}
	for i, k := range kinds {
		if evs[i].Kind != k {
			t.Errorf("event %d: %+v, want %s", i, evs[i], k)
		}
	}
	if evs[2].Input != "bash -lc ls" || evs[3].OK != nil || evs[6].Text != "el turno falló" {
		t.Errorf("%+v %+v %+v", evs[2], evs[3], evs[6])
	}
	if f := p.Final(); f.ResultText != "antiguo" || !f.IsError || f.Usage.Turns != 1 {
		t.Errorf("final %+v", f)
	}
}

// Codex's counters are clamped: negative or absurd values never reach the
// totals, and the totals never overflow.
func TestCodexParserClampsUsage(t *testing.T) {
	p := &CodexParser{}
	feedAll(p,
		`{"type":"turn.completed","usage":{"input_tokens":-100,"cached_input_tokens":1e300,"output_tokens":5}}`+"\n",
		`{"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":1e300,"output_tokens":1e19}}`+"\n",
	)
	f := p.Final()
	if f.Usage.InTokens != 7 || f.Usage.CacheRead != MaxCount || f.Usage.OutTokens != MaxCount || f.Usage.Turns != 2 {
		t.Fatalf("%+v", f.Usage)
	}
}
