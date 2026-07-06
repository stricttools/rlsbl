[package]
name = "{{binCommand}}"
version = "0.0.0"
edition = "2021"
description = "{{binCommand}} -- distributed via crates.io (binary wrapper)"
license = "MIT"
repository = "https://github.com/{{repoName}}"

[[bin]]
name = "{{binCommand}}"
path = "src/main.rs"

[package.metadata.binstall]
pkg-url = "{ repo }/releases/download/v{ version }/{{binCommand}}_{ version }_{ target }{ archive-suffix }"
bin-dir = "{{binCommand}}{ binary-ext }"
pkg-fmt = "tgz"

[package.metadata.binstall.overrides.x86_64-pc-windows-msvc]
pkg-fmt = "zip"

[package.metadata.binstall.overrides.aarch64-pc-windows-msvc]
pkg-fmt = "zip"
