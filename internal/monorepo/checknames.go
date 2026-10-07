package monorepo

import (
	"time"

	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// NameCheck is one member's name checked against one registry or rule set.
type NameCheck struct {
	Member string
	// Name is the name checked: the member's registry_name when it declares
	// one, and the prefix, its name, and the suffix otherwise.
	Name   string
	Result registry.NameResult
}

// CheckNames checks the name of every member that is not dev-only (a
// dev-only member publishes nothing, so it has no name to hold), waiting
// delay between names whose check asked a registry. A member's
// registry_name is its name on the registries and is checked as declared,
// without the prefix and suffix.
func CheckNames(client registry.Client, ws *workspace.Workspace, eco registry.Ecosystem, prefix, suffix string, delay time.Duration) ([]NameCheck, error) {
	var members, names []string
	for _, m := range ws.Members() {
		if m.DevOnly {
			continue
		}
		name := prefix + m.Name + suffix
		if m.RegistryName != "" {
			name = m.RegistryName
		}
		members = append(members, m.Name)
		names = append(names, name)
	}
	out := []NameCheck{}
	if len(names) == 0 {
		return out, nil
	}
	results, err := client.CheckNames(eco, names, delay)
	if err != nil {
		return nil, err
	}
	for i, r := range results {
		out = append(out, NameCheck{Member: members[i], Name: names[i], Result: r})
	}
	return out, nil
}
