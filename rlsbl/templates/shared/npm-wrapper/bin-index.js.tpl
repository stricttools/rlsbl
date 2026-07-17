#!/usr/bin/env node

const { platform, arch } = process;
const { spawnSync } = require("child_process");
const path = require("path");

const PLATFORMS = {
  "linux x64": "{{binCommand}}-linux-x64",
  "linux arm64": "{{binCommand}}-linux-arm64",
  "darwin x64": "{{binCommand}}-darwin-x64",
  "darwin arm64": "{{binCommand}}-darwin-arm64",
  "win32 x64": "{{binCommand}}-win32-x64",
  "win32 arm64": "{{binCommand}}-win32-arm64",
};

const key = `${platform} ${arch}`;
const pkg = PLATFORMS[key];
if (!pkg) {
  console.error(`Unsupported platform: ${key}`);
  process.exit(1);
}

const binName = platform === "win32" ? "{{binCommand}}.exe" : "{{binCommand}}";
let binPath;
try {
  binPath = path.join(path.dirname(require.resolve(`${pkg}/package.json`)), binName);
} catch (e) {
  console.error(`Could not find binary package ${pkg}. Did npm install fail?`);
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: "inherit" });
process.exit(result.status ?? 1);
