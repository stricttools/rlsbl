#!/usr/bin/env node
"use strict";

const { execFileSync, spawnSync } = require("child_process");

// 6c: Windows compatibility -- try python3, python, py in order
function findPython() {
  for (const cmd of ["python3", "python", "py"]) {
    try {
      const output = execFileSync(cmd, ["--version"], {
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
      });
      return { cmd, version: output.trim() };
    } catch {
      continue;
    }
  }
  return null;
}

const python = findPython();
if (!python) {
  console.error("rlsbl requires Python 3.11+. Install from https://python.org/");
  process.exit(1);
}

// 6a: Python version check
const match = python.version.match(/Python (\d+)\.(\d+)/);
if (
  !match ||
  parseInt(match[1]) < 3 ||
  (parseInt(match[1]) === 3 && parseInt(match[2]) < 11)
) {
  console.error(
    `rlsbl requires Python 3.11+, but found ${python.version}.`
  );
  console.error("Install or upgrade: https://python.org/");
  process.exit(1);
}

// 6b: rlsbl installation check
try {
  execFileSync(
    python.cmd,
    [
      "-c",
      "import importlib.util; exit(0 if importlib.util.find_spec('rlsbl') else 1)",
    ],
    { stdio: "pipe" }
  );
} catch {
  console.error("rlsbl Python package is not installed.");
  console.error("Install it: pip install rlsbl");
  process.exit(1);
}

const result = spawnSync(
  python.cmd,
  ["-m", "rlsbl", ...process.argv.slice(2)],
  { stdio: "inherit" }
);
process.exit(result.status ?? 1);
