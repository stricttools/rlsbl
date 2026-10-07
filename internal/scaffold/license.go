package scaffold

import (
	"fmt"
	"strconv"

	"github.com/stricttools/strictspec/go/lifecycle"
)

// Scaffold writes a LICENSE only from the lifecycle-and-license record: the
// license in effect for the member's releasable on the scaffold's date, when
// rlsbl carries that license's text, into a member versioned under a
// releasable, and only while no LICENSE exists (once written it is the
// project's). It never chooses a license: a repository without a record, a
// releasable without a license period, and a proprietary releasable get no
// LICENSE from scaffold.

// licenseTemplates are the license texts rlsbl carries, by SPDX identifier.
var licenseTemplates = map[string]string{
	"MIT": "shared/licenses/MIT.tpl",
}

// licenseText is the LICENSE text the member gets, and false with the
// reason when it gets none.
func (c *memberContext) licenseText() (string, bool, error) {
	text, reason, err := c.licenseDecision()
	return text, reason == "", err
}

// licenseDecision is the LICENSE text, or the reason scaffold writes none.
func (c *memberContext) licenseDecision() (text, reason string, err error) {
	switch {
	case !c.versioned:
		return "", "the member is versioned under no releasable", nil
	case c.license == "":
		return "", fmt.Sprintf("the lifecycle-and-license record holds no license in effect for %q", c.releasable.Name), nil
	case c.license == lifecycle.ProprietaryLicense:
		return "", fmt.Sprintf("%q is proprietary", c.releasable.Name), nil
	}
	template, ok := licenseTemplates[c.license]
	if !ok {
		return "", fmt.Sprintf("rlsbl carries no text of %s, the license of %q", c.license, c.releasable.Name), nil
	}
	if c.author == "" {
		return "", "", fmt.Errorf("the LICENSE scaffold writes for %q names its copyright holder from git's user.name, which is not set; set it with `git config user.name \"<name>\"` and run rlsbl scaffold again", c.releasable.Name)
	}
	text, err = renderTemplate(template, Vars{"year": strconv.Itoa(c.now.Year()), "author": c.author})
	return text, "", err
}
