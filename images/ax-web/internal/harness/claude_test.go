package harness

import (
	"math"
	"strings"
	"testing"
	"unicode/utf8"
)

// A Claude Code 2.1.x stream-json session, shaped like the real one
// (extra fields included), one event per line.
var claudeSession = []string{
	`{"type":"system","subtype":"init","cwd":"/workspace/repo","session_id":"4f6a0c1e-8f1b-4a8e-9d55-2b8f0d1a7c33","tools":["Task","Bash","Glob","Grep","Read","Edit","Write","NotebookEdit","WebFetch","TodoWrite","WebSearch"],"mcp_servers":[],"model":"claude-sonnet-5","permissionMode":"bypassPermissions","slash_commands":["compact","context","cost"],"apiKeySource":"none","claude_code_version":"2.1.274","output_style":"default","agents":["general-purpose"],"skills":[],"plugins":[],"uuid":"0b6c1f8e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"}`,
	`{"type":"assistant","message":{"id":"msg_01A","type":"message","role":"assistant","model":"claude-sonnet-5","content":[{"type":"thinking","thinking":"` + strings.Repeat("pienso ", 400) + `","signature":"EqQBCkYIBxgCKkB"}],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":4,"cache_creation_input_tokens":5321,"cache_read_input_tokens":11000,"output_tokens":8,"service_tier":"standard"}},"parent_tool_use_id":null,"session_id":"4f6a0c1e","uuid":"a1"}`,
	`{"type":"assistant","message":{"id":"msg_01B","type":"message","role":"assistant","model":"claude-sonnet-5","content":[{"type":"text","text":"Voy a ejecutar los tests."},{"type":"tool_use","id":"toolu_01","name":"Bash","input":{"command":"go test ./...","description":"Run the tests"}}],"stop_reason":null,"usage":{"input_tokens":4,"output_tokens":60}},"parent_tool_use_id":null,"session_id":"4f6a0c1e","uuid":"a2"}`,
	`{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01","type":"tool_result","content":"ok  \tapptolast.com/demo\t0.012s` + strings.Repeat("x", 400) + `","is_error":false}]},"parent_tool_use_id":null,"session_id":"4f6a0c1e","uuid":"u1","tool_use_result":{"stdout":"ok","stderr":"","interrupted":false,"isImage":false}}`,
	`{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_02","name":"Grep","input":{"pattern":"func main","path":"cmd","output_mode":"files_with_matches"}},{"type":"tool_use","id":"toolu_03","name":"Read","input":{"file_path":"/workspace/repo/main.go","limit":200}},{"type":"tool_use","id":"toolu_04","name":"TodoWrite","input":{"todos":[{"content":"Leer main.go","status":"in_progress","activeForm":"Leyendo"},{"content":"Arreglar","status":"pending","activeForm":"Arreglando"}]}},{"type":"tool_use","id":"toolu_05","name":"mcp__docs__search","input":{"q":"tar","limit":3}}]}}`,
	`{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_02","content":[{"type":"text","text":"Found 1 file"},{"type":"text","text":"cmd/main.go"}]},{"type":"tool_result","tool_use_id":"toolu_03","content":"<tool_use_error>File does not exist.</tool_use_error>","is_error":true}]}}`,
	`{"type":"system","subtype":"compact_boundary","session_id":"4f6a0c1e","compact_metadata":{"trigger":"auto","pre_tokens":150000}}`,
	`{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}`,
	`{"type":"result","subtype":"success","is_error":false,"duration_ms":48213,"duration_api_ms":40122,"num_turns":6,"result":"## Resumen\nTodos los tests pasan.\n## Lecciones\n- ninguna","session_id":"4f6a0c1e","total_cost_usd":0.4213,"usage":{"input_tokens":21,"cache_creation_input_tokens":10234,"cache_read_input_tokens":88123,"output_tokens":1543,"server_tool_use":{"web_search_requests":0,"web_fetch_requests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":10234}},"modelUsage":{"claude-sonnet-5":{"inputTokens":21,"outputTokens":1543,"costUSD":0.4213,"contextWindow":200000}},"permission_denials":[],"uuid":"r1"}`,
}

func feedAll(p Parser, chunks ...string) []Event {
	var out []Event
	for _, c := range chunks {
		out = append(out, p.Feed([]byte(c))...)
	}
	return append(out, p.Flush()...)
}

// splitEvery cuts s into pieces of n bytes, ignoring line and character
// boundaries, as the guest's stream does.
func splitEvery(s string, n int) []string {
	var out []string
	for len(s) > n {
		out = append(out, s[:n])
		s = s[n:]
	}
	return append(out, s)
}

