package declarations

import (
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/declarations/releasablesspec"
)

// Error is a refused document: the file and every problem found in it, each
// naming the table it is in.
type Error struct {
	File     string
	Problems []string
}

func (e *Error) Error() string {
	if len(e.Problems) == 1 {
		return e.File + ": " + e.Problems[0]
	}
	return e.File + " is refused:\n  " + strings.Join(e.Problems, "\n  ")
}

// refuse is the error for problems found in file, or nil when there are none.
func refuse(file string, problems []string) error {
	if len(problems) == 0 {
		return nil
	}
	return &Error{File: file, Problems: problems}
}

// diagnosticProblems renders strictspec's diagnostics as problems, in the
// order strictspec emitted them.
func diagnosticProblems(diags []strictspec.Diagnostic) []string {
	out := make([]string, 0, len(diags))
	for _, d := range diags {
		where := d.Path
		if where == "" {
			where = "the document"
		}
		out = append(out, fmt.Sprintf("%s: %s [%s]", where, d.Message, d.Code))
	}
	return out
}

// Load reads and parses the release declarations of the repository rooted
// at root. A repository without the file is refused, naming it.
func Load(root string) (*Releasables, error) {
	data, found, err := readRecord(root, ReleasablesFile)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, fmt.Errorf("%s holds no %s: rlsbl reads a repository's release declarations from that file and from nowhere else", root, ReleasablesFile)
	}
	return Parse(data)
}

// Parse parses release declarations: the generated validator, the strict
// typed decode, and the declaration rules, in that order. Each pass reports
// every problem it finds; a later pass runs only when the earlier ones found
// none, since its problems would be noise from the ones already named.
func Parse(data []byte) (*Releasables, error) {
	if _, diags := releasablesspec.ValidateBytes(data, "toml"); len(diags) > 0 {
		return nil, refuse(ReleasablesFile, diagnosticProblems(diags))
	}
	raw, err := tomledit.Unmarshal[rawReleasables](data)
	if err != nil {
		return nil, refuse(ReleasablesFile, []string{err.Error()})
	}
	d, problems := raw.convert()
	if len(problems) > 0 {
		return nil, refuse(ReleasablesFile, problems)
	}
	if problems := check(d); len(problems) > 0 {
		return nil, refuse(ReleasablesFile, problems)
	}
	return d, nil
}

// The raw document: the file's keys as the strict decode reads them. A
// pointer is a key the file may leave out; a slice left nil is one it did.

type rawReleasables struct {
	FormatVersion    int64           `toml:"format_version,required"`
	RepositoryLayout string          `toml:"repository_layout,required"`
	ReleaseBranches  []string        `toml:"release_branches,required"`
	GitHubRepository *string         `toml:"github_repository"`
	EnvironmentFile  *string         `toml:"environment_file"`
	Timeouts         *rawTimeouts    `toml:"timeouts"`
	Releasables      []rawReleasable `toml:"releasables,required"`
	Members          []rawMember     `toml:"members,required"`
}

type rawTimeouts struct {
	PushSeconds  *int64 `toml:"push_seconds"`
	CISeconds    *int64 `toml:"ci_seconds"`
	CheckSeconds *int64 `toml:"check_seconds"`
	HookSeconds  *int64 `toml:"hook_seconds"`
}

type rawReleasable struct {
	Name                  string    `toml:"name,required"`
	TagFormat             string    `toml:"tag_format,required"`
	PublishMode           string    `toml:"publish_mode,required"`
	PublishCICheckPattern *string   `toml:"publish_ci_check_pattern"`
	DeployCommand         []string  `toml:"deploy_command"`
	Hooks                 *rawHooks `toml:"hooks"`
}

// rawHooks holds each entry as decoded: a string, or a table.
type rawHooks struct {
	PreChecks   []any `toml:"pre_checks"`
	PreRelease  []any `toml:"pre_release"`
	PostRelease []any `toml:"post_release"`
}

