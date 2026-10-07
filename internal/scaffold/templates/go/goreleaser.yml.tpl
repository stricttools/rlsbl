version: 2

builds:
  - main: {{goreleaserMain}}
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
