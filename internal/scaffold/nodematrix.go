package scaffold

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// The Node versions an npm package's CI tests on are derived from its
// engines.node range: every supported Node line whose newest release the
// range admits, because actions/setup-node installs a line's newest release
// for node-version: <line>. A package declaring no engines.node states no
// Node support, and its CI is refused rather than given a matrix nobody
// claimed.

// NodeLines are the Node release lines rlsbl scaffolds CI for.
var NodeLines = []int{20, 22, 24}

// newestPart stands in for "the newest minor or patch of a line": any real
// release is lower.
const newestPart = 1_000_000_000

var (
	nodeComparator = regexp.MustCompile(`^(<=|>=|<|>|=|\^|~>|~)?\s*v?([0-9]+|[xX*])(?:\.([0-9]+|[xX*]))?(?:\.([0-9]+|[xX*]))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?`)
	nodeHyphen     = regexp.MustCompile(`^(\S+)\s+-\s+(\S+)$`)
)

// nodeVersion is major, minor, patch.
type nodeVersion [3]int

func (a nodeVersion) compare(b nodeVersion) int {
	for i := range a {
		if a[i] != b[i] {
			if a[i] < b[i] {
				return -1
			}
			return 1
		}
	}
	return 0
}

// nodeBound is one bound of a comparator set: op is >=, >, <, or <=.
type nodeBound struct {
	op string
	v  nodeVersion
}

// nodePart is one version part, or -1 for a wildcard or an absent part.
func nodePart(text string) int {
	if text == "" || text == "x" || text == "X" || text == "*" {
		return -1
	}
	n, err := strconv.Atoi(text)
	if err != nil {
		return -1
	}
	return n
}

func lowerOf(major, minor, patch int) nodeVersion {
	v := nodeVersion{major, minor, patch}
	for i := range v {
		if v[i] < 0 {
			v[i] = 0
		}
	}
	return v
}

func comparatorBounds(op string, major, minor, patch int) []nodeBound {
	if major < 0 {
		return nil
	}
	if op == "" {
		op = "="
	}
	switch op {
	case "=":
		switch {
		case minor < 0:
			return []nodeBound{{">=", nodeVersion{major, 0, 0}}, {"<", nodeVersion{major + 1, 0, 0}}}
		case patch < 0:
			return []nodeBound{{">=", nodeVersion{major, minor, 0}}, {"<", nodeVersion{major, minor + 1, 0}}}
		}
		return []nodeBound{{">=", nodeVersion{major, minor, patch}}, {"<=", nodeVersion{major, minor, patch}}}
	case ">=":
		return []nodeBound{{">=", lowerOf(major, minor, patch)}}
	case ">":
		switch {
		case minor < 0:
			return []nodeBound{{">=", nodeVersion{major + 1, 0, 0}}}
		case patch < 0:
			return []nodeBound{{">=", nodeVersion{major, minor + 1, 0}}}
		}
		return []nodeBound{{">", nodeVersion{major, minor, patch}}}
	case "<":
		return []nodeBound{{"<", lowerOf(major, minor, patch)}}
	case "<=":
		switch {
		case minor < 0:
			return []nodeBound{{"<", nodeVersion{major + 1, 0, 0}}}
		case patch < 0:
			return []nodeBound{{"<", nodeVersion{major, minor + 1, 0}}}
		}
		return []nodeBound{{"<=", nodeVersion{major, minor, patch}}}
	case "~", "~>":
		low := lowerOf(major, minor, patch)
		if minor < 0 {
			return []nodeBound{{">=", low}, {"<", nodeVersion{major + 1, 0, 0}}}
		}
		return []nodeBound{{">=", low}, {"<", nodeVersion{major, minor + 1, 0}}}
	}
	// ^
	low := lowerOf(major, minor, patch)
	switch {
	case major > 0 || minor < 0:
		return []nodeBound{{">=", low}, {"<", nodeVersion{major + 1, 0, 0}}}
	case minor > 0 || patch < 0:
		return []nodeBound{{">=", low}, {"<", nodeVersion{0, minor + 1, 0}}}
	}
	return []nodeBound{{">=", low}, {"<", nodeVersion{0, 0, patch + 1}}}
}

