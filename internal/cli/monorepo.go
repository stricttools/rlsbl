package cli

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/monorepo"
)

// monorepoHelp is the monorepo group's help.
const monorepoHelp = "Commands on a workspace: a repository whose .strictmetadata/releasables/releasables.toml declares repository_layout = \"workspace\", holding several members versioned under one or more releasables"

const monorepoInitHelp = "Write .strictmetadata/releasables/releasables.toml for a repository that declares nothing yet, as a workspace whose only member " +
	"is the root member (path \".\", named root), which owns every file no other member claims. --root-member states what the root member is: " +
	"root-dev-node declares it dev-only and versioned under no releasable, so its files need no changelog entry; root-releasable versions it under " +
	"the releasable --releasable names, created with the --tag-format and --publish-mode stated (neither has a default), and a root releasable " +
	"publishing from CI states --publish-ci-check-pattern, the pattern of the check-run names the root's own CI reports, which publishing waits for. " +
	"--release-branch names each branch a release may run from. A repository whose declarations exist is refused. The declarations are committed " +
	"unless --no-auto-commit is passed; when the commit fails, the files the init created are removed again through saferm, so running it again " +
	"starts afresh."

const monorepoAddHelp = "Declare the directory at <path> (relative to the repository root) as a member of the workspace, then scaffold it as " +
	"`rlsbl scaffold` does, which regenerates the workspace's CI router and publish router, and commit what the three wrote as one commit. The " +
	"member is named --name, or its directory's name. It needs a release target: one detected from its manifests, or the one --target names, which " +
	"the scaffold declares when it is not detected. --releasable names the releasable it is versioned under, or is false for none; naming a " +
	"releasable the workspace does not declare creates it, and then --tag-format and --publish-mode are required, because a tag scheme and a publish " +
	"mode are stated, never derived (both are refused when no releasable is created). --depends-on names a member it depends on (repeatable), " +
	"--library and --dev-only mark it, and --registry-name is its name on the registries. Every refusal is made before anything is written. When " +
	"the scaffold, the routers, or the commit fails, every path the add changed is put back as it was and nothing is committed. A path that had " +
	"uncommitted changes before the add and is written by it is left uncommitted and named. --no-auto-commit leaves everything uncommitted. Under " +
	"--dry-run the declarations are previewed and the scaffold is named, not previewed, since it renders from the declarations the preview did not " +
	"write."

const monorepoRemoveHelp = "Delete the declaration of the member at <path>, the path exactly as releasables.toml writes it (any other spelling is " +
	"refused, naming every member's path), and write the declarations. The member's files are left on disk and nothing is committed. Refused: the " +
	"root member, the only member of a releasable (which would then release nothing), a member another member's depends_on names, and a member " +
	"the lifecycle-and-license record holds an open or pending entry for."

const monorepoListHelp = "List every member the workspace declares, in declaration order: its name, its path, the releasable it is versioned under " +
	"(empty for none), and the flags it declares (library, dev-only, test-only)."

