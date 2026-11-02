#!/usr/bin/env python3
"""SmugMug -> local mirror of ORIGINAL photos (MD5-verified) and best-available videos.

    py smugmug_sync.py                 # sync (incremental)
    py smugmug_sync.py --dry-run       # show what would happen; changes nothing
    py smugmug_sync.py --verify        # re-hash every local file instead of trusting state

Photos are downloaded from the image's ArchivedUri (the file you uploaded) and
checked against SmugMug's ArchivedMD5. Videos: SmugMug does not expose the
uploaded video file through its API, so the largest transcode (LargestVideo) is
downloaded and checked against its MD5.

Layout (compatible with the earlier gallery-dl setup):
    <base>/<NickName>/<Album UrlPath...>/<FileName stem>_<ImageKey>.<ext>

A local file that does not match the expected MD5 (e.g. an older non-original
download, or a photo replaced on SmugMug) is MOVED to `replaced_directory`,
never deleted, and the correct file is downloaded. Nothing is deleted locally
when it is removed from SmugMug; such files are listed in the orphans report.
"""
import argparse, hashlib, json, os, shutil, sqlite3, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

try:
    from requests_oauthlib import OAuth1Session
except ImportError:
    sys.exit("Missing dependency: py -m pip install --user requests-oauthlib")

# gallery-dl's public SmugMug application key; tokens from `gallery-dl oauth:smugmug` work with it.
API_KEY = "RCVHDGjcbc4Fhzq4qzqLdZmvwmwB6LM2"
API_SECRET = "jGrdndvJqhTx8XSNs7TFTSSthhZHq92ddMpbpDpkDVNM7TDgnvLFMtfB5Mg5kH73"
API = "https://api.smugmug.com"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_NAME = ".smugmug-sync.sqlite3"


# ---------------------------------------------------------------- helpers