type rawMember struct {
	Path              string             `toml:"path,required"`
	Name              string             `toml:"name,required"`
	Releasable        any                `toml:"releasable,required"`
	DevOnly           *bool              `toml:"dev_only"`
	Library           *bool              `toml:"library"`
	TestOnly          *bool              `toml:"test_only"`
	DependsOn         []string           `toml:"depends_on"`
	ImportName        *string            `toml:"import_name"`
	RegistryName      *string            `toml:"registry_name"`
	Description       *string            `toml:"description"`
	LintAllow         []string           `toml:"lint_allow"`
	InternalDepFloors []string           `toml:"internal_dep_floors"`
	Targets           []rawTarget        `toml:"targets"`
	Hooks             *rawHooks          `toml:"hooks"`
	ExternalChecks    []rawExternalCheck `toml:"external_checks"`
	Test              *rawTest           `toml:"test"`
	Pipelines         []rawPipeline      `toml:"pipelines"`
}

type rawTarget struct {
	Name string  `toml:"name,required"`
	Path *string `toml:"path"`
}

type rawExternalCheck struct {
	Name      string   `toml:"name,required"`
	Tag       string   `toml:"tag,required"`
	Command   string   `toml:"command,required"`
	DependsOn []string `toml:"depends_on"`
	Cwd       *string  `toml:"cwd"`
}

type rawTest struct {
	PyPIMarkers *string `toml:"pypi_markers"`
	GoCommand   *string `toml:"go_command"`
}

type rawPipeline struct {
	Name           string   `toml:"name,required"`
	Type           string   `toml:"type,required"`
	Target         string   `toml:"target,required"`
	Local          bool     `toml:"local,required"`
	Artifact       string   `toml:"artifact,required"`
	InstallPaths   []string `toml:"install_paths"`
	HomebrewTap    *string  `toml:"homebrew_tap"`
	BinaryPipeline *string  `toml:"binary_pipeline"`
}

func deref[T any](p *T) T {
	var zero T
	if p == nil {
		return zero
	}
	return *p
}

// convert builds the typed declarations, naming every value the typed model
// cannot hold. The schema refuses each of these first; the conversion does
// not assume it ran.
func (raw *rawReleasables) convert() (*Releasables, []string) {
	var problems []string
	if raw.FormatVersion != 1 {
		problems = append(problems, fmt.Sprintf("format_version is %d; this rlsbl reads format_version 1", raw.FormatVersion))
	}
	d := &Releasables{
		Layout:           Layout(raw.RepositoryLayout),
		ReleaseBranches:  raw.ReleaseBranches,
		GitHubRepository: deref(raw.GitHubRepository),
		EnvironmentFile:  deref(raw.EnvironmentFile),
	}
	if t := raw.Timeouts; t != nil {
		d.Timeouts = Timeouts{
			PushSeconds:  deref(t.PushSeconds),
			CISeconds:    deref(t.CISeconds),
			CheckSeconds: deref(t.CheckSeconds),
			HookSeconds:  deref(t.HookSeconds),
		}
	}
	for i, r := range raw.Releasables {
		where := releasableLabel(i, r.Name)
		hooks, hookProblems := convertHooks(r.Hooks, where)
		problems = append(problems, hookProblems...)
		d.Releasables = append(d.Releasables, Releasable{
			Name:                  r.Name,
			TagFormat:             r.TagFormat,
			PublishMode:           PublishMode(r.PublishMode),
			PublishCICheckPattern: deref(r.PublishCICheckPattern),
			DeployCommand:         r.DeployCommand,
			Hooks:                 hooks,
		})
	}
	for i, m := range raw.Members {
		where := memberLabel(i, m.Name)
		member := Member{
			Path:              m.Path,
			Name:              m.Name,
			DevOnly:           deref(m.DevOnly),
			Library:           deref(m.Library),
			TestOnly:          deref(m.TestOnly),
			DependsOn:         m.DependsOn,
			ImportName:        deref(m.ImportName),
			RegistryName:      deref(m.RegistryName),
			Description:       deref(m.Description),
			LintAllow:         m.LintAllow,
			InternalDepFloors: m.InternalDepFloors,
		}
		switch v := m.Releasable.(type) {
		case string:
			member.Releasable = v
		case bool:
			if v {
				problems = append(problems, where+": releasable = true names no releasable; write a releasable's name, or false for a member versioned under none")
			}
		default:
			problems = append(problems, fmt.Sprintf("%s: releasable must be a releasable's name or false, not %T", where, v))
		}
		for _, t := range m.Targets {
			member.Targets = append(member.Targets, Target{Name: t.Name, Path: deref(t.Path)})
		}
		hooks, hookProblems := convertHooks(m.Hooks, where)
		problems = append(problems, hookProblems...)
		member.Hooks = hooks
		for _, c := range m.ExternalChecks {
			member.ExternalChecks = append(member.ExternalChecks, ExternalCheck{
				Name:      c.Name,
				Tag:       c.Tag,
				Command:   c.Command,
				DependsOn: c.DependsOn,
				Cwd:       deref(c.Cwd),
			})
		}
		if t := m.Test; t != nil {
			member.Test = TestSettings{PyPIMarkers: deref(t.PyPIMarkers), GoCommand: deref(t.GoCommand)}
		}
		for _, p := range m.Pipelines {
			member.Pipelines = append(member.Pipelines, Pipeline{
				Name:           p.Name,
				Type:           p.Type,
				Target:         p.Target,
				Local:          p.Local,
				Artifact:       p.Artifact,
				InstallPaths:   p.InstallPaths,
				HomebrewTap:    deref(p.HomebrewTap),
				BinaryPipeline: deref(p.BinaryPipeline),
			})
		}
		d.Members = append(d.Members, member)
	}
	return d, problems
}

