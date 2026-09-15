# GitHub source archive

## Current policy — two shared public repositories (2026-09-15)

The owner approved public source for publicly submitted apps, including migration
of the existing test apps. New public builds use **`vib-app/sources`**; runtime
packages and the future Store catalog use **`vib-app/packages`**. Private local
development does not imply public-source consent. No old repository is deleted
or converted. Historical private receipts remain compatible.

- `github_build.py start --public-source ...` records approval before source
  staging or any GitHub operation. Without that explicit flag new jobs fail
  before credentials/network; normal local/private development is unaffected.
- `github_archive.py --public-source ...` archives exact source to `sources`.
  `--legacy-private` is a separate, explicit recovery-only CLI mode.
- `sources/main/apps/app-<publisher+app hash>/<source digest>/` contains the
  browsable source files. Immutable root snapshot tags/commits are retained for
  the existing isolated CI checkout. Identical source in different apps has
  distinct staging tags. The main update preserves other apps and trusted CI
  files, uses no force push, and reconciles concurrent updates before retrying.
- Registry publication requires explicit `public_source_approved=True` before
  calling `upload_public`. Migration `0006_shared_public_sources.sql` accepts
  exact shared-source receipts while preserving the old per-app constraints.
- Only the trusted host holds the App key. The public CI job has read-only
  repository permission and no publication credentials. The trusted controller
  verifies the build before publishing a package, as before.

`sources` is public (repository ID `1371257005`). After the owner added it to the
App's selected repositories, LED, unit-converter and release-check source migrations
succeeded with real GitHub readback. The LED also compiled successfully in the new
repository's Actions workflow and passed separate local artifact verification. See
[migration checkpoint](SOURCES-MIGRATION.md). Source migration does not itself
make an application browser-compatible or publish a Store entry.

The sections below retain the history and security details of the earlier
per-app private implementation; the current policy above supersedes its routing.

Status: trusted upload worker and Registry publication gate implemented locally;
**not a deployed public service**. Real source upload, GitHub read-back and
write-free replay passed for the explicitly authorized private test repository
on 2026-09-06 (see live validation below).
Added under the user's explicit September 2026 authorization to start GitHub App
setup for `vib-app`. This is a trusted administrator utility outside the Launcher,
website, generated app, CodeAgent and Builder. It does not amend or claim acceptance
of historical Stage 0 gates.

## Run

Requires Python 3.11+ and `/usr/bin/openssl` on macOS/Linux. No pip dependencies.

```sh
python3.11 artifacts/source-archive/github_app_setup.py serve
```

Open the printed `Setup URL` **on this computer**. It is a temporary local browser
entry capability, not an App private key; do not share it. The utility binds only
`127.0.0.1` on an available port and closes after one hour. It prints its PID and
health URL. The server session must remain running throughout registration.

1. The organization owner opens the wizard and clicks the GitHub creation button.
   Login, 2FA/passkey, app-name conflicts and final registration are handled on
   GitHub. The form targets **organization `vib-app`**, not a personal account.
2. GitHub returns a single-use code to localhost. The utility validates state and
   the originating browser cookie, exchanges the code, checks the organization and
   permissions, then saves only the App ID/slug/owner/permissions and private PEM.
   Unused OAuth client and webhook secrets are discarded, not logged.
3. The owner follows the installation link and approves installation in `vib-app`.
   Prefer **Only select repositories**. If GitHub requires a seed repository, use
   a dedicated test repository; do not authorize every existing repository just
   to get past the form. Do not assume repository creation automatically grants
   this installation access: the 2026-09-07 real creation test created a private
   app repository, but its repository-scoped token was rejected with HTTP 422.
   Verify access to the exact numeric repository ID; if missing, the owner must
   add that repository to the selected installation before source upload resumes.
4. Return to the local wizard and press **检查组织安装和权限**. This performs two
   read-only API calls using a short-lived App JWT: `GET /app` and
   `GET /orgs/vib-app/installation`. A browser-supplied installation ID is never
   proof of authorization. The tool checks matching organization numeric ID,
   App ID, permissions and suspension status; it warns about all-repository scope.

The wizard does **not** create repositories, mint installation access tokens,
push source, install apps into the Launcher, or publish to the App Store.
The actual GitHub registration flow remains unverified until the owner completes it.

On 2026-09-06 the owner completed real registration and installation. Read-only
GitHub verification confirmed App `vibapp-source-bot` (App ID `4848527`), organization
`vib-app`, installation `159462341`, matching permissions and `selected` repository
scope. This is identity/installation evidence, not a successful source push.

## Permissions and data boundary