// parseNodeSet reads the bounds of one ||-separated comparator set; whole is
// the full range, for errors.
func parseNodeSet(text, whole string) ([]nodeBound, error) {
	text = strings.TrimSpace(text)
	if text == "" || text == "*" || text == "x" || text == "X" {
		return nil, nil
	}
	if m := nodeHyphen.FindStringSubmatch(text); m != nil {
		low, err := parseNodeSet(m[1], whole)
		if err != nil {
			return nil, err
		}
		high, err := parseNodeSet("<="+m[2], whole)
		if err != nil {
			return nil, err
		}
		var bounds []nodeBound
		for _, b := range low {
			if b.op == ">=" || b.op == ">" {
				bounds = append(bounds, b)
			}
		}
		return append(bounds, high...), nil
	}
	var bounds []nodeBound
	for pos := 0; pos < len(text); {
		if text[pos] == ' ' || text[pos] == '\t' {
			pos++
			continue
		}
		m := nodeComparator.FindStringSubmatchIndex(text[pos:])
		if m == nil || m[1] == 0 {
			return nil, fmt.Errorf("engines.node %q is not a range rlsbl can read (at %q); write it as an npm semver range such as \">=22\" or \"^22 || ^24\"", whole, text[pos:])
		}
		group := func(i int) string {
			if m[2*i] < 0 {
				return ""
			}
			return text[pos+m[2*i] : pos+m[2*i+1]]
		}
		bounds = append(bounds, comparatorBounds(group(1), nodePart(group(2)), nodePart(group(3)), nodePart(group(4)))...)
		pos += m[1]
	}
	return bounds, nil
}

func nodeSatisfies(v nodeVersion, bounds []nodeBound) bool {
	for _, b := range bounds {
		c := v.compare(b.v)
		switch b.op {
		case ">=":
			if c < 0 {
				return false
			}
		case ">":
			if c <= 0 {
				return false
			}
		case "<":
			if c >= 0 {
				return false
			}
		case "<=":
			if c > 0 {
				return false
			}
		}
	}
	return true
}

// NodeLinesSatisfying are the supported Node lines whose newest release
// nodeRange admits. A range rlsbl cannot read, or one admitting no
// supported line, is refused.
func NodeLinesSatisfying(nodeRange string) ([]int, error) {
	var sets [][]nodeBound
	for _, part := range strings.Split(nodeRange, "||") {
		bounds, err := parseNodeSet(part, nodeRange)
		if err != nil {
			return nil, err
		}
		sets = append(sets, bounds)
	}
	var lines []int
	for _, line := range NodeLines {
		for _, bounds := range sets {
			if nodeSatisfies(nodeVersion{line, newestPart, newestPart}, bounds) {
				lines = append(lines, line)
				break
			}
		}
	}
	if len(lines) == 0 {
		return nil, fmt.Errorf("engines.node %q admits none of the Node lines rlsbl tests on (%s), so CI would test on nothing; widen the range in package.json", nodeRange, joinInts(NodeLines))
	}
	return lines, nil
}

func joinInts(ns []int) string {
	parts := make([]string, len(ns))
	for i, n := range ns {
		parts[i] = strconv.Itoa(n)
	}
	return strings.Join(parts, ", ")
}

// errNoEngines is the refusal of a package declaring no engines.node.
func errNoEngines(path string) error {
	return fmt.Errorf("%s declares no engines.node, so rlsbl cannot tell which Node versions CI must test on (it tests every supported line, %s, that the range admits); declare the Node versions the package supports, for example \"engines\": {\"node\": \">=22\"} in package.json, and run rlsbl scaffold again", path, joinInts(NodeLines))
}

// NodeMatrix is the Node CI matrix of the npm package in dir.
func NodeMatrix(dir string) ([]int, error) {
	path := filepath.Join(dir, "package.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	var pkg struct {
		Engines map[string]any `json:"engines"`
	}
	if err := json.Unmarshal(data, &pkg); err != nil {
		var typeErr *json.UnmarshalTypeError
		if errors.As(err, &typeErr) {
			return nil, errNoEngines(path)
		}
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	node, _ := pkg.Engines["node"].(string)
	if strings.TrimSpace(node) == "" {
		return nil, errNoEngines(path)
	}
	return NodeLinesSatisfying(node)
}

// RenderNodeMatrix is the YAML flow sequence a workflow's node-version
// takes.
func RenderNodeMatrix(lines []int) string {
	return "[" + joinInts(lines) + "]"
}
