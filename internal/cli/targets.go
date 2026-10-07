package cli

import (
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/release"
)

const targetsHelp = "List every release target rlsbl supports (go, npm, pypi) with the files its version lives in, and whether the member whose " +
	"directory holds the working directory has it: the targets the member declares in .strictmetadata/releasables/releasables.toml, or, when it " +
	"declares none, the ones its manifests are detected as."

func registerTargets(r *commandSet) {
	row := map[string]any{
		"name":          schemaString,
		"carried":       map[string]any{"type": "boolean"},
		"version_files": schemaStrings,
	}
	r.add(command{
		path:   []string{"targets"},
		help:   targetsHelp,
		effect: readOnly,
		payload: closedObject(false, map[string]any{
			"member":   schemaString,
			"declared": map[string]any{"type": "boolean"},
			"targets":  map[string]any{"type": "array", "items": closedObject(false, row)},
		}),
		render: renderTargets,
		run: func(_ *strictcli.Context, _ map[string]any) (any, error) {
			dir, _, err := workingRepository()
			if err != nil {
				return nil, err
			}
			return release.ListTargets(dir)
		},
	})
}

func renderTargets(payload any) string {
	list, err := decodePayload[release.TargetList](payload)
	if err != nil {
		return renderJSON(payload)
	}
	how := "detected from its manifests"
	if list.Declared {
		how = "declared in .strictmetadata/releasables/releasables.toml"
	}
	rows := make([][]string, 0, len(list.Targets))
	for _, t := range list.Targets {
		rows = append(rows, []string{t.Name, map[bool]string{true: "yes", false: "no"}[t.Carried], strings.Join(t.VersionFiles, ", ")})
	}
	return fmt.Sprintf("Targets of the member %s (%s):\n\n", list.Member, how) + renderTable([]string{"Target", "In this member", "Version file"}, rows)
}
