package cli

import (
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/release"
)

const statusHelp = "Report on the member whose directory holds the working directory and one of its targets: the package name and version the " +
	"target's manifest declares, the branch and whether the working tree is clean, the latest release in the releasable's release archives (annotated " +
	"when this checkout does not contain it, when its commit is unrecoverable, and with the versions archived above it that were never released), " +
	"whether the changelog holds the released file of the current version, changelog coverage of the commits since the nearest release this checkout " +
	"contains (covered of those needing an entry, and how many are exempt), the member's own CI workflows and publish workflow, and a warning when " +
	"commits needing an entry wait since the nearest release. A member with several targets needs --target naming one; a workspace root whose root " +
	"member has no target is refused, naming `rlsbl monorepo status`. With --registry, the registry's package listing (npm's or PyPI's package " +
	"document, or the Go module proxy's version list) is read for the latest published version and compared with the local one; no request names a " +
	"single version, a registry that fails to answer is an error, and a member whose releasable publishes nothing is not asked about."

// The drift spellings the payload declares.
var statusDrifts = []any{release.DriftAhead, release.DriftBehind, release.DriftSame, release.DriftUnpublished, release.DriftPublishesNothing, nil}

func registerStatus(r *commandSet) {
	r.add(command{
		path:   []string{"status"},
		help:   statusHelp,
		effect: readOnly,
		flags: []strictcli.Flag{
			strictcli.StringFlag("target", "The member's target to report on; required when the member has more than one",
				strictcli.Optional(),
				strictcli.Choices(
					strictcli.Ch("go", "the member's Go module"),
					strictcli.Ch("npm", "the member's npm package"),
					strictcli.Ch("pypi", "the member's Python package"),
				)),
			strictcli.BoolFlag("registry", "Read the registry's package listing for the latest published version and compare it with the local one", strictcli.Default(false)),
		},
		payload: statusPayloadSchema(),
		render:  renderStatus,
		run:     runStatus,
	})
}

func runStatus(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, root, err := workingRepository()
	if err != nil {
		return nil, err
	}
	fork, err := forkHistory(ctx.Effects(), root)
	if err != nil {
		return nil, err
	}
	target, _ := strictcli.GetOpt[string](kw, "target")
	return release.ReadStatus(ctx.Effects(), release.StatusRequest{
		Dir:      dir,
		Target:   target,
		Registry: strictcli.Get[bool](kw, "registry"),
		Fork:     fork,
	})
}

// Schema fragments the release payloads share.
var (
	schemaString        = map[string]any{"type": "string"}
	schemaNullString    = map[string]any{"type": []any{"string", "null"}}
	schemaNullBool      = map[string]any{"type": []any{"boolean", "null"}}
	schemaNullInteger   = map[string]any{"type": []any{"integer", "null"}}
	schemaStrings       = map[string]any{"type": "array", "items": schemaString}
	schemaCoverageShape = map[string]any{
		"covered":  map[string]any{"type": "integer"},
		"total":    map[string]any{"type": "integer"},
		"exempted": map[string]any{"type": "integer"},
	}
)

// latestSchemaProperties are the properties of release.LatestFields.
func latestSchemaProperties() map[string]any {
	return map[string]any{
		"latest_release":             schemaNullString,
		"latest_release_in_checkout": schemaNullBool,
		"latest_release_state":       map[string]any{"type": []any{"string", "null"}, "enum": []any{"recorded", "unrecoverable", nil}},
		"never_released_versions":    schemaStrings,
		"latest_release_label":       schemaString,
	}
}

// closedObject is an object schema whose every property is required and
// which admits no other.
func closedObject(nullable bool, properties map[string]any) map[string]any {
	names := make([]string, 0, len(properties))
	for name := range properties {
		names = append(names, name)
	}
	sort.Strings(names)
	required := make([]any, len(names))
	for i, name := range names {
		required[i] = name
	}
	typ := any("object")
	if nullable {
		typ = []any{"object", "null"}
	}
	return map[string]any{"type": typ, "properties": properties, "required": required, "additionalProperties": false}
}

