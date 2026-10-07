package declarations

import (
	"fmt"

	tomledit "github.com/stricttools/go-toml-edit"
)

// SetMemberImportName sets the import_name of the member at path; an empty
// name deletes the key.
func (ed *Editor) SetMemberImportName(path, name string) error {
	d, err := ed.decoded()
	if err != nil {
		return err
	}
	i, err := ed.memberIndex(d, path)
	if err != nil {
		return err
	}
	key := fmt.Sprintf("members[%d].import_name", i)
	if err := ed.apply(func(doc *tomledit.Document) error {
		if name == "" {
			return doc.Delete(key)
		}
		return doc.Set(key, name)
	}); err != nil {
		return err
	}
	want := clone(d)
	want.Members[i].ImportName = name
	return ed.verify("setting a member's import_name", want)
}
