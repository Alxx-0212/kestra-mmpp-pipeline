"""Render explicit FinPay release flows with immutable image references only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


FLOW_FILES = {
    "cell": ("finpay_cell_day_v1.yml", "finpay_cell_range_v1.yml", "finpay_cell_bulk_v1.yml"),
    "legacy": ("finpay_pipeline.yml", "finpay_legacy_day_v1.yml", "finpay_legacy_range_v1.yml"),
}
IMAGE_ARGUMENTS = {
    "finpay-pipeline:3.11": "base_image",
    "finpay-cell-reporting:2.0": "report_image",
    "finpay-digipos-cell-scraper:2.0": "cell_scraper_image",
    "finpay-digipos-scraper:1.0": "legacy_scraper_image",
}
IMAGE_LINE = re.compile(r"(?m)^([ \t]*(?:-[ \t]+)?containerImage:[ \t]*)([^#\r\n]+?)([ \t]*(?:#.*)?)$")
IMMUTABLE_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")


def render_release(source: Path, destination: Path, pipeline: str, images: dict) -> dict:
    """Create a new atomic artifact directory; never change source or an old release."""
    source, destination = source.resolve(), destination.resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("release destination must be new and outside source workflows")
    if not destination.parent.is_dir():
        raise ValueError("release destination parent must already exist")
    if pipeline not in (*FLOW_FILES, "both"):
        raise ValueError("unknown pipeline selection")
    files = (
        (*FLOW_FILES["cell"], *FLOW_FILES["legacy"])
        if pipeline == "both" else FLOW_FILES[pipeline]
    )
    manifest = {"schema_version": 1, "pipeline": pipeline, "flow_directory": "flows", "images": {}, "flows": []}
    rendered = {}
    for name in files:
        original = (source / name).read_bytes().decode("utf-8")
        count = 0

        def replace(match):
            nonlocal count
            value = match.group(2).strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            if value not in IMAGE_ARGUMENTS:
                raise ValueError("flow contains an image outside the release mapping")
            replacement = images.get(value)
            if not isinstance(replacement, str) or not IMMUTABLE_IMAGE.fullmatch(replacement):
                raise ValueError(f"immutable image required for {value}")
            manifest["images"][value] = replacement
            count += 1
            return match.group(1) + json.dumps(replacement) + match.group(3)

        text = IMAGE_LINE.sub(replace, original)
        if not count:
            raise ValueError("selected flow has no static image references")
        if count != len(re.findall(r"\bcontainerImage[ \t]*:", original)):
            raise ValueError("unsupported image field syntax requires explicit review")
        rendered[name] = text
        manifest["flows"].append({
            "file": name,
            "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "rendered_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    with tempfile.TemporaryDirectory(prefix=".finpay-release-", dir=destination.parent) as temporary:
        directory = Path(temporary)
        (directory / "flows").mkdir()
        for name, text in rendered.items():
            (directory / "flows" / name).write_text(text, encoding="utf-8")
        (directory / "release-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )
        os.rename(directory, destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", choices=("cell", "legacy", "both"), default="cell")
    parser.add_argument("--output-directory", required=True, type=Path)
    for argument in IMAGE_ARGUMENTS.values():
        parser.add_argument("--" + argument.replace("_", "-"))
    args = parser.parse_args()
    try:
        manifest = render_release(
            Path(__file__).resolve().parents[1] / "workflows",
            args.output_directory, args.pipeline,
            {image: getattr(args, argument) for image, argument in IMAGE_ARGUMENTS.items()},
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"release rendering failed: {error}\n")
    print(json.dumps({"pipeline": manifest["pipeline"], "flows": len(manifest["flows"])}))


if __name__ == "__main__":
    main()
