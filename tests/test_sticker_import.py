"""Importer cannot use pack names or archive entries to escape its owned directory."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

path=Path(__file__).resolve().parents[1]/"scripts"/"install_stickers.py"
spec=importlib.util.spec_from_file_location("sticker_import",path)
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class StickerImportTests(unittest.TestCase):
    def test_package_name_cannot_escape_or_use_device_names(self):
        with tempfile.TemporaryDirectory(dir=path.parent) as root, patch.object(installer,"STICKER_ROOT",root):
            for name in ("../outside", "..\\outside", "D:\\outside", "NUL", "COM1.png", ".."): 
                with self.subTest(name=name), self.assertRaises(ValueError):
                    installer.pack_destination(name)
            self.assertEqual(installer.pack_destination("开心"),str(Path(root)/"开心"))

    def test_archive_validation_precedes_extraction(self):
        with tempfile.TemporaryDirectory(dir=path.parent) as root:
            base=Path(root); archive=base/"stickers.zip"; dest=base/"dest"; dest.mkdir()
            for member in ("../outside.png", "D:/outside.png", "nested/../../outside.png"):
                with zipfile.ZipFile(archive,"w") as bundle:
                    bundle.writestr("valid.png",b"synthetic")
                    bundle.writestr(member,b"invalid")
                with self.assertRaises(ValueError):
                    installer.extract_zip(str(archive),str(dest))
                self.assertEqual(list(dest.iterdir()),[])
            with zipfile.ZipFile(archive,"w") as bundle:
                bundle.writestr("nested/valid.png",b"synthetic")
            installer.extract_zip(str(archive),str(dest))
            self.assertEqual((dest/"nested"/"valid.png").read_bytes(),b"synthetic")
