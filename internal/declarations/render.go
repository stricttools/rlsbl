package declarations

import (
	"fmt"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
)

// Render is the text of a new releasables.toml holding d, in the layout the
// file is documented in: the top-level keys, [timeouts], every
// [[releasables]] table, then every [[members]] table with its
// [[members.pipelines]]. A key whose value is absent or false is not
// written. Render does not check d: Write parses what it writes.
func Render(d *Releasables) []byte {
	var b strings.Builder
	b.WriteString("format_version = 1\n")
	fmt.Fprintf(&b, "repository_layout = %s\n", quote(string(d.Layout)))
	fmt.Fprintf(&b, "release_branches = %s\n", stringArray(d.ReleaseBranches))
	if d.GitHubRepository != "" {
		fmt.Fprintf(&b, "github_repository = %s\n", quote(d.GitHubRepository))
	}
	if d.EnvironmentFile != "" {
		fmt.Fprintf(&b, "environment_file = %s\n", quote(d.EnvironmentFile))
	}
	if len(d.Releasables) == 0 {
		b.WriteString("releasables = []\n")
	}
	if t := renderTimeouts(d.Timeouts); t != "" {
		b.WriteString("\n" + t)
	}
	for _, r := range d.Releasables {
		b.WriteString("\n" + renderReleasable(r))
	}
	for _, m := range d.Members {
		b.WriteString("\n" + renderMember(m))
	}
	return []byte(b.String())
}

func quote(s string) string { return tomledit.QuoteString(s) }

func stringArray(values []string) string {
	quoted := make([]string, len(values))
	for i, v := range values {
		quoted[i] = quote(v)
	}
	return "[" + strings.Join(quoted, ", ") + "]"
}

// inlineTable renders an inline table from its key = value parts, empty
// parts left out.
func inlineTable(parts ...string) string {
	var kept []string
	for _, p := range parts {
		if p != "" {
			kept = append(kept, p)
		}
	}
	return "{ " + strings.Join(kept, ", ") + " }"
}

// field is "key = value" when value is not empty, and empty otherwise.
func field(key, value string) string {
	if value == "" {
		return ""
	}
	return key + " = " + value
}

func quotedField(key, value string) string {
	if value == "" {
		return ""
	}
	return field(key, quote(value))
}

func arrayField(key string, values []string) string {
	if len(values) == 0 {
		return ""
	}
	return field(key, stringArray(values))
}

func renderTimeouts(t Timeouts) string {
	var b strings.Builder
	for _, kv := range []struct {
		key   string
		value int64
	}{
		{"push_seconds", t.PushSeconds},
		{"ci_seconds", t.CISeconds},
		{"check_seconds", t.CheckSeconds},
		{"hook_seconds", t.HookSeconds},
	} {
		if kv.value != 0 {
			fmt.Fprintf(&b, "%s = %d\n", kv.key, kv.value)
		}
	}
	if b.Len() == 0 {
		return ""
	}
	return "[timeouts]\n" + b.String()
}

func renderHook(h Hook) string {
	if h.Dir == "" && len(h.Env) == 0 {
		return quote(h.Command)
	}
	var env string
	if len(h.Env) > 0 {
		names := make([]string, 0, len(h.Env))
		for name := range h.Env {
			names = append(names, name)
		}
		sort.Strings(names)
		pairs := make([]string, len(names))
		for i, name := range names {
			pairs[i] = tomledit.QuoteKey(name) + " = " + quote(h.Env[name])
		}
		env = "env = " + inlineTable(pairs...)
	}
	return inlineTable("cmd = "+quote(h.Command), quotedField("dir", h.Dir), env)
}

func renderHooks(h Hooks) string {
	if h.IsEmpty() {
		return ""
	}
	point := func(key string, hooks []Hook) string {
		if len(hooks) == 0 {
			return ""
		}
		entries := make([]string, len(hooks))
		for i, hook := range hooks {
			entries[i] = renderHook(hook)
		}
		return key + " = [" + strings.Join(entries, ", ") + "]"
	}
	return "hooks = " + inlineTable(
		point(hookPoints[0], h.PreChecks),
		point(hookPoints[1], h.PreRelease),
		point(hookPoints[2], h.PostRelease),
	)
}

func lines(parts ...string) string {
	var b strings.Builder
	for _, p := range parts {
		if p != "" {
			b.WriteString(p + "\n")
		}
	}
	return b.String()
}

func boolField(key string, value bool) string {
	if !value {
		return ""
	}
	return key + " = true"
}

// renderReleasable is one [[releasables]] table.
func renderReleasable(r Releasable) string {
	return lines(
		"[[releasables]]",
		quotedField("name", r.Name),
		quotedField("tag_format", r.TagFormat),
		quotedField("publish_mode", string(r.PublishMode)),
		quotedField("publish_ci_check_pattern", r.PublishCICheckPattern),
		arrayField("deploy_command", r.DeployCommand),
		renderHooks(r.Hooks),
	)
}

// renderMember is one [[members]] table and its [[members.pipelines]].
func renderMember(m Member) string {
	releasable := "false"
	if m.Releasable != "" {
		releasable = quote(m.Releasable)
	}
	var targets string
	if len(m.Targets) > 0 {
		entries := make([]string, len(m.Targets))
		for i, t := range m.Targets {
			entries[i] = inlineTable(quotedField("name", t.Name), quotedField("path", t.Path))
		}
		targets = "targets = [" + strings.Join(entries, ", ") + "]"
	}
	var checks string
	if len(m.ExternalChecks) > 0 {
		entries := make([]string, len(m.ExternalChecks))
		for i, c := range m.ExternalChecks {
			entries[i] = inlineTable(
				quotedField("name", c.Name),
				quotedField("tag", c.Tag),
				quotedField("command", c.Command),
				arrayField("depends_on", c.DependsOn),
				quotedField("cwd", c.Cwd),
			)
		}
		checks = "external_checks = [" + strings.Join(entries, ", ") + "]"
	}
	var test string
	if m.Test.PyPIMarkers != "" || m.Test.GoCommand != "" {
		test = "test = " + inlineTable(quotedField("pypi_markers", m.Test.PyPIMarkers), quotedField("go_command", m.Test.GoCommand))
	}
	text := lines(
		"[[members]]",
		quotedField("path", m.Path),
		quotedField("name", m.Name),
		"releasable = "+releasable,
		boolField("dev_only", m.DevOnly),
		boolField("library", m.Library),
		boolField("test_only", m.TestOnly),
		arrayField("depends_on", m.DependsOn),
		quotedField("import_name", m.ImportName),
		quotedField("registry_name", m.RegistryName),
		quotedField("description", m.Description),
		arrayField("lint_allow", m.LintAllow),
		arrayField("internal_dep_floors", m.InternalDepFloors),
		targets,
		renderHooks(m.Hooks),
		checks,
		test,
	)
	for _, p := range m.Pipelines {
		text += "\n" + lines(
			"[[members.pipelines]]",
			quotedField("name", p.Name),
			quotedField("type", p.Type),
			quotedField("target", p.Target),
			fmt.Sprintf("local = %t", p.Local),
			quotedField("artifact", p.Artifact),
			arrayField("install_paths", p.InstallPaths),
			quotedField("homebrew_tap", p.HomebrewTap),
			quotedField("binary_pipeline", p.BinaryPipeline),
		)
	}
	return text
}
