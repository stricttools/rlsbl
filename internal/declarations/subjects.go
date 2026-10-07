package declarations

// SubjectNames are every releasable's and every member's name, in
// declaration order: the subjects the lifecycle-and-license record may hold
// open entries for.
func (d *Releasables) SubjectNames() []string {
	names := make([]string, 0, len(d.Releasables)+len(d.Members))
	for _, r := range d.Releasables {
		names = append(names, r.Name)
	}
	for _, m := range d.Members {
		names = append(names, m.Name)
	}
	return names
}
