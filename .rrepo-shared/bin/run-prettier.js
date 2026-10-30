#!/usr/bin/env node
// shared-config `prettier` hook: format web-stack files with the shared Roost baseline.
// Installed by pre-commit (language: node) together with the pinned prettier
// dependency from package.json, so no consumer-side install is needed.
'use strict';

const path = require('path');
const { spawnSync } = require('child_process');

const packageRoot = path.join(__dirname, '..');
const configPath = path.join(packageRoot, 'prettier.config.js');

let prettierCli;
try {
  // prettier's main entry sits at its package root; the CLI lives next to it.
  const prettierEntry = require.resolve('prettier', { paths: [packageRoot] });
  prettierCli = path.join(path.dirname(prettierEntry), 'bin', 'prettier.cjs');
} catch (err) {
  console.error('shared-config/prettier: could not resolve the bundled prettier dependency.');
  console.error(String((err && err.message) || err));
  process.exit(1);
}

const result = spawnSync(
  process.execPath,
  [prettierCli, '--config', configPath, '--ignore-unknown', '--write', ...process.argv.slice(2)],
  { stdio: 'inherit' },
);
process.exit(result.status === null ? 1 : result.status);
