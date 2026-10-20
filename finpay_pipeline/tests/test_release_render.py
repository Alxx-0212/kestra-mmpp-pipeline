"""Offline checks for the immutable-image release artifact boundary."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from finpay_pipeline.tests.render_finpay_release import (
    FLOW_FILES, IMAGE_ARGUMENTS, IMAGE_LINE, render_release,
)


class ReleaseRenderTests(unittest.TestCase):
    source = Path(__file__).resolve().parents[1] / "workflows"
    images = {
        name: f"registry.example/finpay/image-{index}@sha256:{str(index) * 64}"
        for index, name in enumerate(IMAGE_ARGUMENTS, start=1)
    }

    def test_all_primary_flows_have_only_immutable_images_and_preserved_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "release"
            manifest = render_release(self.source, destination, "both", self.images)
            self.assertEqual(len(manifest["flows"]), 6)
            self.assertEqual(json.loads((destination / "release-manifest.json").read_text()), manifest)
            for item in manifest["flows"]:
                original = (self.source / item["file"]).read_text()
                rendered = (destination / "flows" / item["file"]).read_text()
                self.assertEqual(IMAGE_LINE.sub("IMAGE", rendered), IMAGE_LINE.sub("IMAGE", original))
                self.assertEqual(item["rendered_sha256"], hashlib.sha256(rendered.encode()).hexdigest())
                for match in IMAGE_LINE.finditer(rendered):
                    self.assertIn(json.loads(match.group(2).strip()), self.images.values())

    def test_missing_or_mutable_mapping_fails_without_partial_artifact(self):
        for images in ({}, {name: "mutable:latest" for name in self.images}):
            with self.subTest(images=bool(images)), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "release"
                with self.assertRaisesRegex(ValueError, "immutable image required"):
                    render_release(self.source, destination, "cell", images)
                self.assertFalse(destination.exists())

    def test_rejects_existing_destination_and_source_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "must be new"):
                render_release(self.source, Path(temporary), "cell", self.images)
        with self.assertRaisesRegex(ValueError, "outside source"):
            render_release(self.source, self.source / "not-a-release", "cell", self.images)

    def test_unknown_source_image_is_not_silently_deployed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "flows"
            source.mkdir()
            for name in FLOW_FILES["cell"]:
                (source / name).write_text("id: test\ntasks:\n  - containerImage: unreviewed:latest\n")
            with self.assertRaisesRegex(ValueError, "outside the release mapping"):
                render_release(source, root / "release", "cell", self.images)
            self.assertFalse((root / "release").exists())

    def test_unsupported_inline_fields_cannot_hide_beside_known_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "flows"
            source.mkdir()
            for name in FLOW_FILES["cell"]:
                (source / name).write_text(
                    "id: test\ntasks:\n  - containerImage: finpay-cell-reporting:2.0\n"
                    "  - {containerImage: unreviewed:latest}\n"
                )
            with self.assertRaisesRegex(ValueError, "unsupported image field syntax"):
                render_release(source, root / "release", "cell", self.images)
            self.assertFalse((root / "release").exists())


if __name__ == "__main__":
    unittest.main()
