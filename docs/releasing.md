# Publishing an approved release pair

`.github/workflows/publish.yml` is manually started on `main`. It runs the same
focused Python/Node and package checks as pull-request CI, downloads those exact
artifacts, and records their commit and SHA256 digests in the run summary. It
then waits for **jerpint's separate approval** at each registry's environment:
first `pypi`, then `npm`. PR approval or merging does not approve publication.
The workflow uses OIDC Trusted Publishing, without persistent upload secrets.
Tags and GitHub releases are created only after both registry downloads match.
The TUI release is created first without becoming Latest; the primary Python
release becomes Latest last, because lodge update checks read releases/latest.

## Owner setup before enabling publishing

The environment settings are the approval boundary. A workflow's branch check
alone is insufficient because someone dispatching a branch can alter that
branch's workflow. Trusted publishers must bind the **environment name** as
well as repository and workflow filename. Do not grant the GitHub App
administration access or add any bot as an environment reviewer.

1. Open [repository environments](https://github.com/jerpint/woltspace/settings/environments).
   Create `pypi` and `npm`. For **each**, set Required reviewers to the user
   `jerpint` only, turn off administrator bypass, and select deployment branches
   **Selected branches and tags** with one **Branch** rule named `main`, no tag
   rules. Leave Prevent self-review off so the sole owner can start a run and
   approve it. Save all protection rules. Do not store registry upload tokens.
2. In [PyPI woltspace publishing](https://pypi.org/manage/project/woltspace/settings/publishing/),
   add GitHub owner `jerpint`, repository `woltspace`, workflow filename
   `publish.yml`, environment **`pypi`**.
3. After the workflow exists in the repository, open
   [npm package settings](https://www.npmjs.com/package/@woltspace/tui?activeTab=settings).
   Add a GitHub trusted publisher: owner `jerpint`, repository `woltspace`,
   workflow `publish.yml`, environment **`npm`**. Enable direct `npm publish`
   for this publisher; the workflow uses GitHub approvals, not npm staging.
   The package repository URL must refer to `jerpint/woltspace` for provenance.
4. Review protection of `main` and workflow changes. Verify the actual saved
   environment configuration with `python scripts/release.py environments`.
   Only after registry trust and approval settings are correct, set repository
   Actions variable **`TRUSTED_PUBLISHING_ENABLED=true`** in
   [Actions variables](https://github.com/jerpint/woltspace/settings/variables/actions).
   Until then, a real publish run fails before entering publishing jobs.
   Dry-run gate rehearsals do not need this variable or registry trust.

The workflow rechecks both approval environments before staging artifacts in
publishing jobs. Missing, unreadable, or insufficient protection is a failure,
not a reason to fall back to token publishing. Environment protections are
owner-managed settings; a repository administrator can change them, so the
promise depends on preserving those settings and workflow review rules.

## Running and approving

Merge the reviewed release preparation and workflow changes into `main`, then
open Actions → **Publish reviewed release pair** → Run workflow. Select `main`
and enter the exact Python and TUI versions declared by that commit.
**Dry run defaults to true**: leave it on first to exercise both approval waits
with no OIDC permission, registry publishing or GitHub release writes. After the
rehearsal verifies the gate, turn dry run off only for an explicitly authorized
release with registry trust configured and the owner enable variable set.
The rehearsal checks settings and approval waits; it does not exercise registry
state, partial-publication recovery or OIDC authentication.

Inspect the commit, versions and artifact digests in the summary, then use **Review
deployments** to approve `pypi`. After Python is published and downloaded back
successfully, approve the `npm` deployment separately. There is no automatic
publication on merge or tag push.

A successful run uploads the exact Python wheel/sdist and TUI tarball to GitHub
releases. Jobs receive only their needed permissions: publishing jobs can mint
OIDC identities but cannot write repository contents; the final release job can
write tags/releases but has no OIDC permission. Authenticated publishing and the
real approval wait must be exercised on an explicitly authorized release; unit
and ordinary CI success do not prove registry trust or approval configuration.

## Recovering a partial release

Both registries reserve versions permanently. Use **Re-run failed jobs** on the
original run so the existing approved artifact remains the source of truth.
Do not start a fresh build to recover an already published version: build
metadata/timestamps may change the bytes even for the same source commit.
The retained approved artifact lasts 30 days; loss or expiration requires an
owner recovery decision, not a silent rebuild. Without the original artifacts,
a partially published version cannot safely be resumed: prepare a new version
and approve it through the normal release flow.

The registry check has three outcomes: absent files are staged for publication;
present files with matching digests/downloads are verified and skipped; any
mismatch stops for human investigation. A partly uploaded Python version stages
only its missing wheel or sdist. Registry read errors stop the job rather than
pretending the version is absent. A failed npm publish leaves Python available
but creates no tags or GitHub releases. Retrying the failed jobs continues with
the same manifest, and GitHub approval applies to retried publishing jobs.

Finalization verifies both registries again, checks new/Python tags point at the
approved commit, and verifies release asset bytes. It resumes an existing draft
without replacing assets. An unchanged TUI version can reuse its already public
GitHub release at its original commit if its asset bytes match; mismatched or
unpublished tags still fail. Update `docs/release-notes.md` in each reviewed
release preparation. The primary release is published only after its
assets are verified. Do not delete or overwrite an existing version or tag to
make a mismatch disappear. Revoke obsolete manual upload credentials only after
an owner-approved trusted release has succeeded.

References: [GitHub environment approvals](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/),
[npm Trusted Publishing](https://docs.npmjs.com/trusted-publishers/).
