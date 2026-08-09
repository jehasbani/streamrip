"""Post-download library organizer.

Cleans up a downloads folder that follows streamrip's default discography
layout (``<base>/<Artist Name>/<Artist> - <Album> (<Year>) [<Container>]
[<BitDepth>B-<SamplingRate>kHz]/``, the shape produced when ripping a full
artist discography with the default ``filepaths.folder_format``) into a flat,
readable structure:

    <base>/<Artist Name>/<Year> - <Album Title>/DD - Title.ext

Per album folder, this:

  1. Parses the streamrip folder name (artist credit, title, year, quality).
     Folders already in the flat "YYYY - Title" shape (e.g. from a previous
     run) are recognized as-is, so running this repeatedly is idempotent.
  2. Flattens ``Disc N`` subfolders, tagging ``discnumber`` on every file.
  3. Detects DJ sets (a continuous-mix file plus 5+ separate source tracks)
     and archives the unmixed tracks into ``Unmixed Tracks.7z`` (falls back to
     an uncompressed folder if ``py7zr`` isn't installed).
  4. Reads title/artist/albumartist/tracknumber/discnumber tags off every
     track.
  5. Detects a "Live" tag shared by every track and moves it from the tracks
     onto the album folder name; likewise for any other bracketed tag shared
     by every track (e.g. "(Remastered)").
  6. Detects other artists credited on the release (from tags, or the folder
     name as a fallback) and appends "(ft. Other Artist)" to the album name.
  7. Renames tracks to "TT - Title.ext" (or "DD - TT - Title.ext" for
     multi-disc releases), zero-padded, with no artist name or leftover tags.
  8. Resizes cover.jpg to a max of 1000x1000 (never upscaling, keeping it
     under 150 KB) and embeds it into every audio file.
  9. Consolidates cover.jpg into ``artwork/`` (original) and
     ``album_artwork_compressed/`` (resized) at the artist level.
 10. Sets artist/albumartist/album/title/tracknumber/discnumber tags.
 11. Moves the album folder to ``<Artist>/<Year> - <Title>`` (with a quality
     suffix appended if that name already exists under a different album).
 12. Writes an ``ALBUMS_QUALITY.md`` index of the flat albums at the artist
     root, and verifies no files were lost.

Only album folders sitting directly under an artist folder are touched.
Anything already filed into a legacy category subfolder (``Studio``,
``Live``, ``EPs``, ``Singles``, ``Other``, ``Tributes & Collections``) from an
older manual scheme is left alone.
"""

from __future__ import annotations

import io
import os
import re
import shutil
from collections import Counter

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif"}

# Legacy category folders from an older manual classification scheme. New
# downloads never get filed into these, but if they already exist (from an
# older run) they're left untouched -- only album folders sitting directly
# under the artist folder are reorganized.
LEGACY_CATEGORIES = {"Studio", "Live", "Singles", "EPs", "Other", "Tributes & Collections"}
SUPPORT_DIRS = {"artwork", "album_artwork_compressed"}

ENSEMBLE_RE = re.compile(
    r"^(quartet|quintet|sextet|septet|octet|trio|ensemble|band|group|orchestra|collective)$",
    re.IGNORECASE,
)

LIVE_TAG_RE = re.compile(r"^[\(\[]\s*live\b.*[\)\]]$", re.IGNORECASE)
FEAT_RE = re.compile(r"^(feat\.?|featuring|ft\.?|with)\s+(.*)$", re.IGNORECASE)
TAG_RE = re.compile(r"\s*([\(\[][^()\[\]]*[\)\]])")


# ─────────────────────────────────────────────────────────────────────────────
# FOLDER NAME PARSING
# ─────────────────────────────────────────────────────────────────────────────


