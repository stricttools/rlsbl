package scaffold

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// The template placeholder grammar, applied in this order:
//
//   - {{action "owner/name"}} becomes owner/name@<pinned version> and
//     {{actionVersion "owner/name"}} the pinned version alone, both from
//     actions.toml; an action it does not pin is refused.
//   - {{#if name}}...{{/if}} keeps its body when the variable name is set to
//     a non-empty string and drops the whole block otherwise. Blocks do not
//     nest. A run of three or more newlines left behind is collapsed to two.
//   - {{name}} (a dotted name such as {{npm.nodeMatrix}} included) becomes
//     the variable's value. A placeholder no variable answers is refused:
//     a template never reaches disk with a placeholder in it.
//
// \{{ in a template writes a literal {{, for text that spells another tool's
// placeholders. GitHub's ${{ ... }} expressions are never placeholders: a
// {{ right after a $ is left as it is.

// escapedOpen is what \{{ is held as while the passes run.
const escapedOpen = "\x00rlsbl-escaped-open\x00"

var (
	actionVersionPlaceholder = regexp.MustCompile(`\{\{actionVersion\s+"([^"]+)"\}\}`)
	actionPlaceholder        = regexp.MustCompile(`\{\{action\s+"([^"]+)"\}\}`)
	conditionalBlock         = regexp.MustCompile(`(?s)\{\{#if\s+(\w+(?:\.\w+)*)\}\}(.*?)\{\{/if\}\}`)
	blankRun                 = regexp.MustCompile(`\n{3,}`)
	variablePlaceholder      = regexp.MustCompile(`\{\{(\w+(?:\.\w+)*)\}\}`)
)

// Vars are a template's variables by name. A variable set to "" is set: it
// answers its placeholder with nothing and makes its conditional blocks
// false.
type Vars map[string]string

// Merge sets every variable of other in v, other winning.
func (v Vars) Merge(other Vars) {
	for k, val := range other {
		v[k] = val
	}
}

// Render renders template text; source names the template in errors.
func Render(source, text string, vars Vars) (string, error) {
	out := strings.ReplaceAll(text, `\{{`, escapedOpen)
	var actionErr error
	resolve := func(re *regexp.Regexp, lookup func(string) (string, error)) {
		out = re.ReplaceAllStringFunc(out, func(m string) string {
			name := re.FindStringSubmatch(m)[1]
			v, err := lookup(name)
			if err != nil && actionErr == nil {
				actionErr = fmt.Errorf("%s: %w", source, err)
			}
			return v
		})
	}
	resolve(actionVersionPlaceholder, ActionVersion)
	resolve(actionPlaceholder, Action)
	if actionErr != nil {
		return "", actionErr
	}
	out = conditionalBlock.ReplaceAllStringFunc(out, func(m string) string {
		sub := conditionalBlock.FindStringSubmatch(m)
		if vars[sub[1]] != "" {
			return sub[2]
		}
		return ""
	})
	out = blankRun.ReplaceAllString(out, "\n\n")
	var missing []string
	var b strings.Builder
	last := 0
	for _, loc := range variablePlaceholder.FindAllStringSubmatchIndex(out, -1) {
		start, end := loc[0], loc[1]
		name := out[loc[2]:loc[3]]
		b.WriteString(out[last:start])
		last = end
		if start > 0 && out[start-1] == '$' {
			b.WriteString(out[start:end])
			continue
		}
		v, ok := vars[name]
		if !ok {
			missing = append(missing, name)
			b.WriteString(out[start:end])
			continue
		}
		b.WriteString(v)
	}
	b.WriteString(out[last:])
	if len(missing) > 0 {
		sort.Strings(missing)
		return "", fmt.Errorf("%s: no value for the template variable(s) %s; this is a defect in rlsbl's scaffold, which must supply every variable its templates name", source, strings.Join(dedupe(missing), ", "))
	}
	return strings.ReplaceAll(b.String(), escapedOpen, "{{"), nil
}

// dedupe drops repeated strings from a sorted list.
func dedupe(sorted []string) []string {
	var out []string
	for i, s := range sorted {
		if i == 0 || s != sorted[i-1] {
			out = append(out, s)
		}
	}
	return out
}
