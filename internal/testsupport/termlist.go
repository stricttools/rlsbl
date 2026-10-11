package testsupport

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"filippo.io/age"
	"github.com/stricttools/strictspec/go/confidential"
)

// TermList is a confidential-term list location under a fresh directory,
// with no list written there: a list with no terms.
func TermList(t testing.TB) confidential.Location {
	t.Helper()
	return termListIn(t.TempDir())
}

// termListIn is a list and its identity under home, the identity where
// confidential.ConfiguredLocation looks for it.
func termListIn(home string) confidential.Location {
	return confidential.Location{
		List:     filepath.Join(home, "lists", "terms.toml.age"),
		Identity: filepath.Join(home, filepath.FromSlash(confidential.IdentityRel)),
	}
}

// ConfigureTermList writes the family's settings file in the user's
// configuration directory (a test's throwaway one), naming a list under the
// home directory, and returns that list's location, which
// confidential.ConfiguredLocation then gives. No list is written: a list with
// no terms until WriteTermList writes one.
func ConfigureTermList(t testing.TB) confidential.Location {
	t.Helper()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	loc := termListIn(home)
	settings, err := confidential.SettingsPath()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(settings), 0o755); err != nil {
		t.Fatal(err)
	}
	text := fmt.Sprintf("format_version = %d\n%s = %q\n", confidential.SettingsFormatVersion, confidential.TermListSetting, loc.List)
	if err := os.WriteFile(settings, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
	return loc
}

// WriteTermList encrypts plaintext, a confidential-term list, to a fresh
// age identity, writing the list and the identity at loc.
func WriteTermList(t testing.TB, loc confidential.Location, plaintext string) {
	t.Helper()
	id, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	w, err := age.Encrypt(&buf, id.Recipient())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte(plaintext)); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	for path, data := range map[string][]byte{loc.List: buf.Bytes(), loc.Identity: []byte(id.String() + "\n")} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

// TermListOf is a list holding one literal term, scoped everywhere.
func TermListOf(term string) string {
	return "format_version = 1\n\n[[terms]]\nterm = \"" + term + "\"\nadded = 2026-10-01\nreason = \"a private project's name\"\nscope = \"everywhere\"\n"
}