def parse_folder(name: str) -> tuple[str, str, str, str]:
    """Parse a streamrip folder name into its components.

    Returns (artist_credit, album_title, year, quality_str).
    Example: "Artist - Album (2001) [FLAC] [16B-44.1kHz]"
             -> ("Artist", "Album", "2001", "[FLAC] [16B-44.1kHz]")

    Folders already in our own "YYYY - Title" flat format (from a previous
    run) are recognized directly so re-running this is idempotent instead of
    misreading "YYYY" as the artist credit.
    """
    quality_str = ""
    m = re.search(r"((?:\s*\[[^\]]+\])+)$", name)
    if m:
        quality_str = m.group(1).strip()
        core = name[: m.start()].strip()
    else:
        core = name

    # Already-organized "YYYY - Title" -- no artist credit to extract.
    ym = re.match(r"^(\d{4})\s*-\s*(.+)$", core)
    if ym:
        return "", ym.group(2).strip(), ym.group(1), quality_str

    # Split on first ' - ' to get artist credit
    if " - " in core:
        artist_credit, rest = core.split(" - ", 1)
    else:
        artist_credit, rest = "", core

    # Extract year in parentheses
    ym2 = re.search(r"\((\d{4})\)\s*$", rest)
    if ym2:
        year = ym2.group(1)
        album_title = rest[: ym2.start()].strip()
    else:
        ys = re.match(r"^(\d{4})\s+(.*)", rest)
        if ys:
            year, album_title = ys.group(1), ys.group(2).strip()
        else:
            year, album_title = "", rest.strip()

    return artist_credit, album_title, year, quality_str


# ─────────────────────────────────────────────────────────────────────────────
# ARTIST CREDIT / "ft." HANDLING
# ─────────────────────────────────────────────────────────────────────────────


def split_artist_list(s: str) -> list[str]:
    if not s:
        return []
    parts = re.split(
        r"\s*,\s*|\s*&\s*|\s+(?:feat\.?|featuring|ft\.?|with|vs\.?|x)\s+",
        s.strip(),
        flags=re.IGNORECASE,
    )
    return [p.strip() for p in parts if p.strip()]


def extract_other_artists(credit: str, artist_name: str) -> list[str]:
    """Return the artists in `credit` who aren't `artist_name`.

    "Artist X Quartet"/"Artist X, Band Name" style credits that are really
    just this artist's own project are recognized and return no others.
    """
    if not credit:
        return []
    c, a = credit.strip(), artist_name.strip()
    if c.lower() == a.lower():
        return []
    # "Artist X Quartet/Quintet/..." -- whole credit is this artist's ensemble.
    if c.lower().startswith(a.lower()):
        remainder = c[len(a) :].strip()
        if not remainder or ENSEMBLE_RE.match(remainder):
            return []

    others: list[str] = []
    seen: set[str] = set()
    for p in split_artist_list(c):
        pl = p.lower()
        if pl == a.lower() or pl.startswith(a.lower() + " "):
            continue
        if pl not in seen:
            seen.add(pl)
            others.append(p)
    return others


# ─────────────────────────────────────────────────────────────────────────────
# TAG READ/WRITE (via mutagen's format-agnostic "easy" interface)
# ─────────────────────────────────────────────────────────────────────────────


def get_easy_tags(path: str) -> dict:
    """Read title/artist/albumartist/tracknumber/discnumber off a file, best-effort."""
    try:
        from mutagen import File as MutagenFile

        f = MutagenFile(path, easy=True)
        if f is None:
            return {}
        out = {}
        for key in ("title", "artist", "albumartist", "tracknumber", "discnumber", "album"):
            v = f.get(key)
            if v:
                out[key] = v[0]
        return out
    except Exception:
        return {}


def set_core_tags(
    path: str,
    artist: str | None = None,
    albumartist: str | None = None,
    album: str | None = None,
    title: str | None = None,
    tracknumber: int | None = None,
    discnumber: int | None = None,
) -> None:
    """Set only the fields provided; leaves everything else on the file untouched."""
    try:
        from mutagen import File as MutagenFile

        f = MutagenFile(path, easy=True)
        if f is None:
            print(f"      [WARN] no tag support for {os.path.basename(path)}")
            return
        if artist is not None:
            f["artist"] = artist
        if albumartist is not None:
            f["albumartist"] = albumartist
        if album is not None:
            f["album"] = album
        if title is not None:
            f["title"] = title
        if tracknumber is not None:
            f["tracknumber"] = str(tracknumber)
        if discnumber is not None:
            f["discnumber"] = str(discnumber)
        f.save()
    except Exception as e:
        print(f"      [WARN] tag {os.path.basename(path)}: {e}")


def _parse_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).split("/")[0])
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TRACK TITLE / TAG PARSING
# ─────────────────────────────────────────────────────────────────────────────

_DISC_TMP_RE = re.compile(r"^__disc(\d+)__(.*)$")


