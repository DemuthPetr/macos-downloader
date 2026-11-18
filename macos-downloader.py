#!/usr/bin/env python3
"""
macos-downloader - TUI for downloading full macOS / OS X installers from Apple's CDN.

Sources:
  * Apple's software update catalog (swscan.apple.com) for High Sierra and newer.
    Only the newest release of each major version is listed.
  * The fixed DMG links Apple publishes on its "How to download macOS" support
    page for Lion, Mountain Lion, Yosemite, El Capitan and Sierra.

Only the Python 3 standard library is used, so it runs on macOS and Linux.

  ./macos-downloader.py                 # interactive TUI
  ./macos-downloader.py --list          # print what is available
  ./macos-downloader.py --all           # download everything, no TUI
  ./macos-downloader.py -d ~/Installers # choose the output directory
"""

import argparse
import curses
import errno
import http.client
import os
import plistlib
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CATALOG_SUFFIX = (
    "15-14-13-12-10.16-10.15-10.14-10.13-10.12-10.11-10.10-10.9"
    "-mountainlion-lion-snowleopard-leopard.merged-1.sucatalog"
)
CATALOG_BASE = "https://swscan.apple.com/content/catalogs/others/index-"
# Newest first; the first one Apple serves wins.
CATALOG_URLS = [
    CATALOG_BASE + "27-26-" + CATALOG_SUFFIX,
    CATALOG_BASE + "26-" + CATALOG_SUFFIX,
    CATALOG_BASE + CATALOG_SUFFIX,
]

LEGACY = [
    ("Mac OS X Lion", "10.7.5", "11G63", 4720237409,
     "http://updates-http.cdn-apple.com/2021/macos/041-7683-20210614-E610947E-C7CE-46EB-8860-D26D71F0D3EA/InstallMacOSX.dmg"),
    ("OS X Mountain Lion", "10.8.5", "12F45", 4449317520,
     "http://updates-http.cdn-apple.com/2021/macos/031-0627-20210614-90D11F33-1A65-42DD-BBEA-E1D9F43A6B3F/InstallMacOSX.dmg"),
    ("OS X Yosemite", "10.10.5", "14F27", 5718074248,
     "http://updates-http.cdn-apple.com/2019/cert/061-41343-20191023-02465f92-3ab5-4c92-bfe2-b725447a070d/InstallMacOSX.dmg"),
    ("OS X El Capitan", "10.11.6", "15G31", 6204629298,
     "http://updates-http.cdn-apple.com/2019/cert/061-41424-20191024-218af9ec-cf50-4516-9011-228c78eda3d2/InstallMacOSX.dmg"),
    ("macOS Sierra", "10.12.6", "16G29", 5007882126,
     "http://updates-http.cdn-apple.com/2019/cert/061-39476-20191023-48f365f4-0015-4c41-9f44-39d3d2aca067/InstallOS.dmg"),
]

USER_AGENT = "Software%20Update (unknown version) CFNetwork/807.0.1 Darwin/16.0.0 (x86_64)"
CHUNK = 1 << 20
MAX_RETRIES = 20      # consecutive failed reconnects before giving up on a file
RETRY_MAX_DELAY = 60  # seconds


class Release:
    def __init__(self, title, version, build, files, date=""):
        self.title = title
        self.version = version
        self.build = build
        self.files = files  # list of (url, size)
        self.date = date
        self.selected = False
        self.status = ""

    @property
    def size(self):
        return sum(s for _, s in self.files)

    @property
    def label(self):
        return f"{self.title} {self.version} ({self.build})"

    @property
    def folder(self):
        return re.sub(r"[/:]", "-", self.label)

    def version_key(self):
        return tuple(int(x) if x.isdigit() else 0 for x in self.version.split("."))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000


