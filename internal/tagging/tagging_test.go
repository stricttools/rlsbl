package tagging

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// projectWith is a directory holding one manifest.
func projectWith(t *testing.T, name, content string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// edit runs fn with the effects handle of a mutating throwaway command.
func edit(t *testing.T, fn func(e *strictcli.Effects) error) {
	t.Helper()
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, Allowlist: previewapply.Prefixes()}, fn)
}

func TestTheNpmKeywordIsInsertedKeepingTheRestOfTheFile(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ name, before, after string }{
		{
			"an existing one-line array",
			"{\n  \"name\": \"portal\",\n  \"keywords\": [\"cli\"],\n  \"version\": \"1.0.0\"\n}\n",
			"{\n  \"name\": \"portal\",\n  \"keywords\": [\"cli\", \"rlsbl\"],\n  \"version\": \"1.0.0\"\n}\n",
		},
		{
			"an existing multi-line array",
			"{\n    \"name\": \"portal\",\n    \"keywords\": [\n        \"cli\",\n        \"tool\"\n    ]\n}\n",
			"{\n    \"name\": \"portal\",\n    \"keywords\": [\n        \"cli\",\n        \"tool\",\n        \"rlsbl\"\n    ]\n}\n",
		},
		{
			"an empty array",
			"{\n  \"name\": \"portal\",\n  \"keywords\": []\n}",
			"{\n  \"name\": \"portal\",\n  \"keywords\": [\"rlsbl\"]\n}",
		},
		{
			"no array, four-space indent",
			"{\n    \"name\": \"portal\",\n    \"version\": \"1.0.0\"\n}\n",
			"{\n    \"name\": \"portal\",\n    \"version\": \"1.0.0\",\n    \"keywords\": [\"rlsbl\"]\n}\n",
		},
		{
			"compact JSON",
			`{"name":"portal","version":"1.0.0"}`,
			`{"name":"portal","version":"1.0.0", "keywords": ["rlsbl"]}`,
		},
	}
	for _, c := range cases {
		dir := projectWith(t, "package.json", c.before)
		edit(t, func(e *strictcli.Effects) error {
			path, changed, err := EnsureNpmKeyword(e, dir)
			if err != nil || !changed || path != filepath.Join(dir, "package.json") {
				t.Errorf("%s: %v %v", c.name, changed, err)
			}
			return nil
		})
		if got := read(t, filepath.Join(dir, "package.json")); got != c.after {
			t.Errorf("%s:\n%s\nwant:\n%s", c.name, got, c.after)
		}
		if _, err := os.Stat(filepath.Join(dir, "package.json.rlsbl-writing")); !os.IsNotExist(err) {
			t.Errorf("%s: the temporary file was left behind", c.name)
		}
	}
}

func TestAnNpmManifestAlreadyCarryingTheKeywordIsLeftAlone(t *testing.T) {
	hygiene.Isolate(t)
	before := "{\"name\": \"portal\", \"keywords\": [\"rlsbl\"]}"
	dir := projectWith(t, "package.json", before)
	edit(t, func(e *strictcli.Effects) error {
		if _, changed, err := EnsureNpmKeyword(e, dir); err != nil || changed {
			t.Errorf("%v %v", changed, err)
		}
		return nil
	})
	if read(t, filepath.Join(dir, "package.json")) != before {
		t.Fatal("the manifest was rewritten")
	}
}

func TestAmbiguousNpmManifestsAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	for name, content := range map[string]string{
		"duplicate keywords": `{"keywords": ["a"], "keywords": ["b"]}`,
		"keywords not array": `{"keywords": "cli"}`,
		"a non-string":       `{"keywords": ["cli", 3]}`,
		"not an object":      `["portal"]`,
		"not JSON":           `{"name": }`,
	} {
		dir := projectWith(t, "package.json", content)
		edit(t, func(e *strictcli.Effects) error {
			if _, _, err := EnsureNpmKeyword(e, dir); err == nil || !strings.Contains(err.Error(), "package.json") {
				t.Errorf("%s: %v", name, err)
			}
			return nil
		})
		if read(t, filepath.Join(dir, "package.json")) != content {
			t.Errorf("%s: the manifest was rewritten", name)
		}
	}
}

func TestThePypiKeywordIsInserted(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		name, before string
		keep         []string
	}{
		{"a one-line array", "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\nkeywords = [\"cli\"]\n", []string{`"cli"`}},
		{"a multi-line array", "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\nkeywords = [\n    \"cli\",\n    \"tool\"\n]\n", []string{`"cli"`, `"tool"`}},
		{"a project.urls table", "[project]\nname = \"gadget\"\nkeywords = [\"cli\"]  # tags\n\n[project.urls]\nRepository = \"https://example.invalid/gadget\"\n", []string{`"cli"`, "[project.urls]", "https://example.invalid/gadget", "# tags"}},
		{"brackets in a keyword", "[project]\nname = \"gadget\"\nkeywords = [\"[beta]\", \"cli\"]\n", []string{`"[beta]"`, `"cli"`}},
		{"no keywords", "[project]\nname = \"gadget\"\nversion = \"1.0.0\"\n", []string{`version = "1.0.0"`}},
	}
	for _, c := range cases {
		dir := projectWith(t, "pyproject.toml", c.before)
		edit(t, func(e *strictcli.Effects) error {
			if _, changed, err := EnsurePypiKeyword(e, dir); err != nil || !changed {
				t.Errorf("%s: %v %v", c.name, changed, err)
			}
			return nil
		})
		got := read(t, filepath.Join(dir, "pyproject.toml"))
		if !strings.Contains(got, `"rlsbl"`) {
			t.Errorf("%s: no keyword:\n%s", c.name, got)
		}
		for _, k := range c.keep {
			if !strings.Contains(got, k) {
				t.Errorf("%s: lost %q:\n%s", c.name, k, got)
			}
		}
	}
}

