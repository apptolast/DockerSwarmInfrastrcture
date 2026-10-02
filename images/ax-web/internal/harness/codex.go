package harness

import (
	"bytes"
	"encoding/json"
	"strings"
)

// CodexParser reads `codex exec --json` (JSONL thread events). Only the
// documented fields are read and every one of them leniently: an unknown
// event or item type, a missing field or one of another type is skipped,
// never fatal.
type CodexParser struct {
	lines lineBuffer
	usage Usage
	last  string // the last agent message
	saw   bool
	// failed is set by turn.failed; pending by an error event that no
	// later turn.completed superseded (Codex reports retried stream
	// errors as error events and then completes the turn).
	failed  bool
	pending bool
}

type codexObject map[string]json.RawMessage

func (o codexObject) str(key string) string {
	var s string
	if raw, ok := o[key]; ok {
		_ = json.Unmarshal(raw, &s)
	}
	return s
}

func (o codexObject) obj(key string) codexObject {
	var m codexObject
	if raw, ok := o[key]; ok && len(raw) > 0 && raw[0] == '{' {
		_ = json.Unmarshal(raw, &m)
	}
	return m
}

func (o codexObject) list(key string) []codexObject {
	var l []json.RawMessage
	if raw, ok := o[key]; ok && len(raw) > 0 && raw[0] == '[' {
		_ = json.Unmarshal(raw, &l)
	}
	var out []codexObject
	for _, raw := range l {
		var m codexObject
		if len(raw) > 0 && raw[0] == '{' && json.Unmarshal(raw, &m) == nil {
			out = append(out, m)
		}
	}
	return out
}

// number reads a counter, bounded by countOf (a negative or absurd
// value from the agent's output never reaches the totals).
func (o codexObject) number(key string) (int64, bool) {
	var f *float64
	raw, ok := o[key]
	if !ok || json.Unmarshal(raw, &f) != nil || f == nil {
		return 0, false
	}
	return countOf(*f), true
}

func (o codexObject) boolean(key string) bool {
	var b bool
	if raw, ok := o[key]; ok {
		_ = json.Unmarshal(raw, &b)
	}
	return b
}

// command is a command_execution's command: a string, or an argv.
func (o codexObject) command() string {
	if s := o.str("command"); s != "" {
		return s
	}
	var argv []string
	if raw, ok := o["command"]; ok && json.Unmarshal(raw, &argv) == nil {
		return strings.Join(argv, " ")
	}
	return ""
}

// message is an error's text: "message", or "error" as a string or an
// object with a message.
func (o codexObject) message() string {
	if s := o.str("message"); s != "" {
		return s
	}
	if s := o.str("error"); s != "" {
		return s
	}
	return o.obj("error").str("message")
}

// Feed implements Parser.
func (p *CodexParser) Feed(data []byte) []Event {
	var out []Event
	p.lines.feed(data, func(line []byte) { out = append(out, p.line(line)...) },
		func() { out = append(out, Event{Kind: EventSystem, Text: tooLong}) })
	return out
}

// Flush implements Parser.
func (p *CodexParser) Flush() []Event {
	var out []Event
	p.lines.flush(func(line []byte) { out = append(out, p.line(line)...) })
	return out
}

// Final implements Parser. Codex reports no cost and no model.
func (p *CodexParser) Final() Final {
	return Final{ResultText: p.last, IsError: p.failed || p.pending, Usage: SanitizeUsage(p.usage), Saw: p.saw}
}