func registerMonorepo(r *commandSet, version string) {
	r.group([]string{"monorepo"}, monorepoHelp)
	registerMonorepoSync(r)
	r.add(command{
		path:     []string{"monorepo", "init"},
		help:     monorepoInitHelp,
		effect:   mutating,
		noDryRun: "it writes the declarations every other command reads, and there are no declarations to preview against until they exist",
		flags: []strictcli.Flag{
			strictcli.ChoiceFlag("root-member", "What the root member is", strictcli.Required(),
				monorepoRootDevNode,
				monorepoRootReleasable,
			),
			strictcli.StringFlag("release-branch", "A branch a release may run from; repeatable", strictcli.Required(), strictcli.Repeatable(), strictcli.Unique(true)),
			strictcli.BoolFlag("auto-commit", "Commit the declarations (committed when neither --auto-commit nor --no-auto-commit is passed)", strictcli.Optional()),
		},
		run: runMonorepoInit,
	})
	r.add(command{
		path:   []string{"monorepo", "add"},
		help:   monorepoAddHelp,
		effect: mutating,
		args: []strictcli.Arg{
			strictcli.NewArg("path", "The member's directory, relative to the repository root", strictcli.ArgRequired()),
		},
		flags: []strictcli.Flag{
			strictcli.StringFlag("name", "The member's name (its directory's name when not passed)", strictcli.Optional()),
			strictcli.StringFlag("target", "A target the member must have, declared by the scaffold when it is not detected", strictcli.Optional(),
				strictcli.Choices(
					strictcli.Ch("go", "a Go module"),
					strictcli.Ch("npm", "an npm package"),
					strictcli.Ch("pypi", "a Python package"),
				)),
			strictcli.StringFlag("depends-on", "A member this member depends on; repeatable", strictcli.Optional(), strictcli.Repeatable(), strictcli.Unique(true)),
			strictcli.BoolFlag("library", "Declare the member a library (not declared when not passed)", strictcli.Optional()),
			strictcli.BoolFlag("dev-only", "Declare the member dev-only: nothing user-facing may depend on it (not declared when not passed)", strictcli.Optional()),
			strictcli.StringFlag("releasable", "The releasable the member is versioned under, or false for none; an undeclared name creates the releasable", strictcli.Required()),
			strictcli.StringFlag("tag-format", "The tag format of the releasable the add creates, holding {version} and optionally {name}", strictcli.Optional()),
			strictcli.StringFlag("publish-mode", "The publish mode of the releasable the add creates", strictcli.Optional(),
				strictcli.Choices(
					strictcli.Ch("ci", "publish from CI"),
					strictcli.Ch("none", "publish nothing to any registry"),
				)),
			strictcli.StringFlag("registry-name", "The member's name on the registries, when it differs from its name", strictcli.Optional()),
			strictcli.BoolFlag("auto-commit", "Commit what the add wrote as one commit (committed when neither --auto-commit nor --no-auto-commit is passed)", strictcli.Optional()),
		},
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			return nil, runMonorepoAdd(ctx, kw, version)
		},
	})
	r.add(command{
		path:     []string{"monorepo", "remove"},
		help:     monorepoRemoveHelp,
		effect:   mutating,
		noDryRun: "the whole edit is deleting the one declaration you named, and a preview of it would restate the path back to you",
		args: []strictcli.Arg{
			strictcli.NewArg("path", "The member's path as releasables.toml writes it", strictcli.ArgRequired()),
		},
		run: runMonorepoRemove,
	})
	r.add(command{
		path:    []string{"monorepo", "list"},
		help:    monorepoListHelp,
		effect:  readOnly,
		payload: listPayloadSchema(),
		render:  renderList,
		run:     runMonorepoList,
	})
	registerMonorepoReports(r)
	registerMonorepoMaintenance(r)
}

// The choices of monorepo init's --root-member.
var (
	monorepoRootDevNode = strictcli.Choice("root-dev-node",
		"the root member is dev-only and versioned under no releasable: its files need no changelog entry and are never released")
	monorepoRootReleasable = strictcli.Choice("root-releasable",
		"the root member is versioned under a releasable created with it: its files need changelog entries and ship with its releases",
		strictcli.StringFlag("releasable", "The name of the releasable the root member is versioned under", strictcli.Required()),
		strictcli.StringFlag("tag-format", "The releasable's tag format, holding {version} and optionally {name}: \"v{version}\" for bare version tags, \"{name}@v{version}\" for the workspace scheme", strictcli.Required()),
		strictcli.StringFlag("publish-mode", "The releasable's publish mode", strictcli.Required(),
			strictcli.Choices(
				strictcli.Ch("ci", "publish from CI"),
				strictcli.Ch("none", "publish nothing to any registry"),
			)),
		strictcli.StringFlag("publish-ci-check-pattern", "The pattern of the check-run names the root's own CI reports, which publishing waits for on the release commit; required with --publish-mode ci", strictcli.Optional()),
	)
)

// optionalBool is an optional boolean flag's value, fallback when it was not
// passed.
func optionalBool(kw map[string]any, name string, fallback bool) bool {
	v, set := strictcli.GetOpt[bool](kw, name)
	if !set {
		return fallback
	}
	return v
}

// optionalString is an optional string flag's value, empty when it was not
// passed.
func optionalString(kw map[string]any, name string) string {
	v, _ := strictcli.GetOpt[string](kw, name)
	return v
}

// optionalStrings is an optional repeatable string flag's values.
func optionalStrings(kw map[string]any, name string) ([]string, error) {
	if kw[name] == nil {
		return nil, nil
	}
	return stringList(kw[name])
}

func runMonorepoInit(ctx *strictcli.Context, kw map[string]any) (any, error) {
	_, root, err := workingRepository()
	if err != nil {
		return nil, err
	}
	branches, err := stringList(kw["release_branch"])
	if err != nil {
		return nil, err
	}
	req := monorepo.InitRequest{
		Root:            root,
		ReleaseBranches: branches,
		AutoCommit:      optionalBool(kw, "auto_commit", true),
		Say:             ctx.Out,
	}
	elected := strictcli.GetElected(kw, "root_member")
	if elected.Is(monorepoRootReleasable) {
		pattern, _ := strictcli.GetOpt[string](elected.Fields, "publish_ci_check_pattern")
		req.RootReleasable = &declarations.Releasable{
			Name:                  strictcli.Get[string](elected.Fields, "releasable"),
			TagFormat:             strictcli.Get[string](elected.Fields, "tag_format"),
			PublishMode:           declarations.PublishMode(strictcli.Get[string](elected.Fields, "publish_mode")),
			PublishCICheckPattern: pattern,
		}
	}
	return nil, monorepo.Init(ctx.Effects(), req)
}