func TestPypiManifestsTheKeywordCannotGoInto(t *testing.T) {
	hygiene.Isolate(t)
	present := projectWith(t, "pyproject.toml", "[project]\nname = \"gadget\"\nkeywords = [\"rlsbl\"]\n")
	noProject := projectWith(t, "pyproject.toml", "[tool.uv]\nmanaged = true\n")
	notArray := projectWith(t, "pyproject.toml", "[project]\nkeywords = \"rlsbl\"\n")
	edit(t, func(e *strictcli.Effects) error {
		if _, changed, err := EnsurePypiKeyword(e, present); err != nil || changed {
			t.Errorf("already present: %v %v", changed, err)
		}
		if _, _, err := EnsurePypiKeyword(e, noProject); err == nil || !strings.Contains(err.Error(), "[project]") {
			t.Errorf("no [project]: %v", err)
		}
		if _, _, err := EnsurePypiKeyword(e, notArray); err == nil || !strings.Contains(err.Error(), "not an array") {
			t.Errorf("not an array: %v", err)
		}
		return nil
	})
}

// EnsureTags tags the manifests in the directories named, not the working
// directory, and puts the topic on the repository.
func TestEnsureTagsUsesTheNamedDirectoriesAndTheTopic(t *testing.T) {
	hygiene.Isolate(t)
	npmDir := projectWith(t, "package.json", "{\n  \"name\": \"portal\"\n}\n")
	pypiDir := projectWith(t, "pyproject.toml", "[project]\nname = \"gadget\"\n")
	topics := []string{"api", "--method", "GET", "repos/acme/portal/topics"}
	put := []string{"api", "--method", "PUT", "repos/acme/portal/topics", "-f", "names[]=cli", "-f", "names[]=rlsbl"}
	gh := testsupport.FakeGH(t, testsupport.GHAnswer{Args: topics, Stdout: `{"names":["cli"]}`}, testsupport.GHAnswer{Args: put})
	edit(t, func(e *strictcli.Effects) error {
		client, err := github.New(e)
		if err != nil {
			return err
		}
		tagged, err := EnsureTags(e, client, github.Repository{Owner: "acme", Name: "portal"}, Manifests{NpmDirs: []string{npmDir}, PypiDirs: []string{pypiDir}})
		if err != nil {
			return err
		}
		want := []string{filepath.Join(npmDir, "package.json"), filepath.Join(pypiDir, "pyproject.toml")}
		if !slices.Equal(tagged.Edited, want) || !tagged.TopicAdded {
			t.Errorf("%+v", tagged)
		}
		return nil
	})
	if calls := gh.Calls(); len(calls) != 2 || !slices.Equal(calls[1].Args, put) {
		t.Fatalf("calls %+v", calls)
	}
}

func TestDiscoverFiltersToTheAuthenticatedAccount(t *testing.T) {
	hygiene.Isolate(t)
	search := []string{"api", "--method", "GET", "--paginate", "search/repositories?q=topic:rlsbl&sort=updated&per_page=100", "--jq", `"total\t\(.total_count)", (.items[] | [.full_name, (.description // ""), .updated_at, .owner.login] | @tsv)`}
	user := []string{"api", "--method", "GET", "user", "--jq", ".login"}
	testsupport.FakeGH(t,
		testsupport.GHAnswer{Args: search, Stdout: "total\t2\nacme/portal\tA portal\t2026-01-01T00:00:00Z\tacme\nother/widget\t\t2026-01-02T00:00:00Z\tother\n"},
		testsupport.GHAnswer{Args: user, Stdout: "acme\n"},
	)
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		client, err := github.New(e)
		if err != nil {
			return err
		}
		all, err := Discover(client, false)
		if err != nil || len(all) != 2 {
			t.Errorf("all: %+v %v", all, err)
		}
		mine, err := Discover(client, true)
		if err != nil || len(mine) != 1 || mine[0].FullName != "acme/portal" {
			t.Errorf("mine: %+v %v", mine, err)
		}
		return nil
	})
}

func TestRelativeTime(t *testing.T) {
	hygiene.Isolate(t)
	now := time.Date(2026, 6, 15, 12, 0, 0, 0, time.UTC)
	for ago, want := range map[time.Duration]string{
		30 * time.Second:     "just now",
		5 * time.Minute:      "5m ago",
		3 * time.Hour:        "3h ago",
		2 * 24 * time.Hour:   "2d ago",
		21 * 24 * time.Hour:  "3w ago",
		120 * 24 * time.Hour: "4mo ago",
		800 * 24 * time.Hour: "2y ago",
	} {
		got, err := RelativeTime(now.Add(-ago).Format(time.RFC3339), now)
		if err != nil || got != want {
			t.Errorf("%v: %q %v, want %q", ago, got, err, want)
		}
	}
	if got, err := RelativeTime("", now); err != nil || got != "" {
		t.Errorf("empty: %q %v", got, err)
	}
	if _, err := RelativeTime("yesterday", now); err == nil {
		t.Error("an unparsable time was rendered")
	}
}
