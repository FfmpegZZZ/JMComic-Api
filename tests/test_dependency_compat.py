"""Exercise dependency boundaries with real jmcomic models and PDF codecs."""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from jmcomic import JmAlbumDetail, JmApiClient, JmCategoryPage, JmModuleConfig, JmSearchPage
from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services import jm_client
from jmcomic_api.services.pdf_builder import merge_image_paths_to_pdf
from jmcomic_api.services.pdf_paths import webp_folder


def test_runtime_and_album_directory_with_real_option(tmp_path, monkeypatch):
    monkeypatch.setitem(JmModuleConfig.AFIELD_ADVICE, "jmbook", None)
    jm_client.install_album_dirname_advice()
    base_dir = tmp_path / "images"
    option_file = tmp_path / "option.yml"
    option_file.write_text(
        f"log: false\ndir_rule:\n  base_dir: {base_dir}\n  rule: Bd_Ajmbook\n"
        "client:\n  impl: api\n  domain: [example.invalid]\n"
        "  postman:\n    meta_data:\n      cookies: {test: offline}\n",
        encoding="utf-8",
    )

    runtime = jm_client.load_runtime(option_file)
    album = JmAlbumDetail(
        album_id="12345",
        scramble_id="0",
        name="Compatibility album",
        episode_list=[],
        page_count=2,
        pub_date="",
        update_date="",
        likes="0",
        views="0",
        comment_count=0,
        works=[],
        actors=[],
        authors=[],
        tags=["test"],
    )

    assert isinstance(runtime.client, JmApiClient)
    actual = Path(runtime.opt.dir_rule.decide_album_root_dir(album))
    assert actual == webp_folder(base_dir, album.id, album.title)
    assert album.page_count == 2
    assert album.tags == ["test"]


def test_search_adapter_with_real_signature_and_page(client):
    tc, runtime = client
    runtime.client = create_autospec(JmApiClient, instance=True)
    page = JmSearchPage([("12345", {"name": "Search album"})], total=161, page_number=2)
    runtime.client.search_site.return_value = page

    response = tc.get("/search?query=compatibility&page=2")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "results": [{"id": "12345", "title": "Search album"}],
        "current_page": 2,
        "has_next_page": page.page_count > 2,
        "total": 161,
    }
    runtime.client.search_site.assert_called_once_with(search_query="compatibility", page=2)


def test_category_adapter_with_real_signature_and_page(client):
    tc, runtime = client
    runtime.client = create_autospec(JmApiClient, instance=True)
    page = JmCategoryPage([("67890", {"name": "Category album"})], total=161, page_number=1)
    runtime.client.categories_filter.return_value = page

    response = tc.get("/categories?category=hanman&time=week&order_by=view")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["results"] == [{"id": "67890", "title": "Category album"}]
    assert data["has_next_page"] is (page.page_count > 1)
    runtime.client.categories_filter.assert_called_once_with(
        page=1, time="w", category="hanman", order_by="mv"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("password", [None, "12345"])
def test_pillow_jpeg_quality_and_pypdf_encryption(tmp_path, password):
    image_path = tmp_path / "page.webp"
    pixels = random.Random(0).randbytes(128 * 192 * 3)
    Image.frombytes("RGB", (128, 192), pixels).save(image_path, lossless=True)
    outputs = []

    for quality in (20, 70):
        output = tmp_path / f"quality-{quality}.pdf"
        assert (
            merge_image_paths_to_pdf([image_path], output, password=password, jpeg_quality=quality)
            == 1
        )
        reader = PdfReader(output)
        assert reader.is_encrypted is (password is not None)
        if password:
            assert reader.decrypt(password)
        assert len(reader.pages) == 1
        assert list(reader.pages[0].mediabox) == [0, 0, 128, 192]
        outputs.append(output)

    assert outputs[0].stat().st_size < outputs[1].stat().st_size
