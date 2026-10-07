package runstate

import (
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// RetryFormatVersion is the format version of retry.toml.
const RetryFormatVersion = 1

// Retry is what `release retry` dispatches: the workflows to run again, at
// the release tag.
type Retry struct {
	// Ref is the release tag the workflows run at.
	Ref       string
	Workflows []string
}

type rawRetry struct {
	FormatVersion int64    `toml:"format_version,required"`
	Ref           string   `toml:"ref,required"`
	Workflows     []string `toml:"workflows,required"`
}

// ParseRetry parses retry.toml. rel names the file in every problem.
func ParseRetry(rel string, data []byte) (Retry, error) {
	raw, err := decode[rawRetry](rel, data)
	if err != nil {
		return Retry{}, err
	}
	var problems []string
	if raw.FormatVersion != RetryFormatVersion {
		problems = append(problems, fmt.Sprintf("format_version %d is not the retry file format %d", raw.FormatVersion, RetryFormatVersion))
	}
	if strings.TrimSpace(raw.Ref) == "" || raw.Ref != strings.TrimSpace(raw.Ref) {
		problems = append(problems, fmt.Sprintf("ref %q is not a tag: it is the release tag the workflows run at, with no surrounding whitespace", raw.Ref))
	}
	if len(raw.Workflows) == 0 {
		problems = append(problems, "workflows names no workflow to dispatch")
	}
	seen := map[string]bool{}
	for _, w := range raw.Workflows {
		switch {
		case strings.TrimSpace(w) == "":
			problems = append(problems, "workflows holds an empty name")
		case seen[w]:
			problems = append(problems, fmt.Sprintf("workflows names %q twice", w))
		}
		seen[w] = true
	}
	if len(problems) > 0 {
		return Retry{}, &FileError{File: rel, Problems: problems}
	}
	return Retry{Ref: raw.Ref, Workflows: raw.Workflows}, nil
}

// LoadRetry reads a releasable's retry file. found is false when there is
// none.
func LoadRetry(root, releasable string) (r Retry, found bool, err error) {
	rel := RetryPath(releasable)
	data, found, err := readFile(root, rel)
	if err != nil || !found {
		return Retry{}, false, err
	}
	r, err = ParseRetry(rel, data)
	return r, err == nil, err
}

// RenderRetry writes a retry file.
func RenderRetry(r Retry) []byte {
	return []byte(fmt.Sprintf("format_version = %d\nref = %s\nworkflows = %s\n", RetryFormatVersion, quote(r.Ref), stringArray(r.Workflows)))
}

// SaveRetry writes a releasable's retry file, refusing one its own reader
// would refuse.
func SaveRetry(e *strictcli.Effects, root, releasable string, r Retry) error {
	rel := RetryPath(releasable)
	data := RenderRetry(r)
	if _, err := ParseRetry(rel, data); err != nil {
		return err
	}
	return writeFile(e, root, rel, data)
}

// RemoveRetry removes a releasable's retry file; a missing one is nothing to
// remove.
func RemoveRetry(e *strictcli.Effects, root, releasable string) error {
	return removeFile(e, root, RetryPath(releasable))
}
