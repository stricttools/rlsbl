package targets

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// PackageJSON is the npm manifest's file name.
const PackageJSON = "package.json"

// package.json is edited in place: a write replaces one top-level string
// value and keeps every other byte of the file (indentation, key order,
// spacing, the trailing newline). A manifest whose shape leaves the edit
// ambiguous is refused, naming the file: a key declared twice at the top
// level, or a value of the wrong type.

// jsonMember is one top-level member of a JSON object: its key, and where
// its value starts and ends.
type jsonMember struct {
	key        string
	valueStart int
	valueEnd   int
}

// topLevelMembers locates every member of the JSON object raw holds.
func topLevelMembers(raw []byte) ([]jsonMember, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	open, err := dec.Token()
	if err != nil {
		return nil, fmt.Errorf("not JSON: %w", err)
	}
	if d, ok := open.(json.Delim); !ok || d != '{' {
		return nil, errors.New("the manifest is not a JSON object")
	}
	var members []jsonMember
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, fmt.Errorf("not JSON: %w", err)
		}
		key, ok := tok.(string)
		if !ok {
			return nil, errors.New("the manifest is not a JSON object")
		}
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, fmt.Errorf("not JSON: %w", err)
		}
		end := int(dec.InputOffset())
		members = append(members, jsonMember{key: key, valueStart: end - len(value), valueEnd: end})
	}
	if _, err := dec.Token(); err != nil {
		return nil, fmt.Errorf("not JSON: %w", err)
	}
	if _, err := dec.Token(); err == nil {
		return nil, errors.New("the manifest holds more than one JSON value")
	}
	return members, nil
}

// packageJSON is a package.json read for its top-level members.
type packageJSON struct {
	path    string
	raw     []byte
	members []jsonMember
}

// readPackageJSON reads dir/package.json; found is false when there is none.
func readPackageJSON(dir string) (packageJSON, bool, error) {
	path := filepath.Join(dir, PackageJSON)
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return packageJSON{}, false, nil
	}
	if err != nil {
		return packageJSON{}, false, fmt.Errorf("reading %s: %w", path, err)
	}
	members, err := topLevelMembers(raw)
	if err != nil {
		return packageJSON{}, true, fmt.Errorf("%s: %w", path, err)
	}
	return packageJSON{path: path, raw: raw, members: members}, true, nil
}

// member is the one top-level member named key; found is false when there
// is none, and a key declared more than once is refused.
func (p packageJSON) member(key string) (jsonMember, bool, error) {
	var found []jsonMember
	for _, m := range p.members {
		if m.key == key {
			found = append(found, m)
		}
	}
	switch len(found) {
	case 0:
		return jsonMember{}, false, nil
	case 1:
		return found[0], true, nil
	}
	return jsonMember{}, false, fmt.Errorf("%s declares %q %d times at the top level; keep one", p.path, key, len(found))
}

// stringField is the top-level string named key; found is false when the
// manifest does not declare it, and a value that is not a string is
// refused.
func (p packageJSON) stringField(key string) (string, bool, error) {
	m, found, err := p.member(key)
	if err != nil || !found {
		return "", false, err
	}
	var s string
	if err := json.Unmarshal(p.raw[m.valueStart:m.valueEnd], &s); err != nil {
		return "", false, fmt.Errorf("%s: %q is not a string", p.path, key)
	}
	return s, true, nil
}

// field decodes the top-level value named key into v; found is false when
// the manifest does not declare it.
func (p packageJSON) field(key string, v any) (bool, error) {
	m, found, err := p.member(key)
	if err != nil || !found {
		return false, err
	}
	if err := json.Unmarshal(p.raw[m.valueStart:m.valueEnd], v); err != nil {
		return false, fmt.Errorf("%s: %q does not have the shape rlsbl reads: %w", p.path, key, err)
	}
	return true, nil
}

// withString is the manifest with the one top-level string named key
// replaced by value, every other byte kept. The key must be declared once,
// with a string value: a manifest without it is refused rather than given
// one, since where to add it is a formatting guess.
func (p packageJSON) withString(key, value string) ([]byte, error) {
	if _, found, err := p.stringField(key); err != nil {
		return nil, err
	} else if !found {
		return nil, fmt.Errorf("%s declares no top-level %q; add it", p.path, key)
	}
	m, _, _ := p.member(key)
	var encoded bytes.Buffer
	enc := json.NewEncoder(&encoded)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		return nil, err
	}
	quoted := bytes.TrimSuffix(encoded.Bytes(), []byte("\n"))
	out := make([]byte, 0, len(p.raw)+len(quoted))
	out = append(out, p.raw[:m.valueStart]...)
	out = append(out, quoted...)
	return append(out, p.raw[m.valueEnd:]...), nil
}
