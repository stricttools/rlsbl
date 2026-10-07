package cli

import (
	"os"
	"path/filepath"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/monorepo"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/workflows"
	"github.com/stricttools/rlsbl/internal/workspace"
)

const monorepoExtractHelp = "Move the releasable <releasable_name> out of this workspace into a new repository created at <target_path>, which must not " +
	"exist. Its members' history is rewritten by git-filter-repo on a fresh clone, a lone member hoisted to the repository root, and each " +
	"member's tree must be the tree that left. Its changelog and release records move to the new repository's .strictmetadata/, every " +
	"commit id and release commit mapped through git-filter-repo's commit map (a changelog entry none of whose commits carried over is " +
	"dropped, a release commit the rewrite did not carry is left as recorded, and each is named); a recorded tree the rewrite changed stops " +
	"the extract. Its tags take the new repository's scheme (v{version} for a lone member, its own tag format otherwise), the current " +
	"version keeping its old name beside the new one, and another releasable's tags are deleted there. The new repository gets its " +
	"declarations, a lifecycle-and-license record holding every entry of the departing subjects, and a transition record explaining the " +
	"conversion, each committed. This repository then loses the members, the releasable, and its records in one commit: the departure of " +
	"its tag namespace is recorded, the departing subjects' open periods and identities are closed in its lifecycle-and-license record, the " +
	"departing package names join the internal_dep_floors of every member of a releasable that stays (rlsbl:dep-floors switched on where " +
	"it is off), and the routers are regenerated; a failure before that commit puts every path back as it was. Deletions go through saferm " +
	"unless --delete-with-rm is passed. Refused before anything is written: a releasable holding the root member; a member nested in a " +
	"departing one that stays; a departing member with nothing tracked or holding a submodule; a target that exists; a missing git-filter-repo " +
	"or saferm; uncommitted changes; a release in flight; options entries scoped to a departing member; a member that stays depending on " +
	"one that leaves (each dependency named with the edit that severs it); a renamed tag colliding with another tag; and declarations or " +
	"records the result would leave invalid. Nothing is pushed and no remote is created. Use --dry-run to print the plan."

const monorepoAbsorbHelp = "Bring the repository at <source_repo> into this workspace as a member at <dest_path>. Its history is rewritten " +
	"under that path by git-filter-repo on a clone inside this repository's git directory, fetched without tags, and merged; the merge " +
	"carries trailers naming the member, the source's root commit, and the releasable, by which a run completing an interrupted absorb finds " +
	"it. Its version tags are created under the releasable's scheme at the rewritten commits, and its tag of the current version beside them " +
	"under its own name; no tag here is ever moved or deleted. Its records move into the new layout: its changelog and release archives " +
	"into the releasable's directories, every commit id and release commit mapped through the commit map and every recorded tree checked " +
	"at the new commit; its lifecycle-and-license entries into this repository's record under the member's name, with the source recorded " +
	"as the member's closed repository-url identity; and its transition record's events into this repository's, scoped to the releasable. " +
	"The arriving changelog and release directories and its CHANGELOG.md are then deleted (through saferm unless --delete-with-rm is passed); " +
	"the rest of its .strictmetadata/ is residue `rlsbl monorepo cleanup` removes. The member is declared and scaffolded (which regenerates " +
	"the routers), and the absorb is recorded in the transition record, each committed. --releasable joins a declared releasable; without " +
	"it a releasable named after the member is created with the stated --tag-format, and with --publish-mode when the source declares none " +
	"(the source's own otherwise). The source may be a standalone project in the new layout or declare nothing; one in the old layout or a " +
	"workspace is refused. Refused before anything is written: a dirty source or workspace, a destination path taken, a name taken, a tag " +
	"or version this workspace holds already, a version the source cannot state, and records the result would leave invalid. The source " +
	"repository is never changed, and nothing is pushed. Use --dry-run to print the plan."

