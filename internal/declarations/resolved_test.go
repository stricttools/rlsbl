package declarations

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestResolvedTargetsPairEachTargetWithItsPipelines(t *testing.T) {
	hygiene.Isolate(t)
	d := mustParse(t, fullSample)
	root := d.RootMember()
	got, err := ResolveTargets(root, root.Targets, PublishCI, "npm")
	if err != nil {
		t.Fatal(err)
	}
	var summary []string
	for _, r := range got {
		pipeline := "-"
		if r.Pipeline != nil {
			pipeline = r.Pipeline.Name
		}
		mark := ""
		if r.Primary {
			mark = "*"
		}
		summary = append(summary, r.Target.Name+"@"+r.Dir+":"+pipeline+mark)
	}
	if want := "go@.:go npm@web:npm*"; strings.Join(summary, " ") != want {
		t.Fatalf("resolved %q, want %q", strings.Join(summary, " "), want)
	}
}

func TestATargetWithoutAPipelineResolvesAlone(t *testing.T) {
	hygiene.Isolate(t)
	m := Member{Path: "lib", Name: "lib"}
	got, err := ResolveTargets(m, []Target{{Name: "pypi"}}, PublishNone, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Pipeline != nil || got[0].Dir != "lib" || got[0].Primary {
		t.Fatalf("resolved %#v", got)
	}
}

func TestAPipelineOfATargetTheMemberLacksIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	m := Member{Path: "lib", Name: "lib", Pipelines: []Pipeline{{Name: "npm", Type: "npm", Target: "npm", Artifact: "package"}}}
	if _, err := ResolveTargets(m, []Target{{Name: "pypi"}}, PublishCI, ""); err == nil || !strings.Contains(err.Error(), "npm") {
		t.Fatalf("ResolveTargets = %v, want a refusal naming the pipeline", err)
	}
}

func TestTwoReleasablesRenderingOneTagAreRefused(t *testing.T) {
	hygiene.Isolate(t)
	text := strings.Replace(fullSample, "tag_format = \"{name}@v{version}\"", "tag_format = \"v{version}\"", 1)
	if got := refusal(t, text); !strings.Contains(got, "\"portal\"") {
		t.Fatalf("the refusal does not name the other releasable:\n%s", got)
	}
}