func TestClaudeParserSession(t *testing.T) {
	p := &ClaudeParser{}
	evs := feedAll(p, splitEvery(strings.Join(claudeSession, "\n")+"\n", 37)...)
	type want struct{ kind, tool, input, text string }
	wants := []want{
		{EventInit, "", "", "Sesión iniciada (modelo claude-sonnet-5, 11 herramientas, permisos bypassPermissions)"},
		{EventThinking, "", "", ""},
		{EventText, "", "", "Voy a ejecutar los tests."},
		{EventTool, "Bash", "go test ./...", ""},
		{EventToolResult, "", "", ""},
		{EventTool, "Grep", "func main en cmd", ""},
		{EventTool, "Read", "/workspace/repo/main.go", ""},
		{EventTool, "TodoWrite", "2 tareas: Leer main.go", ""},
		{EventTool, "mcp__docs__search", `{"q":"tar","limit":3}`, ""},
		{EventToolResult, "", "", "Found 1 file\ncmd/main.go"},
		{EventToolResult, "", "", "<tool_use_error>File does not exist.</tool_use_error>"},
		{EventSystem, "", "", "compact_boundary"},
		{EventResult, "", "", "## Resumen\nTodos los tests pasan.\n## Lecciones\n- ninguna"},
	}
	if len(evs) != len(wants) {
		for _, e := range evs {
			t.Logf("%+v", e)
		}
		t.Fatalf("%d events, want %d", len(evs), len(wants))
	}
	for i, w := range wants {
		e := evs[i]
		if e.Kind != w.kind || e.Tool != w.tool || e.Input != w.input || (w.text != "" && e.Text != w.text) {
			t.Errorf("event %d: %+v, want %+v", i, e, w)
		}
	}
	if evs[0].Model != "claude-sonnet-5" {
		t.Errorf("init model %q", evs[0].Model)
	}
	if th := evs[1].Text; len(th) > MaxThinking || !strings.HasSuffix(th, "…") {
		t.Errorf("thinking not bounded: %d bytes", len(th))
	}
	if r := evs[4]; r.OK == nil || !*r.OK || len([]rune(r.Text)) != ToolResultRunes+1 ||
		!strings.HasPrefix(r.Text, "ok  \tapptolast.com/demo") {
		t.Errorf("tool result %+v", r)
	}
	if r := evs[9]; r.OK == nil || !*r.OK {
		t.Errorf("text block result %+v", r)
	}
	if r := evs[10]; r.OK == nil || *r.OK {
		t.Errorf("error result %+v", r)
	}
	res := evs[12]
	if res.CostUSD != 0.4213 || res.InTokens != 21 || res.OutTokens != 1543 || res.CacheRead != 88123 ||
		res.CacheMade != 10234 || res.Turns != 6 || res.Millis != 48213 {
		t.Errorf("result %+v", res)
	}
	f := p.Final()
	if !f.Saw || f.IsError || f.ResultText != res.Text || f.Usage.ModelUsed != "claude-sonnet-5" ||
		f.Usage.CostUSD != 0.4213 || f.Usage.InTokens != 21 || f.Usage.OutTokens != 1543 ||
		f.Usage.CacheRead != 88123 || f.Usage.CacheMade != 10234 || f.Usage.Turns != 6 || f.Usage.DurationMS != 48213 {
		t.Errorf("final %+v", f)
	}
}

func TestClaudeParserEdges(t *testing.T) {
	p := &ClaudeParser{}
	evs := feedAll(p,
		"plain text line\n",
		`{"no":"type"}`+"\n",
		"\n   \n",
		`{"type":"assistant","message":{"content":"a string, not blocks"}}`+"\n",
		`{"type":"assistant","message":{"content":[{"type":"redacted_thinking","data":"x"},{"type":"text","text":"   "}]}}`+"\n",
		`{"type":"user","message":{"content":"the prompt"}}`+"\n",
		`{"type":"system","subtype":"init","model":7,"tools":"x"}`+"\n",
		`{"type":"result","subtype":"error_max_turns","is_error":false,"num_turns":"many","total_cost_usd":0.25}`,
	)
	if len(evs) != 4 || evs[0].Kind != EventText || evs[0].Text != "plain text line" ||
		evs[1].Kind != EventText || evs[1].Text != `{"no":"type"}` ||
		evs[2].Kind != EventInit || evs[2].Text != "Sesión iniciada (modelo desconocido, 0 herramientas, permisos desconocido)" ||
		evs[3].Kind != EventResult || evs[3].CostUSD != 0.25 {
		for _, e := range evs {
			t.Logf("%+v", e)
		}
		t.Fatalf("%d events", len(evs))
	}
	if f := p.Final(); !f.Saw || !f.IsError || f.ResultText != "" || f.Usage.CostUSD != 0.25 {
		t.Fatalf("final %+v", f)
	}
}

