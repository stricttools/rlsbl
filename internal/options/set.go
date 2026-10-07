package options

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The options directory's ownership manifest. Several tools share the
// directory, and strictspec holds its schemas.
const (
	manifestFile    = "manifest.toml"
	manifestOwner   = "strictspec"
	manifestContent = "owner = \"" + manifestOwner + "\"\n"
	// entriesHeader opens a new subject document.
	entriesHeader = "format_version = 1\n"
)

// SetRequest is one entry `rlsbl options set` writes.
type SetRequest struct {
	// ID is the option id, rlsbl:<name>.
	ID      string
	Current string
	Ideal   string
	Reason  string
	// Scope is the entry's scope; empty for an entry without one.
	Scope string
}

// The actions of a set.
const (
	ActionCreated   = "created"
	ActionUpdated   = "updated"
	ActionUnchanged = "unchanged"
)

// SetResult is what a set did.
type SetResult struct {
	ID string
	// File is the subject document, repository-relative.
	File    string
	Scope   string
	Current string
	Ideal   string
	Reason  string
	// Action is ActionCreated, ActionUpdated, or ActionUnchanged.
	Action string
	// Class is the entry's classification: settled, debt, or
	// waiting-on-tool.
	Class string
	// Written are the repository-relative files written, in order.
	Written []string
}

func namespaceRefusal(id string) error {
	tool, _, found := strings.Cut(id, ":")
	if !found {
		return fmt.Errorf("%q is not an option id: an id is <tool>:<name>, and rlsbl writes its own, %s<name>. `rlsbl options registry` lists every name", id, Prefix)
	}
	return fmt.Errorf("%q is an option of %s, not of rlsbl: rlsbl writes only its own entries, %s<name>. %s's entries are written by %s's own command, or by hand", id, tool, Prefix, tool, tool)
}

// manifestMissing reports whether the options directory's manifest is
// absent, and refuses one naming another owner or holding anything else.
func manifestMissing(root string) (bool, error) {
	rel := Dir + "/" + manifestFile
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
	if errors.Is(err, fs.ErrNotExist) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("cannot read %s: %w", rel, err)
	}
	doc, err := tomledit.Unmarshal[map[string]any](data)
	if err == nil && len(*doc) == 1 && (*doc)["owner"] == manifestOwner {
		return false, nil
	}
	return false, fmt.Errorf("%s must name %q as the owner of %s/, and nothing else: several tools share that directory, and strictspec holds its schemas. Write this line instead:\n%s", rel, manifestOwner, Dir, strings.TrimSpace(manifestContent))
}

