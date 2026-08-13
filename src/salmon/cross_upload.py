import html
import re
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


@commandgroup.command()
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
) -> None:
    """Cross-upload torrents from SOURCE_TRACKER to TARGET_TRACKER.

    INPUT is a source torrent URL/ID, a .torrent file, or a directory of
    .torrent files. Tracker choices are RED, OPS, and DIC.

    \b
    Examples:
      salmon cross-upload 456 RED OPS --all
      salmon cross-upload 456 RED OPS --target-group-id 123 --transcode 320 --transcode V0
    """
    source, target = source.upper(), target.upper()
    if source == target:
        raise click.UsageError("SOURCE and TARGET must be different trackers.")

    missing = [code for code in (source, target) if code not in salmon.trackers.tracker_list]
    if missing:
        raise click.UsageError(f"Tracker(s) not configured: {', '.join(missing)}")

    source_site = salmon.trackers.get_class(source)()
    target_site = salmon.trackers.get_class(target)()
    items = _input_items(torrent_or_directory, source_site)
    if target_group_id and len(items) != 1:
        raise click.UsageError("--target-group-id requires a single torrent input, not batch mode.")
    await target_site.ensure_authenticated()

    failures = 0
    for item in items:
        try:
            response = await _source_response(item, source_site)
            torrent_id, group_id = await _upload_response(
                response,
                source_site,
                target_site,
                downconvert=downconvert,
                target_group_id=target_group_id,
                all_formats=all_formats,
                transcodes=transcodes,
            )
            if torrent_id:
                click.secho(
                    f"Uploaded: {target_site.base_url}/torrents.php?id={group_id}&torrentid={torrent_id}",
                    fg="green",
                )
            else:
                click.secho(f"Uploaded conversions to: {target_site.base_url}/torrents.php?id={group_id}", fg="green")
        except Exception as error:
            if len(items) == 1:
                if isinstance(error, click.ClickException):
                    raise
                raise click.ClickException(str(error)) from error
            failures += 1
            click.secho(f"Failed {item}: {error}", fg="red", err=True)

    if failures:
        raise click.ClickException(f"{failures} of {len(items)} torrents failed.")


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


async def _source_response(item: int | Path, source_site: "BaseGazelleApi") -> dict[str, Any]:
    if isinstance(item, int):
        return await source_site.api_call("torrent", params={"id": item})

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
    downconvert: bool = False,
    target_group_id: int | None = None,
    all_formats: bool = False,
    transcodes: tuple[str, ...] = (),
) -> tuple[int, int]:
    source_torrent = response["torrent"]
    downconvert, transcodes = _conversion_options(source_torrent, downconvert, transcodes, all_formats)

    path = _release_path(response)
    data = _compile_data(response, source_site, target_site)
    if target_group_id:
        if not downconvert and not transcodes:
            raise click.UsageError("--target-group-id requires --all, --downconvert, or --transcode.")
        await _upload_conversions(
            path,
            data,
            target_site,
            target_group_id,
            f"{target_site.base_url}/torrents.php?id={target_group_id}",
            source_torrent["media"],
            downconvert,
            transcodes,
        )
        return 0, target_group_id
    data = await _rehost_red_images(data, source_site)
    torrent_path, torrent = generate_torrent(target_site, str(path))
    files = await compile_files(str(path), torrent, {"source": source_torrent["media"]})
    click.secho(f"Uploading {path.name} using {torrent_path}...", fg="yellow")
    torrent_id, group_id = await target_site.upload(data, files)

    if downconvert or transcodes:
        original_url = f"{target_site.base_url}/torrents.php?id={group_id}&torrentid={torrent_id}"
        await _upload_conversions(
            path,
            data,
            target_site,
            group_id,
            original_url,
            source_torrent["media"],
            downconvert,
            transcodes,
        )
    return torrent_id, group_id


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


async def _missing_conversions(
    target_site: "BaseGazelleApi",
    group_id: int,
    data: dict[str, Any],
    downconvert: bool,
    transcodes: tuple[str, ...],
) -> tuple[bool, tuple[str, ...]]:
    target_group = await target_site.torrentgroup(group_id)
    group = target_group["group"]

    def has_variant(format_: str, encoding: str) -> bool:
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
) -> None:
    downconvert, transcodes = await _missing_conversions(
        target_site,
        group_id,
        original_data,
        downconvert,
        transcodes,
    )
    if not downconvert and not transcodes:
        click.secho("All requested conversions already exist in the target group.", fg="yellow")
        return

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
        "album_desc",
    }
    base_data = {key: value for key, value in original_data.items() if key not in group_fields}
    base_data["groupid"] = group_id

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

    for label, variant_path, data in variants:
        torrent_path, torrent = generate_torrent(target_site, variant_path)
        files = await compile_files(variant_path, torrent, {"source": media})
        click.secho(f"Uploading {label} using {torrent_path}...", fg="yellow")
        torrent_id, _ = await target_site.upload(data, files)
        click.secho(f"Uploaded {label}: {target_site.base_url}/torrents.php?torrentid={torrent_id}", fg="green")


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
