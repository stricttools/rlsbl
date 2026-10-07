#!/usr/bin/env bash
# Regenerates .strictmetadata/.cli-schema/schema.json, rlsbl's committed help
# document, from this checkout: builds rlsbl into experiments/ and writes its
# `help --json` there. A release writes the same file with the version it
# releases; TestTheCommittedHelpDocumentIsTheApplicationsHelp keeps it fresh.
set -euo pipefail
cd "$(dirname "$0")/.."
go build -o experiments/rlsbl ./cmd/rlsbl
experiments/rlsbl help --json > .strictmetadata/.cli-schema/schema.json
