package scaffold

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// The scaffold state, .strictmetadata/.scaffold-state/scaffold-state.toml,
// records every file scaffold manages with the SHA-256 it had when scaffold
// last wrote or confirmed it, and the rlsbl version that last scaffolded or
// released the repository. A managed file the next scaffold no longer
// renders is an orphan, removed only when its hash still matches (a file
// someone edited is left, named). The three-way merge bases, the template
// text each managed file was last rendered from, sit under
// .strictmetadata/.scaffold-bases/ at the managed file's own path.

// stateFormatVersion is the scaffold state's format version.
const stateFormatVersion = 1

// stateDir is the directory holding the scaffold state.
var stateDir = path.Dir(declarations.ScaffoldStateFile)

// State is the scaffold state.
type State struct {
	// RlsblVersion is the rlsbl version that last scaffolded or released the
	// repository.
	RlsblVersion string
	// Files maps each managed file, repository-relative, to its SHA-256.
	Files map[string]string
}

type rawState struct {
	FormatVersion int64             `toml:"format_version,required"`
	RlsblVersion  string            `toml:"rlsbl_version,required"`
	Files         map[string]string `toml:"files,required"`
}

// ReadState reads the scaffold state of the repository at root; found is
// false when the repository has none.
func ReadState(root string) (state State, found bool, err error) {
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(declarations.ScaffoldStateFile)))
	if errors.Is(err, fs.ErrNotExist) {
		return State{Files: map[string]string{}}, false, nil
	}
	if err != nil {
		return State{}, false, fmt.Errorf("reading %s: %w", declarations.ScaffoldStateFile, err)
	}
	s, err := ParseState(data)
	if err != nil {
		return State{}, true, err
	}
	return s, true, nil
}

// ParseState parses the scaffold state's text.
func ParseState(data []byte) (State, error) {
	raw, err := tomledit.Unmarshal[rawState](data)
	if err != nil {
		return State{}, fmt.Errorf("%s: %w", declarations.ScaffoldStateFile, err)
	}
	var problems []string
	if raw.FormatVersion != stateFormatVersion {
		problems = append(problems, fmt.Sprintf("format_version is %d; this rlsbl reads format_version %d", raw.FormatVersion, stateFormatVersion))
	}
	if strings.TrimSpace(raw.RlsblVersion) == "" {
		problems = append(problems, "rlsbl_version is empty")
	}
	for p, hash := range raw.Files {
		if declarations.PathProblem(p) != "" || p == declarations.RootPath {
			problems = append(problems, fmt.Sprintf("the managed file %q is not a canonical repository-relative path", p))
		}
		if !isSHA256(hash) {
			problems = append(problems, fmt.Sprintf("the managed file %q has %q, which is not a SHA-256 in lowercase hexadecimal", p, hash))
		}
	}
	if len(problems) > 0 {
		sort.Strings(problems)
		return State{}, fmt.Errorf("%s: %s", declarations.ScaffoldStateFile, strings.Join(problems, "; "))
	}
	return State{RlsblVersion: raw.RlsblVersion, Files: raw.Files}, nil
}

func isSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// RenderState is the scaffold state's text: the same bytes for the same
// state, files in path order.
func RenderState(s State) string {
	var b strings.Builder
	fmt.Fprintf(&b, "format_version = %d\n", stateFormatVersion)
	b.WriteString("rlsbl_version = " + tomledit.QuoteString(s.RlsblVersion) + "\n")
	b.WriteString("\n[files]\n")
	paths := make([]string, 0, len(s.Files))
	for p := range s.Files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	for _, p := range paths {
		b.WriteString(tomledit.QuoteString(p) + " = " + tomledit.QuoteString(s.Files[p]) + "\n")
	}
	return b.String()
}

// WriteState writes the scaffold state of the repository at root (absolute)
// through the effects handle, creating its directory and the directory's
// manifest.toml, and refuses a state the reader would refuse.
func WriteState(e *strictcli.Effects, root string, s State) error {
	text := RenderState(s)
	if _, err := ParseState([]byte(text)); err != nil {
		return err
	}
	if err := declarations.EnsureOwnedDirectory(e, root, stateDir); err != nil {
		return err
	}
	target := filepath.Join(root, filepath.FromSlash(declarations.ScaffoldStateFile))
	if _, err := e.Write(target+".tmp", text); err != nil {
		return fmt.Errorf("writing %s: %w", declarations.ScaffoldStateFile, err)
	}
	if _, err := e.Rename(target+".tmp", target); err != nil {
		return fmt.Errorf("writing %s: %w", declarations.ScaffoldStateFile, err)
	}
	return nil
}

// FileHash is the SHA-256 of content in lowercase hexadecimal, the hash the
// scaffold state records.
func FileHash(content []byte) string {
	sum := sha256.Sum256(content)
	return hex.EncodeToString(sum[:])
}

// BasePath is where the merge base of the managed file rel
// (repository-relative) is kept, repository-relative.
func BasePath(rel string) string {
	return declarations.ScaffoldBasesDir + "/" + rel
}

// readBase reads the stored merge base of rel; found is false when none is
// stored.
func readBase(root, rel string) (base string, found bool, err error) {
	data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(BasePath(rel))))
	if errors.Is(err, fs.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("reading the merge base of %s: %w", rel, err)
	}
	return string(data), true, nil
}

// writeBase stores content as the merge base of rel.
func writeBase(e *strictcli.Effects, root, rel, content string) error {
	if err := declarations.EnsureOwnedDirectory(e, root, declarations.ScaffoldBasesDir); err != nil {
		return err
	}
	target := filepath.Join(root, filepath.FromSlash(BasePath(rel)))
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return fmt.Errorf("creating the directory of %s: %w", BasePath(rel), err)
	}
	if _, err := e.Write(target, content); err != nil {
		return fmt.Errorf("writing %s: %w", BasePath(rel), err)
	}
	return nil
}
