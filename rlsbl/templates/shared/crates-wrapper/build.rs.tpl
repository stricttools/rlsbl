//! Build script that downloads the prebuilt binary from GitHub Releases.
//!
//! When `cargo install` is used (no cargo-binstall), this script fetches
//! the correct platform binary at compile time so the main.rs wrapper
//! can exec it.

use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

const REPO: &str = "{{repoName}}";
const BIN_NAME: &str = "{{binCommand}}";

fn main() {
    let version = env!("CARGO_PKG_VERSION");
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    let bin_path = out_dir.join(BIN_NAME);

    // Skip download if the binary already exists (incremental builds)
    if bin_path.exists() {
        println!("cargo:rerun-if-changed=build.rs");
        return;
    }

    let (target_suffix, archive_ext) = match env::var("TARGET").unwrap().as_str() {
        "x86_64-unknown-linux-gnu" | "x86_64-unknown-linux-musl" => ("linux_amd64", "tar.gz"),
        "aarch64-unknown-linux-gnu" | "aarch64-unknown-linux-musl" => ("linux_arm64", "tar.gz"),
        "x86_64-apple-darwin" => ("darwin_amd64", "tar.gz"),
        "aarch64-apple-darwin" => ("darwin_arm64", "tar.gz"),
        "x86_64-pc-windows-msvc" | "x86_64-pc-windows-gnu" => ("windows_amd64", "zip"),
        "aarch64-pc-windows-msvc" => ("windows_arm64", "zip"),
        target => panic!("Unsupported target: {target}"),
    };

    let archive_name = format!("{BIN_NAME}_{version}_{target_suffix}.{archive_ext}");
    let url = format!(
        "https://github.com/{REPO}/releases/download/v{version}/{archive_name}"
    );
    let archive_path = out_dir.join(&archive_name);

    // Download the archive
    let status = Command::new("curl")
        .args(["-sSfL", "-o"])
        .arg(&archive_path)
        .arg(&url)
        .status()
        .expect("failed to run curl");
    assert!(status.success(), "Failed to download {url}");

    // Extract the binary
    if archive_ext == "tar.gz" {
        let status = Command::new("tar")
            .args(["xzf"])
            .arg(&archive_path)
            .arg("-C")
            .arg(&out_dir)
            .arg(BIN_NAME)
            .status()
            .expect("failed to run tar");
        assert!(status.success(), "Failed to extract {archive_name}");
    } else {
        let status = Command::new("unzip")
            .args(["-o"])
            .arg(&archive_path)
            .arg(BIN_NAME)
            .arg("-d")
            .arg(&out_dir)
            .status()
            .expect("failed to run unzip");
        assert!(status.success(), "Failed to extract {archive_name}");
    }

    // Clean up the archive
    let _ = fs::remove_file(&archive_path);

    // Make the binary executable on Unix
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = fs::metadata(&bin_path).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&bin_path, perms).unwrap();
    }

    println!("cargo:rerun-if-changed=build.rs");
}
