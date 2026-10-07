package scaffold

// PrivateModuleStub is the text of the go.mod that keeps a private directory
// (.strictmetadata/ among them) out of a Go module, as scaffold writes it.
func PrivateModuleStub() (string, error) {
	return renderTemplate("shared/private/go.mod.tpl", Vars{})
}
