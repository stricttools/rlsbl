package scaffold

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// The template text of the file the plan tests merge, and its next version.
const (
	templateV1 = "one\ntwo\nthree\nfour\nfive\n"
	templateV2 = "one\ntwo\nthree\nfour\nFIVE\n"
)

// plan plans f in the repository, failing the test on a dispatch error; the
// plan's own error is returned.
func plan(t *testing.T, repo *testsupport.Repo, f render) (filePlan, error) {
	t.Helper()
	var p filePlan
	var planErr error
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating}, func(e *strictcli.Effects) error {
		g, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		p, planErr = planFile(repo.Dir, g, e, f)
		return nil
	})
	return p, planErr
}

// withBase writes the file and its stored merge base.
func withBase(repo *testsupport.Repo, rel, ours, base string) {
	repo.Write(rel, ours)
	repo.Write(BasePath(rel), base)
}

func TestAMissingFileIsCreatedWithItsBase(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV1})
	if err != nil {
		t.Fatal(err)
	}
	if p.status != statusCreated || !p.write || p.content != templateV1 || !p.writeBase || p.base != templateV1 || !p.record || p.hash != FileHash([]byte(templateV1)) {
		t.Fatalf("%+v", p)
	}
}

func TestAnUnmodifiedFileTakesTheTemplateChange(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	withBase(repo, "ci.yml", templateV1, templateV1)
	p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV2})
	if err != nil {
		t.Fatal(err)
	}
	if p.status != statusUpdated || p.content != templateV2 || p.base != templateV2 {
		t.Fatalf("%+v", p)
	}
	unchanged, err := plan(t, repo, render{path: "ci.yml", theirs: templateV1})
	if err != nil || unchanged.status != statusUnchanged || unchanged.changed() {
		t.Fatalf("%+v, %v", unchanged, err)
	}
}

// A local edit and a template change on different lines both remain.
func TestALocalEditAndATemplateChangeMerge(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	withBase(repo, "ci.yml", "ONE\ntwo\nthree\nfour\nfive\n", templateV1)
	p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV2})
	if err != nil {
		t.Fatal(err)
	}
	if p.status != statusMerged || p.content != "ONE\ntwo\nthree\nfour\nFIVE\n" || p.base != templateV2 || len(p.conflicts) != 0 {
		t.Fatalf("%+v", p)
	}
}

// A local edit and a template change on one line conflict: the file is
// written with git's markers and the conflict named by its lines.
func TestAConflictIsWrittenWithMarkersAndNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	withBase(repo, "ci.yml", "one\ntwo\nthree\nfour\nmine\n", templateV1)
	p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV2})
	if err != nil {
		t.Fatal(err)
	}
	if p.status != statusConflicts || len(p.conflicts) != 1 || !strings.Contains(p.content, "<<<<<<<") || !strings.Contains(p.content, "mine") || !strings.Contains(p.content, "FIVE") {
		t.Fatalf("%+v", p)
	}
}

// A file with no stored base is merged against its last scaffold commit.
func TestAMissingBaseIsRebuiltFromTheScaffoldCommit(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("ci.yml", templateV1, commitMessage)
	repo.CommitFile("ci.yml", "ONE\ntwo\nthree\nfour\nfive\n", "my edit")
	p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV2})
	if err != nil {
		t.Fatal(err)
	}
	if p.healed == "" || p.status != statusMerged || p.content != "ONE\ntwo\nthree\nfour\nFIVE\n" || !p.writeBase {
		t.Fatalf("%+v", p)
	}
}

// A file with neither a base nor a scaffold commit is refused unless it
// already is the template. Each way out the refusal names clears it.
func TestAFileWithNoBaseAndNoHistoryIsRefused(t *testing.T) {
	hygiene.Isolate(t)

	t.Run("deleting the file", func(t *testing.T) {
		repo := testsupport.NewRepo(t)
		repo.Write("ci.yml", "something else\n")
		_, err := plan(t, repo, render{path: "ci.yml", theirs: templateV1})
		if err == nil || !strings.Contains(err.Error(), "no stored merge base") || !strings.Contains(err.Error(), "delete ci.yml") {
			t.Fatalf("got %v", err)
		}
		if err := os.Remove(repo.Path("ci.yml")); err != nil {
			t.Fatal(err)
		}
		if p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV1}); err != nil || p.status != statusCreated {
			t.Fatalf("%+v, %v", p, err)
		}
	})

	t.Run("committing the file as scaffold output", func(t *testing.T) {
		repo := testsupport.NewRepo(t)
		repo.Write("ci.yml", "one\ntwo\nthree\nfour\nfive\nsix\n")
		if _, err := plan(t, repo, render{path: "ci.yml", theirs: templateV2}); err == nil || !strings.Contains(err.Error(), commitMessage) {
			t.Fatalf("got %v", err)
		}
		repo.Commit("rlsbl scaffold output, committed by hand", "ci.yml")
		p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV2})
		if err != nil || p.healed == "" {
			t.Fatalf("%+v, %v", p, err)
		}
	})

	t.Run("a file equal to the template", func(t *testing.T) {
		repo := testsupport.NewRepo(t)
		repo.Write("ci.yml", templateV1)
		p, err := plan(t, repo, render{path: "ci.yml", theirs: templateV1})
		if err != nil || p.status != statusSeeded || p.write || !p.writeBase {
			t.Fatalf("%+v, %v", p, err)
		}
	})
}

// A user-owned file is written only while absent; a line-merged file gains
// the template's lines it lacks and loses none.
func TestUserOwnedAndLineMergedFiles(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write(".npmignore", "mine\n")
	p, err := plan(t, repo, render{path: ".npmignore", theirs: "template\n", userOwned: true})
	if err != nil || p.status != statusUserOwned || p.changed() || p.record {
		t.Fatalf("%+v, %v", p, err)
	}
	repo.Write(".gitignore", "node_modules/\n# a note\nmine/\n")
	p, err = plan(t, repo, render{path: ".gitignore", theirs: "node_modules/\n*.log\n", mergeLines: true})
	if err != nil || p.status != statusLinesAdded || p.content != "node_modules/\n# a note\nmine/\n\n*.log\n" || p.record {
		t.Fatalf("%+v, %v", p, err)
	}
}

func TestMergeLinesAddsOnlyWhatIsMissing(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ text, template, want string }{
		{"a\nb\n", "a\nb\n", "a\nb\n"},
		{"a\n", "a\nb\n# comment\n\nc\n", "a\n\nb\nc\n"},
		{"  a  \n", "a\n", "  a  \n"},
		{"a", "b\n", "a\n\nb\n"},
	}
	for _, c := range cases {
		if got := mergeLines(c.text, c.template); got != c.want {
			t.Errorf("mergeLines(%q, %q) = %q, want %q", c.text, c.template, got, c.want)
		}
	}
}
