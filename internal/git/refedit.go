package git

import (
	"fmt"
	"strings"
)

// LocalRefs maps every local ref whose full name starts with prefix
// (refs/tags/, say) to the object it holds, unpeeled: an annotated tag's
// value is its tag object.
func (r Repo) LocalRefs(prefix string) (map[string]string, error) {
	if !strings.HasPrefix(prefix, "refs/") {
		return nil, fmt.Errorf("%q is not a ref prefix (refs/...)", prefix)
	}
	out, err := r.output("for-each-ref", "--format=%(objectname) %(refname)", prefix)
	if err != nil {
		return nil, err
	}
	refs := map[string]string{}
	for _, line := range lines(out) {
		object, name, ok := strings.Cut(line, " ")
		if !ok || !IsObjectID(object) || !strings.HasPrefix(name, prefix) {
			return nil, fmt.Errorf("git for-each-ref in %s printed an unreadable line %q", r.dir, line)
		}
		refs[name] = object
	}
	return refs, nil
}

// RemoteRefs maps every ref remote (a remote's name or a URL) holds under
// the patterns to its object, unpeeled (`git ls-remote --refs`): an
// annotated tag's value is its tag object, the object a tag keeps when it
// moves. A remote that cannot be read is an error, never an empty map.
func (r Repo) RemoteRefs(remote string, patterns ...string) (map[string]string, error) {
	return r.remoteRefs(remote, []string{"--refs"}, patterns...)
}

// HasObject reports whether the object database holds the object id.
func (r Repo) HasObject(id string) (bool, error) {
	if !IsObjectID(id) {
		return false, fmt.Errorf("%q is not a full object id", id)
	}
	args := []string{"cat-file", "-e", id}
	res, err := r.read(localTimeout, nil, args...)
	if err != nil {
		return false, err
	}
	switch res.code {
	case 0:
		return true, nil
	case 1:
		return false, nil
	}
	return false, r.failed(args, res)
}

// nullObjectID is the all-zeros id as long as id, which update-ref reads as
// "the ref must not exist".
func nullObjectID(id string) string { return strings.Repeat("0", len(id)) }

// CreateRef writes the local ref (a full ref name) at object, refusing when
// the ref already exists: update-ref checks the ref against the null id.
func (r Repo) CreateRef(ref, object string) error {
	if !strings.HasPrefix(ref, "refs/") {
		return fmt.Errorf("refusing to write %q: it is not a full ref name (refs/...)", ref)
	}
	if !IsObjectID(object) {
		return fmt.Errorf("refusing to write %s: %q is not a full object id", ref, object)
	}
	return r.mutate(localTimeout, "update-ref", ref, object, nullObjectID(object))
}

// DeleteRef deletes the local ref (a full ref name), refusing unless it
// still holds expected: a ref somebody moved since it was read is left
// alone.
func (r Repo) DeleteRef(ref, expected string) error {
	if !strings.HasPrefix(ref, "refs/") {
		return fmt.Errorf("refusing to delete %q: it is not a full ref name (refs/...)", ref)
	}
	if !IsObjectID(expected) {
		return fmt.Errorf("refusing to delete %s: %q is not a full object id", ref, expected)
	}
	return r.mutate(localTimeout, "update-ref", "-d", ref, expected)
}
