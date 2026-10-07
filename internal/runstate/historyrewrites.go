package runstate

import "github.com/stricttools/strictcli/go/strictcli"

// The run state of the history rewrites: the scrub result a stopped scrub
// resumes from, and the reconcile plan the apply half performs. Their
// formats belong to the commands that write them; this file keeps where
// they live and how they are written, the same way as every other run-state
// file.

// LoadScrubResult reads the scrub result. found is false when no scrub is
// in progress.
func LoadScrubResult(root string) (data []byte, found bool, err error) {
	return readFile(root, ScrubResultPath)
}

// SaveScrubResult replaces the scrub result with data, atomically.
func SaveScrubResult(e *strictcli.Effects, root string, data []byte) error {
	return writeFile(e, root, ScrubResultPath, data)
}

// ClearScrubResult removes the scrub result when it exists.
func ClearScrubResult(e *strictcli.Effects, root string) error {
	return removeFile(e, root, ScrubResultPath)
}

// LoadReconcilePlan reads a releasable's reconcile plan. found is false when
// none was written.
func LoadReconcilePlan(root, releasable string) (data []byte, found bool, err error) {
	return readFile(root, ReconcilePlanPath(releasable))
}

// SaveReconcilePlan replaces a releasable's reconcile plan with data,
// atomically.
func SaveReconcilePlan(e *strictcli.Effects, root, releasable string, data []byte) error {
	return writeFile(e, root, ReconcilePlanPath(releasable), data)
}

// ClearReconcilePlan removes a releasable's reconcile plan when it exists.
func ClearReconcilePlan(e *strictcli.Effects, root, releasable string) error {
	return removeFile(e, root, ReconcilePlanPath(releasable))
}
