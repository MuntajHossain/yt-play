"""Fetch the vendored libmpv Windows build into mpv-lib/.

mpv-lib/ is gitignored (libmpv-2.dll is ~112MB, over GitHub's 100MB file
limit — see CLAUDE.md "Windows-specific notes"), so it isn't checked into
git and must be pulled separately after cloning. Run this directly:

    uv run setup_mpv.py

main.py also calls fetch_mpv_lib() automatically on startup if mpv-lib/
is missing, so a plain `uv run main.py` on a fresh clone pulls it too.

Source: the community Windows builds linked from mpv.io's own download
page (https://mpv.io/installation/), hosted on SourceForge.

Extraction is pure Python — no shelling out to tar/7z/anything external.
py7zr handles the 7z container format and the LZMA/LZMA2 substreams, but
it can't decode the BCJ2 filter these archives use on the main DLL (a
long-standing py7zr gap: "BCJ2 filter is not supported by py7zr"), so
_bcj2_decode() below implements that one filter from the public-domain
7-Zip SDK algorithm. Verified bit-for-bit against a reference extraction.
"""
import concurrent.futures
import io
import os
import re
import sys
import tempfile
import threading
import urllib.request
import xml.etree.ElementTree as ET

import py7zr
from py7zr.compressor import SevenZipDecompressor

MPV_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpv-lib")
REQUIRED_FILE = os.path.join(MPV_LIB_DIR, "libmpv-2.dll")

RSS_URL = "https://sourceforge.net/projects/mpv-player-windows/rss?path=/libmpv"
# Plain x86_64 build, not the "-v3" variant (that one requires an AVX2 CPU).
BUILD_NAME_RE = re.compile(r"^/libmpv/mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$")

# SourceForge round-robins each request to a mirror (different host per
# request) but serves identical bytes and honors Range requests on all of
# them — verified with a 6-way concurrent Range-request test against every
# chunk landing on a different mirror, sha256 matched a plain download.
DOWNLOAD_THREADS = 6
MIN_CHUNKED_SIZE = 2 * 1024 * 1024  # below this, threading overhead isn't worth it

BCJ2_METHOD = b"\x03\x03\x01\x1b"


def _find_latest_build_url() -> str:
    with urllib.request.urlopen(RSS_URL, timeout=30) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if BUILD_NAME_RE.match(title):
            link = item.findtext("link")
            if link:
                return link.strip()
    raise RuntimeError(f"No matching libmpv build found in RSS feed at {RSS_URL}")


