version: 2

# The archives are named <binary>_<version>_<os>_<arch>.tar.gz, the names the
# npm and PyPI packaging jobs download, for every platform of rlsbl's
# platform table.
project_name: {{binaryName}}

builds:
  - main: {{goreleaserMain}}
    binary: {{binaryName}}
    env:
      - CGO_ENABLED=0
    ldflags:
      - -s -w -X main.Version={{.Version}}
    goos:
{{goreleaserGoos}}
    goarch:
{{goreleaserGoarch}}

archives:
  - format: tar.gz
    name_template: "{{ .ProjectName }}_{{ .Version }}_{{ .Os }}_{{ .Arch }}"

checksum:
  name_template: checksums.txt

changelog:
  use: github-native
{{brewsSection}}
