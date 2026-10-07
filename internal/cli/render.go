package cli

import (
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"
)

// renderJSON renders a payload as indented JSON, for a command whose
// payload is its own best human rendering.
func renderJSON(payload any) string {
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return fmt.Sprint(payload)
	}
	return string(data)
}

// renderTable renders rows under a header as columns two spaces apart,
// each column as wide as its widest cell, with no trailing spaces and no
// final newline. Every row has the header's number of cells.
func renderTable(header []string, rows [][]string) string {
	widths := make([]int, len(header))
	all := append([][]string{header}, rows...)
	for _, row := range all {
		if len(row) != len(header) {
			panic(fmt.Sprintf("cli: a table row has %d cells under a header of %d", len(row), len(header)))
		}
		for i, cell := range row {
			widths[i] = max(widths[i], utf8.RuneCountInString(cell))
		}
	}
	lines := make([]string, len(all))
	for r, row := range all {
		var b strings.Builder
		for i, cell := range row {
			b.WriteString(cell)
			if i < len(row)-1 {
				b.WriteString(strings.Repeat(" ", widths[i]-utf8.RuneCountInString(cell)+2))
			}
		}
		lines[r] = strings.TrimRight(b.String(), " ")
	}
	return strings.Join(lines, "\n")
}
