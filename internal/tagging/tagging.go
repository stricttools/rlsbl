// Package tagging marks a project as part of the rlsbl ecosystem: the rlsbl
// keyword in package.json and pyproject.toml, and the rlsbl topic on the
// GitHub repository. It also finds the repositories that carry the topic
// (`rlsbl discover`). Whether a project is tagged at all is the
// rlsbl:ecosystem-tagging option, which the caller reads.
//
// Manifests are edited in place: only the keyword is inserted, and every
// other byte of the file (formatting, key order, comments in TOML) is kept.
// A manifest whose shape leaves the edit ambiguous is refused, naming the
// file, rather than rewritten.
package tagging

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/github"
)

// Keyword is the manifest keyword and the GitHub topic that mark a project
// as released with rlsbl.
const Keyword = "rlsbl"

// Writer writes files: the strictcli effects handle.
type Writer interface {
	Write(path interface{}, content interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
	Rename(src interface{}, dst interface{}, opts ...strictcli.EffectOption) (strictcli.Unsettled, error)
}

// replace writes content to path through a temporary file renamed over it,
// so no reader sees a partly written manifest.
func replace(w Writer, path string, content []byte) error {
	tmp := path + ".rlsbl-writing"
	if _, err := w.Write(tmp, string(content)); err != nil {
		return err
	}
	_, err := w.Rename(tmp, path)
	return err
}

// EnsureNpmKeyword adds the rlsbl keyword to the keywords array of
// dir/package.json, creating the array when the manifest has none. It
// returns the manifest's path and whether it changed.
func EnsureNpmKeyword(w Writer, dir string) (string, bool, error) {
	path := filepath.Join(dir, "package.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return path, false, fmt.Errorf("reading %s: %w", path, err)
	}
	edited, changed, err := addJSONKeyword(raw)
	if err != nil {
		return path, false, fmt.Errorf("%s: %w", path, err)
	}
	if !changed {
		return path, false, nil
	}
	return path, true, replace(w, path, edited)
}

// jsonMember is one top-level member of a JSON object: where its key's
// opening quote sits and where its value starts and ends.
type jsonMember struct {
	key        string
	keyStart   int
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
		before := int(dec.InputOffset())
		tok, err := dec.Token()
		if err != nil {
			return nil, fmt.Errorf("not JSON: %w", err)
		}
		key, ok := tok.(string)
		if !ok {
			return nil, errors.New("the manifest is not a JSON object")
		}
		afterKey := int(dec.InputOffset())
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, fmt.Errorf("not JSON: %w", err)
		}
		end := int(dec.InputOffset())
		keyStart := before + bytes.IndexByte(raw[before:afterKey], '"')
		valueStart := end - len(value)
		members = append(members, jsonMember{key: key, keyStart: keyStart, valueStart: valueStart, valueEnd: end})
	}
	if _, err := dec.Token(); err != nil {
		return nil, fmt.Errorf("not JSON: %w", err)
	}
	if _, err := dec.Token(); err == nil {
		return nil, errors.New("the manifest holds more than one JSON value")
	}
	return members, nil
}

// indentOf is the whitespace that starts the line position sits on, and
// whether that line holds nothing else before position.
func indentOf(raw []byte, position int) (string, bool) {
	lineStart := bytes.LastIndexByte(raw[:position], '\n') + 1
	indent := string(raw[lineStart:position])
	return indent, strings.TrimSpace(indent) == "" && lineStart > 0
}

// addJSONKeyword inserts the keyword into raw's top-level keywords array,
// or adds the array after the last member, keeping every other byte.
func addJSONKeyword(raw []byte) ([]byte, bool, error) {
	members, err := topLevelMembers(raw)
	if err != nil {
		return nil, false, err
	}
	var keywords []jsonMember
	for _, m := range members {
		if m.key == "keywords" {
			keywords = append(keywords, m)
		}
	}
	quoted := `"` + Keyword + `"`
	var out []byte
	switch {
	case len(keywords) > 1:
		return nil, false, errors.New("the manifest declares keywords more than once")
	case len(keywords) == 1:
		m := keywords[0]
		var list []any
		if err := json.Unmarshal(raw[m.valueStart:m.valueEnd], &list); err != nil {
			return nil, false, errors.New("keywords is not an array")
		}
		for _, k := range list {
			s, ok := k.(string)
			if !ok {
				return nil, false, errors.New("keywords holds something that is not a string")
			}
			if s == Keyword {
				return raw, false, nil
			}
		}
		inner := raw[m.valueStart+1 : m.valueEnd-1]
		if len(list) == 0 {
			out = splice(raw, m.valueStart, m.valueEnd, "["+quoted+"]")
			break
		}
		lastEnd := m.valueStart + 1 + len(bytes.TrimRight(inner, " \t\r\n"))
		insert := ", " + quoted
		// An array whose last element starts a line of its own gets the
		// keyword on a line of its own, indented like that element.
		if nl := bytes.LastIndexByte(raw[m.valueStart+1:lastEnd], '\n'); nl >= 0 {
			rest := raw[m.valueStart+1+nl+1:]
			indent := rest[:len(rest)-len(bytes.TrimLeft(rest, " \t"))]
			insert = ",\n" + string(indent) + quoted
		}
		out = splice(raw, lastEnd, lastEnd, insert)
	case len(members) == 0:
		closing := bytes.LastIndexByte(raw, '}')
		out = splice(raw, closing, closing, `"keywords": [`+quoted+`]`)
	default:
		last := members[len(members)-1]
		insert := `, "keywords": [` + quoted + `]`
		if indent, own := indentOf(raw, members[0].keyStart); own {
			insert = ",\n" + indent + `"keywords": [` + quoted + `]`
		}
		out = splice(raw, last.valueEnd, last.valueEnd, insert)
	}
	var check struct {
		Keywords []string `json:"keywords"`
	}
	if err := json.Unmarshal(out, &check); err != nil || !slices.Contains(check.Keywords, Keyword) {
		return nil, false, errors.New("the keyword could not be inserted without breaking the manifest")
	}
	return out, true, nil
}