def _split_disc_tmp(fname: str) -> tuple[int | None, str]:
    m = _DISC_TMP_RE.match(fname)
    return (int(m.group(1)), m.group(2)) if m else (None, fname)


def derive_title_from_filename(fname: str, credit: str) -> str:
    """Fallback title derivation when a file has no 'title' tag."""
    _, remainder = _split_disc_tmp(fname)
    stem = os.path.splitext(remainder)[0]
    stem = re.sub(r"^\d+[\.\s]+", "", stem)  # leading track number
    stem = re.sub(r"^\d+\s*-\s*", "", stem)  # or "01 - " style
    if credit:
        stem = re.sub(rf"^{re.escape(credit)}\s*[-–]\s*", "", stem, flags=re.IGNORECASE)
    return stem.strip()


def extract_tags_from_title(title: str) -> tuple[str, list[str]]:
    """Split a title into (base_title, [bracketed tag strings]) in appearance order."""
    tags = [m.group(1).strip() for m in TAG_RE.finditer(title)]
    base = re.sub(r"\s+", " ", TAG_RE.sub("", title)).strip()
    return base, tags


def _norm_tag(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())


def detect_and_strip_live(tracks: list[dict]) -> bool:
    """If every track has a tag matching /live/i, remove it from all of them."""
    is_live = all(any(LIVE_TAG_RE.match(tag) for tag in t["tags"]) for t in tracks)
    if is_live:
        for t in tracks:
            t["tags"] = [tag for tag in t["tags"] if not LIVE_TAG_RE.match(tag)]
    return is_live


def extract_common_tags(tracks: list[dict]) -> list[str]:
    """Find bracketed tags present (case-insensitive) on every track, strip
    one instance of each from every track, and return them in first-seen
    order (original casing preserved)."""
    if not tracks or any(not t["tags"] for t in tracks):
        return []
    common_norms = {_norm_tag(t) for t in tracks[0]["tags"]}
    for t in tracks[1:]:
        common_norms &= {_norm_tag(x) for x in t["tags"]}
    if not common_norms:
        return []

    seen, common = set(), []
    for tag in tracks[0]["tags"]:
        n = _norm_tag(tag)
        if n in common_norms and n not in seen:
            seen.add(n)
            common.append(tag)

    for t in tracks:
        for n in seen:
            for i, tag in enumerate(t["tags"]):
                if _norm_tag(tag) == n:
                    del t["tags"][i]
                    break
    return common


def normalize_tag_for_album(tag: str) -> str:
    inner = tag.strip("()[]").strip()
    return f"({inner})"


def sanitize(s: str) -> str:
    return re.sub(r"[/]", "-", s).strip()


def _ensure_suffix(title: str, part: str) -> str:
    """Append part unless an equivalent suffix is already present (idempotency)."""
    if part.lower() in title.lower():
        return title
    return f"{title} {part}".strip()


# ─────────────────────────────────────────────────────────────────────────────
# DISC N SUBFOLDER FLATTENING
# ─────────────────────────────────────────────────────────────────────────────


