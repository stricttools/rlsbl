# _is_library misdetects cmd-layout Go projects

## Problem

`GoTarget._is_library()` only checks for `package main` in root-level `.go` files. Projects with `cmd/*/main.go` (standard Go binary layout) are misdetected as libraries. This causes scaffold to skip `publish.yml` and `goreleaser.yml` templates.

## Affected code

`_is_library` in `targets/go.py`. The method `_has_cmd_main` exists and correctly detects cmd-layout but `_is_library` doesn't call it.

## Fix

`_is_library` should return `False` if `_has_cmd_main` returns `True`.
