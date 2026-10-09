package harness

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"unicode/utf8"
)

// Parser turns an agent CLI's stdout into events. Feed takes stdout
// chunks as they arrive (lines may be split across chunks) and returns
// the events of every complete line; Flush handles a last line without a
// newline once the process ended; Final is what the session reported
// about itself, valid after Flush.
type Parser interface {
	Feed([]byte) []Event
	Flush() []Event
	Final() Final
}

// Final is the outcome a session reported: the full final answer
// (bounded by MaxResultText), whether the agent called it an error, its
// usage, and Saw, true once a result event (Claude) or a finished turn
// (Codex) was seen.
type Final struct {
	ResultText string
	IsError    bool
	Usage      Usage
	Saw        bool
	// Windows are the subscription windows last reported, nil if none.
	Windows *UsageWindows
	// ModelCost is the cost of each model in the run, by name.
	ModelCost map[string]float64
}

// NewParser returns the parser of a harness. Anything but Codex gets the
// Claude parser, which passes lines it does not know through as text.
func NewParser(harness string) Parser {
	if harness == Codex {
		return &CodexParser{}
	}
	return &ClaudeParser{}
}

// Bounds of the parsers.
const (
	// MaxLine bounds one line of agent output; a longer one (a huge tool
	// result) is dropped with a notice instead of being buffered.
	MaxLine = 1 << 20
	// MaxThinking bounds a reasoning event's text.
	MaxThinking = 1 << 10
	// ToolResultRunes is how much of a tool's output an event keeps.
	ToolResultRunes = 300
)

// Bounds of what a session reports about itself: an agent's output is
// untrusted, so its usage numbers are clamped before anything sums them
// or writes them as JSON (which has no NaN or infinity).
const (
	// MaxModelBytes bounds a model name in an event or in Usage.
	MaxModelBytes = 128
	// MaxCostUSD bounds the cost of one session.
	MaxCostUSD = 1e4
	// MaxCount bounds token, turn and millisecond counters.
	MaxCount           = 1 << 40
	maxPermissionBytes = 64
)

// SanitizeUsage clamps a session's usage: the cost to a finite 0 to
// MaxCostUSD, every counter to 0 to MaxCount, the model name to
// MaxModelBytes.
func SanitizeUsage(u Usage) Usage {
	switch {
	case math.IsNaN(u.CostUSD) || u.CostUSD < 0:
		u.CostUSD = 0
	case u.CostUSD > MaxCostUSD: // +Inf included
		u.CostUSD = MaxCostUSD
	}
	u.InTokens, u.OutTokens = clampCount(u.InTokens), clampCount(u.OutTokens)
	u.CacheRead, u.CacheMade = clampCount(u.CacheRead), clampCount(u.CacheMade)
	u.DurationMS = clampCount(u.DurationMS)
	u.Turns = int(clampCount(int64(u.Turns)))
	if len(u.ModelUsed) > MaxModelBytes {
		u.ModelUsed = cut(u.ModelUsed, MaxModelBytes)
	}
	return u
}

func clampCount(n int64) int64 { return min(max(n, 0), MaxCount) }

// countOf converts a JSON number to a bounded counter: NaN, infinities
// and negatives are 0, anything over MaxCount is MaxCount.
func countOf(f float64) int64 {
	switch {
	case math.IsNaN(f) || f <= 0:
		return 0
	case f >= MaxCount:
		return MaxCount
	}
	return int64(f)
}

// tooLong is the notice for a dropped line.
const tooLong = "evento demasiado largo, omitido"

// lineBuffer splits a byte stream into lines of at most MaxLine bytes.
type lineBuffer struct {
	pending  []byte
	skipping bool
}

// feed calls line for every complete line and dropped once per line that
// was too long.
func (b *lineBuffer) feed(data []byte, line func([]byte), dropped func()) {
	for len(data) > 0 {
		i := bytes.IndexByte(data, '\n')
		if i < 0 {
			if !b.skipping {
				b.pending = append(b.pending, data...)
				if len(b.pending) > MaxLine {
					b.pending, b.skipping = nil, true
					dropped()
				}
			}
			return
		}
		chunk := data[:i]
		data = data[i+1:]
		if b.skipping {
			b.skipping = false
			continue
		}
		if len(b.pending) > 0 {
			if len(b.pending)+len(chunk) > MaxLine {
				b.pending = nil
				dropped()
				continue
			}
			chunk = append(b.pending, chunk...)
			b.pending = nil
		} else if len(chunk) > MaxLine {
			dropped()
			continue
		}
		line(chunk)
	}
}

// flush calls line for what is left without a final newline.
func (b *lineBuffer) flush(line func([]byte)) {
	rest := b.pending
	b.pending, b.skipping = nil, false
	if len(rest) > 0 {
		line(rest)
	}
}

// decodeJSON unmarshals a JSON object leniently: a field of an unexpected
// type is skipped, not fatal. It reports false when data is not JSON.
func decodeJSON(data []byte, v any) bool {
	err := json.Unmarshal(data, v)
	var typeErr *json.UnmarshalTypeError
	return err == nil || errors.As(err, &typeErr)
}

// isObject reports whether a trimmed line looks like a JSON object.
func isObject(line []byte) bool {
	return len(line) > 0 && line[0] == '{'
}

// Clip bounds s to max bytes at a character boundary, marking the cut
// with an ellipsis; invalid UTF-8 is replaced first. The run manager uses
// it for its own event texts.
func Clip(s string, max int) string { return clip(s, max) }

// clip bounds s to max bytes at a character boundary, marking the cut
// with an ellipsis. Invalid UTF-8 is replaced first.
func clip(s string, max int) string {
	s = strings.ToValidUTF8(s, "�")
	if len(s) <= max {
		return s
	}
	const mark = "…"
	cut := max - len(mark)
	if cut < 0 {
		cut = 0
	}
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + mark
}

// cut bounds s to max bytes at a character boundary, without a mark.
func cut(s string, max int) string {
	s = strings.ToValidUTF8(s, "�")
	if len(s) <= max {
		return s
	}
	n := max
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}

// firstRunes keeps the first n characters of s, marking the cut.
func firstRunes(s string, n int) string {
	s = strings.ToValidUTF8(s, "�")
	count := 0
	for i := range s {
		if count == n {
			return s[:i] + "…"
		}
		count++
	}
	return s
}

func boolPtr(v bool) *bool { return &v }

// textEvent is a raw line passed through as assistant text.
func textEvent(line []byte) Event {
	return Event{Kind: EventText, Text: clip(string(line), MaxEventText)}
}
