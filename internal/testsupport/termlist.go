package testsupport

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"filippo.io/age"
	"github.com/stricttools/strictspec/go/confidential"
)

// TermList is a confidential-term list location under a fresh home
// directory, with no list written there: a list with no terms.
func TermList(t testing.TB) confidential.Location {
	t.Helper()
	return confidential.LocationIn(t.TempDir())
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
