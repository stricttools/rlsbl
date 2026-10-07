package cli

import (
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/tagging"
)

const discoverHelp = "List the GitHub repositories carrying the rlsbl topic, most recently updated first, through the authenticated gh " +
	"command line. With --mine, only the repositories the authenticated account owns. GitHub's search returns at most 1000 " +
	"repositories, and a topic carried by more is refused rather than listed in part."

// discoverPayload is discover's payload.
type discoverPayload struct {
	Mine         bool                     `json:"mine"`
	Repositories []github.FoundRepository `json:"repositories"`
}

func registerDiscover(r *commandSet) {
	r.add(command{
		path:   []string{"discover"},
		help:   discoverHelp,
		effect: readOnly,
		flags: []strictcli.Flag{
			strictcli.BoolFlag("mine", "List only the repositories the authenticated GitHub account owns", strictcli.Default(false)),
		},
		payload: discoverPayloadSchema(),
		render:  renderDiscover,
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			mine := strictcli.Get[bool](kw, "mine")
			gh, err := github.New(ctx.Effects())
			if err != nil {
				return nil, err
			}
			found, err := tagging.Discover(gh, mine)
			if err != nil {
				return nil, err
			}
			return discoverPayload{Mine: mine, Repositories: append([]github.FoundRepository{}, found...)}, nil
		},
	})
}

func discoverPayloadSchema() map[string]any {
	str := map[string]any{"type": "string"}
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"mine": map[string]any{"type": "boolean"},
			"repositories": map[string]any{"type": "array", "items": map[string]any{
				"type":                 "object",
				"properties":           map[string]any{"full_name": str, "description": str, "updated_at": str, "owner": str},
				"required":             []any{"full_name", "description", "updated_at", "owner"},
				"additionalProperties": false,
			}},
		},
		"required":             []any{"mine", "repositories"},
		"additionalProperties": false,
	}
}

func renderDiscover(payload any) string {
	p, err := decodePayload[discoverPayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	if len(p.Repositories) == 0 {
		if p.Mine {
			return "No rlsbl-tagged repositories found for your account."
		}
		return "No rlsbl-tagged repositories found."
	}
	now := time.Now()
	rows := make([][]string, 0, len(p.Repositories))
	for _, repo := range p.Repositories {
		updated, err := tagging.RelativeTime(repo.UpdatedAt, now)
		if err != nil {
			updated = repo.UpdatedAt
		}
		rows = append(rows, []string{repo.FullName, strings.Join(strings.Fields(repo.Description), " "), updated})
	}
	return fmt.Sprintf("rlsbl ecosystem (%d projects)\n\n", len(p.Repositories)) + renderTable([]string{"owner/repo", "description", "updated"}, rows)
}