- GitHub App visibility: private, owner-only installation. This is separate from
  repository visibility: current public submissions use the public `sources`
  repository; historical per-app archives remain private.
- Repository Contents: write, for source commits and repository initialization.
- Repository Administration: write, required by GitHub to create repositories.
  **This permission also permits powerful operations such as repository deletion**;
  it is not a create-only capability. This utility implements none of those APIs.
- Metadata: read. Registration defaults omit Actions/Workflows. On 2026-09-06
  the owner explicitly approved Actions:write and Workflows:write for the cloud
  build path; both App and selected installation were verified. No user OAuth
  or subscribed events. Unknown extra permissions are still rejected.
- Webhook is explicitly inactive **and its URL is empty**. Do not supply a
  loopback/private-IP placeholder: GitHub rejects that address even when
  `active: false`. `redirect_url` and `setup_url` are browser redirects, not webhook
  receivers; those remain loopback so credentials are received on this computer.
- The local server does not use credentials from `gh`, browser sessions, environment
  variables or existing CodeAgent settings. GitHub API requests do not follow
  redirects or inherit proxy variables. Restricted networks may need explicit,
  separately reviewed proxy support; this utility does not silently bypass TLS.

Default credential location, **outside this repository**:

```text
~/Library/Application Support/VibApp/source-archive/github/
  credentials.json           # App private PEM + allowlisted metadata; mode 0600
  registration-attempt.json  # Non-secret ambiguity/replay marker; mode 0600
  setup.lock                 # One setup server for this credential directory
```

The managed directory must be owned by the current user, mode `0700`, and not a
symlink. Credential files must be regular, mode `0600`, owner-owned, and have one
hard link. Writes are atomic and refuse to overwrite an existing destination.
Private keys never appear in command arguments, environment, browser HTML, logs,
or stdout. OpenSSL receives a protected, unlinked file descriptor; no key is passed
to a generated process. Same-user malware/root can still read process memory or
these files: this local bootstrap is **not an encrypted vault**. Production keys
belong in a dedicated source-service secret manager, not on every VibApp client.

No private key import/export web endpoint is provided. Do not paste credentials
into chat, place them in `.env` within a checkout, or mount this directory into a
CodeAgent/Builder. Keep backups private; revoke/rotate the GitHub App key on exposure.

## Recovery and status

```sh
python3.11 artifacts/source-archive/github_app_setup.py status
python3.11 artifacts/source-archive/github_app_setup.py status --check-installation
```

Status prints only allowlisted non-secret metadata. Without the check flag it
reports the installation as **not checked**, not healthy. Running `serve` again
with existing credentials resumes installation verification without another App.

If conversion was attempted but no credentials were saved (network ambiguity,
crash or disk failure), the persistent marker prevents automatic re-creation or
code replay. Inspect **GitHub → vib-app → Settings → Developer settings → GitHub
Apps** first. If an App exists, an owner must recover access by generating a new
private key and arranging a secure local import (not implemented here), then revoke
the lost key. Do not repeatedly create new Apps or delete the marker casually.
If the setup expired before creation, restart and use the new setup link.
Browser-back/double callbacks never perform a second code exchange.

After registration, GitHub will retain this temporary local setup URL. The future
deployment should replace/remove the setup URL. There is no webhook receiver to
deploy. Do not treat this one-hour local server as a production endpoint.

## Verification / health checkpoints

```sh
python3.11 -m unittest discover -s artifacts/source-archive/tests -v
```

- Critical: offline tests cover callback state/cookie/origin/CSRF, replay and crash
  recovery, Host checks, expiry, filesystem permissions/symlinks, secret redaction,
  permission/identity mismatches, suspension, JWT signature and claims.
- Critical while serving: the printed `/health` returns exactly
  `{"status":"ok","scope":"local-setup-only"}`. This proves only the wizard is up.
- Critical before declaring GitHub connected: `status --check-installation` must
  succeed against real GitHub after owner approval. Synthetic tests are not evidence
  of a real installation or successful source push.
- UI check: local wizard loads, creation button is visible, restricted permissions
  are explained, and no secret is shown. Browser checks must not auto-submit the
  GitHub creation/install forms or collect traces of real credential callbacks.
- Manifest regression: parse the actual hidden form field and check
  `hook_attributes == {"url": "", "active": false}` with no events. An inactive
  hook alone is not sufficient: GitHub rejected the earlier loopback URL during
  the first real registration attempt on 2026-09-06. The original 28 offline tests
  missed this; their pass did not establish that GitHub accepted the manifest.
