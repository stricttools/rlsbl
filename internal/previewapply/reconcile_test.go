package previewapply

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestPreviewKeepsItsOrderAndRefusesADuplicateKey(t *testing.T) {
	hygiene.Isolate(t)
	p, err := NewPreview(Item{Key: "b", State: "s1"}, Item{Key: "a", State: "s2"}, Item{Key: "c", State: "s1"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(p.Keys(), ",") != "b,a,c" || strings.Join(p.States(), ",") != "s1,s2,s1" || p.Len() != 3 {
		t.Fatalf("keys %v, states %v", p.Keys(), p.States())
	}
	if it, ok := p.ByKey("a"); !ok || it.State != "s2" {
		t.Fatalf("ByKey(a) = %+v, %v", it, ok)
	}
	if _, ok := p.ByKey("z"); ok {
		t.Fatal("ByKey found a key the preview does not hold")
	}
	if _, err := NewPreview(Item{Key: "a"}, Item{Key: "a"}); err == nil || !strings.Contains(err.Error(), `"a"`) {
		t.Fatalf("a duplicate key: %v", err)
	}
}

func TestSingleAndOnly(t *testing.T) {
	hygiene.Isolate(t)
	it, err := Single(Item{Key: "repo", State: "converged"}).Only()
	if err != nil || it.Key != "repo" {
		t.Fatalf("Only = %+v, %v", it, err)
	}
	p, _ := NewPreview(Item{Key: "a"}, Item{Key: "b"})
	if _, err := p.Only(); err == nil {
		t.Fatal("Only accepted a two-item preview")
	}
}

func TestStateLabel(t *testing.T) {
	hygiene.Isolate(t)
	if got := (Item{State: "scaffold_stale"}).StateLabel(); got != "scaffold-stale" {
		t.Errorf("default label = %q", got)
	}
	if got := (Item{State: "scaffold_stale", Label: "STALE"}).StateLabel(); got != "STALE" {
		t.Errorf("explicit label = %q", got)
	}
}

func TestDataIsCarriedUntouched(t *testing.T) {
	hygiene.Isolate(t)
	record := map[string]int{"occurrences": 3}
	p := Single(Item{Key: "k", Data: record})
	it, _ := p.Only()
	if it.Data.(map[string]int)["occurrences"] != 3 {
		t.Fatal("the observation record changed on its way through the preview")
	}
}

func TestRenderPrintsHeadlineFactsActionsAndDetailInOrder(t *testing.T) {
	hygiene.Isolate(t)
	p, _ := NewPreview(
		Item{Key: "go.mod", State: "rewrite", Summary: "2 occurrences", Facts: []string{"fact one"}, Actions: []string{"would rewrite"}, Detail: "    verbatim block"},
		Item{Key: "README.md", State: "nothing_to_do"},
	)
	want := "rewrite: 2 occurrences\n  fact one\n  would rewrite\n    verbatim block\nnothing-to-do"
	if got := Render(p, false); got != want {
		t.Errorf("Render without keys =\n%s\nwant\n%s", got, want)
	}
	wantKeys := "go.mod: rewrite: 2 occurrences\n  fact one\n  would rewrite\n    verbatim block\nREADME.md: nothing-to-do"
	if got := Render(p, true); got != wantKeys {
		t.Errorf("Render with keys =\n%s\nwant\n%s", got, wantKeys)
	}
}

// reconcileIn runs r inside a throwaway mutating command.
func reconcileIn(t *testing.T, dryRun bool, r Reconciler) (strictcli.Result, Preview, error) {
	t.Helper()
	var p Preview
	var rerr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: Prefixes()}, func(ctx *strictcli.Context) error {
		p, rerr = Reconcile(ctx, r)
		return rerr
	})
	return res, p, rerr
}

