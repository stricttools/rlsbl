package registry

import (
	"encoding/json"
	"fmt"
)

// PypiRelease is what the PyPI project document says about one release of
// a project: whether the document lists it, and how many of its files PyPI
// serves and how many of those are yanked.
type PypiRelease struct {
	Project string
	Version string
	// Listed is whether the document's releases name the version.
	Listed bool
	// Files counts the release's files; Yanked counts those marked yanked.
	Files  int
	Yanked int
}

// IsYanked reports whether the release is listed, has files, and every one
// of them is yanked: what PyPI's "Yank release" leaves behind.
func (r PypiRelease) IsYanked() bool { return r.Listed && r.Files > 0 && r.Yanked == r.Files }

// PypiRelease reads one release of name from the project document, the
// package-level read every PyPI question goes through: the version is looked
// up in the document's releases, never requested by its own URL. found is
// false when PyPI has no project document of that name (404).
func (c Client) PypiRelease(name, version string) (release PypiRelease, found bool, err error) {
	u := pypiDocumentURL(name)
	r, err := c.get(u)
	if err != nil {
		return PypiRelease{}, false, err
	}
	if r.status == 404 {
		return PypiRelease{}, false, nil
	}
	if r.status != 200 {
		return PypiRelease{}, false, unexpected(u, r)
	}
	var doc struct {
		Releases map[string][]struct {
			Yanked *bool `json:"yanked"`
		} `json:"releases"`
	}
	if err := json.Unmarshal(r.body, &doc); err != nil {
		return PypiRelease{}, false, fmt.Errorf("GET %s answered something that is not a project document: %w", u, err)
	}
	if doc.Releases == nil {
		return PypiRelease{}, false, fmt.Errorf("GET %s answered a project document without releases", u)
	}
	release = PypiRelease{Project: name, Version: version}
	files, listed := doc.Releases[version]
	if !listed {
		return release, true, nil
	}
	release.Listed = true
	for _, f := range files {
		if f.Yanked == nil {
			return PypiRelease{}, false, fmt.Errorf("GET %s lists a file of %s without its yanked state", u, version)
		}
		release.Files++
		if *f.Yanked {
			release.Yanked++
		}
	}
	return release, true, nil
}