def parse_dist(text):
    """Pull title, version and build out of a product's .dist file."""
    title = None
    m = re.search(r"<title>([^<]+)</title>", text)
    if m:
        title = m.group(1).strip()
    if not title or title == "SU_TITLE":
        m = re.search(r'"SU_TITLE"\s*=\s*"([^"]+)"', text)
        title = m.group(1) if m else "macOS"
    vals = {}
    for key in ("VERSION", "BUILD"):
        m = re.search(rf"<key>{key}</key>\s*<string>([^<]+)</string>", text)
        vals[key] = m.group(1).strip() if m else "?"
    return title, vals["VERSION"], vals["BUILD"]


def load_catalog(log=print):
    for url in CATALOG_URLS:
        try:
            log(f"Fetching catalog {url.split('/')[-1][:40]}...")
            return plistlib.loads(fetch(url))
        except Exception:
            continue
    raise RuntimeError("could not download any Apple software update catalog")


def catalog_releases(log=print):
    catalog = load_catalog(log)
    products = [
        (pid, p) for pid, p in catalog.get("Products", {}).items()
        if "InstallAssistantPackageIdentifiers" in p.get("ExtendedMetaInfo", {})
    ]
    log(f"Reading {len(products)} installer descriptions...")

    def describe(item):
        pid, p = item
        dists = p.get("Distributions", {})
        dist_url = dists.get("English") or dists.get("en")
        if not dist_url:
            return None
        try:
            title, version, build = parse_dist(fetch(dist_url).decode("utf-8", "replace"))
        except Exception:
            return None
        pkgs = [(x["URL"], x.get("Size", 0)) for x in p.get("Packages", [])]
        # Big Sur and later ship as one self-contained InstallAssistant.pkg;
        # older releases need every package to rebuild the installer app.
        full = [f for f in pkgs if f[0].endswith("/InstallAssistant.pkg")]
        date = p.get("PostDate")
        return Release(title, version, build, full or pkgs,
                       date.strftime("%Y-%m-%d") if date else "")

    with ThreadPoolExecutor(max_workers=16) as pool:
        releases = [r for r in pool.map(describe, products) if r]

    latest = {}
    for r in releases:
        if "beta" in r.title.lower():
            continue
        best = latest.get(r.title)
        if best is None or (r.version_key(), r.date) > (best.version_key(), best.date):
            latest[r.title] = r
    return list(latest.values())


def all_releases(log=print):
    releases = [Release(t, v, b, [(u, s)]) for t, v, b, s, u in LEGACY]
    releases += catalog_releases(log)
    releases.sort(key=lambda r: r.version_key(), reverse=True)
    return releases