func statusPayloadSchema() map[string]any {
	properties := latestSchemaProperties()
	for name, schema := range map[string]any{
		"member":                map[string]any{"type": "string"},
		"releasable":            schemaNullString,
		"workspace":             map[string]any{"type": "boolean"},
		"target":                schemaString,
		"name":                  schemaString,
		"version":               schemaString,
		"version_files":         schemaStrings,
		"releasable_version":    schemaNullString,
		"branch":                schemaNullString,
		"clean":                 map[string]any{"type": "boolean"},
		"changelog_has_version": schemaNullBool,
		"coverage":              closedObject(true, schemaCoverageShape),
		"commits_ahead":         schemaNullInteger,
		"nearest_release_tag":   schemaNullString,
		"ci":                    schemaStrings,
		"publish":               map[string]any{"type": "boolean"},
		"registry_version":      schemaNullString,
		"drift":                 map[string]any{"type": []any{"string", "null"}, "enum": statusDrifts},
	} {
		properties[name] = schema
	}
	return closedObject(false, properties)
}

// coverageText words a coverage count.
func coverageText(c release.Coverage) string {
	text := fmt.Sprintf("%d/%d commits covered", c.Covered, c.Total)
	if c.Exempted > 0 {
		text += fmt.Sprintf(" (%d exempted)", c.Exempted)
	}
	return text
}

func renderStatus(payload any) string {
	st, err := decodePayload[release.Status](payload)
	if err != nil {
		return renderJSON(payload)
	}
	var lines []string
	add := func(label, value string) { lines = append(lines, fmt.Sprintf("%-11s%s", label+":", value)) }
	add("Package", st.Name)
	add("Version", fmt.Sprintf("%s (%s, %s)", st.Version, st.Target, strings.Join(st.VersionFiles, ", ")))
	add("Member", st.Member)
	if st.Releasable != nil {
		releasable := *st.Releasable
		if st.ReleasableVersion != nil {
			releasable += " (version file: " + *st.ReleasableVersion + ")"
		}
		add("Releasable", releasable)
	} else {
		add("Releasable", "(versioned under no releasable)")
	}
	if st.Drift != nil {
		switch *st.Drift {
		case release.DriftUnpublished:
			add("Registry", "(not published on "+st.Target+")")
		case release.DriftPublishesNothing:
			add("Registry", "(not asked: nothing publishes this member)")
		default:
			add("Registry", fmt.Sprintf("%s (%s, %s)", *st.RegistryVersion, st.Target, *st.Drift))
		}
	}
	if st.Branch != nil {
		add("Branch", *st.Branch)
	} else {
		add("Branch", "(detached HEAD)")
	}
	add("Released", st.LatestReleaseLabel)
	add("Clean", map[bool]string{true: "yes", false: "no"}[st.Clean])
	if st.ChangelogHasVersion != nil {
		version := st.Version
		if st.ReleasableVersion != nil {
			version = *st.ReleasableVersion
		}
		if *st.ChangelogHasVersion {
			add("Changelog", "has the released file of "+version)
		} else {
			add("Changelog", "no released file of "+version)
		}
	}
	if st.Coverage != nil {
		add("Coverage", coverageText(*st.Coverage))
	}
	if len(st.CI) > 0 {
		add("CI", strings.Join(st.CI, ", "))
	} else {
		add("CI", "missing")
	}
	add("Publish", map[bool]string{true: "yes", false: "missing"}[st.Publish])
	if st.CommitsAhead != nil && *st.CommitsAhead > 0 && st.NearestReleaseTag != nil {
		noun := "commits"
		if *st.CommitsAhead == 1 {
			noun = "commit"
		}
		lines = append(lines, fmt.Sprintf("! %d %s ahead of %s needing a release: run `rlsbl release run --watch --approve-consequential`, or investigate", *st.CommitsAhead, noun, *st.NearestReleaseTag))
	}
	return strings.Join(lines, "\n")
}
