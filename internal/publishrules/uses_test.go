package publishrules_test

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

const goLibraryPipeline = `targets = [{ name = "go" }]

[[members.pipelines]]
name = "go"
type = "go"
target = "go"
local = false
artifact = "library"
`

const npmPipeline = `targets = [{ name = "npm" }]

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "package"
`

var acmePortal = github.Repository{Owner: "acme", Name: "portal"}

// repoView is the gh call GitHubVisibility makes.
var repoView = []string{"api", "--method", "GET", "repos/acme/portal"}

func visibilityAnswer(visibility string) testsupport.GHAnswer {
	private := "false"
	if visibility != "public" {
		private = "true"
	}
	return testsupport.GHAnswer{Args: repoView, Stdout: `{"full_name":"acme/portal","visibility":"` + visibility + `","private":` + private + `,"archived":false}`}
}

func outputs(uses []publishrules.Use) []string {
	var out []string
	for _, u := range uses {
		out = append(out, string(u.Output))
	}
	return out
}

func TestWhatAReleasablesDeclarationsPublish(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, root+"/portal/package.json", `{"name":"portal","version":"0.1.0","repository":"github:acme/portal","bugs":{"url":"x"}}`)
	for _, c := range []struct {
		name, mode, member string
		want               string
	}{
		{"a go library", "ci", goLibraryPipeline, "registry-package|go-proxy-notification|go-library"},
		{"a go binary from CI", "ci", goBinaryPipeline, ""},
		{"an npm package naming its repository", "ci", npmPipeline, "registry-package|repository-url-in-manifest|repository-url-in-manifest"},
		{"publish mode none", "none", goLibraryPipeline, ""},
	} {
		w := newWorkspace(t, root, portalDeclarations(c.mode, c.member))
		uses, err := publishrules.ReleasableUses(w, "portal")
		if err != nil {
			t.Errorf("%s: %v", c.name, err)
			continue
		}
		if got := strings.Join(outputs(uses), "|"); got != c.want {
			t.Errorf("%s: outputs %q, want %q", c.name, got, c.want)
		}
	}
}

func TestAPyprojectNamingItsRepositoryIsAUse(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, root+"/portal/pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.1.0\"\n\n[project.urls]\nHomepage = \"https://example.com\"\n")
	w := newWorkspace(t, root, portalDeclarations("ci", pypiPipeline))
	uses, err := publishrules.ReleasableUses(w, "portal")
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(outputs(uses), "|"); got != "registry-package|repository-url-in-manifest" {
		t.Errorf("outputs %q", got)
	}
	if !strings.Contains(uses[1].Where, "[project.urls]") {
		t.Errorf("where %q", uses[1].Where)
	}
}

// Nothing that turns on the visibility asks GitHub.
func TestNothingInUseAsksNothing(t *testing.T) {
	hygiene.Isolate(t)
	gh := testsupport.FakeGH(t)
	run(t, strictcli.EffectReadOnly, func(e *strictcli.Effects) error {
		c, err := github.New(e)
		if err != nil {
			return err
		}
		uses := []publishrules.Use{{Subject: "portal", Output: lifecycle.RegistryPackage, Where: "the npm pipeline"}}
		return publishrules.CheckUses(record(t, publicRecord), uses, publishrules.GitHubVisibility(c, acmePortal), today)
	})
	if calls := gh.Calls(); len(calls) != 0 {
		t.Errorf("gh was asked %v", calls)
	}
}

// A go library in a private repository is refused, and publish mode none
// (the fix the refusal names) clears it; a public repository passes.
func TestTheGoProxyRefusalAndItsFix(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.FakeGH(t, visibilityAnswer("private"), visibilityAnswer("public"))
	run(t, strictcli.EffectReadOnly, func(e *strictcli.Effects) error {
		c, err := github.New(e)
		if err != nil {
			return err
		}
		check := func(mode string) error {
			w := newWorkspace(t, root, portalDeclarations(mode, goLibraryPipeline))
			uses, err := publishrules.ReleasableUses(w, "portal")
			if err != nil {
				return err
			}
			return publishrules.CheckUses(record(t, publicRecord), uses, publishrules.GitHubVisibility(c, acmePortal), today)
		}
		err = check("ci")
		if err == nil || !strings.Contains(err.Error(), "the go pipeline \"go\" of the member \"portal\"") || !strings.Contains(err.Error(), string(lifecycle.RulePrivateRepositoryPublishing)) {
			t.Errorf("a private repository's go library: %v", err)
		}
		if err := check("none"); err != nil {
			t.Errorf("publish mode none is still refused: %v", err)
		}
		// The second answer: the repository made public clears it too.
		if err := check("ci"); err != nil {
			t.Errorf("a public repository's go library: %v", err)
		}
		return nil
	})
}

// A proprietary releasable publishes to no registry, whatever GitHub says.
func TestAProprietaryReleasablePublishesNothing(t *testing.T) {
	hygiene.Isolate(t)
	private, err := publishrules.KnownVisibility(lifecycle.VisibilityPrivate)
	if err != nil {
		t.Fatal(err)
	}
	uses := []publishrules.Use{{Subject: "portal", Output: lifecycle.RegistryPackage, Where: "the npm pipeline \"npm\""}}
	err = publishrules.CheckUses(record(t, confidentialRecord), uses, private, today)
	if err == nil || !strings.Contains(err.Error(), string(lifecycle.RuleProprietaryRefusesPublicOutput)) {
		t.Errorf("err = %v", err)
	}
	if err := publishrules.RegistryWriteAllowed(record(t, confidentialRecord), "portal", today); err == nil {
		t.Error("a registry write for a proprietary releasable was allowed")
	}
	if err := publishrules.RegistryWriteAllowed(record(t, publicRecord), "portal", today); err != nil {
		t.Errorf("a registry write for a public releasable: %v", err)
	}
}