def flatten_disc_subfolders(album_dir: str, dry_run: bool) -> None:
    """If album_dir contains Disc 1/, Disc 2/, ... subfolders, move every file
    to the album root (renamed with a unique "__discN__" marker to avoid
    collisions between discs that both start their own track numbering at 1)
    and tag discnumber directly. The final "DD - TT - Title.ext" name is
    applied later, once titles/track numbers are known for the whole album.
    """
    disc_re = re.compile(r"^disc\s*(\d+)$", re.IGNORECASE)
    discs = []
    for entry in os.scandir(album_dir):
        if entry.is_dir():
            mm = disc_re.match(entry.name)
            if mm:
                discs.append((int(mm.group(1)), entry.path))
    if not discs:
        return

    discs.sort(key=lambda x: x[0])
    for disc_num, disc_path in discs:
        for fname in sorted(os.listdir(disc_path)):
            src = os.path.join(disc_path, fname)
            if not os.path.isfile(src):
                continue
            if fname.lower() == "cover.jpg":
                dst = os.path.join(album_dir, "cover.jpg")
                if not os.path.exists(dst):
                    if not dry_run:
                        shutil.move(src, dst)
                elif not dry_run:
                    os.remove(src)
                continue
            if os.path.splitext(fname)[1].lower() not in AUDIO_EXTS:
                dst = os.path.join(album_dir, fname)
                if not os.path.exists(dst) and not dry_run:
                    shutil.move(src, dst)
                continue
            tmp_name = f"__disc{disc_num:02d}__{fname}"
            dst = os.path.join(album_dir, tmp_name)
            if dry_run:
                print(f"      [DRY] disc {disc_num}: {fname} -> {tmp_name}")
            else:
                shutil.move(src, dst)
                set_core_tags(dst, discnumber=disc_num)
        if not dry_run:
            shutil.rmtree(disc_path, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# DJ CONTINUOUS MIX HANDLING
# ─────────────────────────────────────────────────────────────────────────────


def handle_dj_set(album_dir: str, audio_files: list[str], dry_run: bool) -> bool:
    """If the album has continuous-mix files AND 5+ unmixed source tracks,
    move the source tracks to Unmixed Tracks/ and compress to .7z.
    Returns True if this was treated as a DJ set."""
    continuous = [f for f in audio_files if "continuous" in f.lower()]
    others = [f for f in audio_files if "continuous" not in f.lower()]
    if not continuous or len(others) < 5:
        return False

    unmixed_dir = os.path.join(album_dir, "Unmixed Tracks")
    print(f"    [DJ set] {len(continuous)} mix file(s) + {len(others)} source tracks")
    if not dry_run:
        os.makedirs(unmixed_dir, exist_ok=True)
        for f in others:
            shutil.move(os.path.join(album_dir, f), os.path.join(unmixed_dir, f))
        try:
            import py7zr

            archive = os.path.join(album_dir, "Unmixed Tracks.7z")
            with py7zr.SevenZipFile(archive, "w") as z:
                z.writeall(unmixed_dir, "Unmixed Tracks")
            print("    -> compressed to Unmixed Tracks.7z")
        except ImportError:
            print(
                "    [WARN] py7zr not installed -- Unmixed Tracks/ left uncompressed "
                "(pip install streamrip[organize] to enable archiving)"
            )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# COVER ART
# ─────────────────────────────────────────────────────────────────────────────


def resize_cover(path: str) -> bytes | None:
    """Open cover.jpg, cap at 1000x1000 (never upscale), then reduce JPEG
    quality until the result is under 150 KB. Returns bytes or None on error."""
    MAX_PX = 1000
    MAX_BYTES = 150 * 1024

    try:
        from PIL import Image

        with Image.open(path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(img.size) > MAX_PX:
                img = img.resize((MAX_PX, MAX_PX), Image.LANCZOS)
            for quality in (90, 80, 70, 60, 50):
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=quality)
                data = buf.getvalue()
                if len(data) <= MAX_BYTES:
                    return data
            return data
    except Exception as e:
        print(f"    [WARN] cover resize failed ({e}) -- using original")
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return None


def embed_art(path: str, data: bytes) -> None:
    """Embed cover art JPEG bytes into a FLAC, MP3, or M4A/AAC file."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture

            a = FLAC(path)
            p = Picture()
            p.type = 3
            p.mime = "image/jpeg"
            p.data = data
            a.clear_pictures()
            a.add_picture(p)
            a.save()
        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC, ID3NoHeaderError

            try:
                a = ID3(path)
            except ID3NoHeaderError:
                a = ID3()
            a.delall("APIC")
            a.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
            a.save(path)
        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover

            a = MP4(path)
            a["covr"] = [MP4Cover(data, MP4Cover.FORMAT_JPEG)]
            a.save()
        # ogg / opus / wav / aiff: not handled here
    except Exception as e:
        print(f"      [WARN] embed art {os.path.basename(path)}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PER-ALBUM PROCESSING
# ─────────────────────────────────────────────────────────────────────────────


def pick_credit_source(raw_tags: list[dict], folder_credit: str) -> str:
    """Prefer the most common albumartist/artist tag across tracks; fall back
    to the credit parsed from the folder name."""
    vals = [t.get("albumartist") or t.get("artist") for t in raw_tags]
    vals = [v for v in vals if v]
    if vals:
        return Counter(vals).most_common(1)[0][0]
    return folder_credit


def gather_tracks(album_dir: str, credit: str) -> list[dict]:
    files = sorted(
        f
        for f in os.listdir(album_dir)
        if os.path.isfile(os.path.join(album_dir, f))
        and os.path.splitext(f)[1].lower() in AUDIO_EXTS
    )
    tracks = []
    for f in files:
        path = os.path.join(album_dir, f)
        raw = get_easy_tags(path)
        title = (raw.get("title") or derive_title_from_filename(f, credit)).strip()
        tracknum = _parse_int(raw.get("tracknumber"))
        if tracknum is None:
            m = re.match(r"^(\d+)[\.\s-]", _split_disc_tmp(f)[1])
            if m:
                tracknum = int(m.group(1))
        discnum = _parse_int(raw.get("discnumber"))
        if discnum is None:
            disc_from_tmp, _ = _split_disc_tmp(f)
            discnum = disc_from_tmp if disc_from_tmp is not None else 1
        base_title, tag_list = extract_tags_from_title(title)
        tracks.append(
            {
                "file": f,
                "path": path,
                "ext": os.path.splitext(f)[1],
                "raw_tags": raw,
                "title": title,
                "base_title": base_title,
                "tags": tag_list,
                "tracknum": tracknum,
                "discnum": discnum,
            }
        )
    for i, t in enumerate(tracks, start=1):
        if t["tracknum"] is None:
            t["tracknum"] = i
    return tracks


def process_album(album_dir: str, artist_name: str, artist_dir: str, dry_run: bool) -> tuple | None:
    folder_name = os.path.basename(album_dir)
    credit, title, year, quality = parse_folder(folder_name)

    print(f"\n  > {folder_name}")

    flatten_disc_subfolders(album_dir, dry_run)

    try:
        all_items = os.listdir(album_dir)
    except PermissionError:
        print("    [ERROR] cannot read directory -- skipping")
        return None

    audio = [
        f
        for f in all_items
        if os.path.isfile(os.path.join(album_dir, f)) and os.path.splitext(f)[1].lower() in AUDIO_EXTS
    ]

    handle_dj_set(album_dir, audio, dry_run)

    # Re-scan: DJ-set source tracks may have moved into Unmixed Tracks/.
    audio = [
        f
        for f in os.listdir(album_dir)
        if os.path.isfile(os.path.join(album_dir, f)) and os.path.splitext(f)[1].lower() in AUDIO_EXTS
    ]

    if not audio:
        new_folder = (f"{year} - {title}" if year else title) + " [NO AUDIO]"
        dest = _resolve_collision(artist_dir, album_dir, new_folder, quality)
        print(f"    -> {dest} (no audio files)")
        if not dry_run:
            os.rename(album_dir, os.path.join(artist_dir, dest))
        return (year or "?", title, quality or "unknown")

    tracks = gather_tracks(album_dir, credit)
    credit_source = pick_credit_source([t["raw_tags"] for t in tracks], credit)
    others = extract_other_artists(credit_source, artist_name)

    is_live = detect_and_strip_live(tracks)
    common_tags = extract_common_tags(tracks)

    generic_common = []
    for tag in common_tags:
        inner = tag.strip("()[]").strip()
        fm = FEAT_RE.match(inner)
        if fm:
            for name in re.split(r"\s*,\s*|\s*&\s*|\s+and\s+", fm.group(2), flags=re.IGNORECASE):
                name = name.strip()
                if name and name.lower() not in {o.lower() for o in others}:
                    others.append(name)
        else:
            generic_common.append(tag)

    new_title = title
    if is_live:
        new_title = _ensure_suffix(new_title, "(Live)")
    for tag in generic_common:
        new_title = _ensure_suffix(new_title, normalize_tag_for_album(tag))
    if others:
        new_title = _ensure_suffix(new_title, f"(ft. {', '.join(others)})")
    new_title = sanitize(new_title)

    base_folder = f"{year} - {new_title}" if year else new_title
    new_folder = _resolve_collision(artist_dir, album_dir, base_folder, quality)
    print(f"    -> {new_folder}")

    multi_disc = len({t["discnum"] for t in tracks}) > 1

    # Build final filenames, resolving in-album collisions.
    used_names: set[str] = set()
    for t in tracks:
        remaining = " ".join(normalize_tag_for_album(x) for x in t["tags"])
        track_title = sanitize(f"{t['base_title']} {remaining}".strip())
        num = f"{t['tracknum']:02d}"
        prefix = f"{t['discnum']:02d} - {num}" if multi_disc else num
        final_name = f"{prefix} - {track_title}{t['ext']}"
        if final_name in used_names:
            stem, ext = os.path.splitext(final_name)
            n = 2
            while f"{stem} ({n}){ext}" in used_names:
                n += 1
            final_name = f"{stem} ({n}){ext}"
        used_names.add(final_name)
        t["final_name"] = final_name
        t["final_title"] = track_title

    if not dry_run:
        # Rename in two passes to avoid clobbering when names overlap mid-loop.
        for t in tracks:
            tmp = t["path"] + ".tmp_rename"
            os.rename(t["path"], tmp)
            t["path"] = tmp
        for t in tracks:
            dst = os.path.join(album_dir, t["final_name"])
            os.rename(t["path"], dst)
            t["path"] = dst
    else:
        for t in tracks:
            print(f"      [DRY] {t['file']} -> {t['final_name']}")

    # Cover art
    cover_src = os.path.join(album_dir, "cover.jpg")
    cover_bytes = resize_cover(cover_src) if os.path.exists(cover_src) else None
    if cover_bytes and not dry_run:
        for t in tracks:
            embed_art(t["path"], cover_bytes)

    # Metadata tags
    if not dry_run:
        for t in tracks:
            set_core_tags(
                t["path"],
                artist=artist_name,
                albumartist=artist_name,
                album=new_title,
                title=t["final_title"],
                tracknumber=t["tracknum"],
                discnumber=t["discnum"],
            )

    # Consolidate cover art into artist-level folders, keyed by the final folder name.
    if os.path.exists(cover_src) and not dry_run:
        art_name = f"{new_folder}_cover.jpg"
        shutil.move(cover_src, os.path.join(artist_dir, "artwork", art_name))
        if cover_bytes:
            with open(os.path.join(artist_dir, "album_artwork_compressed", art_name), "wb") as f:
                f.write(cover_bytes)

    # Move album folder into place
    dest = os.path.join(artist_dir, new_folder)
    if dry_run:
        print(f"      [DRY] mv -> {new_folder}")
    else:
        os.rename(album_dir, dest)

    return (year or "?", new_title, quality or "unknown")


def _resolve_collision(artist_dir: str, album_dir: str, base_folder: str, quality: str) -> str:
    """If base_folder already exists at the artist root (a different album),
    append the quality spec to disambiguate -- e.g. two masterings of the
    same record."""
    dest_check = os.path.join(artist_dir, base_folder)
    if os.path.exists(dest_check) and os.path.abspath(dest_check) != os.path.abspath(album_dir):
        q = quality.replace("[", "").replace("]", "").strip()
        return f"{base_folder} ({q})" if q else f"{base_folder}_dup"
    return base_folder


# ─────────────────────────────────────────────────────────────────────────────
# ALBUMS_QUALITY.md
# ─────────────────────────────────────────────────────────────────────────────


def _load_previous_quality(path: str) -> dict[tuple[str, str], str]:
    """Best-effort parse of an existing ALBUMS_QUALITY.md to recover quality
    strings for albums that no longer carry a quality suffix in their folder
    name (e.g. re-processing an already-organized album)."""
    prev = {}
    if not os.path.exists(path):
        return prev
    try:
        with open(path) as f:
            lines = f.readlines()
        for line in lines:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|\n").split("|")]
            if len(cells) < 3 or cells[0] in ("Year", "------", ""):
                continue
            if set(cells[0]) <= {"-"}:
                continue
            year, album, quality = cells[0], cells[1], cells[-1]
            prev[(year, album.lower())] = quality
    except Exception:
        pass
    return prev


def write_albums_quality(artist_dir: str, artist_name: str, rows: list[tuple], dry_run: bool) -> None:
    if dry_run:
        return
    path = os.path.join(artist_dir, "ALBUMS_QUALITY.md")
    prev = _load_previous_quality(path)
    resolved = []
    for year, album, quality in rows:
        if quality in ("", "unknown"):
            quality = prev.get((year, album.lower()), quality)
        resolved.append((year, album, quality))
    resolved.sort(key=lambda r: (r[0], r[1]))
    with open(path, "w") as f:
        f.write(f"# {artist_name} -- Albums Quality\n\n")
        f.write("| Year | Album | Quality |\n")
        f.write("|------|-------|---------|\n")
        for year, album, quality in resolved:
            f.write(f"| {year} | {album} | {quality} |\n")


# ─────────────────────────────────────────────────────────────────────────────
# ARTIST ORGANIZER
# ─────────────────────────────────────────────────────────────────────────────


def organize_artist(artist_name: str, base_dir: str, dry_run: bool) -> None:
    artist_dir = os.path.join(base_dir, artist_name)
    if not os.path.isdir(artist_dir):
        print(f"\n[SKIP] Directory not found: {artist_dir}")
        return

    print(f"\n{'=' * 60}\nOrganizing: {artist_name}\n{'=' * 60}")

    files_before = sum(len(fs) for _, _, fs in os.walk(artist_dir))

    if not dry_run:
        os.makedirs(os.path.join(artist_dir, "artwork"), exist_ok=True)
        os.makedirs(os.path.join(artist_dir, "album_artwork_compressed"), exist_ok=True)

    skip_dirs = LEGACY_CATEGORIES | SUPPORT_DIRS
    album_dirs = sorted(d.path for d in os.scandir(artist_dir) if d.is_dir() and d.name not in skip_dirs)

    if any(os.path.isdir(os.path.join(artist_dir, c)) for c in LEGACY_CATEGORIES):
        print(
            "  (leaving existing Studio/Live/EPs/etc. subfolders untouched -- "
            "only album folders at the artist root are reorganized)"
        )

    quality_rows: list[tuple] = []
    for album_dir in album_dirs:
        row = process_album(album_dir, artist_name, artist_dir, dry_run)
        if row:
            quality_rows.append(row)

    write_albums_quality(artist_dir, artist_name, quality_rows, dry_run)

    files_after = sum(len(fs) for _, _, fs in os.walk(artist_dir))
    diff = files_after - files_before
    print(f"\n  Files before: {files_before}, after: {files_after}, diff: {diff:+d}")
    if files_after < files_before:
        print(f"  WARNING: {files_before - files_after} file(s) appear to be missing!")
    else:
        status = "(dry run -- nothing changed)" if dry_run else "OK"
        print(f"  {status}")


_DISC_DIR_RE = re.compile(r"^disc\s*\d+$", re.IGNORECASE)


def _looks_like_album_dir(path: str) -> bool:
    """Heuristic: does `path` look like an album folder (audio files sitting
    directly inside it, or `Disc N` subfolders) rather than an artist folder
    (which holds album subfolders and support dirs, but no audio of its own)?
    """
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False
    for e in entries:
        if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXTS:
            return True
        if e.is_dir() and _DISC_DIR_RE.match(e.name):
            return True
    return False


def organize_library(base_dir: str, artist_names: list[str] | tuple[str, ...], dry_run: bool) -> None:
    """Organize one or more artist folders under `base_dir`.

    If `artist_names` is empty, every directory directly under `base_dir` is
    normally treated as an artist folder and processed. But if `base_dir`'s
    own children look like album folders rather than artist folders (e.g.
    `downloads.folder`/`--base-dir` was pointed directly at one artist's
    folder -- common when downloading individual album URLs for a single
    artist straight into their folder), `base_dir` itself is treated as the
    single artist folder to organize, instead of misreading each album inside
    it as its own "artist".
    """
    base = os.path.expanduser(base_dir).rstrip(os.sep) or os.sep
    if not os.path.isdir(base):
        raise NotADirectoryError(f"Directory not found: {base}")

    if dry_run:
        print("[DRY RUN MODE -- no files will be moved or modified]\n")

    if not artist_names:
        candidates = [
            d for d in os.scandir(base) if d.is_dir() and d.name not in (LEGACY_CATEGORIES | SUPPORT_DIRS)
        ]
        if candidates and all(_looks_like_album_dir(d.path) for d in candidates):
            print(
                f"'{base}' looks like a single artist folder "
                "(its subfolders hold audio directly) -- organizing it as one."
            )
            organize_artist(os.path.basename(base), os.path.dirname(base), dry_run)
            print(f"\n{'=' * 60}")
            print("Done -- 1 artist(s) processed.")
            return

        artists = sorted(d.name for d in os.scandir(base) if d.is_dir())
        print(f"Found {len(artists)} artist folder(s) in {base}")
    else:
        artists = list(artist_names)

    for artist in artists:
        organize_artist(artist, base, dry_run)

    print(f"\n{'=' * 60}")
    print(f"Done -- {len(artists)} artist(s) processed.")