func TestADryRunRendersThePlanAndAppliesNothing(t *testing.T) {
	hygiene.Isolate(t)
	applied := 0
	res, p, err := reconcileIn(t, true, Reconciler{
		Observe: func(Observer) (Preview, error) {
			return NewPreview(Item{Key: "a", State: "rewrite"}, Item{Key: "b", State: "keep"})
		},
		Apply:    func(*strictcli.Effects, Item) error { applied++; return nil },
		ShowKeys: true,
	})
	if err != nil || res.ExitCode != 0 {
		t.Fatalf("exit %d: %v %s", res.ExitCode, err, res.Stderr)
	}
	if applied != 0 {
		t.Fatalf("a dry run applied %d items", applied)
	}
	if p.Len() != 2 || !strings.Contains(res.Stdout, "a: rewrite\nb: keep") {
		t.Fatalf("stdout = %q", res.Stdout)
	}
}

func TestAnApplyVisitsEveryItemInOrderAndPrintsNoPlan(t *testing.T) {
	hygiene.Isolate(t)
	var order []string
	res, _, err := reconcileIn(t, false, Reconciler{
		Observe: func(Observer) (Preview, error) {
			return NewPreview(Item{Key: "a", State: "x"}, Item{Key: "b", State: "x"}, Item{Key: "c", State: "x"})
		},
		Apply:    func(_ *strictcli.Effects, it Item) error { order = append(order, it.Key); return nil },
		ShowKeys: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(order, ",") != "a,b,c" {
		t.Fatalf("applied %v", order)
	}
	if strings.Contains(res.Stdout, "a: x") {
		t.Fatalf("an apply printed the plan: %q", res.Stdout)
	}
}

func TestTheApplyIsBelowTheLineAndMayWrite(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	_, _, err := reconcileIn(t, false, Reconciler{
		Observe: func(Observer) (Preview, error) { return Single(Item{Key: "out.txt", State: "create"}), nil },
		Apply: func(e *strictcli.Effects, it Item) error {
			_, err := e.Write(filepath.Join(dir, it.Key), "written")
			return err
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "out.txt")); err != nil || string(data) != "written" {
		t.Fatalf("the apply's write: %q, %v", data, err)
	}
}

// An observation that starts a program off the allowlist is refused at the
// attempt; one that reads runs for real, under --dry-run too.
func TestObservationRefusesAWriteAndRunsAReadForReal(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	head := repo.CommitFile("a.txt", "a\n", "first")
	for _, dryRun := range []bool{false, true} {
		var read string
		_, _, err := reconcileIn(t, dryRun, Reconciler{
			Observe: func(o Observer) (Preview, error) {
				c, err := o.Run([]interface{}{"git", "rev-parse", "HEAD"}, strictcli.Cwd(repo.Dir))
				if err != nil {
					return Preview{}, err
				}
				read = c.Stdout()
				_, err = o.Run([]interface{}{"git", "tag", "v1.0.0"}, strictcli.Cwd(repo.Dir))
				return Preview{}, err
			},
			Apply: func(*strictcli.Effects, Item) error { return nil },
		})
		var refusal *WriteDuringObservation
		if !errors.As(err, &refusal) || !strings.Contains(err.Error(), "`git tag`") {
			t.Fatalf("dry run %v: the tag during observation was not refused: %v", dryRun, err)
		}
		if read != head {
			t.Fatalf("dry run %v: the read printed %q, want %q", dryRun, read, head)
		}
	}
	if tags := repo.Git("tag", "--list"); tags != "" {
		t.Fatalf("an observation created tags: %q", tags)
	}
}

func TestObservationRefusesEveryProgramOffTheList(t *testing.T) {
	hygiene.Isolate(t)
	for _, argv := range [][]interface{}{
		{"git", "push", "origin", "main"},
		{"gh", "release", "create", "v1.0.0"},
		{"sh", "-c", "true"},
	} {
		_, _, err := reconcileIn(t, false, Reconciler{
			Observe: func(o Observer) (Preview, error) {
				_, err := o.Run(argv)
				return Preview{}, err
			},
			Apply: func(*strictcli.Effects, Item) error { return nil },
		})
		var refusal *WriteDuringObservation
		if !errors.As(err, &refusal) {
			t.Errorf("%v was not refused during observation: %v", argv, err)
		}
	}
}

// A failing apply stops the run and names what this run already wrote:
// without that, "nothing further was written" reads as "nothing at all".
func TestAFailingApplyNamesWhatItAlreadyWrote(t *testing.T) {
	hygiene.Isolate(t)
	_, _, err := reconcileIn(t, false, Reconciler{
		Observe: func(Observer) (Preview, error) {
			return NewPreview(Item{Key: "a"}, Item{Key: "b"}, Item{Key: "c"})
		},
		Apply: func(_ *strictcli.Effects, it Item) error {
			if it.Key == "c" {
				return errors.New("count moved")
			}
			return nil
		},
	})
	if err == nil || !strings.Contains(err.Error(), "c: count moved") || !strings.Contains(err.Error(), "still changed on disk: a, b.") {
		t.Fatalf("err = %v", err)
	}
	_, _, err = reconcileIn(t, false, Reconciler{
		Observe: func(Observer) (Preview, error) { return Single(Item{Key: "a"}), nil },
		Apply:   func(*strictcli.Effects, Item) error { return errors.New("boom") },
	})
	if err == nil || !strings.Contains(err.Error(), "Nothing had been written by this run") {
		t.Fatalf("err = %v", err)
	}
}

// countingReconciler plans one item counting "old" in the file at path, and
// applies it by replacing every "old" with "new" after checking the count
// still holds. When midApply is set, the apply first rewrites the file to it,
// the way another writer could between the preview and the apply.
func countingReconciler(path, midApply string) Reconciler {
	count := func() (int, error) {
		data, err := os.ReadFile(path)
		return strings.Count(string(data), "old"), err
	}
	return Reconciler{
		Observe: func(Observer) (Preview, error) {
			n, err := count()
			return Single(Item{Key: "go.mod", State: "rewrite", Data: n}), err
		},
		Apply: func(e *strictcli.Effects, it Item) error {
			if midApply != "" {
				if _, err := e.Write(path, midApply); err != nil {
					return err
				}
			}
			n, err := count()
			if err != nil {
				return err
			}
			if err := CountMoved(it.Key, it.Data.(int), n); err != nil {
				return err
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			_, err = e.Write(path, strings.ReplaceAll(string(data), "old", "new"))
			return err
		},
	}
}

// CountMoved's refusal names the fix (plan again with --dry-run, then apply);
// performing it against the tree as it now is clears the refusal.
func TestACountThatMovedIsRefusedAndAFreshPlanClearsIt(t *testing.T) {
	hygiene.Isolate(t)
	path := filepath.Join(t.TempDir(), "go.mod")
	testsupport.WriteFile(t, path, "old old\n")
	_, _, err := reconcileIn(t, false, countingReconciler(path, "old old old\n"))
	if err == nil || !strings.Contains(err.Error(), "counted 2 occurrence(s) in go.mod but it now has 3") || !strings.Contains(err.Error(), "--dry-run") {
		t.Fatalf("err = %v", err)
	}
	res, p, err := reconcileIn(t, true, countingReconciler(path, ""))
	if err != nil || !strings.Contains(res.Stdout, "rewrite") {
		t.Fatalf("the fresh plan: %v %q", err, res.Stdout)
	}
	if it, _ := p.Only(); it.Data.(int) != 3 {
		t.Fatalf("the fresh plan counted %v", it.Data)
	}
	if _, _, err := reconcileIn(t, false, countingReconciler(path, "")); err != nil {
		t.Fatalf("applying after a fresh plan: %v", err)
	}
	if data, _ := os.ReadFile(path); string(data) != "new new new\n" {
		t.Fatalf("go.mod = %q", data)
	}
	if CountMoved("k", 2, 2) != nil {
		t.Fatal("equal counts were refused")
	}
}

func TestAReconcilerNeedsBothHalves(t *testing.T) {
	hygiene.Isolate(t)
	_, _, err := reconcileIn(t, false, Reconciler{Observe: func(Observer) (Preview, error) { return Preview{}, nil }})
	if err == nil {
		t.Fatal("a reconciler without an apply ran")
	}
}
