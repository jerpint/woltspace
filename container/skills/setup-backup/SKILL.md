---
name: setup-backup
description: Guide the owner through off-machine lodge backups on Cloudflare R2 — bucket, lock and lifecycle rules, two scoped tokens, rclone, the age key that encrypts every backup, the nightly upload and the restore checks — and verify it without ever seeing a secret. Use when the owner asks to set up, check or repair R2 / S3 backup storage for the lodge.
user_invocable: true
---

# Backups to Cloudflare R2

Goal: one private bucket that holds the lodge's nightly backups, each one a
single `woltspace-backup-<tag>.tar.gz.age` encrypted to the owner's age key,
and a read-only token that can bring the lodge back on another machine. The
full reference is `docs/backup.md` in the woltspace repo; this skill is the
guided path through it.

## Rules

- **The owner types every secret, never you.** Tokens and the age private key
  are entered by the owner in a normal terminal on their own computer, never in
  a wolt session (not even with `!`): anything typed or printed in a session
  lands in its transcript. Never ask for a key in chat, Telegram or Slack, and
  never `cat`, echo or log a secret file. When you check one, mask the values.
- **No personal data in the bucket's paths or anything public.** Use
  placeholders: `<bucket>`, `<lodge-id>`, `<account-id>`.
- Every step that costs money, creates a token or changes the owner's
  Cloudflare account is done by the owner. You guide and verify.
- **The age private key never lives on the lodge.** The lodge only needs the
  public `age1...` key. Whoever has the private key can read every backup;
  whoever loses it can read none.

## The steps (give the owner one at a time)

If the lodge has a site, a one-step-at-a-time page with copy buttons works
better than a long chat message.

1. **Bucket.** Cloudflare dashboard → Storage & databases → R2 → Create
   bucket. Pick a name (e.g. `lodge-backups`); default options.
2. **Lock rule** (snapshots can't be deleted or overwritten, even by a
   compromised lodge). Bucket → Settings → Bucket lock rules → Add rule: name
   `keep-30d`, prefix `lodges/<lodge-id>/snapshots/` — exactly this, with the
   trailing slash. Nothing broader, or the daily `latest.json` rewrite is
   blocked. Retention 30 days or more.
3. **Lifecycle rule** (the bucket doesn't grow forever). Same Settings tab →
   Object lifecycle rules: prefix `lodges/<lodge-id>/snapshots/`, delete some
   time after the lock ends (e.g. 60 days after upload).
4. **Two API tokens.** R2 Overview → Manage API tokens → Create Account API
   token:
   - `lodge-backup`: **Object Read & Write**, *Apply to specific buckets only* →
     this bucket. Goes on the lodge (step 5). R2 has no write-only scope; the
     lock rule is what makes it effectively append-only.
   - `restore`: **Object Read only**, same bucket. Only in the owner's password
     manager, never on the lodge.
   The secret is shown once. The S3 endpoint is
   `https://<account-id>.r2.cloudflarestorage.com`.
5. **rclone config, typed by the owner** in their own terminal:
   `nano ~/.config/rclone/rclone.conf`, then
   ```
   [r2]
   type = s3
   provider = Cloudflare
   access_key_id = <from lodge-backup>
   secret_access_key = <from lodge-backup>
   endpoint = https://<account-id>.r2.cloudflarestorage.com
   acl = private
   no_check_bucket = true
   ```
   and `chmod 600 ~/.config/rclone/rclone.conf`.
6. **The age key**, in the owner's own terminal, on a machine of theirs:
   `age-keygen -o /tmp/lodge-age.txt`. The file has 3 lines; the private key is
   the line starting `AGE-SECRET-KEY-1`. The owner saves the whole file in
   their password manager, then `rm /tmp/lodge-age.txt`, and sends you only the
   `age1...` public key (safe to share). That public key is
   `WOLTSPACE_BACKUP_AGE_RECIPIENT`, and `age` must be on the lodge's `PATH`.
   With it set, `woltspace backup` streams the whole archive through `age`,
   `.env` files and bot tokens included (OAuth files never), and nothing
   unencrypted is written. A recipient set without `age` fails the backup.
7. **The nightly upload.** The upload script and the crontab line (or systemd
   timer) are in `docs/backup.md`, "The daily upload". The script refuses to
   run without `WOLTSPACE_BACKUP_AGE_RECIPIENT` and refuses to upload anything
   that is not a `.tar.gz.age`. `latest.json` carries only the tag, the
   sha256 of the `.age` file, the version and the time. Turning it on for the
   live lodge needs the owner's go.

## Verify (no secret is ever printed)

- Config present and private: `ls -l ~/.config/rclone/rclone.conf` shows
  `-rw-------`. To look at it, mask the values:
  `sed -E 's/^(access_key_id|secret_access_key).*/\1 = <hidden>/' ~/.config/rclone/rclone.conf`.
- Token works: `rclone lsjson r2:<bucket> --max-depth 1`.
- Lock is exactly right:
  - outside the locked folder: `echo v1 | rclone rcat r2:<bucket>/lodges/<lodge-id>/_check.json`,
    again with `v2`, then `rclone deletefile` it. All three must succeed.
  - inside: `echo x | rclone rcat r2:<bucket>/lodges/<lodge-id>/snapshots/_lock-check.txt`,
    then `rclone deletefile` it must FAIL with `ObjectLockedByBucketPolicy`. That
    file stays until its retention ends; it's tiny.
  - If the "outside" delete is refused too, the lock prefix is too broad: ask
    the owner to set it to exactly `lodges/<lodge-id>/snapshots/`.
- `/tmp/lodge-age.txt` no longer exists, and no `AGE-SECRET-KEY` file is
  anywhere on the lodge.
- A first backup: `WOLTSPACE_BACKUP_AGE_RECIPIENT=age1... woltspace backup --out /tmp/backup-check`
  writes one `.tar.gz.age` (mode `-rw-------`) whose first line is
  `age-encryption.org/v1`. Delete it after.

## Checking the backups

- **On the lodge, weekly, no key:** the check in `docs/backup.md` downloads
  the newest snapshot, compares its sha256 with `latest.json`, checks the age
  header and a sane size. The lodge never decrypts; it can't, the key isn't
  there.
- **By hand, monthly, on a machine where the owner's key is:** with the
  read-only token, list `snapshots/`, take the newest tag (not `latest.json`,
  which sits outside the lock), download it, then
  `woltspace restore woltspace-backup-<tag>.tar.gz.age --to <scratch dir> --identity -`
  with the key file piped in by the owner. **Delete the restored `.env` and
  `.space/platform/config.json` before booting it**: they hold the live bot
  tokens, and a second lodge polling the same bot steals the owner's messages.
  Then boot it on a spare port with `WOLTSPACE_PUBLIC_TUNNEL=false` and check
  `/health` and the session count. The steps are in `docs/backup.md`.
- Test against a separate unlocked prefix (e.g. `lodges/test/`), delete what the
  test wrote, and keep a listing that shows it empty.
