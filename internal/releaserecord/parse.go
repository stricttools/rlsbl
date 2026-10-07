package releaserecord

import "github.com/stricttools/rlsbl/internal/semver"

// ParseArchive reads the archive of v from its text, as ReadArchive reads it
// from rel: for a writer that composes an archive before it exists on disk
// (the record migration) and must know the reader accepts it.
func ParseArchive(rel string, v semver.Version, data []byte) (Archive, error) {
	return parseArchive(rel, v, data)
}
