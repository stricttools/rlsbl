package scaffold

import (
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestEveryActionATemplateNamesIsPinned(t *testing.T) {
	hygiene.Isolate(t)
	names, err := TemplateNames()
	if err != nil {
		t.Fatal(err)
	}
	placeholder := regexp.MustCompile(`\{\{action(?:Version)?\s+"([^"]+)"\}\}`)
	found := 0
	for _, name := range names {
		text, err := templateText(name)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range placeholder.FindAllStringSubmatch(text, -1) {
			found++
			if _, err := ActionVersion(m[1]); err != nil {
				t.Errorf("%s: %v", name, err)
			}
		}
	}
	if found < 10 {
		t.Fatalf("only %d action placeholders were found; the scan is reading the wrong files", found)
	}
}

func TestTheDroppedTemplatesAreAbsent(t *testing.T) {
	hygiene.Isolate(t)
	names, err := TemplateNames()
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range names {
		for _, dropped := range []string{"spec/", "launcher", "shim-", "deploy", "lint/", "win32", "CHANGELOG", "changes/"} {
			if strings.Contains(name, dropped) {
				t.Errorf("%s is a dropped template", name)
			}
		}
		text, err := templateText(name)
		if err != nil {
			t.Fatal(err)
		}
		for _, stale := range []string{"win32", ".exe", ".rlsbl/", "config.json", "windows"} {
			if strings.Contains(text, stale) {
				t.Errorf("%s still names %q", name, stale)
			}
		}
	}
}
