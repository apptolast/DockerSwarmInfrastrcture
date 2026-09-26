package runs

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// maxLine bounds one stream-json line; longer ones (a huge tool result) are
// summarized instead of buffered.
const maxLine = 1 << 20

// Renderer turns Claude Code's `--output-format stream-json` (one JSON
// event per line, the only realtime format with -p) into readable text.
// Tool inputs and results are summarized, never echoed in full.
type Renderer struct {
	pending  []byte
	skipping bool
}

// Feed consumes a stdout chunk and returns the text of every complete line.
func (r *Renderer) Feed(data []byte) []byte {
	var out bytes.Buffer
	for len(data) > 0 {
		i := bytes.IndexByte(data, '\n')
		if i < 0 {
			if !r.skipping {
				r.pending = append(r.pending, data...)
				if len(r.pending) > maxLine {
					r.pending, r.skipping = nil, true
					out.WriteString("[evento demasiado largo, omitido]\n")
				}
			}
			break
		}
		line := data[:i]
		data = data[i+1:]
		if r.skipping {
			r.skipping = false
			continue
		}
		if len(r.pending) > 0 {
			line = append(r.pending, line...)
			r.pending = nil
		}
		out.WriteString(RenderEvent(line))
	}
	return out.Bytes()
}

// Flush returns whatever was left without a final newline.
func (r *Renderer) Flush() []byte {
	if len(r.pending) == 0 {
		return nil
	}
	line := r.pending
	r.pending = nil
	return []byte(RenderEvent(line))
}

type block struct {
	Type    string `json:"type"`
	Text    string `json:"text"`
	Name    string `json:"name"`
	IsError bool   `json:"is_error"`
}

type event struct {
	Type    string `json:"type"`
	Subtype string `json:"subtype"`
	Model   string `json:"model"`
	Message *struct {
		Content json.RawMessage `json:"content"`
	} `json:"message"`
	Result   string  `json:"result"`
	NumTurns int     `json:"num_turns"`
	Cost     float64 `json:"total_cost_usd"`
	IsError  bool    `json:"is_error"`
}

// RenderEvent renders one line. Anything that is not a known event is
// passed through as text.
func RenderEvent(line []byte) string {
	trimmed := bytes.TrimSpace(line)
	if len(trimmed) == 0 {
		return ""
	}
	var ev event
	if trimmed[0] != '{' || json.Unmarshal(trimmed, &ev) != nil || ev.Type == "" {
		return string(line) + "\n"
	}
	var b strings.Builder
	switch ev.Type {
	case "system":
		if ev.Subtype == "init" {
			fmt.Fprintf(&b, "[sesión iniciada, modelo %s]\n", ev.Model)
		}
	case "assistant", "user":
		var blocks []block
		if ev.Message != nil && json.Unmarshal(ev.Message.Content, &blocks) == nil {
			for _, bl := range blocks {
				switch bl.Type {
				case "text":
					b.WriteString(bl.Text)
					b.WriteString("\n")
				case "tool_use":
					fmt.Fprintf(&b, "[herramienta: %s]\n", bl.Name)
				case "tool_result":
					if bl.IsError {
						b.WriteString("[la herramienta devolvió un error]\n")
					}
				}
			}
		}
	case "result":
		fmt.Fprintf(&b, "\n=== Resultado (%s, %d turnos, %.4f USD) ===\n%s\n",
			ev.Subtype, ev.NumTurns, ev.Cost, ev.Result)
	}
	return b.String()
}