// hookPoints are the hook points in the order a release reaches them, with
// their keys.
var hookPoints = []string{"pre_checks", "pre_release", "post_release"}

func convertHooks(raw *rawHooks, where string) (Hooks, []string) {
	if raw == nil {
		return Hooks{}, nil
	}
	var problems []string
	convert := func(point string, entries []any) []Hook {
		var out []Hook
		for i, entry := range entries {
			h, problem := convertHook(entry)
			if problem != "" {
				problems = append(problems, fmt.Sprintf("%s: hooks.%s[%d]: %s", where, point, i, problem))
				continue
			}
			out = append(out, h)
		}
		return out
	}
	return Hooks{
		PreChecks:   convert(hookPoints[0], raw.PreChecks),
		PreRelease:  convert(hookPoints[1], raw.PreRelease),
		PostRelease: convert(hookPoints[2], raw.PostRelease),
	}, problems
}

// convertHook reads one hook entry: a command line, or a table with cmd and
// optionally dir and env.
func convertHook(entry any) (Hook, string) {
	switch v := entry.(type) {
	case string:
		return Hook{Command: v}, ""
	case map[string]any:
		var h Hook
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			switch k {
			case "cmd":
				s, ok := v[k].(string)
				if !ok {
					return Hook{}, fmt.Sprintf("cmd must be a string, not %T", v[k])
				}
				h.Command = s
			case "dir":
				s, ok := v[k].(string)
				if !ok {
					return Hook{}, fmt.Sprintf("dir must be a string, not %T", v[k])
				}
				h.Dir = s
			case "env":
				table, ok := v[k].(map[string]any)
				if !ok {
					return Hook{}, fmt.Sprintf("env must be a table of strings, not %T", v[k])
				}
				h.Env = map[string]string{}
				for name, value := range table {
					s, ok := value.(string)
					if !ok {
						return Hook{}, fmt.Sprintf("env.%s must be a string, not %T", name, value)
					}
					h.Env[name] = s
				}
			default:
				return Hook{}, fmt.Sprintf("the key %q is not a hook key; a hook table carries cmd, dir, and env", k)
			}
		}
		if _, ok := v["cmd"]; !ok {
			return Hook{}, "a hook table needs cmd, the command line it runs"
		}
		return h, ""
	default:
		return Hook{}, fmt.Sprintf("a hook is a command line or a table with cmd, dir, and env, not %T", entry)
	}
}

func releasableLabel(i int, name string) string {
	if name == "" {
		return fmt.Sprintf("releasables[%d]", i)
	}
	return fmt.Sprintf("releasables[%d] (%q)", i, name)
}

func memberLabel(i int, name string) string {
	if name == "" {
		return fmt.Sprintf("members[%d]", i)
	}
	return fmt.Sprintf("members[%d] (%q)", i, name)
}
