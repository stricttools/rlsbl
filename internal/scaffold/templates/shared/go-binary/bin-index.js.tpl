#!/usr/bin/env node
// Written by rlsbl scaffold. Runs the {{binaryName}} binary from the platform
// package npm installed for this machine through optionalDependencies.

const { platform, arch } = process;
const { spawnSync } = require("child_process");
const path = require("path");

const PLATFORMS = {
{{platformPackages}}
};

const key = `${platform} ${arch}`;
const pkg = PLATFORMS[key];
if (!pkg) {
  console.error(`Unsupported platform: ${key}`);
  process.exit(1);
}

let binPath;
try {
  binPath = path.join(path.dirname(require.resolve(`${pkg}/package.json`)), "bin", "{{binaryName}}");
} catch (e) {
  console.error(`Could not find binary package ${pkg}. Did npm install fail?`);
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: "inherit" });
process.exit(result.status ?? 1);
