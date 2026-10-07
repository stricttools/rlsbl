package options

import (
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
)

// RegistryFile is the registry document, relative to the repository root.
const RegistryFile = "internal/options/registry.toml"

// ChecksFile is the checks registry the options registry is rendered from,
// relative to the repository root.
const ChecksFile = "internal/checks/checks.toml"

// registryFormatVersion is the options-registry schema version the document
// is written to.
const registryFormatVersion = 2

const registryHeader = `# rlsbl's options registry: every rlsbl check is an option, rlsbl:<check name>,
# validated by strictspec's built-in options-registry schema. Generated from
# internal/checks/checks.toml by ` + "`go run ./internal/options/gen`" + `; never edit it
# by hand.
`

// The options named in code: the adoption options and the option that is
// neither a check nor an adoption.
const (
	// DepFloors is the check option policing ecosystem-internal dependency
	// floors; a member adopts it.
	DepFloors = "dep-floors"
	// TestSandbox is whether the repository distributes the sandboxed test
	// runner.
	TestSandbox = "test-sandbox"
	// EcosystemTagging is whether rlsbl tags a project as part of the rlsbl
	// ecosystem.
	EcosystemTagging = "ecosystem-tagging"
)

// AdoptionOptions are the options that stand for an adoption: each defaults
// to off, takes a path scope, and governs a declaration that is required
// while the option is on and refused while it is off (SettingsProblems).
var AdoptionOptions = []string{DepFloors, TestSandbox}

// FrameworkCheck is a check strictcli registers into rlsbl's app rather than
// rlsbl's own checks registry, with what its option carries.
type FrameworkCheck struct {
	Name        string
	Severity    string
	Subject     string
	Description string
}

// FrameworkChecks are the checks strictcli registers into rlsbl's app. A
// test of the app holds this list to the checks the framework registers.
var FrameworkChecks = []FrameworkCheck{
	{"cli-test-coverage", "error", "tests", "Every registered command path appears in the committed test-coverage manifest."},
	{"consequential-grant-agreement", "warn", "code", "Every command declaring a grant that leaves the process also declares itself consequential."},
	{"effects-bypass", "error", "code", "No process, filesystem-mutation, or network call reachable from a command handler bypasses the effects handle."},
	{"observe-allowlist-breadth", "warn", "code", "No proc_observe_allowlist prefix is a single token."},
}

// otherOption is an option that is not a check.
type otherOption struct {
	name        string
	values      string
	defaultTo   string
	subject     string
	description string
}

var otherOptions = []otherOption{
	{TestSandbox, "on > off", "off", "tests", "Whether the repository distributes the sandboxed test runner .strictmetadata/test-runner/test-runner.toml describes: scaffold renders the runner, and testisolation-floor requires every CI workflow the file names to invoke it."},
	{EcosystemTagging, "on > off", "on", "project", "Whether rlsbl tags the project as part of the rlsbl ecosystem: the rlsbl keyword in package.json and pyproject.toml, and the rlsbl topic on its GitHub repository, which `rlsbl discover` searches."},
}

// checksDocument is the checks registry as the generator reads it.
type checksDocument struct {
	App    string                 `toml:"app,required"`
	Checks map[string]checkFields `toml:"checks"`
	Hooks  map[string]any         `toml:"hooks"`
}

type checkFields struct {
	Description  string   `toml:"description,required"`
	Subject      string   `toml:"subject,required"`
	Tags         []string `toml:"tags,required"`
	Severity     string   `toml:"severity,required"`
	Fast         bool     `toml:"fast"`
	Pure         bool     `toml:"pure"`
	NeedsNetwork bool     `toml:"needs_network"`
	DependsOn    []string `toml:"depends_on"`
	Scope        string   `toml:"scope"`
}

// declaration is one rendered option.
type declaration struct {
	name        string
	subject     string
	values      string
	defaultTo   string
	scope       string
	description string
	requires    []string
}

func ranking(severity string) (string, error) {
	switch severity {
	case "error":
		return "error > warn > off", nil
	case "warn":
		return "warn > off", nil
	}
	return "", fmt.Errorf("severity %q is neither error nor warn", severity)
}

func isAdoption(name string) bool {
	for _, a := range AdoptionOptions {
		if a == name {
			return true
		}
	}
	return false
}

// allDeclarations are every option rlsbl declares, sorted by name.
func allDeclarations(checksTOML []byte) ([]declaration, error) {
	doc, err := tomledit.Unmarshal[checksDocument](checksTOML)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", ChecksFile, err)
	}
	var out []declaration
	seen := map[string]bool{}
	add := func(d declaration) error {
		if seen[d.name] {
			return fmt.Errorf("the option %s%s is declared twice: a check of %s takes the name of a framework check or of an option that is not a check", Prefix, d.name, ChecksFile)
		}
		seen[d.name] = true
		out = append(out, d)
		return nil
	}
	for name, check := range doc.Checks {
		values, err := ranking(check.Severity)
		if err != nil {
			return nil, fmt.Errorf("%s: check %s: %w", ChecksFile, name, err)
		}
		d := declaration{name: name, subject: check.Subject, values: values, defaultTo: check.Severity, scope: NoScope, description: check.Description}
		if isAdoption(name) {
			d.defaultTo, d.scope = Off, PathScope
		}
		d.requires = append([]string{}, check.DependsOn...)
		sort.Strings(d.requires)
		if err := add(d); err != nil {
			return nil, err
		}
	}
	for _, f := range FrameworkChecks {
		values, err := ranking(f.Severity)
		if err != nil {
			return nil, fmt.Errorf("framework check %s: %w", f.Name, err)
		}
		if err := add(declaration{name: f.Name, subject: f.Subject, values: values, defaultTo: f.Severity, scope: NoScope, description: f.Description, requires: []string{}}); err != nil {
			return nil, err
		}
	}
	for _, o := range otherOptions {
		scope := NoScope
		if isAdoption(o.name) {
			scope = PathScope
		}
		if err := add(declaration{name: o.name, subject: o.subject, values: o.values, defaultTo: o.defaultTo, scope: scope, description: o.description, requires: []string{}}); err != nil {
			return nil, err
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, nil
}

// RenderRegistry renders rlsbl's options registry from the checks registry
// document: every check an option ranked by its severity (an adoption
// option defaulting to off with a path scope), requiring the options of
// the checks it depends on; then the framework checks and the options that
// are not checks. The rendering is refused when strictspec refuses it, so a
// registry the generator writes is one rlsbl can load.
func RenderRegistry(checksTOML []byte) ([]byte, error) {
	decls, err := allDeclarations(checksTOML)
	if err != nil {
		return nil, err
	}
	q := tomledit.QuoteString
	lines := []string{registryHeader, fmt.Sprintf("format_version = %d", registryFormatVersion)}
	for _, d := range decls {
		requires := make([]string, len(d.requires))
		for i, r := range d.requires {
			requires[i] = q(r)
		}
		lines = append(lines,
			"",
			"[[option]]",
			"name = "+q(d.name),
			"subject = "+q(d.subject),
			"values = "+q(d.values),
			"default = "+q(d.defaultTo),
			"scope = "+q(d.scope),
			"requires = ["+strings.Join(requires, ", ")+"]",
			"description = "+q(d.description),
		)
	}
	rendered := []byte(strings.Join(lines, "\n") + "\n")
	if _, err := ParseRegistry(rendered); err != nil {
		return nil, err
	}
	return rendered, nil
}
