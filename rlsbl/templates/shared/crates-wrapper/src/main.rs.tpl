//! Thin exec wrapper that runs the prebuilt binary downloaded by build.rs.
//!
//! When installed via `cargo-binstall`, the binary is placed directly by
//! binstall and this wrapper is not used. When installed via `cargo install`,
//! build.rs downloads the binary and this wrapper execs it.

use std::env;
use std::path::PathBuf;
use std::process::{self, Command};

const BIN_NAME: &str = "{{binCommand}}";

fn main() {
    // The binary was downloaded by build.rs into OUT_DIR during compilation.
    // At install time, the binary is co-located with this wrapper.
    let self_path = env::current_exe().expect("cannot determine own path");
    let bin_dir = self_path.parent().expect("cannot determine bin directory");

    // Try the same directory first (cargo install places binaries together)
    let bin_path = bin_dir.join(format!("{BIN_NAME}-bin"));

    // Fall back to the OUT_DIR path embedded at compile time
    let bin_path = if bin_path.exists() {
        bin_path
    } else {
        PathBuf::from(env!("OUT_DIR")).join(BIN_NAME)
    };

    if !bin_path.exists() {
        eprintln!(
            "Error: could not find {BIN_NAME} binary. \
             Try reinstalling with `cargo install {BIN_NAME}` or \
             `cargo binstall {BIN_NAME}`."
        );
        process::exit(1);
    }

    let args: Vec<String> = env::args().skip(1).collect();

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // exec replaces this process entirely
        let err = Command::new(&bin_path).args(&args).exec();
        eprintln!("Failed to exec {}: {err}", bin_path.display());
        process::exit(1);
    }

    #[cfg(not(unix))]
    {
        let status = Command::new(&bin_path)
            .args(&args)
            .status()
            .unwrap_or_else(|e| {
                eprintln!("Failed to run {}: {e}", bin_path.display());
                process::exit(1);
            });
        process::exit(status.code().unwrap_or(1));
    }
}
