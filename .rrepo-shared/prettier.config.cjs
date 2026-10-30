// Roost shared Prettier baseline (carried forward from app.roost.cc's .prettierrc.json).
// Consumed by the `prettier` hook via bin/run-prettier.js, which passes this file
// with an explicit --config flag so it always wins over consumer-repo config discovery.
// Consumers exclude paths with pre-commit `exclude:` or a repo-local .prettierignore.
module.exports = {
  semi: true,
  trailingComma: 'all',
  singleQuote: true,
  printWidth: 120,
  tabWidth: 2,
  useTabs: false,
  endOfLine: 'lf',
  arrowParens: 'always',
  bracketSpacing: true,
  proseWrap: 'preserve',
};
