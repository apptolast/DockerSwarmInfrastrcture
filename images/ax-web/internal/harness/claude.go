package harness

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// ClaudeParser reads Claude Code's `--output-format stream-json` (one JSON
// event per line). Tool inputs and results are summarized and bounded,
// never echoed in full.
type ClaudeParser struct {
	lines lineBuffer
	model string
	final Final
}

type claudeLine struct {
	Type           string            `json:"type"`
	Subtype        string            `json:"subtype"`
	Model          string            `json:"model"`
	Tools          []json.RawMessage `json:"tools"`
	PermissionMode string            `json:"permissionMode"`
	Message        *struct {
		Content json.RawMessage `json:"content"`
	} `json:"message"`
	Result       *string `json:"result"`
	IsError      bool    `json:"is_error"`
	TotalCostUSD float64 `json:"total_cost_usd"`
	Usage        *struct {
		InputTokens   int64 `json:"input_tokens"`
		OutputTokens  int64 `json:"output_tokens"`
		CacheRead     int64 `json:"cache_read_input_tokens"`
		CacheCreation int64 `json:"cache_creation_input_tokens"`
	} `json:"usage"`
	NumTurns   int              `json:"num_turns"`
	DurationMS int64            `json:"duration_ms"`
	RateLimit  *claudeRateLimit `json:"rate_limit_info"`
}

// claudeRateLimit is a rate_limit_event. Only the unified windows are read;
// the rest of the event says nothing the gate needs.
type claudeRateLimit struct {
	Unified *struct {
		FiveHour *claudeWindow `json:"five_hour"`
		SevenDay *claudeWindow `json:"seven_day"`
	} `json:"unifiedWindows"`
}

type claudeWindow struct {
	Utilization float64 `json:"utilization"`
	ResetsAt    int64   `json:"resetsAt"`
}

// observeWindows keeps the latest pair of windows. An event that lacks
// either window says nothing new about the account.
func (p *ClaudeParser) observeWindows(rl *claudeRateLimit) {
	if rl == nil || rl.Unified == nil || rl.Unified.FiveHour == nil || rl.Unified.SevenDay == nil {
		return
	}
	p.final.Windows = &UsageWindows{
		FiveHour: windowOf(*rl.Unified.FiveHour),
		SevenDay: windowOf(*rl.Unified.SevenDay),
	}
}

func windowOf(w claudeWindow) UsageWindow {
	return UsageWindow{Utilization: w.Utilization, ResetsAt: time.Unix(w.ResetsAt, 0)}
}

type claudeBlock struct {
	Type     string          `json:"type"`
	Text     string          `json:"text"`
	Thinking string          `json:"thinking"`
	Name     string          `json:"name"`
	Input    json.RawMessage `json:"input"`
	Content  json.RawMessage `json:"content"`
	IsError  bool            `json:"is_error"`
}

// Feed implements Parser.
func (p *ClaudeParser) Feed(data []byte) []Event {
	var out []Event
	p.lines.feed(data, func(line []byte) { out = append(out, p.line(line)...) },
		func() { out = append(out, Event{Kind: EventSystem, Text: tooLong}) })
	return out
}

// Flush implements Parser.
func (p *ClaudeParser) Flush() []Event {
	var out []Event
	p.lines.flush(func(line []byte) { out = append(out, p.line(line)...) })
	return out
}

// Final implements Parser. ModelUsed is the model the init event named.
func (p *ClaudeParser) Final() Final {
	f := p.final
	f.Usage.ModelUsed = p.model
	f.Usage = SanitizeUsage(f.Usage)
	return f
}

func (p *ClaudeParser) line(raw []byte) []Event {
	line := bytes.TrimSpace(raw)
	if len(line) == 0 {
		return nil
	}
	var ev claudeLine
	if !isObject(line) || !decodeJSON(line, &ev) || ev.Type == "" {
		return []Event{textEvent(line)}
	}
	switch ev.Type {
	case "system":
		if ev.Subtype == "init" {
			// The init event is the agent's own output: only a model name
			// that could have been a --model value is kept as ModelUsed,
			// and both texts are bounded.
			model, perm := clip(ev.Model, MaxModelBytes), clip(ev.PermissionMode, maxPermissionBytes)
			if modelPattern.MatchString(ev.Model) {
				p.model = ev.Model
			}
			return []Event{{Kind: EventInit, Model: model, Text: fmt.Sprintf(
				"Sesión iniciada (modelo %s, %d herramientas, permisos %s)",
				orUnknown(model), len(ev.Tools), orUnknown(perm))}}
		}
		if ev.Subtype != "" {
			return []Event{{Kind: EventSystem, Text: clip(ev.Subtype, MaxEventInput)}}
		}
	case "assistant":
		return assistantEvents(ev)
	case "user":
		return toolResults(ev)
	case "result":
		return []Event{p.result(ev)}
	case "rate_limit_event":
		p.observeWindows(ev.RateLimit)
		return nil
	}
	// Other event types (partial messages, rate limit notices...) carry
	// nothing the timeline shows.
	return nil
}