// A visibility GitHub cannot answer refuses what turns on it, with the
// reason, and is never read as public.
func TestAnUnansweredVisibilityRefusesWithTheReason(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, testsupport.GHAnswer{Args: repoView, Stderr: "HTTP 401: Bad credentials\n", Exit: 1})
	run(t, strictcli.EffectReadOnly, func(e *strictcli.Effects) error {
		c, err := github.New(e)
		if err != nil {
			return err
		}
		source := publishrules.GitHubVisibility(c, acmePortal)
		uses := []publishrules.Use{{Output: lifecycle.BuildAttestation, Where: "publish.yml"}}
		err = publishrules.CheckUses(record(t, publicRecord), uses, source, today)
		if err == nil || !strings.Contains(err.Error(), "Bad credentials") {
			t.Errorf("CheckUses: %v", err)
		}
		if err := publishrules.CheckVisibility(record(t, publicRecord), source, today); err == nil || !strings.Contains(err.Error(), "Bad credentials") {
			t.Errorf("CheckVisibility: %v", err)
		}
		if _, err := publishrules.ScaffoldFeatures(record(t, publicRecord), source, today); err == nil || !strings.Contains(err.Error(), "Bad credentials") {
			t.Errorf("ScaffoldFeatures: %v", err)
		}
		return nil
	})
}

func TestGitHubsInternalVisibilityIsPrivate(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeGH(t, visibilityAnswer("internal"))
	run(t, strictcli.EffectReadOnly, func(e *strictcli.Effects) error {
		c, err := github.New(e)
		if err != nil {
			return err
		}
		v, err := publishrules.GitHubVisibility(c, acmePortal).Visibility()
		if err != nil || v != lifecycle.VisibilityPrivate {
			t.Errorf("visibility %q, %v", v, err)
		}
		return nil
	})
}

func TestTheRepositoryVisibilityAgreesWithTheRecord(t *testing.T) {
	hygiene.Isolate(t)
	private, _ := publishrules.KnownVisibility(lifecycle.VisibilityPrivate)
	public, _ := publishrules.KnownVisibility(lifecycle.VisibilityPublic)
	if err := publishrules.CheckVisibility(record(t, confidentialRecord), private, today); err != nil {
		t.Errorf("confidential and private: %v", err)
	}
	if err := publishrules.CheckVisibility(record(t, confidentialRecord), public, today); err == nil {
		t.Error("confidential and public passed")
	}
	if err := publishrules.CheckVisibility(record(t, publicRecord), private, today); err == nil {
		t.Error("public and private passed")
	}
	if _, err := publishrules.KnownVisibility(lifecycle.VisibilityUnknown); err == nil {
		t.Error("an unknown visibility was accepted as an answer")
	}
}

func TestScaffoldRendersNothingThatRecordsAConfidentialRepository(t *testing.T) {
	hygiene.Isolate(t)
	private, _ := publishrules.KnownVisibility(lifecycle.VisibilityPrivate)
	public, _ := publishrules.KnownVisibility(lifecycle.VisibilityPublic)
	all := publishrules.WorkflowFeatures{BuildAttestations: true, GoProxyNotification: true, RepositoryURLs: true}
	for _, c := range []struct {
		name   string
		record string
		source publishrules.VisibilitySource
		want   publishrules.WorkflowFeatures
	}{
		{"confidential", confidentialRecord, private, publishrules.WorkflowFeatures{}},
		{"public record, private repository", publicRecord, private, publishrules.WorkflowFeatures{}},
		{"public", publicRecord, public, all},
	} {
		got, err := publishrules.ScaffoldFeatures(record(t, c.record), c.source, today)
		if err != nil || got != c.want {
			t.Errorf("%s: %+v, %v", c.name, got, err)
		}
	}
}

// A deploy_command is a server's: proprietary and publishing nothing.
// Classifying the releasable and setting publish_mode none clear it.
func TestADeployCommandNeedsAProprietaryReleasablePublishingNothing(t *testing.T) {
	hygiene.Isolate(t)
	server := declarations.Releasable{Name: "portal", PublishMode: declarations.PublishCI, DeployCommand: []string{"deploy", "{version}"}}
	err := publishrules.DeployAllowed(record(t, publicRecord), server, today)
	if err == nil || !strings.Contains(err.Error(), "licensed MIT") || !strings.Contains(err.Error(), "transition classify --subject portal") || !strings.Contains(err.Error(), "publish_mode = \"none\"") {
		t.Fatalf("err = %v", err)
	}
	server.PublishMode = declarations.PublishNone
	if err := publishrules.DeployAllowed(record(t, confidentialRecord), server, today); err != nil {
		t.Errorf("a proprietary server publishing nothing: %v", err)
	}
	if err := publishrules.DeployAllowed(record(t, publicRecord), declarations.Releasable{Name: "portal", PublishMode: declarations.PublishCI}, today); err != nil {
		t.Errorf("no deploy_command: %v", err)
	}
}