- Form-submit regression: use a real browser to press **检查组织安装和权限**.
  The query-free form page uses `Referrer-Policy: same-origin`; using `no-referrer`
  there makes a browser submit `Origin: null`, which the exact-origin check rejects.
  Entry/code/install redirects and errors still use `no-referrer`; a query-bearing
  `/` request is redirected to clean `/` before rendering the form. Cross-origin,
  missing and null Origin values remain rejected, even with a valid CSRF field.
  Do not replace this browser check with an HTTP test that supplies its own Origin.
  Real Chromium button-click evidence on 2026-09-06: before repair the form sent
  `Origin: null` and received HTTP 403; afterward it sent the exact loopback origin,
  received HTTP 303 back to `/`, and rendered GitHub-confirmed installation success
  using the real App credentials. 31 offline tests also passed. Both runs used the
  normal form button, without overriding browser request headers.

## Historical Public App Store rule (2026-09-06; routing superseded above)

Every release published to the public App Store must first archive its verified
source in organization `vib-app`. Failed or pending upload, mismatched source/package
digests, wrong owner/repository, or missing read-back evidence cannot publish.
Private local applications are unaffected. A public app does **not** imply public
source: this worker explicitly creates **private** repositories and never changes
their visibility. Source visibility/licensing is a separate product decision.

`github_archive.py` is the trusted worker, separate from setup and generated code.
`../registry-store/publication_service.py` sequences archive → receipt recording →
publication. Migration `0005_github_source_archive.sql` makes the gate mandatory at
the database boundary, even when the helper is bypassed. Existing independent
verification, moderation, revocation and user-publication authority still apply.

Example trusted-worker invocation (identities/hashes come from Registry data, not
untrusted client-supplied values):

```sh
python3.11 artifacts/source-archive/github_archive.py \
  --source-root /trusted/verifier-owned/source \
  --publisher-id publisher.example --app-id ai.vibapp.example \
  --package-digest <verified-package-sha256> \
  --source-digest <verified-source-tree-sha256>
```

- Repository mapping: `app-` + SHA-256 of UTF-8 `publisher_id + "\n" + app_id`.
  The destination organization is fixed, not configurable by a client.
- Source is frozen in memory and rehashed with Builder's source-tree algorithm
  **before** issuing an upload token. Limit: 512 files/directories each, 1 MiB per
  file, 16 MiB total. Symlinks, hardlinks, special files, unsafe paths, credentials,
  private-key patterns, dependency/build directories and GitHub workflows are
  rejected. This bounded filter is not a comprehensive secret detector.
- A short-lived initialization token needs Administration:write and Contents:write
  because `auto_init` writes a README. Source writes then use a second token scoped
  to **one numeric repository ID** and Contents:write only.
- Every package uses tag `vibapp-<package-sha256>` pointing to an isolated source
  commit with `.vibapp-source-archive.json` binding source/package/publisher/app.
  The default branch is a bootstrap only: inspect the returned commit/tag for
  source. This worker never force-updates tags, overwrites files, or deletes repos.
- A partial empty repository can be initialized once via a fixed non-executable
  bootstrap file, only after GitHub reports it is empty. The Contents call omits
  `sha` and therefore cannot overwrite an existing file.
- The tag, commit and recursive tree are fetched back and all blob hashes/paths
  compared before returning the six-field `github_archive` receipt. Exact retries
  re-read the same tag without duplicate source commits. Conflicts fail closed.
- API traffic stays on `api.github.com`; redirects/proxy inheritance, arbitrary
  endpoints and deletion are disallowed. Per request: 15 s, bounded response; per
  job: 5 min deadline and 540 calls. Errors expose reason codes only.

No unauthenticated HTTP upload or publication endpoint was added. The future
deployed service must authenticate users, resolve their publisher identity, supply
a verifier-owned immutable source root, use separate DB principals, and store the
App private key in its secret manager. Neither the website nor CodeAgent should
ever receive the App key or an organization installation token.

### Live validation / recovery

On 2026-09-06, local source-archive tests passed (31 setup + 11 worker tests), along
with the disposable PostgreSQL publication-gate integration and desktop/web
consumer regressions. The 42 source-archive tests were rerun after the owner
authorized the test repository; they passed again. Real API evidence follows
separately; offline tests alone do not establish a successful remote upload.

The single real transport test targets synthetic `fixtures/smoke`, NOT an accepted
VibApp application or user source. Its private repository is:

`vib-app/app-460d7bbfd320207054c626331f5819d88f1dfd47b96f3ed38e16a1e47636ee20`

Initially the App returned 404 on read and 422/name-already-exists on creation.
An owner-only lookup confirmed repository ID `1358981750`, private, metadata size 0;
that size alone did not prove an empty Git database. The App's selected repositories
did not include this repo. No receipt was issued for those failed attempts.