def clean(seg):
    """Windows-safe path segment (same rules gallery-dl used)."""
    seg = "".join("_" if c in '\\/:*?"<>|' else c for c in seg if ord(c) >= 32 and c != "\x7f")
    return seg.rstrip(". ") or "_"


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.local = threading.local()

    @property
    def s(self):
        if not hasattr(self.local, "s"):
            s = OAuth1Session(API_KEY, API_SECRET, self.cfg["access_token"], self.cfg["access_token_secret"])
            s.headers["Accept"] = "application/json"
            self.local.s = s
        return self.local.s

    def get(self, path, **params):
        params.setdefault("_verbosity", "1")
        for attempt in range(6):
            try:
                r = self.s.get(API + path, params=params, timeout=60)
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(5 * (attempt + 1)); continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(10 * (attempt + 1)); continue
            if r.status_code == 401:
                sys.exit("401 Unauthorized: re-run OAuth (see README) and update the tokens in config.json")
            r.raise_for_status()
            time.sleep(self.cfg.get("sleep_request", 0.2))
            return r.json()["Response"]
        r.raise_for_status()

    def paged(self, path, **params):
        start = 1
        while True:
            resp = self.get(path, start=start, count=100, **params)
            yield from resp.get(resp.get("Locator"), []) or []
            if "NextPage" not in resp.get("Pages", {}):
                return
            start += 100

    def download(self, url, dest, expect_md5, expect_size):
        """Download and verify. Returns (md5, size, verified). If SmugMug serves the same
        non-matching bytes twice in a row, the file is kept with verified=False (it is
        the best SmugMug will provide; seen for some older photos whose served copy has
        repacked/stripped EXIF but identical pixels)."""
        part = dest + ".part"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        last = prev_bad = None
        for attempt in range(4):
            try:
                h = hashlib.md5()
                with self.s.get(url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(part, "wb") as f:
                        for b in r.iter_content(1 << 20):
                            f.write(b); h.update(b)
                got = h.hexdigest()
                size = os.path.getsize(part)
                if (expect_md5 and got != expect_md5) or (expect_size and size != expect_size):
                    last = f"checksum mismatch (got md5 {got} size {size}, expected {expect_md5} {expect_size})"
                    if got == prev_bad:
                        os.replace(part, dest)
                        return got, size, False
                    prev_bad = got
                    time.sleep(3); continue
                os.replace(part, dest)
                return got, size, True
            except Exception as e:
                last = repr(e); time.sleep(5 * (attempt + 1))
        if os.path.exists(part):
            os.remove(part)
        raise RuntimeError(f"download failed: {last}")


# ---------------------------------------------------------------- seed

class Seed:
    """Optional local sources of originals (e.g. old SmugMug 'download gallery' ZIPs).

    Files (and entries inside .zip files) under `seed_directories` are hashed once into
    <base>/.smugmug-sync-seed.sqlite3; changed/new files are re-hashed on later runs.
    Before downloading, a file whose MD5 equals SmugMug's expected MD5 is copied from
    here instead. This recovers exact originals that SmugMug no longer serves
    byte-for-byte. The index is built lazily, only when a download is needed.
    """

    def __init__(self, base, dirs, say):
        self.dirs = [os.path.normpath(d) for d in dirs]
        self.dbpath = os.path.join(base, ".smugmug-sync-seed.sqlite3")
        self.say = say
        self.lock = threading.Lock()
        self.by_md5 = None

    def _build(self):
        db = sqlite3.connect(self.dbpath)
        db.execute("create table if not exists seed(file text, entry text, size integer, mtime real, md5 text,"
                   " primary key(file, entry))")
        known = {(f, e): (s, m) for f, e, s, m in db.execute("select file, entry, size, mtime from seed")}
        todo = []
        for root in self.dirs:
            for dp, dn, fn in os.walk(root):
                for n in fn:
                    p = os.path.join(dp, n); st = os.stat(p)
                    if n.lower().endswith(".zip"):
                        if all(known.get((p, e)) == (s, st.st_mtime) for e, s in self._zip_entries(p)):
                            continue
                        db.execute("delete from seed where file=?", (p,))
                        todo += [(p, e, s, st.st_mtime) for e, s in self._zip_entries(p)]
                    elif known.get((p, "")) != (st.st_size, st.st_mtime):
                        todo.append((p, "", st.st_size, st.st_mtime))
        if todo:
            self.say(f"  seed index: hashing {len(todo)} files (one-time; later runs reuse it)")
        for i, (p, e, s, m) in enumerate(todo, 1):
            try:
                with self._open(p, e) as f:
                    h = hashlib.md5()
                    for b in iter(lambda: f.read(4 << 20), b""):
                        h.update(b)
                db.execute("insert or replace into seed values (?,?,?,?,?)", (p, e, s, m, h.hexdigest()))
            except Exception as ex:
                self.say(f"  seed index: cannot read {p} {e}: {ex!r}")
            if i % 1000 == 0:
                db.commit(); self.say(f"  seed index: {i}/{len(todo)}")
        db.commit()
        self.by_md5 = {md5: (f, e) for f, e, md5 in db.execute("select file, entry, md5 from seed")}
        db.close()

    @staticmethod
    def _zip_entries(p):
        import zipfile
        try:
            with zipfile.ZipFile(p) as z:
                return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
        except Exception:
            return []

    @staticmethod
    def _open(p, entry):
        if not entry:
            return open(p, "rb")
        import zipfile
        z = zipfile.ZipFile(p)
        f = z.open(entry)
        close = f.close
        f.close = lambda: (close(), z.close())
        return f

    def find(self, md5):
        if not self.dirs:
            return None
        with self.lock:
            if self.by_md5 is None:
                self._build()
        return self.by_md5.get(md5)

    def extract(self, src, dest, md5):
        part = dest + ".part"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        h = hashlib.md5()
        with self._open(*src) as f, open(part, "wb") as out:
            for b in iter(lambda: f.read(4 << 20), b""):
                out.write(b); h.update(b)
        if h.hexdigest() != md5:
            os.remove(part)
            raise RuntimeError(f"seed file changed since indexing: {src}")
        os.replace(part, dest)


# ---------------------------------------------------------------- sync

class Sync:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.api = Api(cfg)
        self.base = os.path.normpath(cfg["base_directory"])
        self.replaced = os.path.normpath(cfg["replaced_directory"])
        self.exclude = set(cfg.get("exclude_album_url_names", []))
        os.makedirs(self.base, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(self.base, STATE_NAME))
        self.db.execute("""create table if not exists files(
            album_key text, image_key text, rel_path text, kind text, md5 text, size integer,
            remote_updated text, synced_at text, primary key(album_key, image_key))""")
        cols = [r[1] for r in self.db.execute("pragma table_info(files)")]
        if "remote_md5" not in cols:  # MD5 SmugMug reported when stored (differs from md5 for served-mismatch)
            self.db.execute("alter table files add column remote_md5 text")
            self.db.execute("update files set remote_md5 = md5")
            self.db.commit()
        self.seed = Seed(self.base, cfg.get("seed_directories", []), self.say)
        self.totals = {}
        self.errors = []
        self.seen_paths = set()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log = open(os.path.join(self.base, ".smugmug-sync.log"), "a", encoding="utf-8")
        self.say(f"=== {stamp} {'DRY RUN ' if args.dry_run else ''}sync start")

    def say(self, msg, console=True):
        if console:
            print(msg, flush=True)
        self.log.write(msg + "\n"); self.log.flush()

    def quarantine(self, path):
        rel = os.path.relpath(path, self.base)
        dest = os.path.join(self.replaced, rel)
        n = 1
        while os.path.exists(dest):
            root, ext = os.path.splitext(os.path.join(self.replaced, rel))
            dest = f"{root}.{n}{ext}"; n += 1
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)
        self.say(f"  moved aside: {rel} -> {dest}", console=False)

    def resolve(self, img):
        """Return (kind, url, md5, size) for what we should store."""
        if img.get("IsVideo"):
            lv = self.api.get(f"/api/v2/image/{img['ImageKey']}-0!largestvideo")["LargestVideo"]
            if not lv.get("Usable", True) or not lv.get("Url"):
                raise RuntimeError("video not usable yet (still processing?)")
            return "video-largest", lv["Url"], lv.get("MD5"), lv.get("Size")
        if not img.get("ArchivedUri"):
            raise RuntimeError("no ArchivedUri (token lacks Full access?)")
        return "original", img["ArchivedUri"], img.get("ArchivedMD5"), img.get("ArchivedSize")

    def process(self, album, adir, rel_dir, img, listing, st):
        key = img["ImageKey"]
        kind, url, md5, size = self.resolve(img)
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        stem = (img.get("FileName") or "").rsplit(".", 1)[0]  # empty FileName -> "_KEY.ext" (gallery-dl compatible)
        name = clean(f"{stem}_{key}.{ext}")
        path = os.path.join(adir, name)
        rel = os.path.join(rel_dir, name)
        status = None
        have = os.path.exists(path) and md5_file(path) == md5
        # other local files for this image key (old name/extension, e.g. .m4v vs .mp4)
        for other in listing:
            if other == name or other.endswith(".part") or not other.rsplit(".", 1)[0].endswith("_" + key):
                continue
            op = os.path.join(adir, other)
            if not have and os.path.exists(op) and md5_file(op) == md5:
                if not self.args.dry_run:
                    if os.path.exists(path):
                        self.quarantine(path)
                    os.replace(op, path)
                    self.say(f"  renamed: {os.path.join(rel_dir, other)} -> {name}", console=False)
                have, status = True, "renamed"
            else:
                if not self.args.dry_run:
                    self.quarantine(op)
                status = status or "replaced"
        if have:
            return (status if status == "renamed" else "adopted"), kind, rel, md5, (
                size if self.args.dry_run and not os.path.exists(path) else os.path.getsize(path)), md5
        if os.path.exists(path):
            # previously accepted served-mismatch copy, SmugMug side unchanged -> keep it
            if (st and st[2].endswith("served-mismatch") and st[6] == md5 and st[1] == rel
                    and md5_file(path) == st[3] and not self.seed.find(md5)):
                return "adopted", st[2], rel, st[3], os.path.getsize(path), md5
            if not self.args.dry_run:
                self.quarantine(path)
            status = "replaced"
        seed = self.seed.find(md5) if md5 else None
        if self.args.dry_run:
            return "would-" + ("seed" if seed else status or "new"), kind, rel, md5, size, md5
        if seed:
            self.seed.extract(seed, path, md5)
            self.say(f"  seeded: {rel} <- {seed[0]}{'!' + seed[1] if seed[1] else ''}", console=False)
            return "seeded", kind, rel, md5, os.path.getsize(path), md5
        got, sz, verified = self.api.download(url, path, md5, size)
        if not verified:
            self.say(f"  WARNING served-mismatch (kept best available): {rel} got {got}, SmugMug reports {md5}")
            return "served-mismatch", kind + "/served-mismatch", rel, got, sz, md5
        return (status or "new"), kind, rel, got, sz, md5

    def run(self):
        me = self.api.get("/api/v2!authuser")["User"]
        nick = me["NickName"]
        albums = list(self.api.paged(f"/api/v2/user/{nick}!albums"))
        self.say(f"user {nick}: {len(albums)} albums ({len(self.exclude)} excluded names)")
        threads = int(self.cfg.get("download_threads", 3))
        for i, album in enumerate(albums, 1):
            if album.get("UrlName") in self.exclude:
                continue
            ak = album["AlbumKey"]
            parts = [clean(p) for p in album["UrlPath"].strip("/").split("/") if p]
            rel_dir = os.path.join(clean(nick), *parts)
            adir = os.path.join(self.base, rel_dir)
            listing = os.listdir(adir) if os.path.isdir(adir) else []
            state = {r[0]: r for r in self.db.execute(
                "select image_key, rel_path, kind, md5, size, remote_updated, remote_md5 from files where album_key=?", (ak,))}
            counts, work = {}, []
            for img in self.api.paged(f"/api/v2/album/{ak}!images"):
                key = img.get("ImageKey")
                if not key:
                    continue
                if img.get("Processing"):
                    counts["processing"] = counts.get("processing", 0) + 1; continue
                st = state.get(key)
                if st and not self.args.verify:
                    p = os.path.join(self.base, st[1])
                    same = (st[5] == img.get("LastUpdated")) if img.get("IsVideo") else (st[6] == img.get("ArchivedMD5"))
                    if same and os.path.exists(p) and os.path.getsize(p) == st[4]:
                        self.seen_paths.add(os.path.normcase(p))
                        counts["unchanged"] = counts.get("unchanged", 0) + 1; continue
                work.append(img)

            def job(img):
                try:
                    return img, self.process(album, adir, rel_dir, img, listing, state.get(img["ImageKey"])), None
                except Exception as e:
                    return img, None, e

            with ThreadPoolExecutor(threads) as ex:
                for img, res, err in ex.map(job, work):
                    if err:
                        counts["error"] = counts.get("error", 0) + 1
                        msg = f"ERROR {album['UrlPath']} {img.get('FileName')} ({img['ImageKey']}): {err}"
                        self.errors.append(msg); self.say("  " + msg)
                        continue
                    status, kind, rel, md5, size, remote_md5 = res
                    counts[status] = counts.get(status, 0) + 1
                    self.seen_paths.add(os.path.normcase(os.path.join(self.base, rel)))
                    if status in ("new", "replaced"):
                        self.say(f"  {status}: {rel}", console=False)
                    if not self.args.dry_run:
                        self.db.execute("insert or replace into files(album_key, image_key, rel_path, kind, md5, size,"
                                        " remote_updated, synced_at, remote_md5) values (?,?,?,?,?,?,?,?,?)",
                                        (ak, img["ImageKey"], rel, kind, md5, size, img.get("LastUpdated"),
                                         datetime.now().isoformat(timespec="seconds"), remote_md5))
            self.db.commit()
            for k, v in counts.items():
                self.totals[k] = self.totals.get(k, 0) + v
            interesting = {k: v for k, v in counts.items() if k != "unchanged"}
            if interesting or self.args.verbose:
                self.say(f"[{i}/{len(albums)}] {album['UrlPath']}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        self.report_orphans(nick)
        self.say("TOTAL: " + ", ".join(f"{k} {v}" for k, v in sorted(self.totals.items())))
        if self.errors:
            self.say(f"{len(self.errors)} errors; re-run to retry.")
        return 1 if self.errors else 0

    def report_orphans(self, nick):
        """Local files not matched by any current SmugMug image (removed/renamed remotely)."""
        if self.totals.get("error") or self.args.dry_run:
            return  # incomplete picture (or files not yet renamed/moved); skip to avoid false orphans
        root = os.path.join(self.base, clean(nick))
        orphans = []
        for dp, dn, fn in os.walk(root):
            for n in fn:
                p = os.path.normcase(os.path.join(dp, n))
                if p not in self.seen_paths and not n.endswith(".part"):
                    orphans.append(os.path.relpath(os.path.join(dp, n), self.base))
        out = os.path.join(self.base, ".smugmug-sync-orphans.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(orphans)))
        self.say(f"local files not on SmugMug (kept, not deleted): {len(orphans)} -> {out}")


def load_config(path):
    cfg = json.load(open(path, encoding="utf-8"))
    for k in ("access_token", "access_token_secret", "base_directory", "replaced_directory"):
        if not cfg.get(k):
            sys.exit(f"config.json: '{k}' is required (see config.json.template)")
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="report only; no downloads, moves, or state changes")
    ap.add_argument("--verify", action="store_true", help="re-hash all local files instead of trusting saved state")
    ap.add_argument("--verbose", action="store_true", help="print every album, not just ones with changes")
    args = ap.parse_args()
    sys.exit(Sync(load_config(args.config), args).run())


if __name__ == "__main__":
    main()