func (p *CodexParser) line(raw []byte) []Event {
	line := bytes.TrimSpace(raw)
	if len(line) == 0 {
		return nil
	}
	var ev codexObject
	if !isObject(line) || json.Unmarshal(line, &ev) != nil {
		return []Event{textEvent(line)}
	}
	switch ev.str("type") {
	case "thread.started":
		return []Event{{Kind: EventInit, Text: "Sesión de Codex iniciada"}}
	case "turn.completed":
		u := ev.obj("usage")
		in, _ := u.number("input_tokens")
		cached, _ := u.number("cached_input_tokens")
		outTokens, _ := u.number("output_tokens")
		reasoning, _ := u.number("reasoning_output_tokens")
		// Each term is at most MaxCount, so the sums cannot overflow
		// before they are clamped again.
		p.usage.InTokens = clampCount(p.usage.InTokens + in)
		p.usage.CacheRead = clampCount(p.usage.CacheRead + cached)
		p.usage.OutTokens = clampCount(p.usage.OutTokens + outTokens + reasoning)
		p.usage.Turns = int(clampCount(int64(p.usage.Turns) + 1))
		p.saw, p.pending = true, false
		// The usage event carries the running totals.
		return []Event{{Kind: EventUsage, InTokens: p.usage.InTokens, CacheRead: p.usage.CacheRead,
			OutTokens: p.usage.OutTokens, Turns: p.usage.Turns}}
	case "turn.failed":
		p.saw, p.failed = true, true
		return []Event{{Kind: EventError, Text: clip(orDefault(ev.message(), "el turno falló"), MaxEventText)}}
	case "error":
		p.pending = true
		return []Event{{Kind: EventError, Text: clip(orDefault(ev.message(), "error"), MaxEventText)}}
	case "item.started":
		item := ev.obj("item")
		if itemType(item) == "command_execution" {
			return []Event{{Kind: EventTool, Tool: "Bash", Input: clip(item.command(), MaxEventInput)}}
		}
	case "item.completed":
		return p.completed(ev.obj("item"))
	}
	return nil
}

// itemType reads "type", or "item_type" as early Codex versions named it.
func itemType(item codexObject) string {
	if t := item.str("type"); t != "" {
		return t
	}
	return item.str("item_type")
}

func (p *CodexParser) completed(item codexObject) []Event {
	if item == nil {
		return nil
	}
	switch t := itemType(item); t {
	case "agent_message":
		text := item.str("text")
		if strings.TrimSpace(text) == "" {
			return nil
		}
		p.last = cut(text, MaxResultText)
		return []Event{{Kind: EventText, Text: clip(text, MaxEventText)}}
	case "reasoning":
		if text := item.str("text"); strings.TrimSpace(text) != "" {
			return []Event{{Kind: EventThinking, Text: clip(text, MaxThinking)}}
		}
	case "command_execution":
		ev := Event{Kind: EventToolResult, Text: firstRunes(item.str("aggregated_output"), ToolResultRunes)}
		if code, ok := item.number("exit_code"); ok {
			ev.OK = boolPtr(code == 0)
		}
		return []Event{ev}
	case "file_change":
		var paths []string
		for _, c := range item.list("changes") {
			if path := c.str("path"); path != "" {
				paths = append(paths, path)
			}
		}
		return []Event{{Kind: EventTool, Tool: "Edit", Input: clip(strings.Join(paths, ", "), MaxEventInput)}}
	case "mcp_tool_call":
		return []Event{{Kind: EventTool, Tool: clip(item.str("server")+"/"+item.str("tool"), MaxModelBytes)}}
	case "web_search":
		return []Event{{Kind: EventTool, Tool: "WebSearch", Input: clip(item.str("query"), MaxEventInput)}}
	case "todo_list":
		var lines []string
		for _, it := range item.list("items") {
			mark := "[ ] "
			if it.boolean("completed") {
				mark = "[x] "
			}
			lines = append(lines, mark+it.str("text"))
		}
		return []Event{{Kind: EventTodo, Text: clip(strings.Join(lines, "\n"), MaxEventText)}}
	case "error":
		// A non-fatal error item: the turn goes on.
		return []Event{{Kind: EventError, Text: clip(orDefault(item.message(), "error"), MaxEventText)}}
	case "":
		return nil
	default:
		return []Event{{Kind: EventSystem, Text: clip(t, MaxEventInput)}}
	}
	return nil
}

func orDefault(s, def string) string {
	if strings.TrimSpace(s) == "" {
		return def
	}
	return s
}
