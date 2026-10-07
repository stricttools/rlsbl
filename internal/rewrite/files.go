package rewrite

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// replaceFile writes data over the file at path through a sibling temporary
// file renamed over it, keeping the file's permission bits, so no reader
// sees a partly written file.
func replaceFile(e *strictcli.Effects, path string, data []byte) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("reading %s: %w", path, err)
	}
	temporary := path + ".rlsbl-writing"
	if _, err := e.Write(temporary, data, strictcli.Mode(info.Mode().Perm())); err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	if _, err := e.Rename(temporary, path); err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	return nil
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

// rel is path relative to root, slash-separated.
func rel(root, path string) string {
	r, err := filepath.Rel(root, path)
	if err != nil {
		return filepath.ToSlash(path)
	}
	return filepath.ToSlash(r)
}

// plural is "1 <singular>" or "n <singular>s".
func plural(n int, singular string) string {
	return counted(n, singular, singular+"s")
}

// counted is "1 <one>" or "n <many>".
func counted(n int, one, many string) string {
	if n == 1 {
		return "1 " + one
	}
	return fmt.Sprintf("%d %s", n, many)
}

// shellQuote quotes s for a POSIX shell, as a printed command line spells
// an argument.
func shellQuote(s string) string {
	if s != "" && strings.Trim(s, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@%+=:,./-_") == "" {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}
