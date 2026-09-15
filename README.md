# SmugMug → Local Mirror (gallery-dl, config-only)

Downloads **all original images and videos** from your SmugMug account into a local
folder tree that mirrors your SmugMug gallery structure. Re-runnable for incremental
syncs (only new images are downloaded). A small list of galleries can be excluded by
name (handy for "Collections" that just duplicate images already in other galleries).

The setup uses [gallery-dl](https://codeberg.org/mikf/gallery-dl) and `config.json`;
no custom sync script is needed. Instructions and templates checked with
**gallery-dl 1.32.11** and the upstream documentation/source.

## Setup

Commands below are single lines and work in Windows PowerShell or Command Prompt.
Replace `YOURNICK` with your SmugMug nickname.

### 1. Install or update gallery-dl

With Python installed (and its Windows `py` launcher available):

```powershell
py -m pip install --upgrade gallery-dl
py -m gallery_dl --version
```

Use version 1.32.11 or newer for this configuration. If you already installed it
with pipx, use `pipx upgrade gallery-dl` and substitute `gallery-dl` for
`py -m gallery_dl` in the commands below. Python/pip installations avoid the
PyInstaller packaging used by the standalone executable; installing through
winget does not inherently avoid that packaging.

### 2. Authorize your account (first-time setup)

Run the OAuth command to create a token pair:

```powershell
py -m gallery_dl --config-ignore oauth:smugmug
```

gallery-dl includes a SmugMug application key; you do not need to register an app.
Follow the browser/terminal prompts to authorize access to your account. Copy the printed
`access-token` and `access-token-secret` into the corresponding fields in
`config.json` (use `config.json.template` as a template to create `config.json`). Keep that file private; it is excluded by `.gitignore`.

Then open **SmugMug Account Settings**, find gallery-dl's authorization, and verify
that **Access is Full** and **Permissions are Read**. The initial authorization
flow may not offer an obvious access-level choice, so check this afterward even
if login succeeded. Public access is insufficient for a complete backup of a
site containing private or password-protected galleries; it can cause misleading
`NotFoundError` / HTTP 404 errors.

### 3. Subsequent runs

After the first setup, leave the saved tokens in `config.json` and run the sync
command again whenever you want an update; authorization is not repeated for
each run. If SmugMug access is revoked or gallery-dl reports an OAuth/401 error,
repeat the first-time authorization step and replace both values.

### 4. Review the configuration

The supplied config template which has backup destination **`D:/smugmug_backup`**. Downloads
will look like:

```text
D:/smugmug_backup/YOURNICK/<folders>/<album>/filename_image-key.jpg
```

The settings under `extractor.smugmug` include:

- `base-directory`: download root. Use forward slashes in JSON Windows paths.
- `archive`: `D:/smugmug_backup/.sync-archive.sqlite3`, a database of downloaded IDs.
- `videos: true`: download videos as well as photos.
- `sleep-request`, `sleep-retries`: API request pacing and retry delays; `timeout`: request timeout, also inherited by media downloads. Retry counts and `.part` files use gallery-dl's defaults.
- `album.directory`: nickname plus `Album.UrlPath`, split into folder components.
- `album.filename`: original filename stem, image key, and the downloaded format's extension. The key prevents duplicate camera filenames from overwriting/skipping one another.
- `album.post-filter`: exclude albums by their URL-safe name (`Album.UrlName`).

The directory/filename expressions beginning with `\fE` are gallery-dl's supported
Python-expression templates. `directory` must be a **list**, and the expression
splits `UrlPath` into separate components so slashes aren't replaced as illegal
filename characters. These settings are used while walking the full account URL.

To change exclusions, edit the existing `post-filter` value:

```json
"post-filter": "Album['UrlName'] not in ('Collections', 'Duplicates', 'Best-of-2024')"
```

Names are case-sensitive URL slugs, not display titles. This excludes matching
**albums anywhere in the account**, not whole folders with that name. Remove
`post-filter` to include every album. Removing an exclusion enables future
downloads; adding one does not delete previously downloaded files.

## Preview before downloading

### List included albums

Start here to check which albums the full-site sync will include. This prints
one line per included album: its name and a URL you can open in your browser.
It respects the exclusions in `config.json` and includes empty albums.

```powershell
py -m gallery_dl --config-ignore --config "D:/smugmug-sync/config.json" -o archive=null --range 0 --print "post:{Album[Name]} | {Album[WebUri]}" "https://YOURNICK.smugmug.com/"
```

`post:` prints once when an album is encountered; `--range 0` prevents processing
its files. This reduces work for included albums, but gallery-dl still requests
image metadata and may traverse excluded albums internally. It does not print
individual files or download media. Use `--range 0` only for this preview, not
the download command.

### List every file's destination

For a detailed check of the local folder layout, this prints one destination
path per included file across the full site:

```powershell
py -m gallery_dl --config-ignore --config "D:/smugmug-sync/config.json" -o archive=null --print "after:{_path}" "https://YOURNICK.smugmug.com/"
```

This enumerates all included files, including those already downloaded, so it
can take much longer than the album preview. Check that paths are under your
chosen destination and reproduce your SmugMug folder structure.

Both previews query SmugMug but do not download media or create/update the sync
archive. They show what is included by the configuration, not just what is new.
`--config-ignore` isolates these commands from other gallery-dl configs; the
explicitly supplied config is still loaded. Add `--verbose` only when debugging.

For metadata (including `Album.UrlPath`, `Album.UrlName`, `Image.FileName`, and
`Image.IsVideo`), inspect the account traversal:

```powershell
py -m gallery_dl --config-ignore --config "D:/smugmug-sync/config.json" --list-keywords "https://YOURNICK.smugmug.com/"
```

The relevant CLI options differ:

| Option | What it shows, without downloading media |
| --- | --- |
| Album preview (`--range 0` with `--print "post:…"`) | One name and browser URL per included album. |
| `--print "after:{_path}"` | Full configured destination paths; use the preview commands above. |
| `--simulate` | Simulated extraction with filenames, not full destination paths. Use `-o archive=null` to avoid opening the sync database. |
| `--get-urls` (`-g`) | Print URLs instead of downloading. |
| `--resolve-urls` (`-G`) | Print resolved media URLs instead of downloading. |
| `--resolve-json` (`-J`) | Print resolved metadata instead of downloading. |

There is no `--dry-run`. Avoid sharing metadata/URL dumps publicly if they reveal
private album details or download links.

## Download and update

Use the same command for the initial download and every incremental update:

```powershell
py -m gallery_dl --config-ignore --config "D:/smugmug-sync/config.json" "https://YOURNICK.smugmug.com/"
```

The archive location is already configured. For an optional log, add
`--write-log "D:/smugmug-sync/.gallery-dl.log"`.

Re-runs enumerate SmugMug again and skip archived IDs or existing destination
files. Interrupted downloads normally leave `.part` files that gallery-dl can
resume; rerun the command. Review errors and rerun failed downloads. Do not use
skip-count stopping options such as `--abort` for this backup: a skipped file
proves nothing about the rest of an album or account. Run one sync at a time.

## Limits

- **Originals are not guaranteed.** Photos use `ImageSizeOriginal` when available,
  otherwise the widest image. Videos use the widest available `VideoSize*` entry,
  not an explicit original-video preference. Successful completion does not prove
  that every file is an original. The config neither rejects all non-original
  renditions nor produces a report identifying them. Such renditions can be
  recorded in the download archive and skipped on later runs, even if an original
  becomes available. Spot checks cannot establish that every original is present.
  Investigate
  [SmugMug's download tools](https://www.smugmughelp.com/hc/en-us/search?query=download%20original%20photos%20videos)
  for originals gallery-dl doesn't deliver; an end-to-end original-file recovery
  and verification procedure has not been validated here. Recovering originals
  must also bypass any existing archive entries for fallback copies and replace
  those copies as needed. Account permissions and API visibility also limit what
  can be retrieved.
- **This is an additive backup, not exact synchronization.** It does not delete
  local files removed remotely, move folders/files after remote renames, or detect
  replacements retaining the same archive key. Changes to the base directory or
  naming templates do not relocate archived files. The tree reflects paths at the
  time of download; empty albums/folders are not recreated.
- **No integrity audit.** Archive membership or filename existence is enough to
  skip a file; neither checks its contents or size. Keep separate backups and use
  the recovery command for suspect files. An immediate `--abort 1` is not a
  completeness or corruption check.
- **Videos disabled does not mean skipped.** In the reviewed SmugMug extractor,
  `videos: false` selects a still image for a video item. To exclude video items
  entirely, keep `videos: true` and add `"image-filter": "not Image['IsVideo']"`
  under `extractor.smugmug.album`. Downloading stills first can archive the same
  IDs that would later represent videos.
- **Names and duplicates.** Windows-invalid characters are sanitized, so local
  names may differ. Different URL paths can still collide on a case-insensitive
  filesystem. The same image in multiple included albums gets a copy in each;
  the album-based archive intentionally preserves gallery membership.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `401` / OAuth errors | Check the saved credentials; repeat OAuth authorization if access was revoked. |
| Missing private albums or `NotFoundError` / HTTP 404 for the full-site URL | In SmugMug Account Settings, change gallery-dl's Access from Public to Full. Valid tokens can still have insufficient access, and repeating OAuth does not upgrade it. If access is already Full and errors persist, run with `--verbose` to identify the failing request. |
| `429` / rate limit reached | Wait before rerunning; increase `extractor.smugmug.sleep-request` (for example, to `1.0`). |
| Timeouts | Rerun; `extractor.smugmug.timeout` applies to API requests and, by default, media downloads. Increase it if needed. |
| Unexpected layout or exclusions | Use the path preview and `--list-keywords`; check `Album.UrlPath` and the case-sensitive `Album.UrlName`. |
| Files missing locally but normal run skips them | Use the recovery command with `-o archive=null`. |

## Local files

| File/location | Purpose |
| --- | --- |
| `config.json` | Credentials and settings; ignored by Git. |
| `.sync-archive.sqlite3` | Download archive, created by normal runs; back it up with the media. |
| `.gallery-dl.log` | Optional log, only created when requested. |
| `D:/smugmug_backup/` | Current configured media destination. |
| `.gitignore` | Excludes credentials, archives, logs, and a local `mirror/` if used. |

## References

- [gallery-dl configuration](https://gdl-org.github.io/docs/configuration.html)
- [gallery-dl formatting](https://gdl-org.github.io/docs/formatting.html)
- [CLI options](https://codeberg.org/mikf/gallery-dl/blob/master/docs/options.md)
- [SmugMug extractor source](https://codeberg.org/mikf/gallery-dl/blob/master/gallery_dl/extractor/smugmug.py) — media selection, metadata, traversal, archive IDs
- [SmugMug API v2](https://api.smugmug.com/api/v2/doc)
- [SmugMug OAuth FAQ](https://api.smugmug.com/api/v2/doc/tutorial/oauth/faq.html) — changing an existing grant's access level
