package releaserecord

import (
	"fmt"
	"sort"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// BatchReleaseFilePath is the repository-relative path of a workspace's
// batch release file.
const BatchReleaseFilePath = declarations.BatchReleasesDir + "/" + ReleaseFileName

// BatchArchivePath is the repository-relative path of the archive of a batch
// release made at t, named by its UTC time to the second.
func BatchArchivePath(t time.Time) string {
	return declarations.BatchReleasesDir + "/batch-" + t.UTC().Format("20060102T150405Z") + ".toml"
}

// BatchReleaseFile is a workspace's batch release: one release per
// releasable, keyed by its name.
type BatchReleaseFile struct {
	Releasables map[string]ReleaseFile
}

// Names lists the releasables the batch releases, in name order.
func (b BatchReleaseFile) Names() []string {
	names := make([]string, 0, len(b.Releasables))
	for name := range b.Releasables {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

type rawBatchReleaseFile struct {
	FormatVersion int64                       `toml:"format_version,required"`
	Releasables   map[string]RawReleaseFields `toml:"releasables,required"`
}

// ParseBatchReleaseFile parses a batch release file: format_version 2 and
// one [releasables.<name>] table per releasable, each with the release
// file's fields and none of an archive's. rel names the file in every
// problem.
func ParseBatchReleaseFile(rel string, data []byte) (BatchReleaseFile, error) {
	if fields, err := tomledit.Unmarshal[map[string]any](data); err == nil {
		var problems []string
		if _, ok := (*fields)["packages"]; ok {
			problems = append(problems, "a [packages] section is refused: a batch release names releasables; write one [releasables.<name>] table per releasable (`rlsbl monorepo release init --all`, or `--releasables <name>` once per releasable, writes them)")
		}
		if v, ok := (*fields)["format_version"].(int64); ok && v != FormatVersion {
			problems = append(problems, fmt.Sprintf("format_version %d is not the batch release file format %d; rlsbl migrate records converts the first format", v, FormatVersion))
		}
		if tables, ok := (*fields)["releasables"].(map[string]any); ok {
			for _, name := range sortedKeys(tables) {
				if table, ok := tables[name].(map[string]any); ok {
					problems = append(problems, retiredProblems(table, "[releasables."+name+"] ")...)
				}
			}
		}
		if len(problems) > 0 {
			return BatchReleaseFile{}, refuseFile(rel, problems...)
		}
	}
	raw, err := tomledit.Unmarshal[rawBatchReleaseFile](data)
	if err != nil {
		return BatchReleaseFile{}, refuseFile(rel, err.Error())
	}
	if len(raw.Releasables) == 0 {
		return BatchReleaseFile{}, refuseFile(rel, "[releasables] names no releasable; a batch release releases at least one")
	}
	batch := BatchReleaseFile{Releasables: map[string]ReleaseFile{}}
	var problems []string
	for _, name := range sortedKeys(raw.Releasables) {
		where := "[releasables." + name + "] "
		d, found := raw.Releasables[name].convert(where)
		problems = append(problems, found...)
		if present := d.flowOwnedPresent(); len(present) > 0 {
			problems = append(problems, fmt.Sprintf("%s%s: only rlsbl writes these, and only into an archive; delete the lines", where, strings.Join(present, ", ")))
		}
		batch.Releasables[name] = d.file
	}
	if len(problems) > 0 {
		return BatchReleaseFile{}, refuseFile(rel, problems...)
	}
	return batch, nil
}

// ReadBatchReleaseFile reads a workspace's batch release file. A missing file
// is refused, naming the command that writes one.
func ReadBatchReleaseFile(root string) (BatchReleaseFile, error) {
	data, found, err := readFile(root, BatchReleaseFilePath)
	if err != nil {
		return BatchReleaseFile{}, err
	}
	if !found {
		return BatchReleaseFile{}, fmt.Errorf("this workspace has no batch release file %s: write one with `rlsbl monorepo release init --all` (or `--releasables <name>` once per releasable), then fill in each releasable's bump and description", BatchReleaseFilePath)
	}
	return ParseBatchReleaseFile(BatchReleaseFilePath, data)
}

// RenderBatchReleaseFile renders a batch release file, releasables in name
// order.
func RenderBatchReleaseFile(b BatchReleaseFile) []byte {
	var out strings.Builder
	fmt.Fprintf(&out, "format_version = %d\n", FormatVersion)
	for _, name := range b.Names() {
		fmt.Fprintf(&out, "\n[releasables.%s]\n", tomledit.QuoteKey(name))
		out.WriteString(renderFields(b.Releasables[name]))
	}
	return []byte(out.String())
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
