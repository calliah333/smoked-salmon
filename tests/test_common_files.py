import anyio

from salmon import cfg
from salmon.common.files import process_files


def test_process_files_respects_configured_concurrency(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "simultaneous_threads", 2)
    active = 0
    peak = 0

    async def process(_file: str, _index: int) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await anyio.sleep(0.01)
        active -= 1

    anyio.run(process_files, [str(index) for index in range(6)], process, "Testing")

    assert peak == 2