func (p *ClaudeParser) result(ev claudeLine) Event {
	text := ""
	if ev.Result != nil {
		text = *ev.Result
	}
	u := Usage{CostUSD: ev.TotalCostUSD, Turns: ev.NumTurns, DurationMS: ev.DurationMS}
	if ev.Usage != nil {
		u.InTokens, u.OutTokens = ev.Usage.InputTokens, ev.Usage.OutputTokens
		u.CacheRead, u.CacheMade = ev.Usage.CacheRead, ev.Usage.CacheCreation
	}
	u = SanitizeUsage(u)
	// A run cut by --max-turns or an execution error ends with an error
	// subtype; is_error is not always set on those.
	p.final = Final{
		ResultText: cut(text, MaxResultText),
		IsError:    ev.IsError || strings.HasPrefix(ev.Subtype, "error"),
		Usage:      u,
		Saw:        true,
	}
	return Event{
		Kind: EventResult, Text: clip(text, MaxEventText), CostUSD: u.CostUSD,
		InTokens: u.InTokens, OutTokens: u.OutTokens, CacheRead: u.CacheRead, CacheMade: u.CacheMade,
		Turns: u.Turns, Millis: u.DurationMS,
	}
}

func blocks(ev claudeLine) []claudeBlock {
	if ev.Message == nil || len(ev.Message.Content) == 0 || ev.Message.Content[0] != '[' {
		return nil
	}
	var bs []claudeBlock
	if !decodeJSON(ev.Message.Content, &bs) {
		return nil
	}
	return bs
}

func assistantEvents(ev claudeLine) []Event {
	var out []Event
	for _, b := range blocks(ev) {
		switch b.Type {
		case "text":
			if strings.TrimSpace(b.Text) != "" {
				out = append(out, Event{Kind: EventText, Text: clip(b.Text, MaxEventText)})
			}
		case "thinking":
			if strings.TrimSpace(b.Thinking) != "" {
				out = append(out, Event{Kind: EventThinking, Text: clip(b.Thinking, MaxThinking)})
			}
		case "tool_use":
			out = append(out, Event{Kind: EventTool, Tool: clip(b.Name, 100),
				Input: clip(toolSummary(b.Name, b.Input), MaxEventInput)})
		}
	}
	return out
}

func toolResults(ev claudeLine) []Event {
	var out []Event
	for _, b := range blocks(ev) {
		if b.Type != "tool_result" {
			continue
		}
		out = append(out, Event{Kind: EventToolResult, OK: boolPtr(!b.IsError),
			Text: firstRunes(resultText(b.Content), ToolResultRunes)})
	}
	return out
}

// resultText is a tool result's content: a string or text blocks.
func resultText(raw json.RawMessage) string {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 {
		return ""
	}
	if raw[0] == '"' {
		var s string
		_ = json.Unmarshal(raw, &s)
		return s
	}
	var bs []claudeBlock
	if raw[0] != '[' || !decodeJSON(raw, &bs) {
		return ""
	}
	var parts []string
	for _, b := range bs {
		if b.Type == "text" && b.Text != "" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "\n")
}

// toolSummary is the one field that tells what a tool call does, or the
// compact JSON of its input for tools it does not know.
func toolSummary(name string, input json.RawMessage) string {
	var in map[string]any
	_ = decodeJSON(input, &in)
	str := func(key string) string {
		v, _ := in[key].(string)
		return v
	}
	var s string
	switch name {
	case "Bash":
		s = str("command")
	case "Read", "Edit", "Write", "MultiEdit":
		s = str("file_path")
	case "NotebookEdit":
		if s = str("file_path"); s == "" {
			s = str("notebook_path")
		}
	case "Grep":
		if s = str("pattern"); s != "" && str("path") != "" {
			s += " en " + str("path")
		}
	case "Glob":
		s = str("pattern")
	case "Task", "Agent":
		s = str("description")
	case "WebFetch":
		s = str("url")
	case "WebSearch":
		s = str("query")
	case "TodoWrite":
		s = todoSummary(in["todos"])
	}
	if s != "" {
		return s
	}
	var compact bytes.Buffer
	if json.Compact(&compact, input) != nil {
		return ""
	}
	return compact.String()
}

func todoSummary(v any) string {
	todos, _ := v.([]any)
	if len(todos) == 0 {
		return ""
	}
	first := ""
	if m, ok := todos[0].(map[string]any); ok {
		first, _ = m["content"].(string)
	}
	noun := "tareas"
	if len(todos) == 1 {
		noun = "tarea"
	}
	if first == "" {
		return fmt.Sprintf("%d %s", len(todos), noun)
	}
	return fmt.Sprintf("%d %s: %s", len(todos), noun, first)
}

func orUnknown(s string) string {
	if s == "" {
		return "desconocido"
	}
	return s
}
