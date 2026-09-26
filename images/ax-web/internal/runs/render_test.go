package runs

import (
	"strings"
	"testing"
)

func TestRenderEvents(t *testing.T) {
	cases := map[string]string{
		`{"type":"system","subtype":"init","model":"claude-x","tools":["Read"]}`:                        "[sesión iniciada, modelo claude-x]\n",
		`{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"secret plan"}]}}`:     "",
		`{"type":"user","message":{"content":[{"type":"tool_result","content":"file body"}]}}`:          "",
		`{"type":"user","message":{"content":[{"type":"tool_result","content":"x","is_error":true}]}}`:  "[la herramienta devolvió un error]\n",
		`{"type":"result","subtype":"error_max_turns","num_turns":3,"total_cost_usd":0.25,"result":""}`: "\n=== Resultado (error_max_turns, 3 turnos, 0.2500 USD) ===\n\n",
		`not json`:      "not json\n",
		`{"no":"type"}`: "{\"no\":\"type\"}\n",
		``:              "",
	}
	for in, want := range cases {
		if got := RenderEvent([]byte(in)); got != want {
			t.Errorf("%s: %q", in, got)
		}
	}
}

func TestRendererSplitsAndBounds(t *testing.T) {
	var r Renderer
	out := string(r.Feed([]byte(`{"type":"assistant","message":{"content":[{"type":"text","text":"a"}]}}` + "\n" + `{"type":"ass`)))
	out += string(r.Feed([]byte(`istant","message":{"content":[{"type":"text","text":"b"}]}}` + "\npartial")))
	out += string(r.Flush())
	if out != "a\nb\npartial\n" {
		t.Fatalf("%q", out)
	}
	// An oversized line is dropped up to its newline.
	huge := strings.Repeat("x", maxLine+10)
	out = string(r.Feed([]byte(huge)))
	out += string(r.Feed([]byte("tail\nnext\n")))
	if out != "[evento demasiado largo, omitido]\nnext\n" {
		t.Fatalf("%q", out)
	}
}
