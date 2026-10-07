package releaserecord_test

import (
	"errors"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/releaserecord"
)

func identities(t *testing.T, text string) []lifecycle.Identity {
	t.Helper()
	r, err := lifecycle.Parse([]byte("format_version = 1\n" + text))
	mustNotFail(t, err)
	return r.Identities()
}

const movedRepositories = `
[[identities]]
subject = "gadget"
facet = "repository-url"
value = "https://github.com/acme/gizmo"
registry = ""
tag_patterns = []
from = 2025-01-01
until = 2026-01-01
reason = "absorbed into the workspace"

[[identities]]
subject = "gadget"
facet = "repository-url"
value = "https://github.com/acme/tools"
registry = ""
tag_patterns = []
from = 2026-01-01
reason = "the workspace"

[[identities]]
subject = "widget"
facet = "repository-url"
value = "git@gitlab.com:acme/widget.git"
registry = ""
tag_patterns = []
from = 2025-01-01
until = 2026-01-01
reason = "absorbed from another forge"
`

func TestOldRepositoriesMustBeArchived(t *testing.T) {
	hygiene.Isolate(t)
	ids := identities(t, movedRepositories)
	var asked []string
	probe := func(answer bool, err error) releaserecord.ArchivedProbe {
		return func(repo github.Repository) (bool, error) {
			asked = append(asked, repo.String())
			return answer, err
		}
	}
	v := releaserecord.EvaluateOldRepositoryArchived(ids, probe(true, nil))
	if !v.OK() || strings.Join(asked, ",") != "acme/gizmo" {
		t.Fatalf("archived: %+v, asked %v", v, asked)
	}
	requireContains(t, strings.Join(v.Notes, "\n"), "1 earlier repositories archived", "git@gitlab.com:acme/widget.git: not a github.com repository")
	v = releaserecord.EvaluateOldRepositoryArchived(ids, probe(false, nil))
	if v.OK() {
		t.Fatal("an active old repository passed")
	}
	requireContains(t, v.Problems[0], "acme/gizmo", "gh repo archive acme/gizmo")
	v = releaserecord.EvaluateOldRepositoryArchived(ids, probe(false, errors.New("HTTP 404")))
	if v.OK() {
		t.Fatal("an unanswered probe passed")
	}
	requireContains(t, v.Problems[0], "HTTP 404", "not an answer")
}

func TestOldRepositoriesAreAskedAboutOnce(t *testing.T) {
	hygiene.Isolate(t)
	ids := identities(t, movedRepositories+`
[[identities]]
subject = "gizmo"
facet = "repository-url"
value = "git@github.com:acme/gizmo.git"
registry = ""
tag_patterns = []
from = 2024-01-01
until = 2025-01-01
reason = "the same repository, spelled for ssh"
`)
	asked := 0
	releaserecord.EvaluateOldRepositoryArchived(ids, func(github.Repository) (bool, error) {
		asked++
		return true, nil
	})
	if asked != 1 {
		t.Fatalf("asked %d times", asked)
	}
}

func TestNoClosedRepositoryIdentitySkips(t *testing.T) {
	hygiene.Isolate(t)
	v := releaserecord.EvaluateOldRepositoryArchived(nil, func(github.Repository) (bool, error) {
		t.Fatal("asked")
		return false, nil
	})
	if v.SkipReason == "" || !v.OK() {
		t.Fatalf("verdict %+v", v)
	}
}

const movedModule = `
[[identities]]
subject = "gadget"
facet = "go-module-path"
value = "github.com/acme/gizmo"
registry = "go"
tag_patterns = ["v*"]
from = 2025-01-01
until = 2026-01-01
reason = "the old path"

[[identities]]
subject = "gadget"
facet = "go-module-path"
value = "github.com/acme/gadget"
registry = "go"
tag_patterns = ["v*"]
from = 2026-01-01
reason = "renamed"
`

func TestOldModulePathsMustServeADeprecationNotice(t *testing.T) {
	hygiene.Isolate(t)
	ids := identities(t, movedModule)
	evaluate := func(answer releaserecord.DeprecationAnswer, err error) releaserecord.FollowupVerdict {
		return releaserecord.EvaluateGoDeprecationPublished(ids, func(module string) (releaserecord.DeprecationAnswer, error) {
			if module != "github.com/acme/gizmo" {
				t.Errorf("asked about %s", module)
			}
			return answer, err
		})
	}
	if v := evaluate(releaserecord.DeprecationAnswer{Status: releaserecord.Deprecated, Version: "v0.9.0"}, nil); !v.OK() {
		t.Fatalf("deprecated: %+v", v)
	}
	v := evaluate(releaserecord.DeprecationAnswer{Status: releaserecord.NotDeprecated, Version: "v0.9.0"}, nil)
	if v.OK() {
		t.Fatal("an undeprecated old path passed")
	}
	requireContains(t, v.Problems[0], "moved to github.com/acme/gadget on 2026-01-01", "v0.9.0", "// Deprecated: moved to github.com/acme/gadget")
	v = evaluate(releaserecord.DeprecationAnswer{Status: releaserecord.NeverPublished}, nil)
	if !v.OK() || !strings.Contains(strings.Join(v.Notes, "\n"), "never served this path") {
		t.Fatalf("never published: %+v", v)
	}
	if v := evaluate(releaserecord.DeprecationAnswer{}, errors.New("proxy unreachable")); v.OK() {
		t.Fatal("an unanswered probe passed")
	}
	if v := releaserecord.EvaluateGoDeprecationPublished(nil, nil); v.SkipReason == "" {
		t.Fatalf("no identity: %+v", v)
	}
}
