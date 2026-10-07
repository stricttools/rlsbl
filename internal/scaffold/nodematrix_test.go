package scaffold

import (
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheNodeLinesARangeAdmits(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		nodeRange string
		lines     []int
	}{
		{">=20", []int{20, 22, 24}},
		{">=22", []int{22, 24}},
		{">= 22.3.0", []int{22, 24}},
		{">=20.10.0 <24", []int{20, 22}},
		{"^22 || ^24", []int{22, 24}},
		{"^22.1.0", []int{22}},
		{"~24.2", nil},
		{"22.x", []int{22}},
		{"22", []int{22}},
		{"20 - 22", []int{20, 22}},
		{"20 - 22.4", []int{20}},
		{"*", NodeLines},
		{">20", []int{22, 24}},
		{"<=22", []int{20, 22}},
		{"<22", []int{20}},
		{">=18", []int{20, 22, 24}},
	}
	for _, c := range cases {
		got, err := NodeLinesSatisfying(c.nodeRange)
		if c.lines == nil {
			if err == nil || !strings.Contains(err.Error(), "admits none") {
				t.Errorf("%q: %v, %v", c.nodeRange, got, err)
			}
			continue
		}
		if err != nil || !slices.Equal(got, c.lines) {
			t.Errorf("%q = %v, %v; want %v", c.nodeRange, got, err, c.lines)
		}
	}
}

func TestAnUnreadableRangeIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := NodeLinesSatisfying(">=twenty"); err == nil || !strings.Contains(err.Error(), "not a range rlsbl can read") {
		t.Fatalf("err = %v", err)
	}
}

func TestAPackageWithoutEnginesNodeIsRefusedUntilItDeclaresOne(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "package.json")
	testsupport.WriteFile(t, path, `{"name": "widget", "version": "0.1.0"}`+"\n")
	_, err := NodeMatrix(dir)
	if err == nil || !strings.Contains(err.Error(), "declares no engines.node") || !strings.Contains(err.Error(), `"engines": {"node": ">=22"}`) {
		t.Fatalf("err = %v", err)
	}
	testsupport.WriteFile(t, path, `{"name": "widget", "version": "0.1.0", "engines": {"node": ">=22"}}`+"\n")
	lines, err := NodeMatrix(dir)
	if err != nil || RenderNodeMatrix(lines) != "[22, 24]" {
		t.Fatalf("after the fix: %v, %v", lines, err)
	}
}
