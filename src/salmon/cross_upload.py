import html
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import anyio
import asyncclick as click
from torf import Torrent

import salmon.trackers
from salmon import cfg
from salmon.common import commandgroup
from salmon.constants import ARTIST_IMPORTANCES
from salmon.converter.downconverting import convert_folder, generate_conversion_description
from salmon.converter.transcoding import generate_transcode_description, transcode_folder
from salmon.images import HOSTS
from salmon.uploader.dupe_checker import (
    check_existing_group,
    generate_dupe_check_searchstrs,
    get_search_results,
)
from salmon.uploader.torrent_client import (
    QBittorrentClient,
    TorrentClientGenerator,
    TorrentClientTorrent,
)
from salmon.uploader.upload import compile_files, generate_torrent

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


_ARTIST_FIELDS = {
    "artists": "main",
    "with": "guest",
    "remixedBy": "remixer",
    "composers": "composer",
    "conductor": "conductor",
    "dj": "djcompiler",
    "producer": "producer",
}

_RED_IMAGE_URL = re.compile(
    r"https?://redacted\.sh/t/[^\s\[\]\"'<>]+",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class QBittorrentInput:
    torrent: TorrentClientTorrent
    path: Path


@dataclass(frozen=True)
class GeneratedTorrent:
    torrent_data: bytes
    content_path: Path
    torrent_name: str


@dataclass(frozen=True)
class CrossUploadResult:
    torrent_id: int
    group_id: int
    generated_torrents: tuple[GeneratedTorrent, ...]
    skipped: bool = False


@commandgroup.command()
@click.option(
    "--inject/--no-inject",
    default=True,
    help="Add target torrents to qBittorrent; --no-inject also avoids writing .torrent files to disk.",
)
@click.option("--downconvert", is_flag=True, help="Also upload a 16-bit FLAC downconversion.")
@click.option(
    "--target-group-id",
    type=click.IntRange(min=1),
    help="Skip the source format and add conversions to this existing target group.",
)
@click.option(
    "--all-formats",
    "--all",
    is_flag=True,
    help="Upload every possible downconversion and MP3 transcode.",
)
@click.option(
    "--transcode",
    "transcodes",
    type=click.Choice(("320", "V0"), case_sensitive=False),
    multiple=True,
    help="Also upload an MP3 transcode; may be passed more than once.",
)
@click.option(
    "--input",
    "additional_inputs",
    multiple=True,
    help="Additional torrent URL, ID, .torrent path, directory, or qBittorrent name search; repeat as needed.",
)
@click.option(
    "--also-source",
    is_flag=True,
    help="Also upload requested downconversions and transcodes to each source tracker group.",
)
@click.argument("torrent_or_directory", metavar="INPUT")
@click.argument(
    "source",
    metavar="SOURCE_TRACKER",
    type=click.Choice(tuple(salmon.trackers.tracker_classes), case_sensitive=False),
)
@click.argument(
    "target",
    metavar="TARGET_TRACKER",
    type=click.Choice(tuple(salmon.trackers.tracker_classes), case_sensitive=False),
)
async def cross_upload(
    torrent_or_directory: str,
    source: str,
    target: str,
    downconvert: bool,
    target_group_id: int | None,
    all_formats: bool,
    transcodes: tuple[str, ...],
    inject: bool = True,
    additional_inputs: tuple[str, ...] = (),
    also_source: bool = False,
) -> None:
    """Cross-upload torrents from SOURCE_TRACKER to TARGET_TRACKER.

    INPUT and each repeatable --input value may be a source torrent URL/ID,
    a .torrent file, a directory of .torrent files, or a qBittorrent
    torrent-name search. Name searches use the dedicated cross_seed client
    running on this server and allow selecting multiple completed torrents.
    Tracker choices are RED, OPS, and DIC.

    Salmon reads each selected release from the absolute content_path reported
    by qBittorrent. That path must exist locally; no files are downloaded or
    copied by this command.
    With --no-inject, generated torrent data is uploaded from memory and no
    target .torrent files are written or added to qBittorrent.

    \b
    Examples:
      salmon cross-upload 456 RED OPS --all
      salmon cross-upload "Album A" RED OPS --input "Album B" --all --also-source
      salmon cross-upload 456 RED OPS --target-group-id 123 --transcode 320 --transcode V0
    """
    source, target = source.upper(), target.upper()
    if also_source and not (downconvert or all_formats or transcodes):
        raise click.UsageError("--also-source requires --all, --downconvert, or --transcode.")
    if source == target:
        raise click.UsageError("SOURCE and TARGET must be different trackers.")
    missing = [code for code in (source, target) if code not in salmon.trackers.tracker_list]
    if missing:
        raise click.UsageError(f"Tracker(s) not configured: {', '.join(missing)}")

    source_site = salmon.trackers.get_class(source)()
    target_site = salmon.trackers.get_class(target)()
    items, qbit = await _resolve_input_items((torrent_or_directory, *additional_inputs), source_site)
    if target_group_id and len(items) != 1:
        raise click.UsageError("--target-group-id requires a single torrent input, not batch mode.")
    await target_site.ensure_authenticated()
    if also_source:
        await source_site.ensure_authenticated()

    failures = 0
    target_groups: dict[int, int] = {}
    for item in items:
        try:
            response = await _source_response(item, source_site)
            source_group_id = _source_group_id(response)
            upload_group_id = target_groups.get(source_group_id) if source_group_id is not None else None
            result = await _upload_response(
                response,
                source_site,
                target_site,
                path=item.path if isinstance(item, QBittorrentInput) else None,
                upload_group_id=upload_group_id,
                downconvert=downconvert,
                target_group_id=target_group_id,
                all_formats=all_formats,
                transcodes=transcodes,
                inject=inject,
                persist_torrents=inject and not isinstance(item, QBittorrentInput),
            )
            if source_group_id is not None:
                target_groups[source_group_id] = result.group_id
            source_generated_torrents: tuple[GeneratedTorrent, ...] = ()
            if also_source and response["torrent"]["format"] == "FLAC":
                if source_group_id is None:
                    raise click.ClickException("Cannot upload source conversions without the source group ID.")
                source_result = await _upload_response(
                    response,
                    source_site,
                    source_site,
                    path=item.path if isinstance(item, QBittorrentInput) else None,
                    target_group_id=source_group_id,
                    downconvert=downconvert,
                    all_formats=all_formats,
                    transcodes=transcodes,
                    inject=inject,
                    persist_torrents=inject and not isinstance(item, QBittorrentInput),
                )
                source_generated_torrents = source_result.generated_torrents
                click.secho(
                    f"Processed requested conversions on {source}: "
                    f"{source_site.base_url}/torrents.php?id={source_group_id}",
                    fg="green",
                )
            if inject and isinstance(item, QBittorrentInput):
                if qbit is None:
                    raise click.ClickException("qBittorrent input lost its client connection.")
                _reintroduce_torrents(
                    item,
                    (*result.generated_torrents, *source_generated_torrents),
                    cfg.cross_seed.label,
                    qbit,
                )
            if not result.skipped:
                if result.torrent_id:
                    click.secho(
                        f"Uploaded: {target_site.base_url}/torrents.php?id={result.group_id}"
                        f"&torrentid={result.torrent_id}",
                        fg="green",
                    )
                else:
                    click.secho(
                        f"Uploaded conversions to: {target_site.base_url}/torrents.php?id={result.group_id}",
                        fg="green",
                    )
        except Exception as error:
            if len(items) == 1:
                if isinstance(error, click.ClickException):
                    raise
                raise click.ClickException(str(error)) from error
            failures += 1
            click.secho(f"Failed {_item_name(item)}: {error}", fg="red", err=True)

    if failures:
        raise click.ClickException(f"{failures} of {len(items)} torrents failed.")


async def _resolve_input_items(
    values: str | tuple[str, ...],
    source_site: "BaseGazelleApi",
) -> tuple[list[int | Path | QBittorrentInput], QBittorrentClient | None]:
    values = (values,) if isinstance(values, str) else values
    items: list[int | Path | QBittorrentInput] = []
    client: QBittorrentClient | None = None

    for value in dict.fromkeys(values):
        try:
            items.extend(_input_items(value, source_site))
            continue
        except click.UsageError:
            if not _is_name_search(value):
                raise

        client = client or _configured_qbittorrent()
        matches = client.search_torrents(value)
        if not matches:
            raise click.UsageError(f"No completed qBittorrent torrents match {value!r}.")
        selected = await _select_qbittorrent_torrents(matches)
        items.extend(
            QBittorrentInput(torrent=torrent, path=_local_qbittorrent_path(torrent)) for torrent in selected
        )

    unique: dict[tuple[str, str], int | Path | QBittorrentInput] = {}
    for item in items:
        if isinstance(item, QBittorrentInput):
            key = ("qbit", item.torrent.hash.upper())
        elif isinstance(item, Path):
            key = ("path", str(item.resolve()))
        else:
            key = ("id", str(item))
        unique.setdefault(key, item)
    return list(unique.values()), client


def _is_name_search(value: str) -> bool:
    stripped = value.strip()
    return (
        bool(stripped)
        and not stripped.isdigit()
        and not urlparse(stripped).scheme
        and not any(separator in stripped for separator in ("/", "\\"))
    )


def _cross_seed_client_url() -> str:
    client_url = cfg.cross_seed.torrent_client.strip()
    qui_proxy_url = cfg.cross_seed.qui_proxy_url.strip()
    if client_url and qui_proxy_url:
        raise click.UsageError("Configure only one of cross_seed.torrent_client or cross_seed.qui_proxy_url.")
    if qui_proxy_url:
        parsed = urlparse(qui_proxy_url)
        proxy_key = parsed.path.rpartition("/proxy/")[2]
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not proxy_key or "/" in proxy_key:
            raise click.UsageError(
                "cross_seed.qui_proxy_url must be the complete http(s)://host/proxy/client-api-key URL from qui."
            )
        return f"qbittorrent+{qui_proxy_url}"
    if not client_url:
        raise click.UsageError("Torrent-name searches require cross_seed.torrent_client or cross_seed.qui_proxy_url.")
    if urlparse(client_url).scheme.split("+", 1)[0] != "qbittorrent":
        raise click.UsageError("cross_seed.torrent_client must be a qbittorrent+http(s) URL.")
    return client_url


def _configured_qbittorrent() -> QBittorrentClient:
    client = TorrentClientGenerator.parse_libtc_url(_cross_seed_client_url())
    if not isinstance(client, QBittorrentClient):
        raise click.UsageError("The cross-seed client is not configured for qBittorrent.")
    return client


async def _select_qbittorrent_torrents(matches: list[TorrentClientTorrent]) -> list[TorrentClientTorrent]:
    if len(matches) == 1:
        click.secho(f"Using qBittorrent torrent: {matches[0].name}", fg="green")
        return matches

    click.secho("\nCompleted qBittorrent torrents matching the name:", fg="cyan", bold=True)
    for index, torrent in enumerate(matches, 1):
        click.echo(f"  {index}. {torrent.name}")
    if cfg.upload.yes_all:
        return matches

    while True:
        selection = await click.prompt(
            click.style('Select torrents (space-separated IDs, or "*" for all)', fg="magenta"),
            default="*",
        )
        try:
            indices = _selection_indices(selection, len(matches))
        except ValueError as error:
            click.secho(str(error), fg="red")
            continue
        return [matches[index - 1] for index in indices]


def _selection_indices(value: str, count: int) -> list[int]:
    if value.strip() == "*":
        return list(range(1, count + 1))
    values = value.split()
    if not values or any(not item.isdigit() for item in values):
        raise ValueError("Enter space-separated numeric IDs or *.")
    indices = list(dict.fromkeys(int(item) for item in values))
    invalid = [index for index in indices if index < 1 or index > count]
    if invalid:
        raise ValueError(f"Invalid choices: {invalid}. Enter numbers between 1 and {count}.")
    return indices


def _local_qbittorrent_path(torrent: TorrentClientTorrent) -> Path:
    path = Path(torrent.content_path).expanduser().resolve()
    if path.is_dir():
        return path
    raise click.ClickException(
        f"qBittorrent content for {torrent.name!r} was not found at {path}. "
        "Run salmon on the qBittorrent server with access to the same filesystem."
    )


def _reintroduce_torrents(
    item: QBittorrentInput,
    generated_torrents: tuple[GeneratedTorrent, ...],
    label: str,
    client: QBittorrentClient,
) -> None:
    category = label or item.torrent.category

    for generated in generated_torrents:
        content_path = generated.content_path.resolve()
        if generated.torrent_name != content_path.name:
            raise click.ClickException(
                f"Cannot cross-seed {generated.torrent_name!r} at {content_path}: "
                "the torrent root name does not match the existing content directory."
            )
        save_path = str(content_path.parent)
        click.secho(f"Linking {generated.torrent_name} to existing content at {content_path}", fg="cyan")
        if not client.add_to_downloader(save_path, generated.torrent_data, is_paused=False, label=category):
            raise click.ClickException(f"Uploaded torrent could not be added to qBittorrent at {content_path}.")


def _item_name(item: int | Path | QBittorrentInput) -> str:
    return item.torrent.name if isinstance(item, QBittorrentInput) else str(item)


def _source_group_id(response: dict[str, Any]) -> int | None:
    value = response.get("group", {}).get("id") or response.get("torrent", {}).get("groupId")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _input_items(value: str, source_site: "BaseGazelleApi") -> list[int | Path]:
    path = Path(value).expanduser()
    if path.is_dir():
        torrents = sorted(path.glob("*.torrent"))
        if not torrents:
            raise click.UsageError(f"No .torrent files found in {path}.")
        return torrents
    if path.is_file():
        return [path]
    return [_torrent_id(value, source_site)]


def _torrent_id(value: str, source_site: "BaseGazelleApi") -> int:
    if value.strip().isdigit():
        return int(value)

    parsed = urlparse(value)
    if parsed.hostname != urlparse(source_site.base_url).hostname:
        raise click.UsageError(f"Expected a torrent URL from {source_site.base_url}, an ID, or a local path.")
    torrent_ids = parse_qs(parsed.query).get("torrentid")
    if not torrent_ids or not torrent_ids[0].isdigit():
        raise click.UsageError("Torrent URL must contain a numeric torrentid parameter.")
    return int(torrent_ids[0])


async def _source_response(item: int | Path | QBittorrentInput, source_site: "BaseGazelleApi") -> dict[str, Any]:
    if isinstance(item, int):
        return await source_site.api_call("torrent", params={"id": item})
    if isinstance(item, QBittorrentInput):
        return await source_site.api_call("torrent", params={"hash": item.torrent.hash.upper()})

    torrent = Torrent.read(item)
    source_host = urlparse(source_site.tracker_url).hostname
    announce_hosts = {urlparse(url).hostname for tier in torrent.trackers for url in tier}
    if source_host not in announce_hosts:
        raise click.ClickException("announce host does not match the source tracker")
    return await source_site.api_call("torrent", params={"hash": torrent.infohash.upper()})


async def _upload_response(
    response: dict[str, Any],
    source_site: "BaseGazelleApi",
    target_site: "BaseGazelleApi",
    *,
    path: Path | None = None,
    upload_group_id: int | None = None,
    downconvert: bool = False,
    target_group_id: int | None = None,
    all_formats: bool = False,
    transcodes: tuple[str, ...] = (),
    inject: bool = True,
    persist_torrents: bool | None = None,
) -> CrossUploadResult:
    source_torrent = response["torrent"]
    downconvert, transcodes = _conversion_options(source_torrent, downconvert, transcodes, all_formats)

    path = path or _release_path(response)
    data = _compile_data(response, source_site, target_site)
    if target_group_id:
        if upload_group_id:
            raise click.UsageError("Cannot upload a selected torrent and conversions to two target groups.")
        if not downconvert and not transcodes:
            raise click.UsageError("--target-group-id requires --all, --downconvert, or --transcode.")
        generated_torrents = await _upload_conversions(
            path,
            data,
            target_site,
            target_group_id,
            f"{target_site.base_url}/torrents.php?id={target_group_id}",
            source_torrent["media"],
            downconvert,
            transcodes,
            inject,
            persist_torrents,
        )
        return CrossUploadResult(0, target_group_id, generated_torrents)

    if not upload_group_id:
        searchstrs = _target_searchstrs(data)
        if searchstrs:
            click.secho(
                f"Checking {getattr(target_site, 'site_string', target_site.base_url)} "
                "for existing groups and torrents...",
                fg="cyan",
                nl=False,
            )
            results = await get_search_results(target_site, searchstrs)
            duplicate_group_id = await _find_duplicate_target_group(target_site, results, data)
            click.secho(" done.", fg="cyan")
            if duplicate_group_id:
                return await _use_existing_target(
                    path,
                    data,
                    target_site,
                    duplicate_group_id,
                    source_torrent["media"],
                    downconvert,
                    transcodes,
                    inject,
                    persist_torrents,
                )
            upload_group_id = await check_existing_group(
                target_site,
                searchstrs,
                offer_deletion=False,
                results=results,
            )
    if upload_group_id:
        target_group = await target_site.torrentgroup(upload_group_id)
        if _has_variant(target_group, data, data["format"], data["bitrate"]):
            return await _use_existing_target(
                path,
                data,
                target_site,
                upload_group_id,
                source_torrent["media"],
                downconvert,
                transcodes,
                inject,
                persist_torrents,
            )
        data = _existing_group_data(data, upload_group_id)
    data = await _rehost_red_images(data, source_site)
    if persist_torrents is None:
        persist_torrents = inject
    torrent_path, torrent = generate_torrent(target_site, str(path), write=persist_torrents)
    files = await compile_files(str(path), torrent, {"source": source_torrent["media"]})
    torrent_source = torrent_path or "an in-memory torrent"
    click.secho(f"Uploading {path.name} using {torrent_source}...", fg="yellow")
    torrent_id, group_id = await target_site.upload(data, files)

    generated_torrents = (
        [GeneratedTorrent(files.torrent_data, path, str(torrent.name))]
        if inject
        else []
    )
    if downconvert or transcodes:
        original_url = f"{target_site.base_url}/torrents.php?id={group_id}&torrentid={torrent_id}"
        generated_torrents.extend(
            await _upload_conversions(
                path,
                data,
                target_site,
                group_id,
                original_url,
                source_torrent["media"],
                downconvert,
                transcodes,
                inject,
                persist_torrents,
            )
        )
    return CrossUploadResult(torrent_id, group_id, tuple(generated_torrents))


async def _use_existing_target(
    path: Path,
    data: dict[str, Any],
    target_site: "BaseGazelleApi",
    group_id: int,
    media: str,
    downconvert: bool,
    transcodes: tuple[str, ...],
    inject: bool,
    persist_torrents: bool | None,
) -> CrossUploadResult:
    click.secho(
        f"Skipping {path.name}: {data['format']} {data['bitrate']} already exists in "
        f"{target_site.base_url}/torrents.php?id={group_id}.",
        fg="yellow",
    )
    if not downconvert and not transcodes:
        return CrossUploadResult(0, group_id, (), skipped=True)

    generated_torrents = await _upload_conversions(
        path,
        data,
        target_site,
        group_id,
        f"{target_site.base_url}/torrents.php?id={group_id}",
        media,
        downconvert,
        transcodes,
        inject,
        persist_torrents,
    )
    return CrossUploadResult(0, group_id, generated_torrents)


def _target_searchstrs(data: dict[str, Any]) -> list[str]:
    importances = data.get("importance[]", [])
    artists = [
        [
            artist,
            "main"
            if index < len(importances) and str(importances[index]) == str(ARTIST_IMPORTANCES["main"])
            else "guest",
        ]
        for index, artist in enumerate(data.get("artists[]", []))
    ]
    return [
        searchstr
        for searchstr in generate_dupe_check_searchstrs(
            artists,
            data.get("title"),
            data.get("catalogue_number"),
        )
        if searchstr.strip()
    ]


def _existing_group_data(data: dict[str, Any], group_id: int) -> dict[str, Any]:
    group_fields = {
        "title",
        "artists[]",
        "importance[]",
        "year",
        "record_label",
        "catalogue_number",
        "releasetype",
        "tags",
        "image",
    }
    return {
        **{key: value for key, value in data.items() if key not in group_fields},
        "groupid": group_id,
    }


async def _find_duplicate_target_group(
    target_site: "BaseGazelleApi",
    results: list[dict[str, Any]],
    data: dict[str, Any],
) -> int | None:
    for result in results:
        try:
            group_id = int(result["groupId"])
        except (KeyError, TypeError, ValueError):
            continue
        target_group = await target_site.torrentgroup(group_id)
        if _same_group(target_group["group"], data) and _has_variant(
            target_group,
            data,
            data["format"],
            data["bitrate"],
        ):
            return group_id
    return None


def _same_group(group: dict[str, Any], data: dict[str, Any]) -> bool:
    if _normalized(group.get("name")) != _normalized(data.get("title")):
        return False
    if _normalized(group.get("year")) != _normalized(data.get("year")):
        return False

    importances = data.get("importance[]", [])
    expected_artists = {
        _normalized(artist)
        for index, artist in enumerate(data.get("artists[]", []))
        if index < len(importances) and str(importances[index]) == str(ARTIST_IMPORTANCES["main"])
    }
    actual_artists = {
        _normalized(artist.get("name"))
        for artist in group.get("musicInfo", {}).get("artists", [])
        if artist.get("name")
    }
    return not expected_artists or not actual_artists or expected_artists == actual_artists


def _conversion_options(
    source_torrent: dict[str, Any],
    downconvert: bool,
    transcodes: tuple[str, ...],
    all_formats: bool,
) -> tuple[bool, tuple[str, ...]]:
    if all_formats and source_torrent["format"] == "FLAC":
        downconvert = downconvert or source_torrent["encoding"] == "24bit Lossless"
        transcodes = tuple(dict.fromkeys((*transcodes, "320", "V0")))
    if (downconvert or transcodes) and source_torrent["format"] != "FLAC":
        raise click.ClickException("Only FLAC torrents can be downconverted or transcoded.")
    if downconvert and source_torrent["encoding"] != "24bit Lossless":
        raise click.ClickException("--downconvert requires a 24bit Lossless source torrent.")
    return downconvert, transcodes


def _has_variant(
    target_group: dict[str, Any],
    data: dict[str, Any],
    format_: str,
    encoding: str,
) -> bool:
    group = target_group["group"]
    expected = (
        data["media"],
        format_,
        encoding,
        data.get("remaster_year") or data.get("year"),
        data.get("remaster_title"),
        data.get("remaster_record_label") or data.get("record_label"),
        data.get("remaster_catalogue_number") or data.get("catalogue_number"),
    )
    for torrent in target_group["torrents"]:
        actual = (
            torrent["media"],
            torrent["format"],
            torrent["encoding"],
            torrent.get("remasterYear") or group.get("year"),
            torrent.get("remasterTitle"),
            torrent.get("remasterRecordLabel") or group.get("recordLabel"),
            torrent.get("remasterCatalogueNumber") or group.get("catalogueNumber"),
        )
        if tuple(_normalized(value) for value in actual) == tuple(_normalized(value) for value in expected):
            return True
    return False


async def _missing_conversions(
    target_site: "BaseGazelleApi",
    group_id: int,
    data: dict[str, Any],
    downconvert: bool,
    transcodes: tuple[str, ...],
) -> tuple[bool, tuple[str, ...]]:
    target_group = await target_site.torrentgroup(group_id)

    def has_variant(format_: str, encoding: str) -> bool:
        return _has_variant(target_group, data, format_, encoding)

    if downconvert and has_variant("FLAC", "Lossless"):
        click.secho("Skipping 16-bit FLAC: it already exists in the target group.", fg="yellow")
        downconvert = False

    missing_transcodes = []
    for bitrate in dict.fromkeys(transcodes):
        encoding = "V0 (VBR)" if bitrate == "V0" else "320"
        if has_variant("MP3", encoding):
            click.secho(f"Skipping MP3 {bitrate}: it already exists in the target group.", fg="yellow")
        else:
            missing_transcodes.append(bitrate)
    return downconvert, tuple(missing_transcodes)


def _normalized(value: Any) -> str:
    return html.unescape(str(value or "")).strip().casefold()


async def _upload_conversions(
    path: Path,
    original_data: dict[str, Any],
    target_site: "BaseGazelleApi",
    group_id: int,
    original_url: str,
    media: str,
    downconvert: bool,
    transcodes: tuple[str, ...],
    inject: bool = True,
    persist_torrents: bool | None = None,
) -> tuple[GeneratedTorrent, ...]:
    downconvert, transcodes = await _missing_conversions(
        target_site,
        group_id,
        original_data,
        downconvert,
        transcodes,
    )
    if not downconvert and not transcodes:
        click.secho("All requested conversions already exist in the target group.", fg="yellow")
        return ()

    base_data = _existing_group_data(original_data, group_id)

    variants: list[tuple[str, str, dict[str, Any]]] = []
    if downconvert:
        sample_rate, converted_path = await convert_folder(str(path))
        variants.append(
            (
                "16-bit FLAC",
                converted_path,
                {
                    **base_data,
                    "format": "FLAC",
                    "bitrate": "Lossless",
                    "vbr": False,
                    "release_desc": generate_conversion_description(original_url, sample_rate),
                },
            )
        )
    for bitrate in dict.fromkeys(transcodes):
        transcoded_path = await transcode_folder(str(path), bitrate)
        variants.append(
            (
                f"MP3 {bitrate}",
                transcoded_path,
                {
                    **base_data,
                    "format": "MP3",
                    "bitrate": "V0 (VBR)" if bitrate == "V0" else "320",
                    "vbr": bitrate == "V0",
                    "release_desc": generate_transcode_description(original_url, bitrate),
                },
            )
        )

    generated_torrents = []
    if persist_torrents is None:
        persist_torrents = inject
    for label, variant_path, data in variants:
        torrent_path, torrent = generate_torrent(target_site, variant_path, write=persist_torrents)
        files = await compile_files(variant_path, torrent, {"source": media})
        torrent_source = torrent_path or "an in-memory torrent"
        click.secho(f"Uploading {label} using {torrent_source}...", fg="yellow")
        torrent_id, _ = await target_site.upload(data, files)
        click.secho(f"Uploaded {label}: {target_site.base_url}/torrents.php?torrentid={torrent_id}", fg="green")
        if inject:
            generated_torrents.append(
                GeneratedTorrent(files.torrent_data, Path(variant_path), str(torrent.name))
            )
    return tuple(generated_torrents)


async def _rehost_red_images(data: dict[str, Any], source_site: "BaseGazelleApi") -> dict[str, Any]:
    if source_site.site_code != "RED":
        return data

    rewritten = data.copy()
    replacements: dict[tuple[str, str], str] = {}
    fields = {
        "image": cfg.image.cover_uploader,
        "album_desc": cfg.image.image_uploader,
        "release_desc": cfg.image.image_uploader,
    }
    for field, image_host in fields.items():
        value = str(rewritten.get(field) or "")
        for url in dict.fromkeys(_RED_IMAGE_URL.findall(value)):
            key = image_host, url
            if key not in replacements:
                click.secho(f"Rehosting RED image to {image_host}: {url}", fg="yellow")
                replacements[key] = await _rehost_red_image(url, source_site, image_host)
            value = value.replace(url, replacements[key])
        rewritten[field] = value
    return rewritten


async def _rehost_red_image(url: str, source_site: "BaseGazelleApi", image_host: str) -> str:
    suffix = Path(urlparse(url).path).suffix or ".jpg"
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {**source_site.headers, "Referer": f"{source_site.base_url}/"}
    try:
        async with (
            aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
                cookies=source_site._get_cookies(),
            ) as session,
            session.get(url) as response,
        ):
            if response.status >= 400 or not response.content_type.startswith("image/"):
                raise click.ClickException(f"Could not download RED image {url} (HTTP {response.status}).")
            content = await response.read()
    except (aiohttp.ClientError, TimeoutError) as error:
        raise click.ClickException(f"Could not download RED image {url}: {error}") from error

    with TemporaryDirectory() as directory:
        image_path = Path(directory) / f"image{suffix}"
        await anyio.Path(image_path).write_bytes(content)
        uploaded_url, _ = await HOSTS[image_host].ImageUploader().upload_file(str(image_path))
    return uploaded_url


