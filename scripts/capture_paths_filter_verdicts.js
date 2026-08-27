#!/usr/bin/env node
//
// Capture how dorny/paths-filter really answers, and write the answers down.
//
// rlsbl generates the monorepo CI router, and the release flow simulates that
// router's paths filter locally (rlsbl/router_filters.py) to refuse a release
// candidate whose push window could only produce skipped jobs. The simulation
// is only worth anything if it agrees with the action. This script asks the
// ACTUAL action -- its own src/filter.ts at the version rlsbl pins, compiled
// with its own TypeScript and matched by its own picomatch -- and writes the
// verdicts to tests/data/paths_filter_verdicts.json, which the Python test
// suite replays against the Python matcher.
//
// Nothing here is hand-written except the corpus. Every true/false in the
// fixture comes out of the action's code.
//
// Usage:
//   node scripts/capture_paths_filter_verdicts.js
//   node scripts/capture_paths_filter_verdicts.js --out some/other.json
//
// Requires network (npm + codeload.github.com) and a node with npm on PATH.
// Everything it downloads lands in a throwaway directory outside the repo.

'use strict'

const fs = require('fs')
const os = require('os')
const path = require('path')
const {execFileSync} = require('child_process')
const crypto = require('crypto')

const REPO_ROOT = path.resolve(__dirname, '..')
const ACTION = 'dorny/paths-filter'
const DEFAULT_OUT = path.join(REPO_ROOT, 'tests', 'data', 'paths_filter_verdicts.json')

// The quantifier the generated router declares. Keep in step with
// rlsbl.router_filters.PREDICATE_QUANTIFIER.
const QUANTIFIER = 'some-with-excludes'

// ---------------------------------------------------------------------------
// The corpus
// ---------------------------------------------------------------------------
//
// Pattern sets are the shapes rlsbl's derivation emits (a member territory, a
// member plus its dependency territories, the root member's match-everything
// narrowed by excludes) plus the negation cases whose answer cannot be guessed
// from the README: ordering, exclusion finality, and a filter of excludes only.

const CORPUS = [
  {
    name: 'member territory and tool machinery',
    patterns: ['packages/core/**', '.github/workflows/ci-router.yml'],
    paths: [
      'packages/core/src/index.ts',
      'packages/core/package.json',
      'packages/core',
      'packages/coreutils/index.ts',
      'packages/other/index.ts',
      '.github/workflows/ci-router.yml',
      '.github/workflows/publish.yml',
      'README.md'
    ]
  },
  {
    name: 'member territory plus dependency territories',
    patterns: [
      'apps/web/**',
      'packages/core/**',
      'packages/util/**',
      'package.json',
      'uv.lock',
      '.github/workflows/ci-router.yml'
    ],
    paths: [
      'apps/web/src/main.ts',
      'packages/core/src/index.ts',
      'packages/util/util.ts',
      'packages/unrelated/x.ts',
      'package.json',
      'uv.lock',
      'apps/api/main.go'
    ]
  },
  {
    name: 'root member: everything minus the other territories',
    patterns: ['**', '!packages/core/**', '!apps/web/**'],
    paths: [
      'README.md',
      'pyproject.toml',
      'docs/guide.md',
      'packages/core/src/index.ts',
      'packages/core',
      'apps/web/main.ts',
      'apps/api/main.go',
      '.rlsbl-monorepo/releasables/alpha/CHANGELOG.md',
      '.github/workflows/ci-router.yml'
    ]
  },
  {
    name: 'negation ordering: exclude declared before the include',
    patterns: ['!packages/core/**', '**'],
    paths: [
      'README.md',
      'packages/core/src/index.ts',
      'packages/other/x.ts'
    ]
  },
  {
    name: 'exclusion is final: an explicit include cannot win it back',
    patterns: ['**', 'packages/core/**', '!packages/core/**'],
    paths: [
      'README.md',
      'packages/core/src/index.ts'
    ]
  },
  {
    name: 'excludes only: a filter with no include matches nothing',
    patterns: ['!packages/core/**'],
    paths: [
      'README.md',
      'packages/core/src/index.ts'
    ]
  },
  {
    name: 'exact paths, including dotted directories',
    patterns: [
      '.rlsbl-monorepo/releasables/alpha/CHANGELOG.md',
      'go.mod',
      'go.sum'
    ],
    paths: [
      '.rlsbl-monorepo/releasables/alpha/CHANGELOG.md',
      '.rlsbl-monorepo/releasables/beta/CHANGELOG.md',
      '.rlsbl-monorepo/releasables/alpha/changes/unreleased.jsonl',
      'go.mod',
      'go.sum',
      'nested/go.mod'
    ]
  },
  {
    name: 'interior globstar and single-segment star',
    patterns: ['shared/**/*.proto', 'tools/*.sh'],
    paths: [
      'shared/a.proto',
      'shared/proto/a.proto',
      'shared/proto/deep/a.proto',
      'shared/proto/a.txt',
      'tools/build.sh',
      'tools/nested/build.sh'
    ]
  },
  {
    name: 'a nested member outranks nothing: both territories are literal',
    patterns: ['pkg/**'],
    paths: ['pkg/inner/a.py', 'pkg/a.py', 'pkgx/a.py', 'pkg']
  }
]

// ---------------------------------------------------------------------------
// Fetching and compiling the real thing
// ---------------------------------------------------------------------------

