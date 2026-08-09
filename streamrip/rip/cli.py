import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from functools import wraps
from typing import Any

import aiofiles
import aiohttp
import click
from click_help_colors import HelpColorsGroup  # type: ignore
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.traceback import install

from .. import __version__, db
from ..config import DEFAULT_CONFIG_PATH, Config, OutdatedConfigError, set_user_defaults
from ..console import console
from ..media import PendingArtist
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .main import Main
from .organize import organize_library
from .parse_url import parse_url


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.group(
    cls=HelpColorsGroup,
    help_headers_color="yellow",
    help_options_color="green",
)
@click.version_option(version=__version__)
@click.option(
    "--config-path",
    default=DEFAULT_CONFIG_PATH,
    help="Path to the configuration file",
    type=click.Path(readable=True, writable=True),
)
@click.option(
    "-f",
    "--folder",
    help="The folder to download items into.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "-ndb",
    "--no-db",
    help="Download items even if they have been logged in the database",
    default=False,
    is_flag=True,
)
@click.option(
    "-q",
    "--quality",
    help="The maximum quality allowed to download",
    type=click.IntRange(min=0, max=4),
)
@click.option(
    "-c",
    "--codec",
    help="Convert the downloaded files to an audio codec (ALAC, FLAC, MP3, AAC, or OGG)",
)
@click.option(
    "--no-progress",
    help="Do not show progress bars",
    is_flag=True,
    default=False,
)
@click.option(
    "--no-ssl-verify",
    help="Disable SSL certificate verification (use if you encounter SSL errors)",
    is_flag=True,
    default=False,
)
@click.option(
    "-v",
    "--verbose",
    help="Enable verbose output (debug mode)",
    is_flag=True,
)
@click.pass_context
def rip(
    ctx, config_path, folder, no_db, quality, codec, no_progress, no_ssl_verify, verbose
):
    """Streamrip: the all in one music downloader."""
    global logger
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )
    logger = logging.getLogger("streamrip")
    if verbose:
        install(
            console=console,
            suppress=[
                click,
            ],
            show_locals=True,
            locals_hide_sunder=False,
        )
        logger.setLevel(logging.DEBUG)
        logger.debug("Showing all debug logs")
    else:
        install(console=console, suppress=[click, asyncio], max_frames=1)
        logger.setLevel(logging.INFO)

    if not os.path.isfile(config_path):
        console.print(
            f"No file found at [bold cyan]{config_path}[/bold cyan], creating default config.",
        )
        set_user_defaults(config_path)

    # pass to subcommands
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    try:
        c = Config(config_path)
    except OutdatedConfigError as e:
        console.print(e)
        console.print("Auto-updating config file...")
        Config.update_file(config_path)
        c = Config(config_path)
    except Exception as e:
        console.print(
            f"Error loading config from [bold cyan]{config_path}[/bold cyan]: {e}\n"
            "Try running [bold]rip config reset[/bold]",
        )
        ctx.obj["config"] = None
        return

    # set session config values to command line args
    if no_db:
        c.session.database.downloads_enabled = False
    if folder is not None:
        c.session.downloads.folder = folder

    if quality is not None:
        c.session.qobuz.quality = quality
        c.session.tidal.quality = quality
        c.session.deezer.quality = quality
        c.session.soundcloud.quality = quality

    if codec is not None:
        c.session.conversion.enabled = True
        assert codec.upper() in ("ALAC", "FLAC", "OGG", "MP3", "AAC")
        c.session.conversion.codec = codec.upper()

    if no_progress:
        c.session.cli.progress_bars = False

    if no_ssl_verify:
        c.session.downloads.verify_ssl = False

    ctx.obj["config"] = c


