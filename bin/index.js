#!/usr/bin/env node
// Written by rlsbl scaffold. Runs the rlsbl binary from the platform
// package npm installed for this machine through optionalDependencies.

const { platform, arch } = process;
const { spawnSync } = require("child_process");
const path = require("path");

const PLATFORMS = {
  "linux x64": "rlsbl-linux-x64",
  "linux arm64": "rlsbl-linux-arm64",
  "darwin x64": "rlsbl-darwin-x64",
  "darwin arm64": "rlsbl-darwin-arm64",
};

const key = `${platform} ${arch}`;
const pkg = PLATFORMS[key];
if (!pkg) {
  console.error(`Unsupported platform: ${key}`);
  process.exit(1);
}

let binPath;
try {
  binPath = path.join(path.dirname(require.resolve(`${pkg}/package.json`)), "rlsbl");
} catch (e) {
  console.error(`Could not find binary package ${pkg}. Did npm install fail?`);
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: "inherit" });
process.exit(result.status ?? 1);