// splice replaces raw[from:to] with text.
func splice(raw []byte, from, to int, text string) []byte {
	out := make([]byte, 0, len(raw)+len(text))
	out = append(out, raw[:from]...)
	out = append(out, text...)
	return append(out, raw[to:]...)
}

// EnsurePypiKeyword adds the rlsbl keyword to [project].keywords of
// dir/pyproject.toml, creating the array when the table has none. A
// pyproject.toml without a [project] table is refused: it declares no
// package to tag.
func EnsurePypiKeyword(w Writer, dir string) (string, bool, error) {
	path := filepath.Join(dir, "pyproject.toml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return path, false, fmt.Errorf("reading %s: %w", path, err)
	}
	doc, err := tomledit.Parse(raw)
	if err != nil {
		return path, false, fmt.Errorf("%s: %w", path, err)
	}
	if !doc.Has("project") {
		return path, false, fmt.Errorf("%s has no [project] table, so it declares no package to carry the %s keyword", path, Keyword)
	}
	node, ok := doc.Lookup("project.keywords")
	if !ok {
		if err := doc.SetCreate("project.keywords", []any{Keyword}); err != nil {
			return path, false, fmt.Errorf("%s: %w", path, err)
		}
		return path, true, replace(w, path, doc.Bytes())
	}
	array, ok := node.(*tomledit.ArrayNode)
	if !ok {
		return path, false, fmt.Errorf("%s: [project].keywords is not an array", path)
	}
	for _, el := range array.Elements() {
		s, ok := el.(tomledit.Scalar)
		if !ok {
			return path, false, fmt.Errorf("%s: [project].keywords holds something that is not a string", path)
		}
		text, ok := s.Value().(string)
		if !ok {
			return path, false, fmt.Errorf("%s: [project].keywords holds something that is not a string", path)
		}
		if text == Keyword {
			return path, false, nil
		}
	}
	if err := doc.AppendToArray("project.keywords", Keyword); err != nil {
		return path, false, fmt.Errorf("%s: %w", path, err)
	}
	return path, true, replace(w, path, doc.Bytes())
}

// Manifests are the directories whose manifests carry the keyword.
type Manifests struct {
	// NpmDirs hold a package.json each.
	NpmDirs []string
	// PypiDirs hold a pyproject.toml each.
	PypiDirs []string
}

// Tagged is what EnsureTags changed.
type Tagged struct {
	// Edited are the manifests that gained the keyword.
	Edited []string
	// TopicAdded is whether the repository gained the topic.
	TopicAdded bool
}

// EnsureTags puts the keyword into every manifest named and the topic on the
// repository. A manifest or topic write that fails is an error; nothing is
// skipped.
func EnsureTags(w Writer, gh github.Client, repo github.Repository, m Manifests) (Tagged, error) {
	var tagged Tagged
	for _, dir := range m.NpmDirs {
		path, changed, err := EnsureNpmKeyword(w, dir)
		if err != nil {
			return tagged, err
		}
		if changed {
			tagged.Edited = append(tagged.Edited, path)
		}
	}
	for _, dir := range m.PypiDirs {
		path, changed, err := EnsurePypiKeyword(w, dir)
		if err != nil {
			return tagged, err
		}
		if changed {
			tagged.Edited = append(tagged.Edited, path)
		}
	}
	added, err := gh.EnsureTopic(repo, Keyword)
	if err != nil {
		return tagged, fmt.Errorf("adding the %s topic to %s: %w", Keyword, repo, err)
	}
	tagged.TopicAdded = added
	return tagged, nil
}