func TestClaudeParserBounds(t *testing.T) {
	p := &ClaudeParser{}
	huge := `{"type":"user","message":{"content":[{"type":"tool_result","content":"` +
		strings.Repeat("y", MaxLine) + `"}]}}`
	big := strings.Repeat("é", MaxResultText) // twice MaxResultText bytes
	evs := feedAll(p,
		huge[:MaxLine/2], huge[MaxLine/2:]+"\n",
		`{"type":"assistant","message":{"content":[{"type":"text","text":"después"}]}}`+"\n",
		`{"type":"result","subtype":"success","result":"`+big+`"}`+"\n",
	)
	if len(evs) != 3 || evs[0].Kind != EventSystem || evs[0].Text != tooLong ||
		evs[1].Text != "después" || evs[2].Kind != EventResult || len(evs[2].Text) > MaxEventText {
		t.Fatalf("%d events: %+v", len(evs), evs[0])
	}
	f := p.Final()
	if len(f.ResultText) > MaxResultText || len(f.ResultText) < MaxResultText-1 ||
		!strings.HasPrefix(big, f.ResultText) {
		t.Fatalf("result text %d bytes", len(f.ResultText))
	}
	// A huge tool input is summarized and bounded.
	long := strings.Repeat("ls; ", 1000)
	evs = (&ClaudeParser{}).Feed([]byte(`{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"` + long + `"}}]}}` + "\n"))
	if len(evs) != 1 || len(evs[0].Input) > MaxEventInput || !strings.HasPrefix(evs[0].Input, "ls; ls;") {
		t.Fatalf("%+v", evs)
	}
}

// The init event and the usage are the agent's own output: a huge or
// odd model name, a huge permission mode or absurd numbers are bounded.
func TestClaudeParserSanitizesInitAndUsage(t *testing.T) {
	p := &ClaudeParser{}
	evs := feedAll(p,
		`{"type":"system","subtype":"init","model":"`+strings.Repeat("m", 500)+`","permissionMode":"`+strings.Repeat("p", 500)+`"}`+"\n",
		`{"type":"result","subtype":"success","result":"ok","total_cost_usd":-3,"num_turns":-2,"duration_ms":-1,`+
			`"usage":{"input_tokens":-5,"output_tokens":99999999999999999,"cache_read_input_tokens":1e30}}`+"\n",
	)
	if len(evs) != 2 || len(evs[0].Model) > MaxModelBytes || len(evs[0].Text) > 300 {
		t.Fatalf("%+v", evs)
	}
	f := p.Final()
	if f.Usage.ModelUsed != "" {
		t.Fatalf("an invalid model became ModelUsed: %q", f.Usage.ModelUsed)
	}
	if f.Usage.CostUSD != 0 || f.Usage.Turns != 0 || f.Usage.DurationMS != 0 || f.Usage.InTokens != 0 ||
		f.Usage.OutTokens != MaxCount || evs[1].OutTokens != MaxCount || evs[1].CostUSD != 0 {
		t.Fatalf("usage %+v, event %+v", f.Usage, evs[1])
	}
	q := &ClaudeParser{}
	feedAll(q, `{"type":"system","subtype":"init","model":"claude-opus-5-5"}`+"\n",
		`{"type":"result","result":"x","total_cost_usd":123456789}`+"\n")
	if f := q.Final(); f.Usage.ModelUsed != "claude-opus-5-5" || f.Usage.CostUSD != MaxCostUSD {
		t.Fatalf("%+v", f.Usage)
	}
	// A model name with a newline or a space is shown bounded, never kept.
	r := &ClaudeParser{}
	feedAll(r, `{"type":"system","subtype":"init","model":"opus\nrm -rf"}`+"\n")
	if f := r.Final(); f.Usage.ModelUsed != "" {
		t.Fatalf("%q", f.Usage.ModelUsed)
	}
}

func TestSanitizeUsage(t *testing.T) {
	u := SanitizeUsage(Usage{CostUSD: math.NaN(), InTokens: -1, OutTokens: math.MaxInt64, CacheRead: 7,
		CacheMade: MaxCount + 1, Turns: -4, DurationMS: 12, ModelUsed: strings.Repeat("é", 100)})
	if u.CostUSD != 0 || u.InTokens != 0 || u.OutTokens != MaxCount || u.CacheRead != 7 || u.CacheMade != MaxCount ||
		u.Turns != 0 || u.DurationMS != 12 || len(u.ModelUsed) > MaxModelBytes || !utf8.ValidString(u.ModelUsed) {
		t.Fatalf("%+v", u)
	}
	if u := SanitizeUsage(Usage{CostUSD: math.Inf(1)}); u.CostUSD != MaxCostUSD {
		t.Fatalf("%+v", u)
	}
	if u := SanitizeUsage(Usage{CostUSD: math.Inf(-1)}); u.CostUSD != 0 {
		t.Fatalf("%+v", u)
	}
}
