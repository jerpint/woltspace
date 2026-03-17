# Migrations

Migration scripts for minor and major version bumps. Patch bumps don't need migrations.

## Naming

- `v0.1.0.sh` — migration for the v0.1.0 release
- `v0.2.0.sh` — migration for the v0.2.0 release
- `v1.0.0.sh` — migration for the v1.0.0 release

## Structure

Each migration script should:
1. Check preconditions (current version, required files)
2. Automate what it can (move files, update configs)
3. Print clear instructions for anything that needs manual action
4. Be idempotent — safe to run twice

## Usage

The `/update` skill runs the appropriate migration automatically when it detects a minor or major bump.