After the owner added **this repository only** to installation `159462341`, a fresh
App token scoped to its numeric ID successfully read it. The installation remains
`selected`, not `all`. The trusted uploader then wrote three synthetic source files
plus the binding marker and read back the exact tag/commit/tree successfully:

- Commit: [`70adfefe89d7435f97f634f8ecc9769ca8b16b1d`](https://github.com/vib-app/app-460d7bbfd320207054c626331f5819d88f1dfd47b96f3ed38e16a1e47636ee20/tree/70adfefe89d7435f97f634f8ecc9769ca8b16b1d)
- Source digest: `90e5a6cca22bf0c3d9cd93129921807e3dc0ce3e061f7ec296eddd644638fb27`.
- Synthetic package-binding digest: `8b0c0f3b5ae2cd21bccff8584b0a90a481304ab283fa1fbdafc9c5c1b59f2fd0`
  (transport fixture only, not an accepted package digest).
- First successful upload: 14 API calls, 7 repository writes (4 blobs, tree,
  commit, tag), **no repository creation or bootstrap recovery**.
- One immediate replay failed with `archive_network_or_response`. A subsequent
  bounded replay succeeded: 6 API calls, **zero repository writes**, identical
  six-field receipt and commit. A test wrapper rejected any attempted repository
  mutation during that replay; it still used the normal uploader implementation.
- Final read-back/replay evidence recorded by 2026-09-06 11:00 UTC.

Only GitHub App credentials were used for source writes. No personal-token fallback,
new repository, public visibility, application publication or production migration
was used. New-repository automatic creation/access assignment is **not** established
by this manually authorized-repository test; it still needs its own scoped test.
The former initialization-permission hypothesis is not a proven cause of the initial
access failure. Keep initialization Contents:write, but do not infer that changing
that token permission automatically repairs installation access.

## Cloud compilation and explicit package publication (2026-09-06)

`stage_source` creates a separate `vibapp-source-<source digest>` snapshot before
compilation. It deliberately omits any package digest and is NOT accepted as a
source-publication receipt. The existing post-verification `upload` contract and
publication gate remain unchanged.

`ci_builder.py`, `github_build.py`, and `github_smoke.py` implement the tested cloud
adapter, not an always-on deployed release service. They use a platform-owned
workflow and a separate immutable source checkout; generated workflow files and
Cargo configuration are rejected. Setup receives only a validated manifest and
reviewed lock. Compilation is designed for non-root, network-none Docker with
CPU/memory/PID/time/output limits, and output must still pass the existing
independent local Component verifier. This does not assert production isolation.

App access to public `vib-app/packages` (ID `1359065065`) was verified with a
repository-ID-scoped Contents:write token. Private synthetic source was uploaded
and read back. An early owner-CLI workflow attempt lacked permission; that
prototype was replaced entirely by owner-approved, scoped GitHub App transport.
No personal token fallback exists in the build/release controller.

Actual Actions run `34038928625` succeeded and its downloaded artifact passed the
separate pinned Wasm/import/guest-descriptor verifier. Two preceding runs exposed
Docker tmpfs's noexec default blocking reviewed Rust dependency build scripts.
The fix allows exec only in the bounded compiler workspace; network remains off,
non-root/read-only/capability/PID/RAM restrictions remain in place.

```sh
python3.11 artifacts/source-archive/github_build.py start --public-source --handoff /absolute/handoff.json --publisher-id publisher.example --output /absolute/new-job
python3.11 artifacts/source-archive/github_build.py resume --output /absolute/new-job
# Explicit operator consent to share the compiled package publicly:
python3.11 artifacts/source-archive/release_pipeline.py publish --output /absolute/new-job
```

`resume` runs full verification again, including existing candidates; it does
not trust a saved success string. `publish` connects the real post-build source
receipt to the verified package and uses the packages-only publisher. See
[PACKAGES.md](PACKAGES.md) for immutable Raw/Release/torrent layout, proof and
retry semantics. Builds do not implicitly publish. No always-on publisher,
automatic AppStore catalog update or universal browser-app compatibility is
claimed by these CLI operations.

## GitHub references (checked 2026-09-06)

- [Manifest registration and single-use exchange](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
- [GitHub App API and organization-installation lookup](https://docs.github.com/en/rest/apps/apps)
- [Installation scope and access to newly created repositories](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party)
- [Organization repository creation permissions](https://docs.github.com/en/rest/repos/repos#create-an-organization-repository)
- [Private-key management](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
- [Registration: leave Webhook inactive when only authentication is needed](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [First-hand successful manifest submission with an empty, inactive webhook](https://github.com/orgs/community/discussions/25611)
