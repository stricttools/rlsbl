package historyrewrite

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/semver"
)

var (
	shaA = strings.Repeat("a", 40)
	shaB = strings.Repeat("b", 40)
	shaC = strings.Repeat("c", 40)
	shaD = strings.Repeat("d", 40)
)

func TestTheEnvelopeIsReadWhole(t *testing.T) {
	hygiene.Isolate(t)
	p, err := parseMachineDocument(`{"interface_version":3,"payload":{"rewrites":{"` + shaA + `":"` + shaB + `"},"new_head":"` + shaB + `","tags":[{"refname":"refs/tags/v1","old_sha":"` + shaA + `","new_sha":"` + shaB + `","annotated":false}],"cleanup_ok":true,"something_new":1},"writes":null}`)
	if err != nil {
		t.Fatal(err)
	}
	if !p.rewritten() || p.Rewrites[shaA] != shaB || p.NewHead != shaB || len(p.Tags) != 1 || p.CleanupOK == nil || !*p.CleanupOK {
		t.Fatalf("the payload read as %+v", p)
	}
	if p, err := parseMachineDocument(`{"interface_version":3,"payload":null}`); err != nil || p != nil {
		t.Fatalf("a null payload read as %+v, %v", p, err)
	}
}

func TestAnythingButTheEnvelopeIsRefusedNamingTheSafegitNeeded(t *testing.T) {
	hygiene.Isolate(t)
	for name, stdout := range map[string]string{
		"empty":                "",
		"bare payload":         `{"rewrites":{}}`,
		"earlier interface":    `{"interface_version":2,"payload":null}`,
		"not JSON":             "Found 3 matches",
		"two documents":        `{"interface_version":3,"payload":null}` + "\n" + `{"x":1}`,
		"unknown interface":    `{"interface_version":4,"payload":null}`,
		"interface not number": `{"interface_version":"3","payload":null}`,
	} {
		_, err := parseMachineDocument(stdout)
		if err == nil || !strings.Contains(err.Error(), "safegit 0.31.1 or newer") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestEachModeBuildsSafegitsOwnArgv(t *testing.T) {
	hygiene.Isolate(t)
	globs := []string{".strictmetadata/changelog/*/*.jsonl"}
	pattern := ScrubRequest{Mode: ModePattern, Pattern: "tok", Replace: "x", FromCommit: shaA, Reason: "why"}
	want := []string{"--approve-consequential", "scrub", "match", "--json", "--pattern", "tok", "--replace", "x", "--from", shaA, "--remap-shas-in", globs[0], "--reason", "why"}
	if got := pattern.safegitArgs("/repo", globs, false, false); !slices.Equal(got, want) {
		t.Errorf("pattern: %q", got)
	}
	file := ScrubRequest{Mode: ModeFile, File: "secrets/key.pem", EntireHistory: true, Reason: "why"}
	if got := file.safegitArgs("/repo", globs, true, false); !slices.Equal(got, []string{"--approve-consequential", "scrub", "file", "--json", "--replace-with", filepath.Join("/repo", "secrets/key.pem"), "--entire-history", "--remap-shas-in", globs[0], "--reason", "why", "secrets/key.pem"}) {
		t.Errorf("file on disk: %q", got)
	}
	if got := file.safegitArgs("/repo", globs, false, true); !slices.Contains(got, "--delete") || !slices.Contains(got, "--dry-run") || got[len(got)-1] != "secrets/key.pem" {
		t.Errorf("file gone from disk, dry run: %q", got)
	}
	recipe := ScrubRequest{Mode: ModeRecipe, Recipe: "/r.toml", EntireHistory: true, Reason: "why"}
	if got := recipe.safegitArgs("/repo", nil, false, false); !slices.Equal(got, []string{"--approve-consequential", "scrub", "run", "--json", "/r.toml", "--entire-history", "--reason", "why"}) {
		t.Errorf("recipe: %q", got)
	}
}

func TestARequestNoScrubCanRunIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	for name, req := range map[string]ScrubRequest{
		"empty reason":     {Mode: ModePattern, Pattern: "x", Mangle: true, EntireHistory: true},
		"no range":         {Mode: ModePattern, Pattern: "x", Mangle: true, Reason: "r"},
		"both ranges":      {Mode: ModePattern, Pattern: "x", Mangle: true, EntireHistory: true, FromCommit: shaA, Reason: "r"},
		"empty pattern":    {Mode: ModePattern, Mangle: true, EntireHistory: true, Reason: "r"},
		"no substitution":  {Mode: ModePattern, Pattern: "x", EntireHistory: true, Reason: "r"},
		"file outside":     {Mode: ModeFile, File: "../x", EntireHistory: true, Reason: "r"},
		"absolute file":    {Mode: ModeFile, File: "/etc/x", EntireHistory: true, Reason: "r"},
		"empty recipe":     {Mode: ModeRecipe, EntireHistory: true, Reason: "r"},
		"no mode":          {EntireHistory: true, Reason: "r"},
		"replace & mangle": {Mode: ModePattern, Pattern: "x", Replace: "y", Mangle: true, EntireHistory: true, Reason: "r"},
	} {
		if err := req.validate(); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestComposingRewritesMapsEachOriginalCommitToItsFinalOne(t *testing.T) {
	hygiene.Isolate(t)
	saved := &scrubState{
		Rewrites: map[string]string{shaA: shaB},
		Tags:     []TagRewrite{{Refname: "refs/tags/v1", OldSHA: shaA, NewSHA: shaB}},
		NewHead:  shaB,
	}
	composeRewrites(saved, &scrubPayload{
		Rewrites: map[string]string{shaB: shaC, shaD: shaC},
		Tags:     []TagRewrite{{Refname: "refs/tags/v1", OldSHA: shaB, NewSHA: shaC}, {Refname: "refs/tags/v2", OldSHA: shaD, NewSHA: shaC}},
		NewHead:  shaC,
	})
	if saved.Rewrites[shaA] != shaC || saved.Rewrites[shaD] != shaC || len(saved.Rewrites) != 2 {
		t.Fatalf("composed %v", saved.Rewrites)
	}
	if saved.Tags[0].OldSHA != shaA || saved.Tags[0].NewSHA != shaC || len(saved.Tags) != 2 || saved.NewHead != shaC {
		t.Fatalf("composed tags %+v and head %s", saved.Tags, saved.NewHead)
	}
}

func TestSuccessiveRewritesChainAndACycleStops(t *testing.T) {
	hygiene.Isolate(t)
	x := explanations{
		commitMap: map[string]string{shaA: shaB, shaB: shaC},
		origins:   map[string]string{shaA: "the first", shaB: "the second"},
	}
	if final, chain := x.resolve(shaA); final != shaC || !slices.Equal(chain, []string{"the first", "the second"}) {
		t.Fatalf("resolved to %s through %v", final, chain)
	}
	x.commitMap[shaC] = shaA
	if final, _ := x.resolve(shaA); final != shaC {
		t.Fatalf("a cycle resolved to %s", final)
	}
}

func TestTheDigestCoversOriginAndTheReleaseListing(t *testing.T) {
	hygiene.Isolate(t)
	base := observation{remote: map[string]string{"refs/tags/v1": shaA}, releases: map[string]bool{"v1": true}}
	moved := observation{remote: map[string]string{"refs/tags/v1": shaB}, releases: map[string]bool{"v1": true}}
	unlisted := observation{remote: map[string]string{"refs/tags/v1": shaA}, releases: map[string]bool{}}
	local := observation{remote: map[string]string{"refs/tags/v1": shaA}, local: map[string]string{"refs/tags/v2": shaC}, releases: map[string]bool{"v1": true}}
	if base.digest() == moved.digest() || base.digest() == unlisted.digest() {
		t.Fatal("the digest did not change with the world")
	}
	if base.digest() != local.digest() {
		t.Fatal("the digest covers local refs, which the plan's items cover instead")
	}
}

func repairItem(key, state, target, observed string) previewapply.Item {
	v := semver.Version{Minor: 1}
	return previewapply.Item{Key: key, State: state, Data: refAction{subject: subjectRef, ref: key, version: v, hasVersion: true, target: target, observed: observed}}
}

func TestAnApplyPerformsOnlyWhatThePlanNamed(t *testing.T) {
	hygiene.Isolate(t)
	plan := &reconcilePlan{Items: []planItem{
		{Key: "refs/tags/v1", State: "re-point-with-lease", Target: shaB, Observed: shaA},
		{Key: "refs/tags/v2", State: "materialize", Target: shaC},
	}}
	fresh, _ := previewapply.NewPreview(repairItem("refs/tags/v1", stateRePoint, shaB, shaA))
	noops, err := checkPlanCovers(plan, fresh, "plan.toml")
	if err != nil || !slices.Equal(noops, []string{"refs/tags/v2"}) {
		t.Fatalf("a subject that became correct: %v, %v", noops, err)
	}
	for name, item := range map[string]previewapply.Item{
		"a subject the plan does not name": repairItem("refs/tags/v3", stateMaterialize, shaC, ""),
		"a changed verdict":                repairItem("refs/tags/v1", stateMaterialize, shaB, shaA),
		"a moved lease":                    repairItem("refs/tags/v1", stateRePoint, shaB, shaD),
		"a moved target":                   repairItem("refs/tags/v1", stateRePoint, shaD, shaA),
	} {
		p, _ := previewapply.NewPreview(item)
		if _, err := checkPlanCovers(plan, p, "plan.toml"); err == nil || !strings.Contains(err.Error(), "--mode plan") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestThePlanRoundTripsThroughItsSchema(t *testing.T) {
	hygiene.Isolate(t)
	p, _ := previewapply.NewPreview(
		repairItem("refs/tags/v1", stateRePoint, shaB, shaA),
		previewapply.Item{Key: "release:v1", State: stateAlreadyCorrect, Summary: "the GitHub Release exists"},
	)
	data := renderPlan(p, "digest", "0.1.0", time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC))
	plan, err := parsePlan("plan.toml", data)
	if err != nil {
		t.Fatalf("%v\n%s", err, data)
	}
	if plan.WorldDigest != "digest" || len(plan.Items) != 2 || plan.Items[0].Observed != shaA || plan.Items[0].Version != "0.1.0" || plan.Items[1].SubjectType != subjectRelease {
		t.Fatalf("the plan read back as %+v", plan)
	}
	empty, _ := previewapply.NewPreview()
	if _, err := parsePlan("plan.toml", renderPlan(empty, "digest", "0.1.0", time.Now())); err != nil {
		t.Fatalf("an empty plan: %v", err)
	}
	for name, doc := range map[string]string{
		"no format version": "generated_at = \"x\"\ngenerated_by = \"x\"\nworld_digest = \"x\"\nitems = []\n",
		"unknown verdict":   "format_version = 2\ngenerated_at = \"x\"\ngenerated_by = \"x\"\nworld_digest = \"x\"\n\n[[items]]\nkey = \"k\"\nsubject_type = \"ref\"\nstate = \"guess\"\n",
	} {
		if _, err := parsePlan("plan.toml", []byte(doc)); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestTheBumpIsTheHighestComponentThatMoved(t *testing.T) {
	hygiene.Isolate(t)
	v := func(s string) semver.Version {
		parsed, err := semver.Parse(s)
		if err != nil {
			t.Fatal(err)
		}
		return parsed
	}
	prev := v("0.1.0")
	for _, c := range []struct {
		version     string
		predecessor *semver.Version
		want        semver.Bump
	}{
		{"0.1.0", nil, semver.Minor},
		{"0.4.0", &prev, semver.Minor},
		{"0.1.3", &prev, semver.Patch},
		{"1.0.0", &prev, semver.Major},
	} {
		if got := deriveBump(v(c.version), c.predecessor); got != c.want {
			t.Errorf("%s: %s, want %s", c.version, got, c.want)
		}
	}
}

func TestADescriptionIsTakenFromTheBodysContentOnly(t *testing.T) {
	hygiene.Isolate(t)
	for body, want := range map[string]string{
		"**Full Changelog**: https://x/compare/a...b": "",
		"":                                   "",
		"<!-- rlsbl-ci-sha: x -->\n## Notes": "",
		"Fixes the parser.\nAnd the lexer.\n\n- a bullet":          "Fixes the parser. And the lexer.",
		"## Fixes\n\n- Fixed the parser\n- Fixed the lexer":        "Fixed the parser",
		"> A quoted summary":                                       "A quoted summary",
		"| file | size |\n| --- | --- |\n| portal.tar.gz | 1 MB |": "",
	} {
		if got := descriptionFromBody(body); got != want {
			t.Errorf("%q: %q, want %q", body, got, want)
		}
	}
	if !bodyIsSubstantive("| a | b |") || bodyIsSubstantive("# Heading\n\n**Full Changelog**: x") {
		t.Fatal("a table is content and a heading with the compare link is not")
	}
	if got := leadParagraph("The first release.\nIt works.\n\n### Fixes\n\n- x"); got != "The first release. It works." {
		t.Fatalf("lead paragraph %q", got)
	}
	if got := subjectDescription([]string{"a", "b", "c", "d", "e", "f"}); got != "Reconstructed from this version's commit subjects: a; b; c; d; e. (and later commits)" {
		t.Fatalf("subjects %q", got)
	}
}

func TestAnOverridesFileHoldsReviewedDescriptionsAndNothingElse(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	write := func(content string) string {
		path := filepath.Join(dir, "overrides.toml")
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		return path
	}
	got, err := readOverrides(write("[versions.\"0.1.0\"]\ndescription = \" Reviewed \"\ncontext = \"Why\"\n"))
	if err != nil || got["0.1.0"].description != "Reviewed" || got["0.1.0"].context != "Why" {
		t.Fatalf("read %v, %v", got, err)
	}
	for name, content := range map[string]string{
		"unknown top-level key": "[versions.\"0.1.0\"]\ndescription = \"x\"\n[other]\n",
		"no versions table":     "x = 1\n",
		"unknown entry key":     "[versions.\"0.1.0\"]\ndescription = \"x\"\nbump = \"minor\"\n",
		"empty description":     "[versions.\"0.1.0\"]\ndescription = \" \"\n",
		"not a version":         "[versions.\"latest\"]\ndescription = \"x\"\n",
		"context not a string":  "[versions.\"0.1.0\"]\ndescription = \"x\"\ncontext = 1\n",
	} {
		if _, err := readOverrides(write(content)); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
	if _, err := readOverrides(filepath.Join(dir, "missing.toml")); err == nil {
		t.Fatal("a missing file was accepted")
	}
}

func TestAGeneratedFileNamesItsFirstDifferentLine(t *testing.T) {
	hygiene.Isolate(t)
	got := firstDifference("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n", "# Changelog\n\n## 0.2.0\n")
	if !strings.Contains(got, "line 3") || !strings.Contains(got, "0.1.0") || !strings.Contains(got, "0.2.0") {
		t.Fatalf("difference %q", got)
	}
}
