package runstate

import (
	"fmt"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// BatchPlanFormatVersion is the format version of batch-plan.toml.
const BatchPlanFormatVersion = 1

// BatchPlanItem is one releasable of a batch release in progress, frozen
// when the batch was planned.
type BatchPlanItem struct {
	Name string
	// BaseVersion is the releasable's version when the batch was planned.
	BaseVersion string
	// TargetVersion is the version its release produces.
	TargetVersion string
	Tag           string
	// Registry is the registry it publishes to, empty when none.
	Registry string
	Bump     string
}

// BatchPlan is the plan of a batch release in progress, items in release
// order.
type BatchPlan struct {
	// BatchFile is the git blob id of the batch release file the plan was
	// made from, which tells that file from a later one naming the same
	// releasables; empty in a plan the record migration converted, which
	// does not know it.
	BatchFile string
	Items     []BatchPlanItem
}

// Item is the plan's item for the releasable name, and false when the plan
// holds none.
func (p BatchPlan) Item(name string) (BatchPlanItem, bool) {
	for _, it := range p.Items {
		if it.Name == name {
			return it, true
		}
	}
	return BatchPlanItem{}, false
}

type rawBatchPlanItem struct {
	Name          string `toml:"name,required"`
	BaseVersion   string `toml:"base_version,required"`
	TargetVersion string `toml:"target_version,required"`
	Tag           string `toml:"tag,required"`
	Registry      string `toml:"registry,required"`
	Bump          string `toml:"bump,required"`
}

type rawBatchPlan struct {
	FormatVersion int64              `toml:"format_version,required"`
	BatchFile     *string            `toml:"batch_file"`
	Items         []rawBatchPlanItem `toml:"items,required"`
}

// ParseBatchPlan parses batch-plan.toml. rel names the file in every
// problem. A releasable planned twice is refused: which of its two items the
// batch follows cannot be guessed.
func ParseBatchPlan(rel string, data []byte) (BatchPlan, error) {
	raw, err := decode[rawBatchPlan](rel, data)
	if err != nil {
		return BatchPlan{}, err
	}
	var problems []string
	if raw.FormatVersion != BatchPlanFormatVersion {
		problems = append(problems, fmt.Sprintf("format_version %d is not the batch plan format %d", raw.FormatVersion, BatchPlanFormatVersion))
	}
	if len(raw.Items) == 0 {
		problems = append(problems, "items plans no releasable")
	}
	plan := BatchPlan{}
	if raw.BatchFile != nil {
		if strings.TrimSpace(*raw.BatchFile) == "" {
			problems = append(problems, "batch_file is empty; leave it out when the batch release file is not known")
		}
		plan.BatchFile = *raw.BatchFile
	}
	seen := map[string]bool{}
	for i, it := range raw.Items {
		for _, f := range []struct{ name, value string }{
			{"name", it.Name}, {"base_version", it.BaseVersion}, {"target_version", it.TargetVersion}, {"tag", it.Tag}, {"bump", it.Bump},
		} {
			if strings.TrimSpace(f.value) == "" {
				problems = append(problems, fmt.Sprintf("items[%d]: %s is empty", i, f.name))
			}
		}
		if seen[it.Name] {
			problems = append(problems, fmt.Sprintf("items[%d]: the releasable %q is planned twice", i, it.Name))
		}
		seen[it.Name] = true
		plan.Items = append(plan.Items, BatchPlanItem(it))
	}
	if len(problems) > 0 {
		return BatchPlan{}, &FileError{File: rel, Problems: problems}
	}
	return plan, nil
}

// LoadBatchPlan reads the batch plan. found is false when no batch release
// is in progress.
func LoadBatchPlan(root string) (p BatchPlan, found bool, err error) {
	data, found, err := readFile(root, BatchPlanPath)
	if err != nil || !found {
		return BatchPlan{}, false, err
	}
	p, err = ParseBatchPlan(BatchPlanPath, data)
	return p, err == nil, err
}

// RenderBatchPlan writes a batch plan.
func RenderBatchPlan(p BatchPlan) []byte {
	var b strings.Builder
	fmt.Fprintf(&b, "format_version = %d\n", BatchPlanFormatVersion)
	if p.BatchFile != "" {
		fmt.Fprintf(&b, "batch_file = %s\n", quote(p.BatchFile))
	}
	for _, it := range p.Items {
		b.WriteString("\n[[items]]\n")
		fmt.Fprintf(&b, "name = %s\n", quote(it.Name))
		fmt.Fprintf(&b, "base_version = %s\n", quote(it.BaseVersion))
		fmt.Fprintf(&b, "target_version = %s\n", quote(it.TargetVersion))
		fmt.Fprintf(&b, "tag = %s\n", quote(it.Tag))
		fmt.Fprintf(&b, "registry = %s\n", quote(it.Registry))
		fmt.Fprintf(&b, "bump = %s\n", quote(it.Bump))
	}
	return []byte(b.String())
}

// SaveBatchPlan writes the batch plan, refusing one its own reader would
// refuse.
func SaveBatchPlan(e *strictcli.Effects, root string, p BatchPlan) error {
	data := RenderBatchPlan(p)
	if _, err := ParseBatchPlan(BatchPlanPath, data); err != nil {
		return err
	}
	return writeFile(e, root, BatchPlanPath, data)
}

// ClearBatchPlan removes the batch plan; a missing one is nothing to remove.
func ClearBatchPlan(e *strictcli.Effects, root string) error {
	return removeFile(e, root, BatchPlanPath)
}
