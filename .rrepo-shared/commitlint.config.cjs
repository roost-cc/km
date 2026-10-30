// Roost shared commit-message rules: Conventional Commits.
// Consumed by the `commitlint` hook via bin/run-commitlint.js, which passes this
// file with an explicit --config flag.
//
// Accepted types (from @commitlint/config-conventional):
//   build, chore, ci, docs, feat, fix, perf, refactor, revert, style, test
module.exports = {
  extends: ['@commitlint/config-conventional'],
};
