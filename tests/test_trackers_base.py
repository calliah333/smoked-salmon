import time

import anyio
import pytest

from salmon.common import UploadFiles
from salmon.trackers.base import BaseGazelleApi, _SlidingWindowRateLimiter


@pytest.mark.parametrize("api_key", ["api-key", ""])
def test_upload_shares_strict_request_rate_limit(monkeypatch, api_key: str) -> None:
    request_starts: list[float] = []

    class Response:
        def __init__(self, url: str) -> None:
            self.url = url
            self.status = 200
            self.ok = True
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self) -> str:
            if "ajax.php" in self.url:
                return '{"status":"success","response":{"torrentid":1,"groupid":2}}'
            return (
                '<a class="tooltip" href="torrents.php?torrentid=1"></a>'
                '<a class="brackets" href="upload.php?groupid=2"></a>'
            )

    class Session:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def request(self, _method: str, url: str, **_kwargs) -> Response:
            request_starts.append(time.monotonic())
            return Response(url)

    tracker = object.__new__(BaseGazelleApi)
    tracker.api_key = api_key
    tracker.authkey = "authkey"
    tracker.passkey = "passkey"
    tracker.cookie = "cookie"
    tracker.base_url = "https://tracker.example"
    tracker.tracker_url = "https://announce.example"
    tracker.site_string = "TEST"
    tracker.headers = {}
    tracker._authenticated = True
    tracker._rate_limiter = _SlidingWindowRateLimiter(4, 0.05)
    monkeypatch.setattr("salmon.trackers.base.aiohttp.ClientSession", Session)

    async def fill_window_and_upload() -> None:
        for _ in range(4):
            response = await tracker.api_call("index")
            assert response == {"torrentid": 1, "groupid": 2}

        result = await tracker.upload({}, UploadFiles(torrent_data=b"torrent"))
        assert result == (1, 2)

    anyio.run(fill_window_and_upload)

    assert len(request_starts) == 5
    assert request_starts[4] - request_starts[0] >= 0.045
