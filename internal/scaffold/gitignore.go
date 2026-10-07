package scaffold

import "strings"

// GitignoreEntries are the lines scaffold merges into every member's
// .gitignore: the shared template's lines that are neither blank nor a
// comment, in template order.
func GitignoreEntries() ([]string, error) {
	text, err := renderTemplate("shared/gitignore.tpl", Vars{})
	if err != nil {
		return nil, err
	}
	var entries []string
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "#") {
			entries = append(entries, line)
		}
	}
	return entries, nil
}