// Set writes one rlsbl entry into the options directory of the repository
// rooted at root, or updates the entry already there for the same option
// and scope, through the effects handle (a dry run records the writes).
//
// strictspec validates the result (every document's shape and every rlsbl
// entry with this one in place) before anything is written, and so does
// rlsbl's path-scope rule against d. The directory and its manifest naming
// strictspec are created when absent. Every other line of the subject
// document keeps its bytes.
func Set(e *strictcli.Effects, reg *Registry, root string, d *declarations.Releasables, req SetRequest) (SetResult, error) {
	if !strings.HasPrefix(req.ID, Prefix) {
		return SetResult{}, namespaceRefusal(req.ID)
	}
	if !filepath.IsAbs(root) {
		return SetResult{}, fmt.Errorf("the repository root %q is not absolute", root)
	}
	missing, err := manifestMissing(root)
	if err != nil {
		return SetResult{}, err
	}
	loaded, err := strictspec.LoadOptionsEntries(root)
	if err != nil {
		return SetResult{}, fmt.Errorf("cannot read %s: %w", filepath.Join(root, filepath.FromSlash(Dir)), err)
	}
	if len(loaded.Invalid) > 0 {
		var lines []string
		for _, inv := range loaded.Invalid {
			for _, diag := range inv.Diagnostics {
				lines = append(lines, fmt.Sprintf("%s/%s: %s: %s", Dir, inv.File, diag.Code, diag.Message))
			}
		}
		return SetResult{}, fmt.Errorf("rlsbl will not edit %s/ while a document in it is not a valid options-entries document. Fix what each diagnostic names:\n  %s", Dir, strings.Join(lines, "\n  "))
	}

	subject := "project"
	if decl, ok := reg.Declaration(strings.TrimPrefix(req.ID, Prefix)); ok {
		subject = decl.Subject
	}
	subjectFile := subject + ".toml"
	hasScope := req.Scope != ""
	existing := -1
	unchanged := false
	inSubject := 0
	candidates := make([]strictspec.OptionsEntry, 0, len(loaded.Entries)+1)
	for _, entry := range loaded.Entries {
		if entry.File == subjectFile {
			inSubject++
		}
		if existing < 0 && entry.File == subjectFile && entry.ID == req.ID && entry.HasScope == hasScope && entry.Scope == req.Scope {
			existing = entry.Index
			unchanged = entry.Current == req.Current && entry.Ideal == req.Ideal && entry.Reason == req.Reason
			entry.Current, entry.Ideal, entry.Reason = req.Current, req.Ideal, req.Reason
		}
		candidates = append(candidates, entry)
	}
	if existing < 0 {
		candidates = append(candidates, strictspec.OptionsEntry{
			File: subjectFile, Index: inSubject, ID: req.ID, Scope: req.Scope, HasScope: hasScope,
			Current: req.Current, Ideal: req.Ideal, Reason: req.Reason,
		})
	}
	if lines := refusals(reg, nil, candidates, d); len(lines) > 0 {
		return SetResult{}, fmt.Errorf("%s/ as it would stand with this entry is refused, so nothing was written:\n  %s", Dir, strings.Join(lines, "\n  "))
	}
	result := SetResult{
		ID: req.ID, File: Dir + "/" + subjectFile, Scope: req.Scope,
		Current: req.Current, Ideal: req.Ideal, Reason: req.Reason,
	}
	accepted, _ := strictspec.ValidateOptionsNamespace(Tool, reg.checkedRegistry(), candidates)
	for _, a := range accepted {
		if a.Entry.File == subjectFile && a.Entry.ID == req.ID && a.Entry.HasScope == hasScope && a.Entry.Scope == req.Scope {
			result.Class = string(a.Class)
		}
	}
	if unchanged {
		result.Action = ActionUnchanged
		return result, nil
	}

	target := filepath.Join(root, filepath.FromSlash(result.File))
	current, err := os.ReadFile(target)
	mode := os.FileMode(0o644)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		current = []byte(entriesHeader)
	case err != nil:
		return SetResult{}, fmt.Errorf("cannot read %s: %w", result.File, err)
	default:
		info, err := os.Stat(target)
		if err != nil {
			return SetResult{}, fmt.Errorf("cannot read %s: %w", result.File, err)
		}
		mode = info.Mode().Perm()
	}
	content, err := renderEntry(current, existing, req)
	if err != nil {
		return SetResult{}, fmt.Errorf("editing %s: %w", result.File, err)
	}
	if _, diags := strictspec.ReadOptionsEntries(subjectFile, content); len(diags) > 0 {
		return SetResult{}, fmt.Errorf("the rendered %s is not a valid options-entries document: %s: %s", result.File, diags[0].Code, diags[0].Message)
	}
	if _, err := e.Mkdir(filepath.Join(root, filepath.FromSlash(Dir))); err != nil {
		return SetResult{}, fmt.Errorf("creating %s: %w", Dir, err)
	}
	if missing {
		if _, err := e.Write(filepath.Join(root, filepath.FromSlash(Dir), manifestFile), manifestContent); err != nil {
			return SetResult{}, fmt.Errorf("writing %s/%s: %w", Dir, manifestFile, err)
		}
		result.Written = append(result.Written, Dir+"/"+manifestFile)
	}
	temporary := target + ".tmp"
	if _, err := e.Write(temporary, content, strictcli.Mode(mode)); err != nil {
		return SetResult{}, fmt.Errorf("writing %s: %w", result.File, err)
	}
	if _, err := e.Rename(temporary, target); err != nil {
		return SetResult{}, fmt.Errorf("writing %s: %w", result.File, err)
	}
	result.Written = append(result.Written, result.File)
	result.Action = ActionCreated
	if existing >= 0 {
		result.Action = ActionUpdated
	}
	return result, nil
}

// renderEntry is the subject document with the entry written: entry index
// updated in place, or a new entry appended when index is negative. Every
// other line keeps its bytes.
func renderEntry(current []byte, index int, req SetRequest) ([]byte, error) {
	if index >= 0 {
		doc, err := tomledit.Parse(current)
		if err != nil {
			return nil, err
		}
		for key, value := range map[string]string{"current": req.Current, "ideal": req.Ideal, "reason": req.Reason} {
			if err := doc.Set(fmt.Sprintf("entry[%d].%s", index, key), value); err != nil {
				return nil, err
			}
		}
		return doc.Bytes(), nil
	}
	q := tomledit.QuoteString
	text := strings.TrimRight(string(current), "\n") + "\n\n[[entry]]\nid = " + q(req.ID) + "\n"
	if req.Scope != "" {
		text += "scope = " + q(req.Scope) + "\n"
	}
	text += "current = " + q(req.Current) + "\nideal = " + q(req.Ideal) + "\nreason = " + q(req.Reason) + "\n"
	return []byte(text), nil
}
