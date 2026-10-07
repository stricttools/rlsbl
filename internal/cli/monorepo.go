package cli

// monorepoHelp is the monorepo group's help.
const monorepoHelp = "Commands on a workspace: a repository whose .strictmetadata/releasables/releasables.toml declares repository_layout = \"workspace\", holding several members versioned under one or more releasables"

// registerMonorepo declares the monorepo group and registers its commands.
func registerMonorepo(r *commandSet) {
	r.group([]string{"monorepo"}, monorepoHelp)
	registerMonorepoSync(r)
}
