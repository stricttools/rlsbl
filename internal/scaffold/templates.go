package scaffold

import (
	"embed"
	"fmt"
	"io/fs"
	"sort"
)

// templateFS holds every template scaffold renders, by target (go, npm,
// pypi) and shared across targets (shared).
//
//go:embed templates
var templateFS embed.FS

// templateText is the text of the template at name, relative to
// internal/scaffold/templates.
func templateText(name string) (string, error) {
	data, err := templateFS.ReadFile("templates/" + name)
	if err != nil {
		return "", fmt.Errorf("rlsbl carries no scaffold template %s: %w", name, err)
	}
	return string(data), nil
}

// renderTemplate renders the template at name with vars.
func renderTemplate(name string, vars Vars) (string, error) {
	text, err := templateText(name)
	if err != nil {
		return "", err
	}
	return Render("templates/"+name, text, vars)
}

// TemplateNames are every template rlsbl carries, relative to
// internal/scaffold/templates, sorted.
func TemplateNames() ([]string, error) {
	var names []string
	err := fs.WalkDir(templateFS, "templates", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			names = append(names, p[len("templates/"):])
		}
		return nil
	})
	sort.Strings(names)
	return names, err
}
