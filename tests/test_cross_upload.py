from functools import partial
from pathlib import Path
from types import SimpleNamespace

import anyio
from torf import Torrent

import salmon.cross_upload as cross_upload_module
import salmon.uploader.torrent_client as torrent_client_module
from salmon.common import UploadFiles
from salmon.cross_upload import (
    GeneratedTorrent,
    QBittorrentInput,
    _compile_data,
    _conversion_options,
    _cross_seed_client_url,
    _input_items,
    _local_qbittorrent_path,
    _missing_conversions,
    _reintroduce_torrents,
    _select_qbittorrent_torrents,
    _source_response,
    _upload_conversions,
)
from salmon.uploader.torrent_client import (
    QBittorrentClient,
    TorrentClientGenerator,
    TorrentClientTorrent,
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


def test_qbittorrent_name_selection_uses_server_content_path(tmp_path: Path, monkeypatch) -> None:
    first_path = tmp_path / "Artist - Album [FLAC]"
    second_path = tmp_path / "Artist - Album [MP3 320]"
    second_path.mkdir()
    first = TorrentClientTorrent(
        name=first_path.name,
        hash="abc",
        content_path=str(first_path),
        save_path=str(tmp_path),
        category="music",
    )
    second = TorrentClientTorrent(
        name=second_path.name,
        hash="def",
        content_path=str(second_path),
        save_path=str(tmp_path),
        category="music",
    )
    responses = iter(("bad", "2"))

    async def fake_prompt(*_args, **_kwargs):
        return next(responses)

    messages = []
    monkeypatch.setattr(cross_upload_module.click, "prompt", fake_prompt)
    monkeypatch.setattr(cross_upload_module.click, "secho", lambda message, **_kwargs: messages.append(message))

    selected = anyio.run(_select_qbittorrent_torrents, [first, second])

    assert selected == [second]
    assert any("numeric IDs" in message for message in messages)
    assert _local_qbittorrent_path(second) == second_path


def test_qbittorrent_search_returns_completed_name_matches() -> None:
    calls = []

    class Api:
        def torrents_info(self, **kwargs):
            calls.append(kwargs)
            return [
                {
                    "name": "Other release",
                    "hash": "000",
                    "content_path": "/downloads/Other release",
                    "save_path": "/downloads",
                },
                {
                    "name": "Artist - Album [FLAC]",
                    "hash": "ABC",
                    "content_path": "/downloads/Artist - Album [FLAC]",
                    "save_path": "/downloads",
                    "category": "music",
                },
            ]

    client = object.__new__(QBittorrentClient)
    client.client = Api()

    assert client.search_torrents("artist - album") == [
        TorrentClientTorrent(
            name="Artist - Album [FLAC]",
            hash="ABC",
            content_path="/downloads/Artist - Album [FLAC]",
            save_path="/downloads",
            category="music",
        )
    ]
    assert calls == [{"status_filter": "completed"}]


def test_qui_proxy_url_is_used_as_qbittorrent_api_base(monkeypatch) -> None:
    proxy_url = "http://127.0.0.1:7476/proxy/client-api-key"
    client_args = []
    messages = []

    class Api:
        def __init__(self, **kwargs):
            client_args.append(kwargs)

        def auth_log_in(self):
            pass

    monkeypatch.setattr(cross_upload_module.cfg.cross_seed, "torrent_client", "")
    monkeypatch.setattr(cross_upload_module.cfg.cross_seed, "qui_proxy_url", proxy_url)
    monkeypatch.setattr(torrent_client_module.qbittorrentapi, "Client", Api)
    monkeypatch.setattr(torrent_client_module.click, "secho", lambda message, **_kwargs: messages.append(message))

    assert _cross_seed_client_url() == f"qbittorrent+{proxy_url}"
    client = TorrentClientGenerator.parse_libtc_url(_cross_seed_client_url())

    assert isinstance(client, QBittorrentClient)
    assert client_args == [{"host": proxy_url, "username": None, "password": None}]
    assert all("client-api-key" not in message for message in messages)
    assert any("/proxy/****" in message for message in messages)


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
    monkeypatch.setattr(
        cross_upload_module,
        "generate_torrent",
        lambda _site, path, **_kwargs: (f"{path}.torrent", object()),
    )
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
        return ()

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

    result = anyio.run(run)
    assert (result.torrent_id, result.group_id, result.generated_torrents) == (0, 9, ())
    assert len(conversion_calls) == 1
    assert conversion_calls[0][3] == 9


def test_selected_variant_upload_joins_existing_target_group(tmp_path: Path, monkeypatch) -> None:
    uploads = []
    torrent_path = tmp_path / "target.torrent"
    torrent_path.write_bytes(b"torrent")

    class Target:
        async def upload(self, data, _files):
            uploads.append(data)
            return 101, 9

    async def fake_compile_files(*_args):
        return UploadFiles(torrent_data=b"torrent")

    monkeypatch.setattr(
        cross_upload_module,
        "_compile_data",
        lambda *_args: {
            "title": "Album",
            "artists[]": ["Artist"],
            "year": 2020,
            "releasetype": 1,
            "format": "MP3",
            "bitrate": "320",
            "media": "WEB",
            "release_desc": "description",
        },
    )
    monkeypatch.setattr(cross_upload_module, "_rehost_red_images", lambda data, _site: _async_value(data))
    monkeypatch.setattr(
        cross_upload_module,
        "generate_torrent",
        lambda *_args, **_kwargs: (str(torrent_path), object()),
    )
    monkeypatch.setattr(cross_upload_module, "compile_files", fake_compile_files)

    result = anyio.run(
        partial(
            cross_upload_module._upload_response,
            {"torrent": {"format": "MP3", "encoding": "320", "media": "WEB"}},
            SourceSite(),
            Target(),
            path=tmp_path,
            upload_group_id=9,
        )
    )

    assert uploads[0]["groupid"] == 9
    assert "title" not in uploads[0]
    assert result.generated_torrents == (GeneratedTorrent(str(torrent_path), tmp_path),)


def test_no_inject_uploads_torrent_without_writing_artifact(tmp_path: Path, monkeypatch) -> None:
    release_path = tmp_path / "Artist - Album"
    torrent_directory = tmp_path / "torrents"
    release_path.mkdir()
    torrent_directory.mkdir()
    (release_path / "track.flac").write_bytes(b"audio")
    uploaded_files = []

    class Target:
        announce = "https://tracker.example/passkey/announce"
        base_url = "https://tracker.example"
        dot_torrents_dir = str(torrent_directory)
        site_string = "TARGET"

        async def upload(self, _data, files):
            uploaded_files.append(files)
            return 101, 9

    monkeypatch.setattr(
        cross_upload_module,
        "_compile_data",
        lambda *_args: {"format": "FLAC", "bitrate": "Lossless", "media": "WEB"},
    )
    monkeypatch.setattr(cross_upload_module, "_rehost_red_images", lambda data, _site: _async_value(data))

    result = anyio.run(
        partial(
            cross_upload_module._upload_response,
            {"torrent": {"format": "FLAC", "encoding": "Lossless", "media": "WEB"}},
            SourceSite(),
            Target(),
            path=release_path,
            inject=False,
        )
    )

    assert Torrent.read_stream(uploaded_files[0].torrent_data).name == release_path.name
    assert list(torrent_directory.iterdir()) == []
    assert result.generated_torrents == ()


def test_no_inject_skips_qbittorrent_handoff(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "Artist - Album"
    source_path.mkdir()
    item = QBittorrentInput(
        TorrentClientTorrent(
            name=source_path.name,
            hash="ABC",
            content_path=str(source_path),
            save_path=str(tmp_path),
            category="music",
        ),
        source_path,
    )

    class Site:
        base_url = "https://tracker.example"

        async def ensure_authenticated(self):
            pass

    async def fake_resolve(*_args):
        return [item], object()

    async def fake_source_response(*_args):
        return {"group": {"id": 7}, "torrent": {"id": 1}}

    async def fake_upload_response(*_args, **kwargs):
        assert kwargs["inject"] is False
        return cross_upload_module.CrossUploadResult(100, 99, ())

    monkeypatch.setattr(cross_upload_module.salmon.trackers, "tracker_list", ["RED", "OPS"])
    monkeypatch.setattr(cross_upload_module.salmon.trackers, "get_class", lambda _code: Site)
    monkeypatch.setattr(cross_upload_module, "_resolve_input_items", fake_resolve)
    monkeypatch.setattr(cross_upload_module, "_source_response", fake_source_response)
    monkeypatch.setattr(cross_upload_module, "_upload_response", fake_upload_response)
    monkeypatch.setattr(
        cross_upload_module,
        "_reintroduce_torrents",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not inject")),
    )

    anyio.run(
        partial(
            cross_upload_module.cross_upload.callback,
            "Album",
            "RED",
            "OPS",
            False,
            None,
            False,
            (),
            inject=False,
        )
    )


def test_batch_variants_reuse_first_target_group(monkeypatch) -> None:
    responses = [
        {"group": {"id": 7}, "torrent": {"id": 1}},
        {"group": {"id": 7}, "torrent": {"id": 2}},
    ]
    upload_group_ids = []

    class Site:
        base_url = "https://tracker.example"

        async def ensure_authenticated(self):
            pass

    async def fake_resolve(*_args):
        return [1, 2], None

    async def fake_source_response(*_args):
        return responses.pop(0)

    async def fake_upload_response(*_args, **kwargs):
        upload_group_ids.append(kwargs["upload_group_id"])
        return cross_upload_module.CrossUploadResult(100, 99, ())

    monkeypatch.setattr(cross_upload_module.salmon.trackers, "tracker_list", ["RED", "OPS"])
    monkeypatch.setattr(cross_upload_module.salmon.trackers, "get_class", lambda _code: Site)
    monkeypatch.setattr(cross_upload_module, "_resolve_input_items", fake_resolve)
    monkeypatch.setattr(cross_upload_module, "_source_response", fake_source_response)
    monkeypatch.setattr(cross_upload_module, "_upload_response", fake_upload_response)

    anyio.run(
        partial(
            cross_upload_module.cross_upload.callback,
            "Album",
            "RED",
            "OPS",
            False,
            None,
            False,
            (),
        )
    )

    assert upload_group_ids == [None, 99]


def test_generated_torrents_are_reintroduced_beside_server_content(tmp_path: Path) -> None:
    source_path = tmp_path / "library" / "Artist - Album [FLAC]"
    variant_path = tmp_path / "library" / "Artist - Album [MP3 320]"
    source_path.mkdir(parents=True)
    variant_path.mkdir()
    original_torrent = tmp_path / "original.torrent"
    variant_torrent = tmp_path / "variant.torrent"
    original_torrent.write_bytes(b"original")
    variant_torrent.write_bytes(b"variant")
    item = QBittorrentInput(
        torrent=TorrentClientTorrent(
            name=source_path.name,
            hash="ABC",
            content_path=str(source_path),
            save_path=str(source_path.parent),
            category="source-category",
        ),
        path=source_path,
    )
    calls = []

    class Client:
        def add_to_downloader(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

    _reintroduce_torrents(
        item,
        (
            GeneratedTorrent(str(original_torrent), source_path),
            GeneratedTorrent(str(variant_torrent), variant_path),
        ),
        "cross-seed",
        Client(),
    )

    assert calls == [
        ((str(source_path.parent), b"original"), {"is_paused": False, "label": "cross-seed"}),
        ((str(variant_path.parent), b"variant"), {"is_paused": False, "label": "cross-seed"}),
    ]


async def _async_value(value):
    return value


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
