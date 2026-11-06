"""Private subprocess entry point; no application or upstream initialisation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jmcomic_api.services.pdf_builder import merge_image_paths_to_pdf


def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    paths = [Path(p) for p in spec.pop("paths")]
    expected = spec.pop("expected_digests", None)
    if expected:
        from jmcomic_api.services.storage import digest_file

        if [digest_file(p) for p in paths] != expected:
            raise ValueError("original images changed during build")
    merge_image_paths_to_pdf(paths, spec.pop("output"), **spec)


if __name__ == "__main__":
    main()
