#!/usr/bin/env node
// shared-config `commitlint` hook: validate the commit message (commit-msg stage)
// against the shared Conventional Commits config. pre-commit passes the commit
// message file as the only positional argument.
'use strict';

const path = require('path');
const { spawnSync } = require('child_process');

const packageRoot = path.join(__dirname, '..');
const configPath = path.join(packageRoot, 'commitlint.config.js');

let commitlintCli;
try {
  // The package's main entry is the API re-export; spawn its declared bin instead.
  const pkgJsonPath = require.resolve('@commitlint/cli/package.json', { paths: [packageRoot] });
  const pkg = require(pkgJsonPath);
  const binRel = typeof pkg.bin === 'string' ? pkg.bin : pkg.bin.commitlint;
  commitlintCli = path.join(path.dirname(pkgJsonPath), binRel);
} catch (err) {
  console.error('shared-config/commitlint: could not resolve the bundled @commitlint/cli dependency.');
  console.error(String((err && err.message) || err));
  process.exit(1);
}

const result = spawnSync(
  process.execPath,
  [commitlintCli, '--config', configPath, '--edit', ...process.argv.slice(2)],
  { stdio: 'inherit' },
);
process.exit(result.status === null ? 1 : result.status);
