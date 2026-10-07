// Package monorepo is the `rlsbl monorepo` commands on a workspace: init
// writes a repository's first declarations, add and remove change its
// members, list, status, outdated, graph, and impact report on them,
// check-names asks the registries about their names, cleanup removes the
// old layout's residue, and rename-releasable renames a releasable with its
// state and its identities in the lifecycle-and-license record.
//
// Every command but init reads the declarations of the repository holding
// the working directory and refuses a standalone layout: a standalone
// project's commands are the top-level ones.
package monorepo

import (
	"fmt"
	"path/filepath"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Load reads the workspace holding the absolute directory dir, refusing
// declarations of a standalone project. command names the monorepo command
// for the refusal.
func Load(dir, command string) (*workspace.Workspace, error) {
	ws, err := workspace.Discover(dir)
	if err != nil {
		return nil, err
	}
	if err := requireWorkspace(ws, command); err != nil {
		return nil, err
	}
	return ws, nil
}

// requireWorkspace refuses declarations of a standalone project.
func requireWorkspace(ws *workspace.Workspace, command string) error {
	if ws.IsWorkspace() {
		return nil
	}
	return fmt.Errorf("`rlsbl monorepo %s` works on a workspace, and %s declares repository_layout = %q: a standalone project has one member, the root, and its commands are the top-level ones (rlsbl status, rlsbl release run). A repository becomes a workspace when that file declares repository_layout = %q",
		command, declarations.ReleasablesFile, declarations.LayoutStandalone, declarations.LayoutWorkspace)
}

// MemberVersion is the version a member's first target reads from its
// manifest, with that target's name; found is false for a member with no
// target. A manifest whose version cannot be read is an error naming the
// member.
func MemberVersion(ws *workspace.Workspace, m declarations.Member) (version, target string, found bool, err error) {
	list, err := targets.MemberTargets(ws.Root, m)
	if err != nil {
		return "", "", false, err
	}
	if len(list) == 0 {
		return "", "", false, nil
	}
	first := list[0]
	t, err := targets.Get(first.Name)
	if err != nil {
		return "", "", false, err
	}
	v, err := t.ReadVersion(filepath.Join(ws.Root, filepath.FromSlash(m.TargetDir(first))))
	if err != nil {
		return "", "", false, fmt.Errorf("the member %q: reading the version of its %s target: %w", m.Name, first.Name, err)
	}
	return v.String(), first.Name, true, nil
}

// TargetNames are the names of the member's targets, declared or detected.
func TargetNames(ws *workspace.Workspace, m declarations.Member) ([]string, error) {
	list, err := targets.MemberTargets(ws.Root, m)
	if err != nil {
		return nil, err
	}
	names := []string{}
	for _, t := range list {
		names = append(names, t.Name)
	}
	return names, nil
}

// relativePath is the absolute path relative to the repository root,
// slash-separated.
func relativePath(root, abs string) string {
	rel, err := filepath.Rel(root, abs)
	if err != nil {
		return filepath.ToSlash(abs)
	}
	return filepath.ToSlash(rel)
}

// recordWriter is the lifecycle library's file writer backed by the effects
// handle, so a dry run records the record's writes instead of making them.
type recordWriter struct {
	e *strictcli.Effects
}

func (w recordWriter) WriteFile(path string, data []byte) error {
	_, err := w.e.Write(path, data)
	return err
}

func (w recordWriter) MkdirAll(path string) error {
	_, err := w.e.Mkdir(path)
	return err
}
