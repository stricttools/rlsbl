package dependencies

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"

	tomledit "github.com/stricttools/go-toml-edit"
)

// readTOML decodes the TOML file at path. found is false, with no error,
// when the file does not exist; a file that exists and does not parse is an
// error naming it.
func readTOML(path string) (doc map[string]any, found bool, err error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", path, err)
	}
	decoded, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, true, fmt.Errorf("%s does not parse as TOML: %w", path, err)
	}
	return *decoded, true, nil
}

// readJSON decodes the JSON file at path into an object. found is false,
// with no error, when the file does not exist.
func readJSON(path string) (doc map[string]any, found bool, err error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("reading %s: %w", path, err)
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, true, fmt.Errorf("%s does not parse as a JSON object: %w", path, err)
	}
	return doc, true, nil
}

// table is the table at keys under doc, and false when any key is absent or
// does not hold a table.
func table(doc map[string]any, keys ...string) (map[string]any, bool) {
	current := doc
	for _, k := range keys {
		next, ok := current[k].(map[string]any)
		if !ok {
			return nil, false
		}
		current = next
	}
	return current, true
}

// fileExists reports whether path is an existing regular file (or a symlink
// to one); an error other than its absence is an error.
func fileExists(path string) (bool, error) {
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return info.Mode().IsRegular(), nil
}