@rip.command()
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--studio-only",
    is_flag=True,
    default=False,
    help="For artist URLs: download only studio albums, excluding live recordings, "
    "compilations, and extra editions.",
)
@click.option(
    "--organize",
    is_flag=True,
    default=False,
    help="After downloading, organize the downloads folder (see `rip organize --help`).",
)
@click.option(
    "--organize-dry-run",
    is_flag=True,
    default=False,
    help="With --organize, print what would be reorganized without changing any files.",
)
@click.pass_context
@coro
async def url(ctx, urls, studio_only, organize, organize_dry_run):
    """Download content from URLs.

    To download an entire Qobuz discography, pass an artist URL, e.g.:

        rip url https://www.qobuz.com/us-en/artist/12345

    Use --studio-only to limit the download to studio albums only.

    Use --organize to tidy up the downloads folder afterwards (equivalent to
    running `rip organize` with no arguments once the download finishes).
    """
    if ctx.obj["config"] is None:
        return

    try:
        with ctx.obj["config"] as cfg:
            cfg: Config

            if studio_only:
                cfg.session.qobuz_filters.non_studio_albums = True

            updates = cfg.session.misc.check_for_updates
            if updates:
                # Run in background
                version_coro = asyncio.create_task(
                    latest_streamrip_version(
                        verify_ssl=cfg.session.downloads.verify_ssl
                    )
                )
            else:
                version_coro = None

            async with Main(cfg) as main:
                await main.add_all(urls)
                await main.resolve()
                await main.rip()

            if organize:
                console.print(
                    f"\n[bold]Organizing[/bold] [cyan]{cfg.session.downloads.folder}[/cyan]"
                )
                organize_library(
                    cfg.session.downloads.folder, [], organize_dry_run
                )

            if version_coro is not None:
                latest_version, notes = await version_coro
                if latest_version != __version__:
                    console.print(
                        f"\n[green]A new version of streamrip [cyan]v{latest_version}[/cyan]"
                        " is available! Run [white][bold]pip3 install streamrip --upgrade[/bold][/white]"
                        " to update.[/green]\n"
                    )

                    console.print(Markdown(notes))

    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()


@rip.command("artist-albums")
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print 'YYYY - Title' lines instead of URLs. Nothing is downloaded.",
)
@click.pass_context
@coro
async def artist_albums(ctx, urls, dry_run):
    """List studio and live album URLs for one or more artist pages.

    Resolves the full discography and prints only studio and live full albums,
    dropping singles, features, compilations, and duplicate versions. Output is
    one URL per line, ready to pipe into `rip url` or redirect to a file.

    Example:

        rip artist-albums https://www.qobuz.com/us-en/artist/12345

        rip artist-albums --dry-run https://www.qobuz.com/us-en/artist/12345

        rip artist-albums https://www.qobuz.com/us-en/artist/12345 > urls.txt && rip file urls.txt
    """
    if ctx.obj["config"] is None:
        return

    _url_templates: dict[str, str] = {
        "qobuz": "https://www.qobuz.com/album/{id}",
        "tidal": "https://www.tidal.com/album/{id}",
        "deezer": "https://www.deezer.com/album/{id}",
    }

    with ctx.obj["config"] as cfg:
        cfg: Config
        async with Main(cfg) as main:
            for artist_url in urls:
                parsed = parse_url(artist_url)
                if parsed is None:
                    console.print(
                        f"[red]Cannot parse URL:[/red] [cyan]{artist_url}[/cyan]",
                        stderr=True,
                    )
                    continue

                client = await main.get_logged_in_client(parsed.source)
                pending = await parsed.into_pending(client, cfg, main.database)

                if not isinstance(pending, PendingArtist):
                    console.print(
                        f"[red]{artist_url} is not an artist URL[/red]",
                        stderr=True,
                    )
                    continue

                artist = await pending.resolve()
                if artist is None:
                    console.print(
                        f"[red]Could not resolve artist from {artist_url}[/red]",
                        stderr=True,
                    )
                    continue

                with tempfile.TemporaryDirectory() as tmp_dir:
                    cfg.session.downloads.folder = tmp_dir
                    albums = await artist.resolve_studio_and_live()
                template = _url_templates.get(
                    artist.client.source,
                    f"https://www.{artist.client.source}.com/album/{{id}}",
                )

                lines = []
                for album_id, year, title in albums:
                    if dry_run:
                        lines.append(f"{year} - {title}")
                    else:
                        lines.append(template.format(id=album_id))
                if lines:
                    click.echo(" ".join(lines))


