package pipelines

import (
	"regexp"
	"strings"
)

// Platform is one platform a go binary pipeline's binaries are built for and
// packaged for, on npm as a platform package (selected through the main
// package's optionalDependencies by os and cpu, with no install script) and
// on PyPI as a binary wheel. This table is the one platform set for both
// registries: there is no win32 platform.
type Platform struct {
	// Name is npm's platform spelling, the suffix of the platform package's
	// name.
	Name string
	// OS and CPU are the platform package's npm os and cpu fields.
	OS  string
	CPU string
	// GOOS and GOARCH build the binary.
	GOOS   string
	GOARCH string
	// WheelTag is the PyPI wheel's platform tag.
	WheelTag string
}

// platforms is the platform table.
var platforms = []Platform{
	{Name: "linux-x64", OS: "linux", CPU: "x64", GOOS: "linux", GOARCH: "amd64", WheelTag: "manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64"},
	{Name: "linux-arm64", OS: "linux", CPU: "arm64", GOOS: "linux", GOARCH: "arm64", WheelTag: "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64"},
	{Name: "darwin-x64", OS: "darwin", CPU: "x64", GOOS: "darwin", GOARCH: "amd64", WheelTag: "macosx_10_12_x86_64"},
	{Name: "darwin-arm64", OS: "darwin", CPU: "arm64", GOOS: "darwin", GOARCH: "arm64", WheelTag: "macosx_11_0_arm64"},
}

// Platforms are the platforms go binaries are packaged for, in table order.
func Platforms() []Platform {
	out := make([]Platform, len(platforms))
	copy(out, platforms)
	return out
}

// PlatformPackageName is the npm platform package carrying the main
// package's binary for p: <main package>-<os>-<cpu>.
func PlatformPackageName(mainPackage string, p Platform) string {
	return mainPackage + "-" + p.Name
}

// ArchiveName is the release archive goreleaser builds for p, the one a
// platform package and a wheel take the binary from:
// <binary>_<version>_<goos>_<goarch>.tar.gz.
func ArchiveName(binary, version string, p Platform) string {
	return binary + "_" + version + "_" + p.GOOS + "_" + p.GOARCH + ".tar.gz"
}

// wheelNameRun is a run of the characters a wheel's distribution name folds
// into one underscore.
var wheelNameRun = regexp.MustCompile(`[-_.]+`)

// WheelDistribution is a distribution name as a wheel's file name and its
// .data directory spell it: lowercased, each run of '-', '_', and '.' one
// underscore.
func WheelDistribution(name string) string {
	return wheelNameRun.ReplaceAllString(strings.ToLower(name), "_")
}

// WheelName is the binary wheel of distribution at version for p:
// <distribution>-<version>-py3-none-<platform tag>.whl.
func WheelName(distribution, version string, p Platform) string {
	return WheelDistribution(distribution) + "-" + version + "-py3-none-" + p.WheelTag + ".whl"
}
