# key-management

The `km` CLI — a project-driven, plugin-extensible key-management tool.
Manifests under `projects/<name>/manifests/` declare the secrets each
project owns; `km` mints fresh values via a registered **producer** and
distributes them via one or more registered **publishers** (Infisical,
env-file rendering, …). km itself ships a handful of built-in producers
and publishers; project-specific implementations live under each
project and are loaded by name at dispatch time.

## Install

```sh
cd km
uv sync            # the package and its test tools (the `dev` dependency group) into .venv/
uv run km --help
uv run pytest -q -m 'not integration'
```

`uv sync` installs the `dev` dependency group by default, so `uv run pytest` is the
venv's pytest, not whatever is on `PATH`. The `.venv/` directory is gitignored.

## Commands

km resolves the active project by walking up until a directory containing
`config.yaml` is found. It starts from `-C/--dir` when given, else from
`$KM_PROJECT_DIR` when set and non-empty, else from the cwd.

| Command                                                                       | Purpose                                                                                                                      |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `km publish <target> [<env>…] (--all \| --only <slug>[:<name>]…) [--execute]` | Write all or the selected manifest secrets to the target's destinations (dry run by default). See [Publishing](#publishing). |
| `km validate <target> [<env>…] [--json] [--verbose] [--schema-only]`          | Verify the target is in a valid state (schema + freshness + publication). See [Validity](#validity) below.                   |
| `km rotate [<slug>] [--force <group>]`                                        | Rotate stale or forced groups of every manifest, or of the one named by `<slug>`.                                            |
| `km mfa <slug> [--no-copy]`                                                   | Print the current TOTP of manifest `<slug>`'s login and copy it to the clipboard. See [Logins](#logins).                     |
| `km login <slug> [--no-open]`                                                 | Open manifest `<slug>`'s login `url`, then copy username, password or MFA token on keypress.                                 |

`km login` and `km mfa` take the slug of any manifest with a `login:` block (inline or
`from`) and read its effective login. An unknown slug, or a manifest without `login:`, is an
error listing the slugs that have one (for `km mfa`, those whose login has an `mfa`).
`km login` shows a one-key menu: `u` copies `id`, `p` copies `password`, `t` copies a fresh TOTP
from `mfa`, and `x`, `q`, Esc, Ctrl-C or Ctrl-D exit. Only the keys the login can use are
listed, and no value is ever printed. Running the command hands km the clipboard: each key
overwrites it, and km always clears it when the process exits. km never reads the clipboard.
Clearing does not stop a clipboard-history manager from keeping the value.

## Projects

A "project" is any directory containing a `config.yaml`. The directory
and everything under it belong to that project.

A manifest is `manifests/<slug>.yaml` (default subdir: `manifests/`),
directly in that directory. Its **slug** is the file stem, and every
command names a manifest by it: `km rotate <slug>`, `km publish --only
<slug>`, the keys of `targets/<target>.yaml` and a target's
`manifests:` list. `km validate` fails on any `*.yaml` below a
subdirectory of `manifests/`, naming each file (`_template.yaml` is
ignored everywhere).

`config.yaml` is per-project. It carries two axes: **environments**
(the application's deployment environments, declared once) and
**targets** (named mappings of the manifests the target publishes, by
slug, and an ordered list of destinations, each naming a publisher, its
configuration and the environments it accepts):

```yaml
# Project-relative directories — defaults applied when omitted.
directories:
  manifests: manifests
  producers: producers
  publishers: publishers
  runbooks: runbooks
  templates: templates # ${var} templates consumed by template_file live here
  targets: targets # per-target values: targets/<target>.yaml

environments: [dev, test]

targets:
  local:
    manifests: [infisical, jwt, postgresql, synapse]
    destinations:
      - publisher: template_file
        envs: [dev]
        root: $HOME/secrets # rendered files land here, outside every repo
      - publisher: infisical
        host: http://localhost:8888
        envs: [dev, test]
        admin_email: admin@example.com # ensure: bootstrap an empty instance
        org_name: Example Org
        project_name: example-app # ensure: create the project
        values: # ensure: also record these as per-target manifest values
          project_id: infisical:secret_manager_project
          services_token: infisical:secret_manager_token
      - publisher: postgresql
        envs: [dev]
        dev: { host: localhost, port: 5432, database: postgres, user: postgres, password: postgres }
      - publisher: yaml_file # after template_file, which renders its files
        envs: [dev]
        files:
          homeserver: $HOME/secrets/synapse/homeserver.{env}.yaml
          appservice: $HOME/secrets/synapse/app-bot.{env}.yaml
        apply: [docker, compose, -f, $HOME/stack/docker-compose.yml, restart, synapse]
  staging:
    manifests: [infisical, jwt, postgresql, synapse] # the same manifests, no copies
    destinations:
      - publisher: template_file # remote: written through the transport, nothing locally
        envs: [dev]
        transport: [ssh, deploy@staging.example.com]
        remote_dir: /home/deploy/secrets
        apply: [ssh, deploy@staging.example.com, systemctl, --user, restart, app-stack]
      # Producer configuration, not a destination (most producers need none).
      - producer: aws_iam
        aws_profile: example-admin
```

A target publishes and validates only the manifests its `manifests:`
lists, so a key one target needs (a CI App key) never lands on another.
The list is required and non-empty, and a slug that names no manifest
is a load error listing the slugs. A manifest is never copied per
target: targets that share manifests list the same slugs, and what
differs per instance lives in [`targets/<target>.yaml`](#per-target-values).

Every destination carries its whole configuration: there are no shared
defaults to inherit. Destinations run in the order listed (the files
that let a store start come before the store). Each publisher maps an
environment to its store's notion of one (an Infisical environment slug,
`{env}` in a rendered file name). Manifests stay destination-independent:
their `publishers:` block says where inside a store a secret goes per
environment, never which store.

`directories:` has no `files` key: km writes nothing rendered into the
project. Each `template_file` destination names where its outputs go
(`root:`, or `transport:` and `remote_dir:`), and a destination without
one is a load error (exit 2); a config that still sets
`directories.files` (or the retired `directories.accounts`) fails to
load naming it.

The old top-level `publishers:` and `producers:` sections are a load
error naming the new keys, and so is a target that is still a bare list
of destinations (the message names the `{manifests, destinations}`
shape; its `producer:` entries move into `destinations` unchanged). See
`config.example.yaml` for the full
annotated shape. String values support `$VAR` / `${VAR}` env-var
expansion (an undefined variable is an error, not a silent empty
string). Each target expands on its own: a target that references an
unset variable fails, naming it, only when a command names that target,
so `km publish local` needs none of the variables another target uses.

### Per-target values

A few secrets exist per secret-manager instance and can never be shared
(an Infisical project id, the token the services read with, km's own
publishing token). They live in `targets/<target>.yaml`, git-crypted
like the manifests:

```yaml
# targets/local.yaml
infisical: # manifest slug (manifests/infisical.yaml)
  secret_manager_token: eyJ…
  secret_manager_project: dd26…
_publishers: # what km itself needs to reach the instance
  infisical:
    project_id: dd26…
    token: eyJ…
```

A per-target value is still declared in its manifest (name, empty
value, `static` producer); km overlays `<slug>: {<secret>: <value>}`
onto that manifest's `secrets:` before publish and validate, and never
mints an overlaid name. A value whose slug or secret no manifest
declares, or whose slug names a manifest the target does not list, is a
schema error in validate and stops publish. The infisical publisher takes its project id and
token from `_publishers.infisical`, not from `config.yaml`, so a target
may list the infisical publisher once: a second one is a load error in
publish and validate.

Nobody writes `targets/<target>.yaml` by hand: the infisical publisher's
`ensure` writes it. On an instance with no admin it bootstraps one
(`admin_email`, `org_name`, a minted password) and records
`_publishers.infisical.admin_password`, `admin_token` (the bootstrap
identity, which creates projects and identities) and `org_id`. With no
`project_id` it creates the project (`project_name`), then the `km`
identity (project `admin`; its token is `_publishers.infisical.token`)
and the `services` identity (project `viewer`; its token goes where
`values.services_token` says, else `_publishers.infisical.services_token`),
both with Token Auth. Each credential is written the moment the
instance issues it. A file that already holds the project id and both
tokens makes ensure create nothing and leaves the file byte-identical.
An instance that already has an admin and no target file cannot be
ensured: km holds no credential for it, and publish fails naming the
target and the file.

## Manifest schema

Two shapes. Most files use the **rotation-bearing** shape — every
group declares a `producer:` that mints (or no-ops on, or paste-back-
collects) its values. A **login-only** manifest holds one login (a
console or vendor account the operator signs into) and nothing a
target publishes.

### Rotation-bearing shape

```yaml
# Optional human-facing label.
service: google

# Optional: the login used to rotate this credential (see Logins below).
# Either inline (any of url, id, mfa) or `from: <slug>` alone.
login:
  from: google-user

# Optional: anything else about the account (backup codes, SSO
# settings, hints). km never reads it.
notes:
  project_id: example-org

# Required: actual secret values. Producers write fresh values here on
# rotation; `km publish <target>` writes them into each destination.
secrets:
  client_id: '…'
  client_secret: '…'

# Required: per-publisher addresses. Each section is interpreted by
# its publisher implementation (the publisher table below lists every
# built-in). The two most common:
#
# `infisical` — env-parent: each top-level key is an env slug; the
#   value maps local secret names to Infisical paths. Glob keys
#   (containing `*`) match against secrets: names, with the value
#   treated as a folder prefix:
#       "pub_*": /jwt/account/   →  /jwt/account/<matched_name>
#
# `template_file` — renders ${var} templates to disk; the section
#   is a list of `{template, output, envs}` specs (see
#   template_file source for the spec shape). `output` is relative
#   to the destination's `root`.
#
# `yaml_file` — env-parent: each value is `<key>:<query>`, a file key
#   of the destination's `files:` and an RFC 9535 JSONPath query that
#   selects exactly one existing scalar node:
#       db_password: homeserver:$.database.args.password
publishers:
  infisical:
    dev:
      client_id: /oauth_client/google/client_id
      client_secret: /oauth_client/google/client_secret
    prod:
      client_id: /oauth_client/google/client_id
      client_secret: /oauth_client/google/client_secret

# Required: rotation metadata.
rotation:
  runbook: runbooks/rotate-vendor-api-key.md#google
  groups:
    google:
      max_age_days: 365 # required positive int; max_age_days: 0 + producer: static = never auto-rotate
      last_rotated: null
      producer: interactive
      secrets:
        - name: client_id # required; matches a key in `secrets:`
        - name: client_secret
          format: opaque # optional; producers consume it as needed
```

`last_rotated` is a scalar — a date, an ISO date string, or `null`
(never rotated).

A producer that needs per-manifest parameters reads them from its
rotation group, beside `producer:` (`aws-iam`'s `iam_user`).

### Login-only shape

```yaml
# manifests/aws-root.yaml
service: aws
login:
  url: https://signin.aws.amazon.com/console
  id: robert@example.com
  mfa: '<base32 TOTP secret>'
notes:
  backup_codes: ['…']
secrets:
  password: '…'
rotation:
  runbook: runbooks/rotate-file-credential.md#aws-root
  groups:
    password:
      max_age_days: 0
      producer: static
      secrets:
        - name: password
```

It has no `publishers:` and no target lists it; `km rotate` writes only
the manifest. Without `secrets:` it may omit `rotation:` too, and
`km rotate` then rejects it with a "no rotation block" message.

### Logins

A login lives in one manifest (F-56 D2). `login:` holds only what the
login verbs use:

- **Inline**: a mapping of some of `url` (the well-known login URL),
  `id` (the username) and `mfa` (a base32 TOTP secret), each a string.
  A manifest with an inline login is its own login.
- **`from: <slug>`**, alone: the login is the one in manifest `<slug>`,
  such as `apple-api` naming `apple-user`, the login used to rotate the
  API key. The named manifest must have an inline `login:` (one hop).

The password is never in `login:`; it is the `secrets.password` of the
manifest that holds the inline login. A manifest's **effective login**
is that inline `login:` plus `password` when `secrets.password` is a
string. With `from`, nothing merges over the referenced login: it is
used as is, with the referenced manifest's password. `km login`,
`km mfa` and the `interactive` producer read the effective login.

Every other field goes in `notes:`, an optional mapping (`notes: {}`
is allowed) that km never reads.

`km validate` reports, naming the manifest and never a value: a
`login:` key other than `url`, `id`, `mfa` or a lone `from`; a
non-string value; `login: {}`; a `from` naming no manifest, a manifest
without an inline `login:`, or one whose own `login:` has `from`; a
non-mapping `notes:`; and the old `account:` block ("renamed to
`login:`"). A leftover `accounts/` directory fails `km validate`, and
`directories.accounts` in `config.yaml` fails the load: both are
retired.

## Producers

A producer mints fresh values for one rotation group. The km package
ships these built-ins; a project-local `producers/<name>.py` file
overrides any built-in by name (the loader checks the project's
`producers/` directory first).

| Producer      | Behaviour                                                                                                                                                                                                                                                                                                                                                   |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `static`      | No-op — preserves the values currently in `secrets:`.                                                                                                                                                                                                                                                                                                       |
| `interactive` | Opens the effective login's `url` and shows the `km login` menu (u/p/t), then prompts for each new value (paste-back). The clipboard is cleared when km exits.                                                                                                                                                                                              |
| `ed25519`     | Mints an ed25519 private key (PKCS8 PEM) per entry. Public keys are derived from the private on demand by consumers; historical verification is the publisher's job (e.g. Infisical's per-secret version history).                                                                                                                                          |
| `uuid`        | Mints a UUIDv4 per entry.                                                                                                                                                                                                                                                                                                                                   |
| `base64-32`   | Mints 32 random bytes encoded as base64-with-padding per entry.                                                                                                                                                                                                                                                                                             |
| `passphrase`  | Mints a uniform random string per entry. Each entry's `format:` may be a mapping of `{length, alphabet}`; defaults are length=32, alphabet=alphanumeric. Built-in alphabets: `alphanumeric`, `alpha`, `lower-alphanumeric`, `upper-alphanumeric`, `numeric`, `hex`, `hex-upper`, `urlsafe`, `printable`. Or pass an explicit char-set string of length ≥ 2. |
| `aws-iam`     | Mints a fresh IAM access-key/secret pair for the group's `iam_user`; with `--revoke-old`, deletes prior keys after the new pair is live.                                                                                                                                                                                                                    |
| `sendgrid`    | Mints a new API key with the prior key's scopes; with `--revoke-old`, deletes the prior key.                                                                                                                                                                                                                                                                |
| `twilio`      | Rotates the auth token via Twilio's Secondary → Promote flow.                                                                                                                                                                                                                                                                                               |

The Producer ABC and contract live in `km.producers.contract`. To add
a producer drop a file at `<project>/producers/<name>.py` containing
exactly one concrete subclass of `Producer`; manifests reference it by
the file's stem (hyphens map to underscores: yaml `aws-iam` →
`aws_iam.py`). Constructors receive their config block (`{}` if
absent) and demarshal it.

## Publishers

A publisher writes secret values to a backing store and reads them
back for `km validate`'s publication check. Its contract, in the order
`km publish` drives it per destination and env: `ensure` (create the
destination's structure if missing; idempotent), `publish` (write the
values, unconditionally: no compare-before-write), `apply` (restart or
reload what reads the destination; default nothing). `collect` reads
back for validate. km ships:

| Publisher       | `ensure` creates                                                      | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| --------------- | --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `infisical`     | instance admin, project, environments, identities and tokens, folders | Drives Infisical via HTTP. Per-env client lifecycle owned by the publisher; `pub_*` glob mappings expand to per-secret paths.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `template_file` | the output directories under `root`                                   | Renders `${var}` templates to `<root>/<output>` (`root` is required: absolute after `$VAR` expansion, or `./`-relative to the config dir; km supplies no default). Section is a list of `{template, output, envs}` specs. Drift is whole-file; deletions are no-ops (rendering replaces). `transport` + `remote_dir` instead of `root` make it remote, and `apply` runs a command once per env after its files are written (below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `yaml_file`     | nothing                                                               | Sets secrets in place in existing yaml files the destination names (`files: {<key>: <path>}`, `{env}` substituted; absolute or `./`-relative). The env-keyed section maps `<local_name>: <key>:<query>`, split at the first `:`, where `<query>` is an RFC 9535 JSONPath query that must select exactly one existing scalar (a filter picks a list entry by content: `homeserver:$.listeners[?@.type=='http'].port`). Comments, quoting and indentation are kept. An env's update is all or nothing: every manifest's addresses are checked first (in a dry run too, and two addresses may not select one node), then every file is written under a lock on its directory, the originals put back if one write fails. Optional `apply` runs once per env after the writes. Validate reads each node back; a file or node that is absent is `missing`. A destination writes only the addresses whose key its `files` name: a key another target's `yaml_file` destination names is left to it, and a key no destination names is an error. |
| `postgresql`    | missing roles (`CREATE ROLE … LOGIN`)                                 | Sets role passwords with `ALTER USER`; validate test-authenticates as the role. An env connects with `user`/`password`, or with `login_file`: a yaml file of `user:` and `password:` read at connect time, so the admin login stays outside the project.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

A remote `template_file` destination (`transport:`, `remote_dir:`)
writes each file straight to `<remote_dir>/<output>` by running a shell
command through the transport, the bytes on stdin: it is staged beside
the target's file, then moved over it, so a failure leaves the live file
as it was. Nothing is written locally, and `root:` is not allowed. The
transport is any command that runs its one argument as a shell command
on the target: `[ssh, <host>]`, or `[sh, -c]` locally.
`km validate <target>` hashes the remote file through the same
transport (no secret travels back).

Project-local publishers live at `<project>/publishers/<name>.py`.
The Publisher ABC lives in `km.publishers`.

## Publishing

`km publish <target> [<env>…] (--all | --only <sel>…) [--execute]`
writes the selected manifest secrets to every destination of `<target>`
that publishes them, for the given environments (default: every
environment the target accepts). It is a dry run unless `--execute` is
given. Exactly one of `--all` (every secret of every manifest the
target lists) and `--only` is required, so writing everything is always
said out loud. `--only` repeats; `<sel>` is a manifest slug (`synapse`,
`apple-api`: the whole manifest) or `<slug>:<name>`, where `<name>` is
one of the manifest's rotation groups, else one of its secrets. An
unknown slug or name is a usage error listing the manifest's groups and
secrets, and a slug the target does not list is a usage error saying
the target does not publish it. In order:

1. Every manifest the target lists is loaded and the target's
   per-target values are overlaid. A manifest that fails to parse stops
   the run; a manifest the target does not list is never read.
2. Among the selected secrets, an empty generated secret (`passphrase`,
   `uuid`, `base64-32`, `ed25519` producer) is minted into its manifest.
   An empty `static`, `interactive` or vendor secret fails the publish
   naming manifest and secret, before anything is written: only a human
   or the vendor can produce it. Unselected secrets are neither minted
   nor checked.
3. Per destination, in the target's order, per env: `ensure`,
   `publish`, `apply`. `infisical` and `postgresql` write
   only the selected addresses; `template_file` re-renders, in full,
   every file that uses a selected secret. `apply` runs only when the
   destination wrote something for that env and no write there failed.
   The dry run lists every address or file it would write and every
   apply it would run.
4. When an `ensure` recorded new per-target values (a fresh Infisical's
   project id and services token), the destinations before it that use
   them (the rendered client file) are published again with just those
   values. An empty secret an `ensure` provides is not a step-2 error.

`km rotate <slug>` rotates the due (or `--force`d) groups of
`manifests/<slug>.yaml`; an unknown slug is a usage error listing the
slugs. With no slug it sweeps every `manifests/*.yaml`, skipping
`interactive` and `static` groups unless `--force` names one. It
changes the manifest only and calls no publisher; publish the rotated
group afterwards to each target that should get the new values:

```
km rotate synapse --force roost_bot
km publish local dev test --only synapse:roost_bot             # dry run
km publish local dev test --only synapse:roost_bot --execute
```

## Bootstrap chain (empty project → fully populated)

1. `km publish <target> --all` — dry run: lists what would be minted,
   created and written; fails on secrets only a human can provide.
2. `km publish <target> --all --execute` — mints the empty generated
   secrets into the manifests, creates what is missing, writes every
   value.
3. `km validate <target>` — verifies the target is in a valid state.

## Validity

`km validate <target> [<env>…]` answers a single question: is the
target in a valid state? Three gates run in order:

1. **Schema** — no `*.yaml` sits below a subdirectory of
   `<project>/<directories.manifests>/` (each is named), and every
   manifest the target lists parses, has the required keys, references resolve (every `producer:`
   and `publishers.<name>:` resolves via the loader), and each
   publisher's own `validate()` accepts its section.
2. **Freshness** — every rotation group's `last_rotated` is non-null
   and within `max_age_days`.
3. **Publication** — read back through the same destinations
   `km publish` writes through (with the per-target values overlaid):
   every secret in `secrets:` matches the same value at every address
   it declares, for the given envs (default: every env the target
   accepts), and no destination holds a secret no manifest declares.

The system is valid iff all three pass. Schema errors short-circuit
their file (a malformed manifest can't sensibly be checked for
freshness/publication). Freshness and publication then run on every
file that passed schema.

Output rows fall into four categories:

| Category   | Meaning                                                                                                |
| ---------- | ------------------------------------------------------------------------------------------------------ |
| schema     | Manifest is malformed.                                                                                 |
| stale      | Group's `last_rotated` is null or older than `max_age_days`.                                           |
| missing    | Manifest claims a secret; publisher has no entry at the declared address.                              |
| differs    | Manifest and publisher both have the secret, but the values diverge (typical right after `km rotate`). |
| store_only | The destination holds a secret no manifest declares (the mirror of `missing`).                         |

A `store_only` row invalidates the target like a `missing` one: nothing
is created in a store by hand, so an extra is drift. `template_file` has
no store-only (the rendered file is the store).

Pass `--json` for machine-readable output (`{schema, freshness,
publication, valid}`, with `publication.store_only` rows); `--verbose`
adds the `manifest=… store=…`
value pair on `differs` rows; `--schema-only` skips freshness and
publication (useful in pre-commit hooks where we don't want network
calls). There is no offline mode: a remote destination is always read
through its transport, because a local copy written by the same publish
could never show drift on the target.

Exit codes: `0` valid; `1` invalid; `2` configuration / IO error
(couldn't load config, couldn't reach a publisher to compute
publication state).

## Tests

```sh
uv run pytest                          # everything
uv run pytest -m 'not integration'     # unit only (no network, no creds)
uv run pytest test/infisical_int_test.py
```

The three integration tests (`infisical_int_test.py`,
`publishers_infisical_int_test.py`, `bootstrap_int_test.py`) all read
the same two env vars:

| Variable                    | Purpose                                                         |
| --------------------------- | --------------------------------------------------------------- |
| `INFISICAL_TEST_PROJECT_ID` | Workspace id of a **throwaway** project the tests may mutate.   |
| `INFISICAL_TEST_TOKEN`      | Bearer token for a machine identity with R/W/D on that project. |

The pytest harness loads them from `test/.env` automatically (gitignored
— each developer keeps their own). One-time setup walkthrough — how to
create the throwaway project, mint the token, and populate the file —
lives at [docs/integration-tests.md](docs/integration-tests.md).
Without the file, `infisical_int_test.py` and
`publishers_infisical_int_test.py` skip-on-miss; `bootstrap_int_test.py`
errors loudly (no silent skip — by design).

## Prerequisites

- Python ≥ 3.12
- `git-crypt` unlocked (manifest yamls under `projects/*/manifests/`
  are encrypted at rest)