func registerMonorepoConversions(r *commandSet, version string) {
	r.add(command{
		path:          []string{"monorepo", "extract"},
		help:          monorepoExtractHelp,
		effect:        mutating,
		consequential: true,
		args: []strictcli.Arg{
			strictcli.NewArg("releasable_name", "The releasable to extract", strictcli.ArgRequired()),
			strictcli.NewArg("target_path", "Where the new repository is created; it must not exist", strictcli.ArgRequired()),
		},
		flags: []strictcli.Flag{
			strictcli.BoolFlag("delete-with-rm", "Delete with a plain removal instead of saferm (saferm when not passed)", strictcli.Optional()),
		},
		run: runMonorepoExtract,
	})
	r.add(command{
		path:          []string{"monorepo", "absorb"},
		help:          monorepoAbsorbHelp,
		effect:        mutating,
		consequential: true,
		args: []strictcli.Arg{
			strictcli.NewArg("source_repo", "The repository to absorb", strictcli.ArgRequired()),
			strictcli.NewArg("dest_path", "The member's directory, relative to the workspace root; it must not exist", strictcli.ArgRequired()),
		},
		flags: []strictcli.Flag{
			strictcli.StringFlag("name", "The member's name (its directory's name when not passed)", strictcli.Optional()),
			strictcli.StringFlag("registry-name", "The member's name on the registries, when it differs from its name", strictcli.Optional()),
			strictcli.StringFlag("releasable", "A declared releasable the member joins (a releasable named after the member is created when not passed)", strictcli.Optional()),
			strictcli.StringFlag("tag-format", "The tag format of the releasable the absorb creates, holding {version} and optionally {name}", strictcli.Optional()),
			strictcli.StringFlag("publish-mode", "The publish mode of the releasable the absorb creates, when the source declares none", strictcli.Optional(),
				strictcli.Choices(
					strictcli.Ch("ci", "publish from CI"),
					strictcli.Ch("none", "publish nothing to any registry"),
				)),
			strictcli.BoolFlag("delete-with-rm", "Delete with a plain removal instead of saferm (saferm when not passed)", strictcli.Optional()),
		},
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			return nil, runMonorepoAbsorb(ctx, kw, version)
		},
	})
}

func runMonorepoExtract(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	ws, err := monorepo.Load(dir, "extract")
	if err != nil {
		return nil, err
	}
	target, err := filepath.Abs(strictcli.Get[string](kw, "target_path"))
	if err != nil {
		return nil, err
	}
	gh, err := github.New(ctx.Effects())
	if err != nil {
		return nil, err
	}
	actions, err := scaffold.ActionVersions()
	if err != nil {
		return nil, err
	}
	now := time.Now()
	return nil, monorepo.Extract(ctx, ws, monorepo.ExtractRequest{
		Releasable:   strictcli.Get[string](kw, "releasable_name"),
		Target:       target,
		DeleteWithRm: optionalBool(kw, "delete_with_rm", false),
		Now:          now,
		Say:          ctx.Out,
		Sync: func(remaining *workspace.Workspace) (workflows.SyncResult, error) {
			return workflows.Sync(ctx.Effects(), remaining, workflows.SyncInputs{
				Actions: actions,
				RootPublishWorkflow: func(root declarations.Member) (string, error) {
					return scaffold.MemberPublishWorkflow(ctx.Effects(), remaining, root, gh, now)
				},
			})
		},
	})
}

func runMonorepoAbsorb(ctx *strictcli.Context, kw map[string]any, version string) error {
	dir, err := os.Getwd()
	if err != nil {
		return err
	}
	ws, err := monorepo.Load(dir, "absorb")
	if err != nil {
		return err
	}
	source, err := filepath.Abs(strictcli.Get[string](kw, "source_repo"))
	if err != nil {
		return err
	}
	gh, err := github.New(ctx.Effects())
	if err != nil {
		return err
	}
	return monorepo.Absorb(ctx, ws, monorepo.AbsorbRequest{
		Source:       source,
		Dest:         strictcli.Get[string](kw, "dest_path"),
		Name:         optionalString(kw, "name"),
		RegistryName: optionalString(kw, "registry_name"),
		Releasable:   optionalString(kw, "releasable"),
		TagFormat:    optionalString(kw, "tag_format"),
		PublishMode:  optionalString(kw, "publish_mode"),
		DeleteWithRm: optionalBool(kw, "delete_with_rm", false),
		Now:          time.Now(),
		Version:      version,
		GitHub:       gh,
		Say:          ctx.Out,
	})
}
