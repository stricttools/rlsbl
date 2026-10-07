package scaffold

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTheScaffoldStateRoundTrips(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	if _, found, err := ReadState(repo.Dir); found || err != nil {
		t.Fatalf("a repository without a state: %v, %v", found, err)
	}
	want := State{RlsblVersion: "0.132.0", Files: map[string]string{
		".github/workflows/ci.yml": FileHash([]byte("ci")),
		"VERSION":                  FileHash([]byte("0.1.0\n")),
	}}
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		return WriteState(e, repo.Dir, want)
	})
	got, found, err := ReadState(repo.Dir)
	if err != nil || !found || got.RlsblVersion != want.RlsblVersion || len(got.Files) != 2 || got.Files["VERSION"] != want.Files["VERSION"] {
		t.Fatalf("read back %+v, %v, %v", got, found, err)
	}
	data, err := os.ReadFile(repo.Path(".strictmetadata/.scaffold-state/scaffold-state.toml"))
	if err != nil || !strings.HasPrefix(string(data), "format_version = 1\nrlsbl_version = \"0.132.0\"\n\n[files]\n\".github/workflows/ci.yml\" = ") {
		t.Fatalf("the file:\n%s", data)
	}
	if manifest, err := os.ReadFile(repo.Path(".strictmetadata/.scaffold-state/manifest.toml")); err != nil || string(manifest) != "owner = \"rlsbl\"\n" {
		t.Fatalf("the directory's manifest: %q, %v", manifest, err)
	}
}

func TestAScaffoldStateTheReaderCannotTrustIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	cases := map[string]string{
		"format version": "format_version = 2\nrlsbl_version = \"0.1.0\"\n[files]\n",
		"hash":           "format_version = 1\nrlsbl_version = \"0.1.0\"\n[files]\n\"VERSION\" = \"abc\"\n",
		"path":           "format_version = 1\nrlsbl_version = \"0.1.0\"\n[files]\n\"../VERSION\" = \"" + FileHash(nil) + "\"\n",
		"unknown key":    "format_version = 1\nrlsbl_version = \"0.1.0\"\nversion = 1\n[files]\n",
		"no files":       "format_version = 1\nrlsbl_version = \"0.1.0\"\n",
	}
	for name, text := range cases {
		if _, err := ParseState([]byte(text)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