@rip.command()
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.pass_context
@coro
async def file(ctx, path):
    """Download content from URLs in a file.

    Example usage:

        rip file urls.txt
    """
    try:
        with ctx.obj["config"] as cfg:
            async with Main(cfg) as main:
                async with aiofiles.open(path, "r") as f:
                    content = await f.read()
                    try:
                        items: Any = json.loads(content)
                        loaded = True
                    except json.JSONDecodeError:
                        items = content.split()
                        loaded = False
                if loaded:
                    console.print(
                        f"Detected json file. Loading [yellow]{len(items)}[/yellow] items"
                    )
                    await main.add_all_by_id(
                        [(i["source"], i["media_type"], i["id"]) for i in items]
                    )
                else:
                    s = set(items)
                    if len(s) < len(items):
                        console.print(
                            f"Found [orange]{len(items)-len(s)}[/orange] repeated URLs!"
                        )
                        items = list(s)
                    console.print(
                        f"Detected list of urls. Loading [yellow]{len(items)}[/yellow] items"
                    )
                    await main.add_all(items)

                await main.resolve()
                await main.rip()
    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()


@rip.command()
@click.argument("artists", nargs=-1)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would happen without moving or modifying any files.",
)
@click.option(
    "--base-dir",
    type=click.Path(file_okay=False, dir_okay=True),
    metavar="PATH",
    help="Root directory containing per-artist folders to organize. "
    "Defaults to the configured downloads folder.",
)
@click.pass_context
def organize(ctx, artists, dry_run, base_dir):
    """Organize a downloaded music library into a clean per-artist layout.

    Tidies up folders shaped like streamrip's discography downloads
    (`<Artist>/<Artist> - <Album> (<Year>) [<Container>] [...]`) into a flat
    `<Artist>/<Year> - <Album>/DD - Title.ext` layout: flattens `Disc N`
    subfolders, archives DJ-set source tracks, strips redundant "(Live)" and
    other tags shared by every track onto the album name, tags every file
    with clean artist/album/title/track/disc metadata, resizes and embeds
    cover art, and writes an ALBUMS_QUALITY.md index per artist.

    Only album folders sitting directly under an artist folder are touched;
    anything already filed into a legacy category subfolder (Studio, Live,
    EPs, Singles, Other, Tributes & Collections) is left alone. Re-running is
    safe -- already-organized folders are recognized and left as-is.

    Pass one or more artist folder names to limit the run; omit them to
    organize every artist folder found in the base directory.

    Example usage:

        rip organize

        rip organize "Miles Davis" "John Coltrane"

        rip organize --dry-run

        rip organize --base-dir ~/Music/StreamripDownloads
    """
    if ctx.obj["config"] is None:
        return

    cfg = ctx.obj["config"]
    with cfg as c:
        c: Config
        base = base_dir or c.session.downloads.folder

    try:
        organize_library(base, artists, dry_run)
    except NotADirectoryError as e:
        console.print(f"[red]{e}[/red]")


@rip.group()
def config():
    """Manage configuration files."""


@config.command("open")
@click.option("-v", "--vim", help="Open in (Neo)Vim", is_flag=True)
@click.pass_context
def config_open(ctx, vim):
    """Open the config file in a text editor."""
    config_path = ctx.obj["config_path"]

    console.print(f"Opening file at [bold cyan]{config_path}")
    if vim:
        if shutil.which("nvim") is not None:
            subprocess.run(["nvim", config_path])
        elif shutil.which("vim") is not None:
            subprocess.run(["vim", config_path])
        else:
            logger.error("Could not find nvim or vim. Using default launcher.")
            click.launch(config_path)
    else:
        click.launch(config_path)


@config.command("reset")
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.pass_context
def config_reset(ctx, yes):
    """Reset the config file."""
    config_path = ctx.obj["config_path"]
    if not yes:
        if not Confirm.ask(
            f"Are you sure you want to reset the config file at {config_path}?",
        ):
            console.print("[green]Reset aborted")
            return

    set_user_defaults(config_path)
    console.print(f"Reset the config file at [bold cyan]{config_path}!")


