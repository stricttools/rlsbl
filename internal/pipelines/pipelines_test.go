package pipelines

import (
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestThePipelineTypesAndTheirSecrets(t *testing.T) {
	hygiene.Isolate(t)
	var names []string
	for _, ty := range Types() {
		names = append(names, ty.Name)
	}
	if strings.Join(names, ",") != "go,npm,pypi" {
		t.Fatalf("types = %q", names)
	}
	if _, err := TypeOf("cloudflare-pages"); err == nil || !strings.Contains(err.Error(), "go, npm, pypi") {
		t.Fatalf("a dropped type: %v", err)
	}
	npmCI := declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}
	if got, _ := CISecretNames(npmCI); !slices.Equal(got, []string{"NPM_TOKEN"}) {
		t.Errorf("npm from CI: %q", got)
	}
	npmLocal := npmCI
	npmLocal.Local = true
	if got, _ := CISecretNames(npmLocal); len(got) != 0 {
		t.Errorf("npm locally needs a repository secret: %q", got)
	}
	if got, _ := LocalSecretNames(npmLocal); !slices.Equal(got, []string{"NPM_TOKEN"}) {
		t.Errorf("npm locally: %q", got)
	}
	// PyPI publishes from CI through Trusted Publishing: no secret exists.
	if got, _ := CISecretNames(declarations.Pipeline{Type: "pypi", Artifact: "package"}); len(got) != 0 {
		t.Errorf("pypi from CI: %q", got)
	}
	headers, rows := TypeTable()
	if len(headers) != len(rows[0]) || len(rows) != 3 {
		t.Errorf("TypeTable = %q %q", headers, rows)
	}
}

func TestWhichGoPipelinesAskTheProxy(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct {
		p    declarations.Pipeline
		want bool
	}{
		{declarations.Pipeline{Type: "go", Artifact: "binary"}, false},
		{declarations.Pipeline{Type: "go", Artifact: "library"}, true},
		{declarations.Pipeline{Type: "go", Artifact: "binary", Local: true}, true},
		{declarations.Pipeline{Type: "npm", Artifact: "package", Local: true}, false},
	}
	for _, c := range cases {
		if got := AsksGoProxy(c.p); got != c.want {
			t.Errorf("%+v: %v", c.p, got)
		}
	}
}

func TestOnePlatformTableNamesTheNpmPackagesAndTheWheels(t *testing.T) {
	hygiene.Isolate(t)
	var packages, wheels, archives []string
	for _, p := range Platforms() {
		packages = append(packages, PlatformPackageName("rlsbl", p))
		wheels = append(wheels, WheelName("rlsbl", "0.132.0", p))
		archives = append(archives, ArchiveName("rlsbl", "0.132.0", p))
		if p.OS == "win32" {
			t.Errorf("a win32 platform: %+v", p)
		}
	}
	if !slices.Equal(packages, []string{"rlsbl-linux-x64", "rlsbl-linux-arm64", "rlsbl-darwin-x64", "rlsbl-darwin-arm64"}) {
		t.Errorf("packages = %q", packages)
	}
	if wheels[0] != "rlsbl-0.132.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64.whl" || wheels[3] != "rlsbl-0.132.0-py3-none-macosx_11_0_arm64.whl" {
		t.Errorf("wheels = %q", wheels)
	}
	if archives[1] != "rlsbl_0.132.0_linux_arm64.tar.gz" {
		t.Errorf("archives = %q", archives)
	}
	if got := WheelDistribution("Portal.Kit--cli"); got != "portal_kit_cli" {
		t.Errorf("WheelDistribution = %q", got)
	}
}

var v120 = semver.Version{Major: 1, Minor: 2}

