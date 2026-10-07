package release

import (
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// TargetRow is one supported release target as `rlsbl targets` lists it.
type TargetRow struct {
	Name string `json:"name"`
	// Carried is whether the member the working directory selects has this
	// target.
	Carried bool `json:"carried"`
	// VersionFiles are the files the target's version lives in.
	VersionFiles []string `json:"version_files"`
}

// TargetList is `rlsbl targets`'s report: every target rlsbl supports, and
// which of them the selected member has.
type TargetList struct {
	Member string `json:"member"`
	// Declared is whether the member declares its targets in
	// releasables.toml; otherwise they are detected from the manifests in
	// its directory.
	Declared bool        `json:"declared"`
	Targets  []TargetRow `json:"targets"`
}

// ListTargets lists every supported target against the member whose
// territory holds the absolute directory dir.
func ListTargets(dir string) (TargetList, error) {
	ws, err := workspace.Discover(dir)
	if err != nil {
		return TargetList{}, err
	}
	m, err := ws.MemberAtDirectory(dir)
	if err != nil {
		return TargetList{}, err
	}
	carried, err := targets.MemberTargets(ws.Root, m)
	if err != nil {
		return TargetList{}, err
	}
	has := map[string]bool{}
	for _, t := range carried {
		has[t.Name] = true
	}
	out := TargetList{Member: m.Name, Declared: m.Targets != nil, Targets: []TargetRow{}}
	for _, t := range targets.All() {
		out.Targets = append(out.Targets, TargetRow{
			Name:         t.Name(),
			Carried:      has[t.Name()],
			VersionFiles: append([]string{}, t.Facts().VersionFiles...),
		})
	}
	return out, nil
}
