package cli

import (
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestRenderTableAlignsColumnsWithoutTrailingSpace(t *testing.T) {
	hygiene.Isolate(t)
	got := renderTable([]string{"NAME", "VERSION", "PATH"}, [][]string{
		{"portal", "0.4.0", "."},
		{"widgeté", "12.0.1", "packages/widget"},
		{"gadget", "1.0.0", ""},
	})
	want := "NAME     VERSION  PATH\n" +
		"portal   0.4.0    .\n" +
		"widgeté  12.0.1   packages/widget\n" +
		"gadget   1.0.0"
	if got != want {
		t.Fatalf("table =\n%s\nwant\n%s", got, want)
	}
}

func TestRenderTableRefusesARaggedRow(t *testing.T) {
	hygiene.Isolate(t)
	mustPanic(t, "2 cells under a header of 3", func() {
		renderTable([]string{"a", "b", "c"}, [][]string{{"1", "2"}})
	})
}

func TestRenderJSONIndents(t *testing.T) {
	hygiene.Isolate(t)
	if got := renderJSON(map[string]any{"version": "0.4.0"}); got != "{\n  \"version\": \"0.4.0\"\n}" {
		t.Fatalf("json = %q", got)
	}
}