def _release_path(response: dict[str, Any]) -> Path:
    root = Path(cfg.directory.download_directory).expanduser().resolve()
    path = (root / html.unescape(response["torrent"]["filePath"])).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise click.ClickException("Source torrent path is outside download_directory.") from error
    if not path.is_dir():
        raise click.ClickException(f"Release files not found at {path}.")
    return path


def _compile_data(
    response: dict[str, Any], source_site: "BaseGazelleApi", target_site: "BaseGazelleApi"
) -> dict[str, Any]:
    group, torrent = response["group"], response["torrent"]
    artists: list[str] = []
    importances: list[int] = []
    for field, importance in _ARTIST_FIELDS.items():
        for artist in group["musicInfo"].get(field, []):
            artists.append(html.unescape(artist["name"]))
            importances.append(ARTIST_IMPORTANCES[importance])
    if not artists:
        raise click.ClickException("Source torrent has no artists.")

    source_release_types = {value: name for name, value in source_site.release_types.items()}
    release_type = source_release_types.get(group["releaseType"], "Unknown")
    source_url = f"{source_site.base_url}/torrents.php?torrentid={torrent['id']}"
    description = html.unescape(torrent.get("description") or "")
    uploader_name = html.unescape(torrent.get("username") or "the original uploader")
    uploader = (
        f"[url={source_site.base_url}/user.php?id={torrent['userId']}]{uploader_name}[/url]"
        if torrent.get("userId")
        else uploader_name
    )
    cross_post = (
        f"[align=center][size=3][b]{source_site.site_code} → {target_site.site_code}[/b][/size]\n"
        f"[size=1]Original upload by {uploader} · [url={source_url}]View source torrent[/url]\n"
        "Cross-uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]smoked-salmon[/url]"
        "[/size][/align]"
    )
    media = torrent["media"]
    if target_site.site_code == "OPS" and media == "Blu-Ray":
        media = "BD"
    elif target_site.site_code != "OPS" and media == "BD":
        media = "Blu-Ray"

    return {
        "submit": True,
        "type": 0,
        "title": html.unescape(group["name"]),
        "artists[]": artists,
        "importance[]": importances,
        "year": group["year"],
        "record_label": html.unescape(group.get("recordLabel") or ""),
        "catalogue_number": html.unescape(group.get("catalogueNumber") or ""),
        "releasetype": target_site.release_types.get(release_type, target_site.release_types["Unknown"]),
        "remaster": True,
        "remaster_year": torrent.get("remasterYear") or group["year"],
        "remaster_title": html.unescape(torrent.get("remasterTitle") or ""),
        "remaster_record_label": html.unescape(torrent.get("remasterRecordLabel") or group.get("recordLabel") or ""),
        "remaster_catalogue_number": html.unescape(
            torrent.get("remasterCatalogueNumber") or group.get("catalogueNumber") or ""
        ),
        "format": torrent["format"],
        "bitrate": torrent["encoding"],
        "other_bitrate": None,
        "vbr": "VBR" in torrent["encoding"],
        "media": media,
        "tags": ",".join(group.get("tags") or []),
        "image": group.get("wikiImage") or "",
        "album_desc": group.get("bbBody") or group.get("wikiBBcode") or "",
        "release_desc": f"{cross_post}\n\n{description}",
        **({"scene": True} if torrent.get("scene") else {}),
    }
