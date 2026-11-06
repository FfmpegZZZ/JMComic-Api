from pathlib import Path
from types import SimpleNamespace

from jmcomic import DownloadResult


def download_result(album, paths: list[Path]):
    return DownloadResult(
        album,
        SimpleNamespace(
            manifest_dict={
                album: SimpleNamespace(image_filepath_list=[str(p) for p in paths]),
            }
        ),
    )