@config.command("path")
@click.pass_context
def config_path(ctx):
    """Display the path of the config file."""
    config_path = ctx.obj["config_path"]
    console.print(f"Config path: [bold cyan]'{config_path}'")


@rip.group()
def database():
    """View and modify the downloads and failed downloads databases."""


@database.command("browse")
@click.argument("table")
@click.pass_context
def database_browse(ctx, table):
    """Browse the contents of a table.

    Available tables:

        * Downloads

        * Failed
    """
    from rich.table import Table

    cfg: Config = ctx.obj["config"]

    if table.lower() == "downloads":
        downloads = db.Downloads(cfg.session.database.downloads_path)
        t = Table(title="Downloads database")
        t.add_column("Row")
        t.add_column("ID")
        for i, row in enumerate(downloads.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    elif table.lower() == "failed":
        failed = db.Failed(cfg.session.database.failed_downloads_path)
        t = Table(title="Failed downloads database")
        t.add_column("Source")
        t.add_column("Media Type")
        t.add_column("ID")
        for i, row in enumerate(failed.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    else:
        console.print(
            f"[red]Invalid database[/red] [bold]{table}[/bold]. [red]Choose[/red] [bold]downloads "
            "[red]or[/red] failed[/bold].",
        )


@rip.command()
@click.option(
    "-f",
    "--first",
    help="Automatically download the first search result without showing the menu.",
    is_flag=True,
)
@click.option(
    "-o",
    "--output-file",
    help="Write search results to a file instead of showing interactive menu.",
    type=click.Path(writable=True),
)
@click.option(
    "-n",
    "--num-results",
    help="Maximum number of search results to show",
    default=100,
    type=click.IntRange(min=1),
)
@click.argument("source", required=True)
@click.argument("media-type", required=True)
@click.argument("query", required=True)
@click.pass_context
@coro
async def search(ctx, first, output_file, num_results, source, media_type, query):
    """Search for content using a specific source.

    Example:

        rip search qobuz album 'rumours'
    """
    if first and output_file:
        console.print("Cannot choose --first and --output-file!")
        return
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            if first:
                await main.search_take_first(source, media_type, query)
            elif output_file:
                await main.search_output_file(
                    source, media_type, query, output_file, num_results
                )
            else:
                await main.search_interactive(source, media_type, query)
            await main.resolve()
            await main.rip()


@rip.command()
@click.option("-s", "--source", help="The source to search tracks on.")
@click.option(
    "-fs",
    "--fallback-source",
    help="The source to search tracks on if no results were found with the main source.",
)
@click.argument("url", required=True)
@click.pass_context
@coro
async def lastfm(ctx, source, fallback_source, url):
    """Download tracks from a last.fm playlist."""
    config = ctx.obj["config"]
    if source is not None:
        config.session.lastfm.source = source
    if fallback_source is not None:
        config.session.lastfm.fallback_source = fallback_source
    with config as cfg:
        async with Main(cfg) as main:
            await main.resolve_lastfm(url)
            await main.rip()


@rip.command()
@click.argument("source")
@click.argument("media-type")
@click.argument("id")
@click.pass_context
@coro
async def id(ctx, source, media_type, id):
    """Download an item by ID."""
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            await main.add_by_id(source, media_type, id)
            await main.resolve()
            await main.rip()


async def latest_streamrip_version(verify_ssl: bool = True) -> tuple[str, str | None]:
    """Get the latest streamrip version from PyPI and release notes from GitHub.

    Args:
        verify_ssl: Whether to verify SSL certificates

    Returns:
        A tuple of (version, release_notes)
    """
    # Create connector with appropriate SSL settings
    connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
    connector = aiohttp.TCPConnector(**connector_kwargs)

    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get("https://pypi.org/pypi/streamrip/json") as resp:
            data = await resp.json()
        version = data["info"]["version"]

        if version == __version__:
            return version, None

        async with s.get(
            "https://api.github.com/repos/nathom/streamrip/releases/latest"
        ) as resp:
            json = await resp.json()
        notes = json["body"]
    return version, notes


if __name__ == "__main__":
    rip()