def _content_length(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return int(resp.headers.get("Content-Length", 0))


def _download_single(url: str, dest: str) -> None:
    with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            read += len(chunk)
            if total:
                print(f"\r  {read / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()


def _download_range(url: str, dest: str, start: int, end: int, progress: list, lock: threading.Lock) -> None:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        if resp.status != 206:
            raise RuntimeError(f"Server did not honor Range request (status={resp.status})")
        with open(dest, "r+b") as f:
            f.seek(start)
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                with lock:
                    progress[0] += len(chunk)
                    read, total = progress
                    print(f"\r  {read / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)


def _download_chunked(url: str, dest: str, total: int) -> None:
    with open(dest, "wb") as f:
        f.truncate(total)

    chunk_size = -(-total // DOWNLOAD_THREADS)  # ceil division
    ranges = [
        (start, min(start + chunk_size, total) - 1)
        for start in range(0, total, chunk_size)
    ]

    progress = [0, total]
    lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = [pool.submit(_download_range, url, dest, s, e, progress, lock) for s, e in ranges]
        for future in concurrent.futures.as_completed(futures):
            future.result()  # re-raise any chunk's exception here
    print()


def _download(url: str, dest: str) -> None:
    print(f"Downloading {url}")
    total = _content_length(url)
    if total < MIN_CHUNKED_SIZE:
        _download_single(url, dest)
        return
    try:
        _download_chunked(url, dest, total)
    except Exception as e:
        print(f"\nChunked download failed ({e}), retrying single-threaded...")
        _download_single(url, dest)


# ---------------------------------------------------------------------------
# Pure-Python 7z extraction (py7zr for the container + LZMA/LZMA2, our own
# BCJ2 filter on top — see module docstring).
# ---------------------------------------------------------------------------

class _Bcj2RangeDecoder:
    """The binary range decoder BCJ2 uses to encode its per-opcode
    convert/don't-convert decisions. Same construction as LZMA's range
    coder: 11-bit adaptive probabilities, 258 contexts (256 indexed by the
    byte preceding an 0xE8 CALL, plus one each for 0xE9 JMP and 0x0F 8x
    Jcc)."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 5  # byte 0 is always 0 and ignored; bytes 1-4 seed `code`
        self.code = int.from_bytes(data[1:5], "big")
        self.range = 0xFFFFFFFF
        self.probs = [1024] * 258  # kBitModelTotal(2048) / 2

    def _next_byte(self) -> int:
        if self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            return b
        return 0

    def decode_bit(self, index: int) -> int:
        prob = self.probs[index]
        bound = (self.range >> 11) * prob
        if self.code < bound:
            self.range = bound
            self.probs[index] = prob + ((2048 - prob) >> 5)
            bit = 0
        else:
            self.range = (self.range - bound) & 0xFFFFFFFF
            self.code = (self.code - bound) & 0xFFFFFFFF
            self.probs[index] = prob - (prob >> 5)
            bit = 1
        if self.range < (1 << 24):
            self.range = (self.range << 8) & 0xFFFFFFFF
            self.code = ((self.code << 8) | self._next_byte()) & 0xFFFFFFFF
        return bit


def _bcj2_decode(main: bytes, call: bytes, jump: bytes, rc: bytes, out_size: int) -> bytes:
    """Reverse the BCJ2 x86 CALL/JMP/Jcc filter: reconstruct absolute-address
    4-byte fields (stored in `call`/`jump`) back into the little-endian
    relative offsets x86 CALL/JMP/Jcc instructions actually use, splicing
    them back into `main` at the positions `rc`'s decisions mark as
    "converted"."""
    rd = _Bcj2RangeDecoder(rc)
    out = bytearray()
    mi = ci = ji = 0
    prev = 0
    while len(out) < out_size:
        b = main[mi]
        mi += 1
        out.append(b)
        idx = None
        if b == 0xE8:
            idx = prev
        elif b == 0xE9:
            idx = 256
        elif prev == 0x0F and (b & 0xF0) == 0x80:
            idx = 257
        if idx is not None and rd.decode_bit(idx) == 1:
            if b == 0xE8:
                addr = int.from_bytes(call[ci:ci + 4], "big")
                ci += 4
            else:
                addr = int.from_bytes(jump[ji:ji + 4], "big")
                ji += 4
            rel = (addr - (len(out) + 4)) & 0xFFFFFFFF
            out += rel.to_bytes(4, "little")
            prev = out[-1]
            continue
        prev = b
    return bytes(out)


def _decompress_single(coder: dict, packed: bytes, unpacksize: int) -> bytes:
    """Decompress one simple (1-in/1-out) coder's stream using py7zr's own
    native LZMA/LZMA2/Copy support — this is the well-trodden path py7zr
    already handles, just invoked directly on a substream instead of a
    whole folder."""
    dec = SevenZipDecompressor([coder], len(packed), [unpacksize], crc=None)
    fp = io.BytesIO(packed)
    out = bytearray()
    while len(out) < unpacksize:
        chunk = dec.decompress(fp, max_length=-1)
        if not chunk:
            break
        out += chunk
    return bytes(out[:unpacksize])


def _resolve_input(folder, global_in_idx: int, base_pack_idx: int, archive_fp, afterheader: int, packinfo) -> bytes:
    """Return the raw bytes feeding folder input `global_in_idx`: either
    another coder's decompressed output (per the folder's bindpairs), or
    the archive's own packed bytes if this input is fed directly."""
    for bond in folder.bindpairs:
        if bond.incoder == global_in_idx:
            return _resolve_output(folder, bond.outcoder, base_pack_idx, archive_fp, afterheader, packinfo)
    pack_pos = folder.packed_indices.index(global_in_idx)
    global_pack_idx = base_pack_idx + pack_pos
    archive_fp.seek(afterheader + packinfo.packpositions[global_pack_idx])
    return archive_fp.read(packinfo.packsizes[global_pack_idx])


def _resolve_output(folder, global_out_idx: int, base_pack_idx: int, archive_fp, afterheader: int, packinfo) -> bytes:
    """Return the fully decompressed output of whichever coder produces
    `global_out_idx`, decompressing its input first if needed."""
    out_cursor = 0
    for coder_idx, coder in enumerate(folder.coders):
        if out_cursor <= global_out_idx < out_cursor + coder["numoutstreams"]:
            break
        out_cursor += coder["numoutstreams"]
    else:
        raise RuntimeError(f"No coder produces output stream {global_out_idx}")

    if coder["method"] == BCJ2_METHOD:
        raise RuntimeError("Nested BCJ2 folders are not supported")

    in_start = sum(c["numinstreams"] for c in folder.coders[:coder_idx])
    packed = _resolve_input(folder, in_start, base_pack_idx, archive_fp, afterheader, packinfo)
    return _decompress_single(coder, packed, folder.unpacksizes[coder_idx])


def _decode_folder(folder, base_pack_idx: int, archive_fp, afterheader: int, packinfo) -> bytes:
    """Return one folder's fully decompressed, concatenated byte stream."""
    bcj2_idx = next((i for i, c in enumerate(folder.coders) if c["method"] == BCJ2_METHOD), None)
    if bcj2_idx is None:
        # Plain folder (no BCJ2) — a single coder chain py7zr already handles.
        assert len(folder.coders) == 1, "unexpected multi-coder non-BCJ2 folder"
        return _resolve_output(folder, 0, base_pack_idx, archive_fp, afterheader, packinfo)

    in_start = sum(c["numinstreams"] for c in folder.coders[:bcj2_idx])
    # BCJ2's 4 inputs are fixed by the format: main, call, jump, rc/control.
    main = _resolve_input(folder, in_start + 0, base_pack_idx, archive_fp, afterheader, packinfo)
    call = _resolve_input(folder, in_start + 1, base_pack_idx, archive_fp, afterheader, packinfo)
    jump = _resolve_input(folder, in_start + 2, base_pack_idx, archive_fp, afterheader, packinfo)
    rc = _resolve_input(folder, in_start + 3, base_pack_idx, archive_fp, afterheader, packinfo)
    out_size = folder.unpacksizes[bcj2_idx]
    return _bcj2_decode(main, call, jump, rc, out_size)


def _extract(archive_path: str, dest_dir: str) -> None:
    print("Extracting...")
    with py7zr.SevenZipFile(archive_path, "r") as z:
        folders = z.header.main_streams.unpackinfo.folders
        packinfo = z.header.main_streams.packinfo
        afterheader = z.afterheader
        files = z.files

        with open(archive_path, "rb") as archive_fp:
            base_pack_idx = 0
            folder_base_pack_idx = []
            for folder in folders:
                folder_base_pack_idx.append(base_pack_idx)
                base_pack_idx += len(folder.packed_indices)

            decoded_folders: dict[int, bytes] = {}
            # Files belonging to the same (solid) folder are concatenated in
            # the order they're listed; track how far we've consumed each.
            folder_cursor: dict[int, int] = {}

            for file_info in files:
                dest_path = os.path.join(dest_dir, *file_info.filename.split("/"))
                if file_info.is_directory:
                    os.makedirs(dest_path, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                if file_info.emptystream:
                    open(dest_path, "wb").close()
                    continue

                folder = file_info.folder
                folder_id = id(folder)
                if folder_id not in decoded_folders:
                    folder_index = next(i for i, f in enumerate(folders) if f is folder)
                    decoded_folders[folder_id] = _decode_folder(
                        folder, folder_base_pack_idx[folder_index], archive_fp, afterheader, packinfo
                    )
                    folder_cursor[folder_id] = 0

                start = folder_cursor[folder_id]
                size = file_info.uncompressed
                folder_cursor[folder_id] = start + size

                data = decoded_folders[folder_id]
                with open(dest_path, "wb") as out:
                    out.write(data[start:start + size])


def fetch_mpv_lib(force: bool = False) -> None:
    """Download and extract libmpv into mpv-lib/ if not already present."""
    if os.path.exists(REQUIRED_FILE) and not force:
        print(f"mpv-lib/ already present ({REQUIRED_FILE}) — skipping. Pass --force to re-fetch.")
        return

    os.makedirs(MPV_LIB_DIR, exist_ok=True)
    url = _find_latest_build_url()

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, "libmpv.7z")
        _download(url, archive_path)
        _extract(archive_path, MPV_LIB_DIR)

    if not os.path.exists(REQUIRED_FILE):
        raise RuntimeError(
            f"Extraction finished but {REQUIRED_FILE} is still missing — "
            "the archive layout may have changed upstream."
        )
    print(f"mpv-lib/ ready ({REQUIRED_FILE}).")


if __name__ == "__main__":
    fetch_mpv_lib(force="--force" in sys.argv)
