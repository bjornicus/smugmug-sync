# SmugMug → Local Originals Mirror

`smugmug_sync.py` downloads your SmugMug photos and videos into a local folder tree that
mirrors your gallery structure, and **verifies every file by MD5** against what SmugMug
reports. Re-running is incremental.

- **Photos:** the original file you uploaded, from the API's `ArchivedUri`, checked
  against `ArchivedMD5`.
- **Videos:** the largest available rendition (`LargestVideo`), checked against its MD5.
  SmugMug's API does not expose the uploaded video file (see *Why not gallery-dl*).

Album exclusions are by URL name (handy for "collection" albums that duplicate photos
already in other galleries).

## Why not gallery-dl

The previous setup used gallery-dl (config only). Its SmugMug extractor downloads the
`ImageSizeOriginal` ("O") rendition. That is SmugMug's display copy at original size, not
the uploaded file. In November 2026, about 2,200 of ~17,000 photos had an "O" copy that
SmugMug had re-encoded, and about 1,000 of those were also downscaled. Gallery settings
such as "Largest size: 5K" did **not** matter, because the owner's token sees "O" in every
gallery. gallery-dl has no option to download `ArchivedUri`, does not verify checksums, and
its archive would keep skipping the bad copies.

For videos, SmugMug keeps no uploaded file that the API can reach. `ArchivedUri` and
`ArchivedMD5` for a video describe a poster JPG, and `!download` returns the largest
transcode. SmugMug's "download gallery" ZIPs from 2023 contained the same transcodes. Keep
your own copies of original videos.

## Setup

Requires Python 3 (Windows `py` launcher):

```powershell
py -m pip install --user requests-oauthlib
```

### OAuth tokens (first time, or after revoking access)

The script uses gallery-dl's public SmugMug application key, so the easiest way to get a
token pair is gallery-dl's OAuth helper:

```powershell
gallery-dl --config-ignore oauth:smugmug
```

Copy the printed `access-token` / `access-token-secret` into `access_token` /
`access_token_secret` in `config.json` (create it from `config.json.template`; it is
git-ignored). Then in **SmugMug Account Settings → Privacy → Authorized Services**, confirm
gallery-dl's grant has **Access: Full** and **Permissions: Read**. With Public access the
API does not return `ArchivedUri` and every photo fails with "no ArchivedUri".

### config.json

| Key | Meaning |
| --- | --- |
| `base_directory` | Mirror root. Files go to `<base>/<NickName>/<Album UrlPath…>/<stem>_<ImageKey>.<ext>`. |
| `replaced_directory` | Where local files that fail verification are **moved** (never deleted) before the correct file is downloaded. Same relative layout. |
| `exclude_album_url_names` | Album `UrlName`s (case-sensitive URL slugs, not titles) to skip, anywhere in the account. |
| `seed_directories` | Optional folders, such as old SmugMug "download gallery" ZIPs, to search for an exact original (matched by MD5, including entries inside `.zip` files) **before** downloading. Hashed once into `.smugmug-sync-seed.sqlite3`. Use it to recover originals that SmugMug no longer serves byte-for-byte. |
| `download_threads` | Parallel downloads/hashes per album (default 3). |
| `sleep_request` | Pause after each API call, in seconds. Increase it if you hit 429 rate limits. |

## Running

```powershell
py C:/old_D/smugmug-sync/smugmug_sync.py --dry-run   # report only: nothing downloaded/moved/recorded
py C:/old_D/smugmug-sync/smugmug_sync.py             # sync
py C:/old_D/smugmug-sync/smugmug_sync.py --verify    # re-hash every local file (periodic audit)
```

What a run does for each image in each included album:

| Status | Meaning |
| --- | --- |
| `unchanged` | Saved state says this file was already verified, the remote checksum (photos) or `LastUpdated` (videos) is the same, and the local file size matches. Not re-hashed, unless you pass `--verify`. |
| `adopted` | A file already at the target path matches the expected MD5. It is recorded with no download. |
| `renamed` | A local file with the expected MD5 existed under an older name or extension (for example `.m4v` instead of `.mp4`). It was renamed into place with no download. |
| `new` | Downloaded and verified. |
| `replaced` | A local file for this image failed verification, or had an outdated name or extension. It was moved to `replaced_directory` and the verified file was downloaded. |
| `seeded` | The exact original was copied from a `seed_directories` file whose MD5 matches. |
| `served-mismatch` | SmugMug twice served the same bytes that do not match its own recorded MD5. This was seen for some older photos: the pixels are identical but the EXIF has been repacked or stripped. The file is kept as the best available copy and a warning is logged. If the exact original later shows up in a seed directory, run with `--verify` to swap it in. |
| `processing` | SmugMug is still processing the upload. It is picked up on a later run. |
| `error` | Download or verification failed after retries. The run exits non-zero; run it again to retry. |

The script writes these files into `base_directory`:

- `.smugmug-sync.sqlite3`: verified-file state (album key, image key, path, MD5, size).
- `.smugmug-sync.log`: an appended log of every run, including each new or replaced file.
- `.smugmug-sync-orphans.txt`: local files that no current SmugMug image maps to, for
  example because the image was deleted or its album was renamed or moved. They are
  **not** deleted. Review them yourself.

## Limits

- **Additive.** Remote deletions, renames and moves are not applied locally. An album that
  is renamed or moved gets downloaded again at its new path, and the old copy shows up in
  the orphans report.
- **The same image in several albums is stored once per album.** That is why exclusions
  exist.
- **Videos are SmugMug transcodes**, not the files you uploaded (see above).
- **Excluding an album does not move or delete files** that were already downloaded from it.
