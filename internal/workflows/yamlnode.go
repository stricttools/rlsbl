package workflows

import (
	"bytes"
	"errors"
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

// A workflow's YAML is read into yaml.v3 nodes, so a member's jobs keep
// their key order and comments when they are inlined. A node's shape is read
// from its tag (ShortTag), which yaml.v3 resolves for every node it parses
// and every node built here.

const (
	mapTag  = "!!map"
	seqTag  = "!!seq"
	nullTag = "!!null"
)

func isMap(n *yaml.Node) bool  { return n != nil && n.Alias == nil && n.ShortTag() == mapTag }
func isSeq(n *yaml.Node) bool  { return n != nil && n.Alias == nil && n.ShortTag() == seqTag }
func isNull(n *yaml.Node) bool { return n == nil || (n.Alias == nil && n.ShortTag() == nullTag) }
func isScalar(n *yaml.Node) bool {
	return n != nil && n.Alias == nil && !isMap(n) && !isSeq(n) && len(n.Content) == 0
}

// parseDocument parses one YAML document and returns its root node; nil for
// an empty document. Aliases are refused: a workflow rlsbl rewrites must say
// each value where it applies, or a rewrite of one place would change
// another.
func parseDocument(text, source string) (*yaml.Node, error) {
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		return nil, fmt.Errorf("%s is not YAML rlsbl can read: %w", source, err)
	}
	if len(doc.Content) == 0 {
		return nil, nil
	}
	if len(doc.Content) != 1 {
		return nil, fmt.Errorf("%s holds more than one YAML document", source)
	}
	root := doc.Content[0]
	if err := refuseAliases(root, source); err != nil {
		return nil, err
	}
	return root, nil
}

func refuseAliases(n *yaml.Node, source string) error {
	if n.Alias != nil || n.Anchor != "" {
		return fmt.Errorf("%s uses a YAML anchor or alias (line %d); rlsbl inlines workflow jobs and cannot rewrite one place without changing the other, so write the value out where it applies", source, n.Line)
	}
	for _, c := range n.Content {
		if err := refuseAliases(c, source); err != nil {
			return err
		}
	}
	return nil
}

// mapGet is the value of key in the mapping m, or nil.
func mapGet(m *yaml.Node, key string) *yaml.Node {
	if !isMap(m) {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

// mapKeys are the keys of the mapping m, in order.
func mapKeys(m *yaml.Node) []string {
	var keys []string
	if !isMap(m) {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		keys = append(keys, m.Content[i].Value)
	}
	return keys
}

// mapSet sets key in the mapping m, in place when it is there and appended
// otherwise.
func mapSet(m *yaml.Node, key string, value *yaml.Node) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = value
			return
		}
	}
	m.Content = append(m.Content, scalar(key), value)
}

// mapDelete removes key from the mapping m.
func mapDelete(m *yaml.Node, key string) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content = append(m.Content[:i:i], m.Content[i+2:]...)
			return
		}
	}
}

// mapEnsure is the mapping under key in m, created empty when key is absent
// or null. A value of another shape is an error naming where.
func mapEnsure(m *yaml.Node, key, where string) (*yaml.Node, error) {
	v := mapGet(m, key)
	if isNull(v) {
		child := mapping()
		mapSet(m, key, child)
		return child, nil
	}
	if !isMap(v) {
		return nil, fmt.Errorf("%s: %q is not a mapping (line %d)", where, key, v.Line)
	}
	return v, nil
}

// scalar is a string scalar node.
func scalar(s string) *yaml.Node {
	var n yaml.Node
	n.SetString(s)
	return &n
}

// mapping is an empty block mapping node.
func mapping() *yaml.Node {
	var n yaml.Node
	if err := n.Encode(map[string]string{}); err != nil {
		panic(fmt.Sprintf("workflows: building an empty mapping: %v", err))
	}
	n.Style = 0
	return &n
}

// sequence is a block sequence node of string scalars.
func sequence(items ...string) *yaml.Node {
	var n yaml.Node
	if err := n.Encode([]string{}); err != nil {
		panic(fmt.Sprintf("workflows: building an empty sequence: %v", err))
	}
	n.Style = 0
	for _, it := range items {
		n.Content = append(n.Content, scalar(it))
	}
	return &n
}

// stringsOf reads a scalar or a sequence of scalars as strings; null reads
// as none. Anything else is an error naming where.
func stringsOf(n *yaml.Node, where string) ([]string, error) {
	switch {
	case isNull(n):
		return nil, nil
	case isScalar(n):
		return []string{n.Value}, nil
	case isSeq(n):
		var out []string
		for _, item := range n.Content {
			if !isScalar(item) {
				return nil, fmt.Errorf("%s: an entry is not a string (line %d)", where, item.Line)
			}
			out = append(out, item.Value)
		}
		return out, nil
	}
	return nil, fmt.Errorf("%s is neither a string nor a list of strings (line %d)", where, n.Line)
}

// cloneNode is a deep copy of n.
func cloneNode(n *yaml.Node) *yaml.Node {
	c := *n
	c.Content = make([]*yaml.Node, len(n.Content))
	for i, child := range n.Content {
		c.Content[i] = cloneNode(child)
	}
	return &c
}

// encodeJobs renders the jobs, in order, as the lines that sit under a
// workflow's `jobs:` key, indented two spaces.
func encodeJobs(keys []string, jobs map[string]*yaml.Node) (string, error) {
	if len(keys) == 0 {
		return "", nil
	}
	m := mapping()
	for _, k := range keys {
		job, ok := jobs[k]
		if !ok {
			return "", fmt.Errorf("workflows: no job %q to encode", k)
		}
		m.Content = append(m.Content, scalar(k), job)
	}
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(2)
	if err := enc.Encode(m); err != nil {
		return "", fmt.Errorf("encoding the inlined jobs: %w", err)
	}
	if err := enc.Close(); err != nil {
		return "", fmt.Errorf("encoding the inlined jobs: %w", err)
	}
	text := strings.TrimPrefix(buf.String(), "---\n")
	if strings.TrimSpace(text) == "" {
		return "", errors.New("encoding the inlined jobs produced nothing")
	}
	return indentBlock(text, "  ") + "\n", nil
}

// stripExpressionWrapper is a GitHub expression without its optional
// ${{ ... }} wrapper.
func stripExpressionWrapper(expr string) string {
	e := strings.TrimSpace(expr)
	if strings.HasPrefix(e, "${{") && strings.HasSuffix(e, "}}") {
		return strings.TrimSpace(e[3 : len(e)-2])
	}
	return e
}
