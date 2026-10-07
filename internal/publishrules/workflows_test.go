package publishrules_test

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

const attesting = `name: Publish
on:
  release:
    types: [published]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@v1
        with:
          skip-existing: true
`

const provenance = `name: Publish
jobs:
  npm:
    runs-on: ubuntu-latest
    steps:
      - run: npm publish --provenance --access public
`

const proxy = `name: Publish
jobs:
  go:
    runs-on: ubuntu-latest
    steps:
      - run: GOPROXY=https://proxy.golang.org go list -m example.com/portal@v1.2.3
`

func workflowOutputs(t *testing.T, text string) string {
	t.Helper()
	uses, err := publishrules.WorkflowUses([]publishrules.WorkflowFile{{Path: ".github/workflows/publish.yml", Text: text}})
	if err != nil {
		t.Fatal(err)
	}
	return strings.Join(outputs(uses), "|")
}

func TestWhatAWorkflowPublishes(t *testing.T) {
	hygiene.Isolate(t)
	for name, c := range map[string]struct{ text, want string }{
		"a pypi publish step attests by default":    {attesting, string(lifecycle.BuildAttestation)},
		"a pypi publish step with attestations off": {attesting + "          attestations: false\n", ""},
		"npm provenance":                {provenance, string(lifecycle.BuildAttestation)},
		"a Go module proxy request":     {proxy, string(lifecycle.GoProxyNotification)},
		"downloading through the proxy": {"jobs:\n  b:\n    steps:\n      - run: go mod download\n", ""},
	} {
		if got := workflowOutputs(t, c.text); got != c.want {
			t.Errorf("%s: %q, want %q", name, got, c.want)
		}
	}
	if _, err := publishrules.WorkflowUses([]publishrules.WorkflowFile{{Path: "broken.yml", Text: "jobs: [\n"}}); err == nil || !strings.Contains(err.Error(), "broken.yml") {
		t.Errorf("an unreadable workflow: %v", err)
	}
}

// The release judges the committed workflows: an attesting workflow in a
// confidential repository is refused until the regenerated workflow, with
// attestations off, is committed.
func TestTheCommittedWorkflowIsJudged(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	first := repo.CommitFile(".github/workflows/publish.yml", attesting, "publish")
	repo.Write(".github/workflows/publish.yml", attesting+"          attestations: false\n")
	private, _ := publishrules.KnownVisibility(lifecycle.VisibilityPrivate)
	judge := func(r git.Repo, rev string) error {
		files, err := publishrules.CommittedWorkflows(r, rev)
		if err != nil {
			return err
		}
		uses, err := publishrules.WorkflowUses(files)
		if err != nil {
			return err
		}
		return publishrules.CheckUses(record(t, confidentialRecord), uses, private, today)
	}
	run(t, strictcli.EffectReadOnly, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		if err := judge(r, first); err == nil || !strings.Contains(err.Error(), ".github/workflows/publish.yml (job publish)") {
			t.Errorf("the uncommitted fix cleared the refusal: %v", err)
		}
		return nil
	})
	fixed := repo.Commit("regenerate", ".github/workflows/publish.yml")
	run(t, strictcli.EffectReadOnly, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		if err := judge(r, fixed); err != nil {
			t.Errorf("the committed fix is still refused: %v", err)
		}
		return nil
	})
}

func TestWorkingTreeWorkflows(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	if files, err := publishrules.WorkingTreeWorkflows(root); err != nil || len(files) != 0 {
		t.Errorf("no workflows directory: %v, %v", files, err)
	}
	testsupport.WriteFile(t, root+"/.github/workflows/publish.yml", proxy)
	testsupport.WriteFile(t, root+"/.github/workflows/README.md", "not a workflow\n")
	files, err := publishrules.WorkingTreeWorkflows(root)
	if err != nil || len(files) != 1 || files[0].Path != ".github/workflows/publish.yml" {
		t.Errorf("files = %v, %v", files, err)
	}
}