class Downloader:
    """Downloads releases one after another with resumable .part files."""

    def __init__(self, releases, outdir):
        self.releases = releases
        self.outdir = outdir
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.current = None
        self.file_name = ""
        self.file_done = self.file_size = 0
        self.rel_done = 0
        self.speed = 0.0
        self.note = ""
        self.finished = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        for r in self.releases:
            if self.cancel.is_set():
                break
            with self.lock:
                self.current, self.rel_done = r, 0
                r.status = "downloading"
            try:
                folder = os.path.join(self.outdir, r.folder)
                os.makedirs(folder, exist_ok=True)
                for url, size in r.files:
                    self.get_file(url, size, folder)
                    self.rel_done += size
                    if self.cancel.is_set():
                        break
                r.status = "cancelled" if self.cancel.is_set() else "done"
            except Exception as e:
                r.status = f"error: {e}"
        with self.lock:
            self.current = None
            self.finished = True

    def get_file(self, url, size, folder):
        name = url.rsplit("/", 1)[-1]
        dest = os.path.join(folder, name)
        part = dest + ".part"
        with self.lock:
            self.file_name, self.file_size, self.file_done = name, size, 0
        if os.path.exists(dest) and (not size or os.path.getsize(dest) == size):
            self.file_done = size
            return
        def on_disk():
            return os.path.getsize(part) if os.path.exists(part) else 0

        failures = 0
        while not self.cancel.is_set():
            before = on_disk()
            try:
                self.fetch_part(url, part, size)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise
                err = e
            except (OSError, http.client.HTTPException) as e:
                if getattr(e, "errno", None) == errno.ENOSPC:
                    raise
                err = e
            # Only consecutive failures without progress count towards giving up.
            failures = 1 if on_disk() > before else failures + 1
            if failures > MAX_RETRIES:
                raise err
            delay = min(RETRY_MAX_DELAY, 2 ** failures)
            for left in range(delay, 0, -1):
                self.note = (f"connection lost ({err}); reconnecting in {left}s "
                             f"[{failures}/{MAX_RETRIES}]")
                if self.cancel.wait(1):
                    break
            self.speed = 0.0
        self.note = ""
        if self.cancel.is_set():
            return
        os.replace(part, dest)

    def fetch_part(self, url, part, size):
        """Download into the .part file, continuing from whatever it already holds."""
        name = os.path.basename(part)[:-5]
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if size and have > size:
            os.remove(part)
            have = 0
        self.file_done = have
        if size and have == size:
            return
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            if have and resp.status != 206:
                have = 0  # server ignored the range; start over
            if not size:
                size = have + int(resp.headers.get("Content-Length", 0))
                self.file_size = size
            self.file_done = have
            window_start, window_bytes = time.monotonic(), 0
            with open(part, "ab" if have else "wb") as f:
                while not self.cancel.is_set():
                    # read1 returns what has arrived, so little is lost if the link drops.
                    chunk = resp.read1(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    self.file_done += len(chunk)
                    window_bytes += len(chunk)
                    elapsed = time.monotonic() - window_start
                    if elapsed >= 1:
                        self.speed = window_bytes / elapsed
                        window_start, window_bytes = time.monotonic(), 0
        if not self.cancel.is_set() and size and os.path.getsize(part) < size:
            raise ConnectionError(f"{name}: connection closed early")


# ---------------------------------------------------------------- TUI

def bar(frac, width):
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            win.addnstr(y, x, text, w - x - 1, attr)
        except curses.error:
            pass


def loading_screen(scr):
    def log(msg):
        scr.erase()
        put(scr, 1, 2, "macOS downloader", curses.A_BOLD)
        put(scr, 3, 2, msg)
        scr.refresh()
    return all_releases(log)


def select_screen(scr, releases, outdir):
    pos, top = 0, 0
    while True:
        h, w = scr.getmaxyx()
        rows = max(1, h - 7)
        top = min(max(top, pos - rows + 1), pos)
        scr.erase()
        put(scr, 0, 2, "macOS downloader — latest release of each version", curses.A_BOLD)
        put(scr, 1, 2, f"Output: {outdir}", curses.A_DIM)
        for i, r in enumerate(releases[top:top + rows]):
            idx = top + i
            mark = "[x]" if r.selected else "[ ]"
            line = f"{mark} {r.label:<42} {human(r.size):>10}  {r.date}"
            put(scr, 3 + i, 2, line, curses.A_REVERSE if idx == pos else 0)
        chosen = [r for r in releases if r.selected]
        put(scr, h - 3, 2, f"Selected: {len(chosen)}  ({human(sum(r.size for r in chosen))})")
        put(scr, h - 2, 2, "↑/↓ move  space toggle  a all/none  enter download  q quit", curses.A_DIM)
        scr.refresh()

        key = scr.getch()
        if key in (curses.KEY_UP, ord("k")):
            pos = max(0, pos - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            pos = min(len(releases) - 1, pos + 1)
        elif key == ord(" "):
            releases[pos].selected = not releases[pos].selected
            pos = min(len(releases) - 1, pos + 1)
        elif key == ord("a"):
            state = not all(r.selected for r in releases)
            for r in releases:
                r.selected = state
        elif key in (10, 13, curses.KEY_ENTER):
            if chosen:
                return chosen
        elif key in (ord("q"), 27):
            return []


def progress_screen(scr, chosen, outdir):
    dl = Downloader(chosen, outdir)
    dl.start()
    scr.timeout(250)
    confirm_quit = False
    while True:
        h, w = scr.getmaxyx()
        width = max(10, w - 30)
        scr.erase()
        put(scr, 0, 2, "Downloading to " + outdir, curses.A_BOLD)
        with dl.lock:
            cur = dl.current
            fdone, fsize = dl.file_done, dl.file_size
        for i, r in enumerate(chosen):
            status = r.status or "queued"
            put(scr, 2 + i, 2, f"{r.label:<42} {status}",
                curses.A_BOLD if r is cur else 0)
        y = 3 + len(chosen)
        if cur:
            total = cur.size or 1
            done = dl.rel_done + fdone
            eta = (total - done) / dl.speed if dl.speed else 0
            put(scr, y, 2, cur.label, curses.A_BOLD)
            put(scr, y + 1, 2, f"{bar(done / total, width)} {done * 100 // total:3d}%")
            put(scr, y + 2, 2, f"{dl.file_name}: {human(fdone)} / {human(fsize)}"
                               f"   {human(dl.speed)}/s   ETA {int(eta // 60)}m{int(eta % 60):02d}s")
            if dl.note:
                put(scr, y + 3, 2, dl.note, curses.A_BOLD)
        if dl.finished:
            put(scr, h - 2, 2, "Finished. Press any key to exit.", curses.A_BOLD)
        elif confirm_quit:
            put(scr, h - 2, 2, "Cancel downloads? Partial files are kept for resuming. (y/n)", curses.A_BOLD)
        else:
            put(scr, h - 2, 2, "q cancel", curses.A_DIM)
        scr.refresh()

        key = scr.getch()
        if dl.finished and key != -1:
            return
        if confirm_quit and key == ord("y"):
            dl.cancel.set()
            dl.thread.join()
            return
        if key in (ord("q"), 27):
            confirm_quit = True
        elif key != -1:
            confirm_quit = False


def tui(scr, outdir):
    curses.curs_set(0)
    releases = loading_screen(scr)
    chosen = select_screen(scr, releases, outdir)
    if chosen:
        progress_screen(scr, chosen, outdir)
    return chosen


# ---------------------------------------------------------------- CLI

def plain_download(releases, outdir):
    dl = Downloader(releases, outdir)
    dl.start()
    try:
        while not dl.finished:
            time.sleep(1)
            if dl.current:
                done = dl.rel_done + dl.file_done
                pct = done * 100 // (dl.current.size or 1)
                extra = f"  {dl.note}" if dl.note else ""
                print(f"\r\033[K{dl.current.label}: {pct:3d}%  {human(dl.speed)}/s{extra}",
                      end="", flush=True)
    except KeyboardInterrupt:
        dl.cancel.set()
        dl.thread.join()
    print()
    for r in releases:
        print(f"{r.label}: {r.status}")


def main():
    ap = argparse.ArgumentParser(description="Download full macOS installers from Apple's CDN.")
    ap.add_argument("-d", "--dir", default="macOS-installers", help="output directory")
    ap.add_argument("--list", action="store_true", help="list available releases and URLs")
    ap.add_argument("--all", action="store_true", help="download every release without the TUI")
    args = ap.parse_args()
    outdir = os.path.abspath(os.path.expanduser(args.dir))

    if args.list or args.all:
        releases = all_releases(lambda m: print(m, file=sys.stderr))
        if args.list:
            for r in releases:
                print(f"{r.label}  {human(r.size)}")
                for url, _ in r.files:
                    print(f"    {url}")
        else:
            plain_download(releases, outdir)
        return

    chosen = curses.wrapper(tui, outdir)
    for r in chosen:
        print(f"{r.label}: {r.status} -> {os.path.join(outdir, r.folder)}")


if __name__ == "__main__":
    main()
