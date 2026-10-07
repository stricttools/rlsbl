package cli

const releaseGroupHelp = "Make releases of a releasable and act on the releases it has made."

// registerReleaseGroup declares the release group; each file registering
// release commands adds them to it.
func registerReleaseGroup(r *commandSet) {
	r.group([]string{"release"}, releaseGroupHelp)
}
