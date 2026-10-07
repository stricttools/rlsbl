package github

import (
	"fmt"

	"github.com/stricttools/strictcli/go/strictcli"
)

// SetVisibility makes the repository public or private on GitHub. gh asks
// for the consequences of the change to be accepted on its command line,
// and the calling command's own consent is that acceptance.
func (c Client) SetVisibility(repo Repository, v Visibility, extra ...strictcli.EffectOption) error {
	if v != Public && v != Private {
		return fmt.Errorf("a repository is made %s or %s, not %q", Public, Private, v)
	}
	return c.write(nil, extra, "repo", "edit", repo.String(), "--visibility", string(v), "--accept-visibility-change-consequences")
}
