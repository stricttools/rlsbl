package scaffold

import (
	_ "embed"
	"fmt"
	"sort"
	"sync"

	tomledit "github.com/stricttools/go-toml-edit"
)

// actionsDocument is the table of pinned GitHub Actions versions,
// internal/scaffold/actions.toml: every workflow rlsbl generates takes its
// action versions from it, so an upgrade is one edit there and a scaffold.
//
//go:embed actions.toml
var actionsDocument []byte

var (
	actionsOnce  sync.Once
	actionsTable map[string]string
	actionsErr   error
)

// actions is the parsed table; a table that does not parse is an error on
// every lookup, naming the file.
func actions() (map[string]string, error) {
	actionsOnce.Do(func() {
		table, err := tomledit.Unmarshal[map[string]string](actionsDocument)
		if err != nil {
			actionsErr = fmt.Errorf("internal/scaffold/actions.toml: %w", err)
			return
		}
		actionsTable = *table
	})
	return actionsTable, actionsErr
}

// ActionVersion is the pinned version of action ("owner/name"). An action
// the table does not pin is refused: a workflow never names an action at a
// version nobody chose.
func ActionVersion(action string) (string, error) {
	table, err := actions()
	if err != nil {
		return "", err
	}
	v, ok := table[action]
	if !ok {
		return "", fmt.Errorf("the action %q is not pinned in internal/scaffold/actions.toml; add it there with the version workflows use instead of naming a version in a template", action)
	}
	return v, nil
}

// Action is the full reference to action at its pinned version:
// owner/name@version.
func Action(action string) (string, error) {
	v, err := ActionVersion(action)
	if err != nil {
		return "", err
	}
	return action + "@" + v, nil
}

// PinnedActions are the table's actions, sorted.
func PinnedActions() ([]string, error) {
	table, err := actions()
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(table))
	for name := range table {
		names = append(names, name)
	}
	sort.Strings(names)
	return names, nil
}
