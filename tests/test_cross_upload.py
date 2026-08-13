from pathlib import Path
from types import SimpleNamespace

import anyio
from torf import Torrent

import salmon.cross_upload as cross_upload_module
from salmon.common import UploadFiles
from salmon.cross_upload import (
    _compile_data,
    _conversion_options,
    _input_items,
    _missing_conversions,
    _source_response,
    _upload_conversions,
)


class SourceSite:
    base_url = "https://redacted.sh"
    tracker_url = "https://flacsfor.me"
    site_code = site_string = "RED"
    release_types = {"Demo": 17, "Unknown": 21}

    def __init__(self) -> None:
        self.params = None

    async def api_call(self, action, params):
        self.params = (action, params)
        return {"torrent": {"id": 42}}


def test_single_and_batch_inputs(tmp_path: Path) -> None:
    source = SourceSite()
    assert _input_items("42", source) == [42]
    assert _input_items("https://redacted.sh/torrents.php?id=1&torrentid=42", source) == [42]

    release = tmp_path / "release"
    release.mkdir()
    (release / "track.flac").write_bytes(b"audio")
    torrent = Torrent(release, trackers=["https://flacsfor.me/passkey/announce"], private=True, source="RED")
    torrent.generate()
    torrent_file = tmp_path / "release.torrent"
    torrent.write(torrent_file)

    assert _input_items(str(tmp_path), source) == [torrent_file]
    assert anyio.run(_source_response, torrent_file, source) == {"torrent": {"id": 42}}
    assert source.params == ("torrent", {"hash": torrent.infohash.upper()})


def test_cross_upload_data_maps_source_to_target() -> None:
    response = {
        "group": {
            "name": "Album &amp; More",
            "year": 2020,
            "releaseType": 17,
            "recordLabel": "Label",
            "catalogueNumber": "CAT-1",
            "tags": ["rock", "demo"],
            "wikiImage": "https://img.example/cover.jpg",
            "wikiBBcode": "Group notes",
            "musicInfo": {
                "artists": [{"name": "Main &amp; Artist"}],
                "with": [{"name": "Guest"}],
            },
        },
        "torrent": {
            "id": 42,
            "username": "uploader",
            "userId": 7,
            "description": "Release notes",
            "filePath": "Artist - Album",
            "remasterYear": 2021,
            "remasterTitle": "Deluxe",
            "remasterRecordLabel": "",
            "remasterCatalogueNumber": "",
            "format": "FLAC",
            "encoding": "Lossless",
            "media": "Blu-Ray",
            "scene": True,
        },
    }
    target = SimpleNamespace(
        site_code="OPS",
        release_types={"Demo": 10, "Unknown": 21},
    )

    data = _compile_data(response, SourceSite(), target)

    assert data["title"] == "Album & More"
    assert data["artists[]"] == ["Main & Artist", "Guest"]
    assert data["importance[]"] == [1, 2]
    assert data["releasetype"] == 10
    assert data["media"] == "BD"
    assert data["tags"] == "rock,demo"
    assert data["scene"] is True
    assert "torrentid=42" in data["release_desc"]
    assert "[b]RED → OPS[/b]" in data["release_desc"]
    assert "[url=https://redacted.sh/user.php?id=7]uploader[/url]" in data["release_desc"]
    assert "Cross-uploaded with" in data["release_desc"]
    assert data["release_desc"].endswith("\n\nRelease notes")


def test_conversion_uploads_share_original_group(tmp_path: Path, monkeypatch) -> None:
    async def fake_convert(_path):
        return 44100, str(tmp_path / "16bit")

    async def fake_transcode(_path, bitrate):
        return str(tmp_path / bitrate)

    async def fake_compile_files(_path, _torrent, _metadata):
        return UploadFiles(torrent_data=b"torrent")

    monkeypatch.setattr(cross_upload_module, "convert_folder", fake_convert)
    monkeypatch.setattr(cross_upload_module, "transcode_folder", fake_transcode)
    monkeypatch.setattr(cross_upload_module, "generate_torrent", lambda _site, path: (f"{path}.torrent", object()))
    monkeypatch.setattr(cross_upload_module, "compile_files", fake_compile_files)
    monkeypatch.setattr(cross_upload_module, "generate_conversion_description", lambda *_args: "16-bit description")
    monkeypatch.setattr(cross_upload_module, "generate_transcode_description", lambda _url, rate: f"{rate} description")

    class Target:
        base_url = "https://orpheus.network"

        def __init__(self):
            self.uploads = []

        async def upload(self, data, _files):
            self.uploads.append(data)
            return 100 + len(self.uploads), 9

        async def torrentgroup(self, _group_id):
            return {
                "group": {"year": 2020, "recordLabel": "Label", "catalogueNumber": "CAT-1"},
                "torrents": [],
            }

    target = Target()
    original_data = {
        "title": "Album",
        "artists[]": ["Artist"],
        "importance[]": [1],
        "year": 2020,
        "releasetype": 1,
        "format": "FLAC",
        "bitrate": "24bit Lossless",
        "media": "WEB",
        "release_desc": "original",
    }

    anyio.run(
        _upload_conversions,
        tmp_path,
        original_data,
        target,
        9,
        "https://orpheus.network/torrents.php?torrentid=99",
        "WEB",
        True,
        ("V0", "320", "V0"),
    )

    assert [(upload["format"], upload["bitrate"]) for upload in target.uploads] == [
        ("FLAC", "Lossless"),
        ("MP3", "V0 (VBR)"),
        ("MP3", "320"),
    ]
    assert all(upload["groupid"] == 9 for upload in target.uploads)
    assert all("title" not in upload for upload in target.uploads)