func runMonorepoAdd(ctx *strictcli.Context, kw map[string]any, version string) error {
	dir, err := os.Getwd()
	if err != nil {
		return err
	}
	dependsOn, err := optionalStrings(kw, "depends_on")
	if err != nil {
		return err
	}
	gh, err := github.New(ctx.Effects())
	if err != nil {
		return err
	}
	return monorepo.Add(ctx.Effects(), monorepo.AddRequest{
		Dir:          dir,
		Path:         strictcli.Get[string](kw, "path"),
		Name:         optionalString(kw, "name"),
		Target:       optionalString(kw, "target"),
		DependsOn:    dependsOn,
		Library:      optionalBool(kw, "library", false),
		DevOnly:      optionalBool(kw, "dev_only", false),
		Releasable:   strictcli.Get[string](kw, "releasable"),
		TagFormat:    optionalString(kw, "tag_format"),
		PublishMode:  optionalString(kw, "publish_mode"),
		RegistryName: optionalString(kw, "registry_name"),
		AutoCommit:   optionalBool(kw, "auto_commit", true),
		DryRun:       ctx.DryRun(),
		Version:      version,
		Now:          time.Now(),
		GitHub:       gh,
		Say:          ctx.Out,
	})
}

func runMonorepoRemove(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	ws, err := monorepo.Load(dir, "remove")
	if err != nil {
		return nil, err
	}
	m, err := monorepo.Remove(ctx.Effects(), ws, strictcli.Get[string](kw, "path"))
	if err != nil {
		return nil, err
	}
	ctx.Out(fmt.Sprintf("Removed the member %q at %s from %s; its files are left as they are, and nothing was committed.", m.Name, m.Path, declarations.ReleasablesFile))
	return nil, nil
}

// listPayload is monorepo list's payload.
type listPayload struct {
	Members []monorepo.ListedMember `json:"members"`
}

func runMonorepoList(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	ws, err := monorepo.Load(dir, "list")
	if err != nil {
		return nil, err
	}
	return listPayload{Members: monorepo.List(ws)}, nil
}

func listPayloadSchema() map[string]any {
	return objectSchema(map[string]any{
		"members": arraySchema(objectSchema(map[string]any{
			"name":       stringSchema(),
			"path":       stringSchema(),
			"releasable": nullableStringSchema(),
			"library":    booleanSchema(),
			"dev_only":   booleanSchema(),
			"test_only":  booleanSchema(),
		})),
	})
}

func renderList(payload any) string {
	p, err := decodePayload[listPayload](payload)
	if err != nil {
		return renderJSON(payload)
	}
	var rows [][]string
	for _, m := range p.Members {
		releasable := ""
		if m.Releasable != nil {
			releasable = *m.Releasable
		}
		var flags []string
		for _, f := range []struct {
			set  bool
			word string
		}{{m.Library, "library"}, {m.DevOnly, "dev-only"}, {m.TestOnly, "test-only"}} {
			if f.set {
				flags = append(flags, f.word)
			}
		}
		rows = append(rows, []string{m.Name, m.Path, releasable, strings.Join(flags, ", ")})
	}
	return renderTable([]string{"Name", "Path", "Releasable", "Flags"}, rows)
}

// objectSchema is the JSON Schema of an object holding every property in
// props and nothing else.
func objectSchema(props map[string]any) map[string]any {
	required := make([]any, 0, len(props))
	names := make([]string, 0, len(props))
	for name := range props {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		required = append(required, name)
	}
	return map[string]any{"type": "object", "properties": props, "required": required, "additionalProperties": false}
}

func arraySchema(items map[string]any) map[string]any {
	return map[string]any{"type": "array", "items": items}
}

func stringSchema() map[string]any { return map[string]any{"type": "string"} }

func nullableStringSchema() map[string]any {
	return map[string]any{"type": []any{"string", "null"}}
}

func booleanSchema() map[string]any { return map[string]any{"type": "boolean"} }

func integerSchema() map[string]any { return map[string]any{"type": "integer"} }

func stringArraySchema() map[string]any { return arraySchema(stringSchema()) }
