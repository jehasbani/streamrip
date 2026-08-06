from streamrip.media import Album, Artist
from streamrip.metadata import AlbumInfo, AlbumMetadata

ARTIST = "Pink Floyd"


def _album(
    title: str,
    albumartist: str,
    year: str,
    num_tracks: int,
    album_id: str,
    sampling_rate: float | None = 44.1,
    bit_depth: int | None = 16,
) -> Album:
    info = AlbumInfo(
        id=album_id,
        quality=2,
        container="FLAC",
        explicit=False,
        sampling_rate=sampling_rate,
        bit_depth=bit_depth,
    )
    meta = AlbumMetadata(
        info=info,
        album=title,
        albumartist=albumartist,
        year=year,
        genre=["Rock"],
        covers=None,  # type: ignore
        tracktotal=num_tracks,
    )
    # tracks list length is what _non_albums checks
    return Album(meta=meta, tracks=[None] * num_tracks, config=None, folder=".", db=None)  # type: ignore


def _artist(albums: list[Album]) -> Artist:
    return Artist(name=ARTIST, albums=[], client=None, config=None)  # type: ignore


def test_studio_album_kept():
    a = _album("The Wall", ARTIST, "1979", 26, "tw")
    artist = _artist([a])
    result = artist._filter_studio_and_live([a])
    assert len(result) == 1


def test_live_album_kept():
    a = _album("Live at Pompeii", ARTIST, "1972", 8, "lap")
    artist = _artist([a])
    result = artist._filter_studio_and_live([a])
    assert len(result) == 1, "Live albums must not be filtered out"


def test_single_dropped():
    studio = _album("Wish You Were Here", ARTIST, "1975", 5, "wywh")
    single = _album("Another Brick in the Wall", ARTIST, "1979", 1, "abw")
    artist = _artist([studio, single])
    result = artist._filter_studio_and_live([studio, single])
    ids = {a.meta.info.id for a in result}
    assert ids == {"wywh"}


def test_feature_and_compilation_dropped():
    studio = _album("Animals", ARTIST, "1977", 5, "anim")
    tribute = _album("Pink Floyd Tribute", "Various Artists", "2000", 12, "trib")
    collab = _album("Collaboration Record", "David Gilmour & Friends", "2015", 10, "coll")
    artist = _artist([studio, tribute, collab])
    result = artist._filter_studio_and_live([studio, tribute, collab])
    assert len(result) == 1
    assert result[0].meta.info.id == "anim"


def test_remaster_deduped_best_quality_wins():
    original = _album("Dark Side", ARTIST, "1973", 10, "ds_orig", 44.1, 16)
    remaster = _album("Dark Side (2011 Remaster)", ARTIST, "2011", 10, "ds_rm", 96.0, 24)
    artist = _artist([original, remaster])
    result = artist._filter_studio_and_live([original, remaster])
    assert len(result) == 1
    assert result[0].meta.info.id == "ds_rm", "Higher-quality version should be kept"


def test_full_mixed_discography():
    """Studio + live kept; single, compilation, and remaster-dup dropped/deduped."""
    studio = _album("Meddle", ARTIST, "1971", 6, "med")
    live = _album("Delicate Sound of Thunder (Live)", ARTIST, "1988", 14, "dsot")
    single = _album("See Emily Play", ARTIST, "1967", 1, "sep")
    compilation = _album("Best of Pink Floyd", "Various Artists", "2001", 20, "bopf")
    remaster_dup = _album("Meddle (2021 Remaster)", ARTIST, "2021", 6, "med_rm", 96.0, 24)

    albums = [studio, live, single, compilation, remaster_dup]
    artist = _artist(albums)
    result = artist._filter_studio_and_live(albums)
    ids = {a.meta.info.id for a in result}
    # studio deduped (remaster wins), live kept, single and compilation dropped
    assert ids == {"med_rm", "dsot"}


def test_dry_run_format_sorted_by_year():
    a1 = _album("Atom Heart Mother", ARTIST, "1970", 6, "ahm")
    a2 = _album("Piper at the Gates of Dawn", ARTIST, "1967", 11, "patgod")
    artist = _artist([a1, a2])
    result = artist._filter_studio_and_live([a1, a2])
    result.sort(key=lambda a: a.meta.year)
    lines = [f"{a.meta.year} - {a.meta.album}" for a in result]
    assert lines == [
        "1967 - Piper at the Gates of Dawn",
        "1970 - Atom Heart Mother",
    ]
