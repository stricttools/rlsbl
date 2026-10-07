package pipelines

import (
	"os"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestThePipelineTypesTableIsFresh(t *testing.T) {
	hygiene.Isolate(t)
	fresh, err := RenderTypeTable()
	if err != nil {
		t.Fatal(err)
	}
	committed, err := os.ReadFile("pipeline-types.json")
	if err != nil {
		t.Fatal(err)
	}
	if string(committed) != string(fresh) {
		t.Fatalf("%s does not match the pipeline types; run `%s` from the repository root and commit the result", TypeTablePath, RegenerateCommand)
	}
}
