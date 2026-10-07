package runstate

import (
	"errors"
	"fmt"
	"os"
	"sync"
	"syscall"

	"github.com/stricttools/strictcli/go/strictcli"
)

// The advisory lock is one file per repository, .release-state/lock, and
// every operation that mutates release state takes it: release run, the
// batch release, scaffold, scrub, and the repository conversions. There is
// deliberately no second lock: a lock a release does not take excludes
// nothing.
//
// The lock is flock(2) on the file, taken through a read-only descriptor, so
// taking it writes nothing. The file is created through the effects handle
// when missing and never removed: removing it would let a process waiting on
// the old file and one creating a new file both hold "the" lock. The
// directory's .gitignore keeps it out of git.
//
// Under --dry-run the file is not created. A preview takes the lock when the
// file exists, so it waits for (or refuses) a live run mutating the state it
// reads, and takes none when it does not, which means no run ever held it.

// Wait is what Acquire does when another process holds the lock.
type Wait string

// The two answers.
const (
	// WaitForHolder blocks until the holder releases the lock.
	WaitForHolder Wait = "wait"
	// RefuseWhenHeld refuses with *HeldError, for a long destructive
	// operation better refused with the holder named than queued behind a
	// release that may be waiting on CI.
	RefuseWhenHeld Wait = "refuse"
)

// HeldError is the refusal of a held lock under RefuseWhenHeld.
type HeldError struct{ Path string }

func (e *HeldError) Error() string {
	return fmt.Sprintf("another rlsbl process holds %s: a release or a repository conversion is already mutating this repository's release state; wait for it to finish (or resume it) and re-run", e.Path)
}

// held is one lock this process holds, with how many nested acquires it
// carries: a release takes the lock and the code it runs (the batch release
// running each releasable's release) takes it again, and the inner release
// must not drop the outer holder's lock. flock conflicts between two
// descriptors of one process, so a nested acquire must not open another.
type held struct {
	file  *os.File
	depth int
}

var (
	heldMu sync.Mutex
	locks  = map[string]*held{}
)

// Lock is a taken advisory lock; Release gives it back.
type Lock struct {
	path string
	// took is false for a preview that found no lock file and took nothing.
	took     bool
	released bool
}

// AcquireOptions declares how a lock is taken.
type AcquireOptions struct {
	// DryRun is whether the command runs under --dry-run.
	DryRun bool
	Wait   Wait
	// OnWait is told once, before blocking, that another process holds the
	// lock; it is required with WaitForHolder.
	OnWait func(path string)
}

// Acquire takes the advisory lock of the repository rooted at root.
func Acquire(e *strictcli.Effects, root string, o AcquireOptions) (*Lock, error) {
	if o.Wait != WaitForHolder && o.Wait != RefuseWhenHeld {
		return nil, fmt.Errorf("acquiring the lock: state %q or %q, not %q", WaitForHolder, RefuseWhenHeld, o.Wait)
	}
	if o.Wait == WaitForHolder && o.OnWait == nil {
		return nil, errors.New("acquiring the lock: waiting for a holder needs OnWait, so the operator learns why nothing happens")
	}
	path := absolute(root, LockPath)
	heldMu.Lock()
	if h, ok := locks[path]; ok {
		h.depth++
		heldMu.Unlock()
		return &Lock{path: path, took: true}, nil
	}
	heldMu.Unlock()
	found, err := exists(root, LockPath)
	if err != nil {
		return nil, err
	}
	if !found {
		if o.DryRun {
			return &Lock{path: path}, nil
		}
		if err := ensureDirectory(e, root); err != nil {
			return nil, err
		}
		if _, err := e.Write(path, "", strictcli.Mode(stateFileMode)); err != nil {
			return nil, fmt.Errorf("creating %s: %w", LockPath, err)
		}
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("opening %s: %w", LockPath, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		if !errors.Is(err, syscall.EWOULDBLOCK) {
			f.Close()
			return nil, fmt.Errorf("locking %s: %w", LockPath, err)
		}
		if o.Wait == RefuseWhenHeld {
			f.Close()
			return nil, &HeldError{Path: LockPath}
		}
		o.OnWait(LockPath)
		if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
			f.Close()
			return nil, fmt.Errorf("locking %s: %w", LockPath, err)
		}
	}
	heldMu.Lock()
	locks[path] = &held{file: f}
	heldMu.Unlock()
	return &Lock{path: path, took: true}, nil
}

// Release gives the lock back: a nested acquire leaves it with its outer
// holder, and the outermost release unlocks it. Releasing one Lock twice is
// an error.
func (l *Lock) Release() error {
	if l.released {
		return fmt.Errorf("releasing %s twice", l.path)
	}
	l.released = true
	if !l.took {
		return nil
	}
	heldMu.Lock()
	defer heldMu.Unlock()
	h, ok := locks[l.path]
	if !ok {
		return fmt.Errorf("releasing %s: this process does not hold it", l.path)
	}
	if h.depth > 0 {
		h.depth--
		return nil
	}
	delete(locks, l.path)
	unlockErr := syscall.Flock(int(h.file.Fd()), syscall.LOCK_UN)
	closeErr := h.file.Close()
	if unlockErr != nil {
		return fmt.Errorf("unlocking %s: %w", l.path, unlockErr)
	}
	return closeErr
}

// IsStale reports whether the lock file exists while no process holds it:
// left by a run that ended without releasing it, which the next acquire
// takes over.
func IsStale(root string) (bool, error) {
	found, err := exists(root, LockPath)
	if err != nil || !found {
		return false, err
	}
	f, err := os.Open(absolute(root, LockPath))
	if err != nil {
		return false, fmt.Errorf("opening %s: %w", LockPath, err)
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return false, nil
		}
		return false, fmt.Errorf("locking %s: %w", LockPath, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_UN); err != nil {
		return false, fmt.Errorf("unlocking %s: %w", LockPath, err)
	}
	return true, nil
}