function run(cmd, args, opts) {
  return execFileSync(cmd, args, {stdio: ['ignore', 'pipe', 'inherit'], timeout: 600000, ...opts})
}

function pinnedVersion() {
  const table = fs.readFileSync(
    path.join(REPO_ROOT, 'rlsbl', 'data', 'action_versions.toml'),
    'utf8'
  )
  const line = new RegExp(`^"${ACTION}"\\s*=\\s*"([^"]+)"`, 'm').exec(table)
  if (!line) {
    throw new Error(`${ACTION} is not pinned in rlsbl/data/action_versions.toml`)
  }
  return line[1]
}

function resolveTagCommit(version) {
  // ls-remote needs no credentials and no API quota.
  const out = run('git', [
    'ls-remote',
    `https://github.com/${ACTION}.git`,
    `refs/tags/${version}`
  ]).toString()
  const first = out.split('\n').find(l => l.trim())
  return first ? first.split(/\s+/)[0] : null
}

function main() {
  const argv = process.argv.slice(2)
  const outIdx = argv.indexOf('--out')
  const outPath = outIdx === -1 ? DEFAULT_OUT : path.resolve(argv[outIdx + 1])

  const version = pinnedVersion()
  const commit = resolveTagCommit(version)
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'rlsbl-paths-filter-'))
  console.error(`work dir: ${work}`)

  const tarball = path.join(work, 'action.tar.gz')
  run('curl', [
    '-sSL',
    '--max-time', '120',
    `https://codeload.github.com/${ACTION}/tar.gz/refs/tags/${version}`,
    '-o', tarball
  ])
  const tarballSha = crypto
    .createHash('sha256')
    .update(fs.readFileSync(tarball))
    .digest('hex')
  run('tar', ['xzf', tarball, '-C', work])

  const srcDir = fs
    .readdirSync(work)
    .map(n => path.join(work, n))
    .find(p => fs.existsSync(path.join(p, 'src', 'filter.ts')))
  if (!srcDir) {
    throw new Error('downloaded action has no src/filter.ts')
  }

  // Build with the action's own declared dependency versions -- picomatch is
  // the matcher whose behavior is being recorded, so it must not float.
  const actionPkg = JSON.parse(
    fs.readFileSync(path.join(srcDir, 'package.json'), 'utf8')
  )
  const picomatchSpec = actionPkg.dependencies.picomatch
  const jsyamlSpec = actionPkg.devDependencies['js-yaml']
  const tsSpec = actionPkg.devDependencies.typescript

  const build = path.join(work, 'build')
  fs.mkdirSync(build)
  fs.writeFileSync(
    path.join(build, 'package.json'),
    JSON.stringify({name: 'rlsbl-paths-filter-probe', private: true, type: 'commonjs'})
  )
  for (const f of ['filter.ts', 'file.ts']) {
    fs.copyFileSync(path.join(srcDir, 'src', f), path.join(build, f))
  }
  run('npm', [
    'install', '--no-audit', '--no-fund', '--loglevel', 'error',
    `picomatch@${picomatchSpec}`,
    `js-yaml@${jsyamlSpec}`,
    `typescript@${tsSpec}`
  ], {cwd: build})
  run('npx', [
    'tsc', 'filter.ts', 'file.ts',
    '--module', 'commonjs', '--target', 'es2020',
    '--esModuleInterop', '--skipLibCheck',
    '--outDir', 'out'
  ], {cwd: build})

  const {Filter} = require(path.join(build, 'out', 'filter.js'))
  const installed = JSON.parse(
    fs.readFileSync(path.join(build, 'node_modules', 'picomatch', 'package.json'), 'utf8')
  ).version

  const verdictsFor = (patterns, paths, quantifier) => {
    // Go through the action's own YAML loader so the fixture exercises the
    // same parse path a workflow does.
    const yaml =
      'probe:\n' + patterns.map(p => `  - ${JSON.stringify(p)}`).join('\n') + '\n'
    const filter = new Filter(yaml, quantifier ? {predicateQuantifier: quantifier} : undefined)
    const files = paths.map(filename => ({filename, status: 'modified'}))
    const matched = new Set(filter.match(files).probe.map(f => f.filename))
    const out = {}
    for (const p of paths) {
      out[p] = matched.has(p)
    }
    return out
  }

  const cases = CORPUS.map(entry => ({
    name: entry.name,
    patterns: entry.patterns,
    has_negation: entry.patterns.some(p => p.startsWith('!')),
    verdicts: verdictsFor(entry.patterns, entry.paths, QUANTIFIER),
    verdicts_default_quantifier: verdictsFor(entry.patterns, entry.paths, undefined)
  }))

  const doc = {
    _comment:
      'Generated by scripts/capture_paths_filter_verdicts.js -- do not hand-edit. ' +
      'Every verdict was produced by running the pinned dorny/paths-filter ' +
      "action's own src/filter.ts (compiled with its own TypeScript, matched " +
      'by its own picomatch) over the corpus in that script.',
    action: ACTION,
    action_version: version,
    action_tag_commit: commit,
    action_tarball_sha256: tarballSha,
    picomatch_version: installed,
    predicate_quantifier: QUANTIFIER,
    generator: 'scripts/capture_paths_filter_verdicts.js',
    cases
  }

  fs.mkdirSync(path.dirname(outPath), {recursive: true})
  fs.writeFileSync(outPath, JSON.stringify(doc, null, 2) + '\n')
  console.error(`wrote ${outPath} (${cases.length} cases, ${ACTION}@${version})`)
}

main()