// publish runs PublishLocal in a mutating throwaway command (a preview
// when dryRun), with registry reads answered by fake, and returns the
// outcome, what the command printed, and its error.
func publish(t *testing.T, fake *testsupport.FakeHTTP, dryRun bool, env map[string]string, in LocalPublish) (Published, string, error) {
	t.Helper()
	var out Published
	var publishErr error
	opts := testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}
	if fake != nil {
		opts.HTTPClient = fake.Client()
	}
	r := testsupport.RunCommand(t, opts, func(ctx *strictcli.Context) error {
		reg, err := registry.New(registry.Reads(ctx.Effects()))
		if err != nil {
			return err
		}
		out, publishErr = PublishLocal(ctx.Effects(), reg, func(name string) (string, bool) {
			v, ok := env[name]
			return v, ok
		}, in)
		return nil
	})
	return out, r.Stdout + r.Stderr, publishErr
}

func TestPipelinesThatCannotPublishLocallyAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	ci := declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}
	if _, _, err := publish(t, nil, false, nil, LocalPublish{Pipeline: ci, Dir: t.TempDir(), Version: v120}); err == nil || !strings.Contains(err.Error(), "publishes from CI") {
		t.Errorf("a CI pipeline: %v", err)
	}
	wrapper := declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "go-binary", BinaryPipeline: "go", Local: true}
	if _, _, err := publish(t, nil, false, nil, LocalPublish{Pipeline: wrapper, Dir: t.TempDir(), Version: v120}); err == nil || !strings.Contains(err.Error(), "go binaries") {
		t.Errorf("a go-binary pipeline: %v", err)
	}
}

func TestAVersionTheListingHoldsIsNotPublishedAgainAndOneItLacksNeedsTheToken(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(dir, "package.json"), "{\"name\": \"portal\", \"version\": \"1.2.0\"}\n")
	p := declarations.Pipeline{Name: "npm", Type: "npm", Target: "npm", Artifact: "package", Local: true}
	listed := testsupport.NewFakeHTTP(t, testsupport.HTTPAnswer{Method: "GET", URL: "https://registry.npmjs.org/portal", Status: 200,
		Body: "{\"name\": \"portal\", \"dist-tags\": {\"latest\": \"1.2.0\"}, \"versions\": {\"1.1.0\": {}, \"1.2.0\": {}}}"})
	out, _, err := publish(t, listed, false, nil, LocalPublish{Pipeline: p, Dir: dir, Version: v120})
	if err != nil || !out.Skipped {
		t.Fatalf("a listed version: %+v, %v", out, err)
	}
	unlisted := testsupport.NewFakeHTTP(t, testsupport.HTTPAnswer{Method: "GET", URL: "https://registry.npmjs.org/portal", Status: 404})
	if _, _, err := publish(t, unlisted, false, nil, LocalPublish{Pipeline: p, Dir: dir, Version: v120}); err == nil || !strings.Contains(err.Error(), "NPM_TOKEN, which is not set") {
		t.Fatalf("no token: %v", err)
	}
	for _, req := range listed.Requests() {
		if strings.Contains(req.URL, "1.2.0") {
			t.Errorf("a version-specific URL was requested: %s", req.URL)
		}
	}
}

// The Python asked origin whether the version's tag existed before a local
// go publish, and the tag is pushed before the pipelines publish, so every
// local go publish was skipped. The notification and the install run.
func TestALocalGoPublishNotifiesTheProxyAndInstallsWithTheTagPushed(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH")
	}
	t.Setenv("GOPROXY", "off")
	dir := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(dir, "go.mod"), "module example.com/portal\n\ngo 1.21\n")
	testsupport.WriteFile(t, filepath.Join(dir, "cmd", "portal", "main.go"), "package main\n\nfunc main() {}\n")
	p := declarations.Pipeline{Name: "go", Type: "go", Target: "go", Artifact: "binary", Local: true, InstallPaths: []string{"./cmd/portal"}}
	_, log, err := publish(t, nil, true, nil, LocalPublish{Pipeline: p, Dir: dir, Version: v120})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"go list -json -m example.com/portal@v1.2.0", "go install ./cmd/portal"} {
		if !strings.Contains(log, want) {
			t.Errorf("the preview does not record %q:\n%s", want, log)
		}
	}
}