def test_all_formats_selects_every_possible_conversion() -> None:
    assert _conversion_options(
        {"format": "FLAC", "encoding": "24bit Lossless"},
        False,
        (),
        True,
    ) == (True, ("320", "V0"))
    assert _conversion_options(
        {"format": "FLAC", "encoding": "Lossless"},
        False,
        (),
        True,
    ) == (False, ("320", "V0"))


def test_existing_group_skips_duplicate_original(tmp_path: Path, monkeypatch) -> None:
    conversion_calls = []

    async def fake_upload_conversions(*args):
        conversion_calls.append(args)

    class Target:
        base_url = "https://orpheus.network"

        async def upload(self, _data, _files):
            raise AssertionError("original torrent must not be uploaded")

    monkeypatch.setattr(cross_upload_module, "_release_path", lambda _response: tmp_path)
    monkeypatch.setattr(cross_upload_module, "_compile_data", lambda *_args: {"format": "FLAC"})
    monkeypatch.setattr(cross_upload_module, "_upload_conversions", fake_upload_conversions)

    async def run():
        return await cross_upload_module._upload_response(
            {"torrent": {"format": "FLAC", "encoding": "Lossless", "media": "WEB"}},
            SourceSite(),
            Target(),
            target_group_id=9,
            transcodes=("320", "V0"),
        )

    assert anyio.run(run) == (0, 9)
    assert len(conversion_calls) == 1
    assert conversion_calls[0][3] == 9


def test_existing_conversions_are_filtered_before_processing() -> None:
    class Target:
        async def torrentgroup(self, _group_id):
            return {
                "group": {"year": 2020, "recordLabel": "Label", "catalogueNumber": "CAT-1"},
                "torrents": [
                    {
                        "media": "WEB",
                        "format": "FLAC",
                        "encoding": "Lossless",
                        "remasterYear": 2020,
                    },
                    {
                        "media": "WEB",
                        "format": "MP3",
                        "encoding": "V0 (VBR)",
                        "remasterYear": 2020,
                    },
                    {
                        "media": "CD",
                        "format": "MP3",
                        "encoding": "320",
                        "remasterYear": 2020,
                    },
                ],
            }

    data = {
        "media": "WEB",
        "year": 2020,
        "remaster_year": 2020,
        "record_label": "Label",
        "catalogue_number": "CAT-1",
    }

    assert anyio.run(_missing_conversions, Target(), 9, data, True, ("V0", "320")) == (False, ("320",))


def test_red_images_are_rehosted_to_configured_hosts(monkeypatch) -> None:
    cover = "https://redacted.sh/t/cover.jpg"
    inline = "https://redacted.sh/t/inline"
    calls = []

    async def fake_rehost(url, _source_site, image_host):
        calls.append((image_host, url))
        return f"https://{image_host}.example/{Path(url).name}"

    monkeypatch.setattr(cross_upload_module, "_rehost_red_image", fake_rehost)
    data = {
        "image": cover,
        "album_desc": f"[img]{cover}[/img]\n[img]{inline}[/img]",
        "release_desc": f"[img]{inline}[/img]",
    }

    result = anyio.run(
        cross_upload_module._rehost_red_images,
        data,
        SimpleNamespace(site_code="RED"),
    )

    assert all("redacted.sh/t/" not in result[field] for field in ("image", "album_desc", "release_desc"))
    assert set(calls) == {
        (cross_upload_module.cfg.image.cover_uploader, cover),
        (cross_upload_module.cfg.image.image_uploader, cover),
        (cross_upload_module.cfg.image.image_uploader, inline),
    }
