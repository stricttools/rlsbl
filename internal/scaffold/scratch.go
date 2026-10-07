package scaffold

import (
	"fmt"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/git"
)

// Every scaffolded member carries two scratch directories at its root:
// experiments/ for throwaway probes and screenshots/ for screenshots taken
// while verifying a piece of work. Each holds a committed .gitignore whose
// content is "*" and "!.gitignore" (and "!go.mod" beside a go.mod), so the
// directory exists in a fresh clone and nothing inside it is committed by
// accident. A Go project's scratch directories each carry a go.mod, which
// keeps the go command out of them; a Python project's pytest is told to
// skip them through norecursedirs in pyproject.toml.

// ScratchDirs are the scratch directories, at a member's root.
var ScratchDirs = []string{"experiments", "screenshots"}

// scaffoldScratchFiles are the files scaffold itself commits into a scratch
// directory.
var scaffoldScratchFiles = map[string]bool{".gitignore": true, "go.mod": true}

// PytestDefaultNorecursedirs is pytest's own default norecursedirs. Setting
// the option replaces the default, so a project that had none is given the
// default back beside the scratch directories.
var PytestDefaultNorecursedirs = []string{"*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}"}

// refuseTrackedScratchFiles refuses to scaffold a member whose scratch
// directories hold tracked files besides the ones scaffold commits there:
// the ignore-everything .gitignore would leave them tracked while every new
// file beside them is ignored. memberPath is the member's
// repository-relative path.
func refuseTrackedScratchFiles(repo git.Repo, memberPath string) error {
	tracked, err := repo.TrackedFiles()
	if err != nil {
		return err
	}
	var found []string
	dirs := map[string]bool{}
	for _, f := range tracked {
		rel := f
		if memberPath != "." {
			var ok bool
			if rel, ok = strings.CutPrefix(f, memberPath+"/"); !ok {
				continue
			}
		}
		dir, rest, nested := strings.Cut(rel, "/")
		if !nested || !isScratchDir(dir) {
			continue
		}
		if !strings.Contains(rest, "/") && scaffoldScratchFiles[rest] {
			continue
		}
		found = append(found, f)
		dirs[joinDir(memberPath, dir)] = true
	}
	if len(found) == 0 {
		return nil
	}
	var names []string
	for _, d := range ScratchDirs {
		if dirs[joinDir(memberPath, d)] {
			names = append(names, joinDir(memberPath, d)+"/")
		}
	}
	first := found[0]
	inMember := first
	if memberPath != "." {
		inMember = strings.TrimPrefix(first, memberPath+"/")
	}
	_, rest, _ := strings.Cut(inMember, "/")
	dest := joinDir(memberPath, "assets/"+rest)
	verb := "is a scratch directory"
	if len(names) > 1 {
		verb = "are scratch directories"
	}
	return fmt.Errorf("%s %s (scaffold makes git ignore everything inside), but git tracks these files there:\n  %s\nMove them out before scaffolding: images a reader sees belong in the committed assets/ directory, for example `mkdir -p %s && git mv %s %s`; or, if the directory is not scratch space at all, rename it (`git mv %s <another name>`). Commit the move and run rlsbl scaffold again",
		strings.Join(names, " and "), verb, strings.Join(found, "\n  "), parentDir(dest), first, dest, strings.TrimSuffix(names[0], "/"))
}

func isScratchDir(name string) bool {
	for _, d := range ScratchDirs {
		if d == name {
			return true
		}
	}
	return false
}

// parentDir is the slash-separated parent of p.
func parentDir(p string) string {
	if i := strings.LastIndex(p, "/"); i >= 0 {
		return p[:i]
	}
	return "."
}

// norecursedirsPath is pytest's norecursedirs in pyproject.toml.
const norecursedirsPath = "tool.pytest.ini_options.norecursedirs"

// MergePytestNorecursedirs adds the scratch directories to pyproject.toml's
// [tool.pytest.ini_options] norecursedirs in the text data, keeping every
// other byte. A project with no setting is given pytest's default with the
// scratch directories after it, since setting the option replaces the
// default. path names the file in errors; changed is false when nothing is
// missing.
func MergePytestNorecursedirs(path string, data []byte) ([]byte, bool, error) {
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	var patterns []string
	found := false
	if node, ok := doc.Lookup(norecursedirsPath); ok {
		found = true
		if s, isScalar := node.(tomledit.Scalar); isScalar {
			text, isString := s.Value().(string)
			if !isString {
				return nil, false, fmt.Errorf("%s: %s is neither a string nor an array of strings", path, norecursedirsPath)
			}
			patterns = strings.Fields(text)
		} else {
			array, isArray := node.(*tomledit.ArrayNode)
			if !isArray {
				return nil, false, fmt.Errorf("%s: %s is neither a string nor an array of strings", path, norecursedirsPath)
			}
			for _, el := range array.Elements() {
				s, ok := el.(tomledit.Scalar)
				text, isString := "", false
				if ok {
					text, isString = s.Value().(string)
				}
				if !isString {
					return nil, false, fmt.Errorf("%s: %s holds something that is not a string", path, norecursedirsPath)
				}
				patterns = append(patterns, text)
			}
		}
	} else {
		patterns = append([]string(nil), PytestDefaultNorecursedirs...)
	}
	var missing []string
	for _, d := range ScratchDirs {
		if !containsString(patterns, d) {
			missing = append(missing, d)
		}
	}
	if found && len(missing) == 0 {
		return data, false, nil
	}
	all := append(patterns, missing...)
	values := make([]any, len(all))
	for i, p := range all {
		values[i] = p
	}
	if doc.Has("tool.pytest.ini_options") {
		if err := doc.SetCreate(norecursedirsPath, values); err != nil {
			return nil, false, fmt.Errorf("%s: %w", path, err)
		}
		return doc.Bytes(), true, nil
	}
	var b strings.Builder
	b.Write(data)
	if len(data) > 0 && !strings.HasSuffix(string(data), "\n") {
		b.WriteString("\n")
	}
	if len(data) > 0 {
		b.WriteString("\n")
	}
	b.WriteString("[tool.pytest.ini_options]\nnorecursedirs = [")
	for i, p := range all {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(tomledit.QuoteString(p))
	}
	b.WriteString("]\n")
	out := []byte(b.String())
	if _, err := tomledit.Parse(out); err != nil {
		return nil, false, fmt.Errorf("%s: adding [tool.pytest.ini_options] would break the file: %w", path, err)
	}
	return out, true, nil
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
