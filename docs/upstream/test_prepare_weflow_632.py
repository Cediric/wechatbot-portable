"""Code-copy safety tests only; no installer, ASAR/PE, native or network execution."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("prepare632",HERE / "prepare_weflow_632.py")
prepare632=importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare632)

class CodeCopySafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get("WEFLOW_SOURCE_DIR")
        if not source:
            raise RuntimeError("Set WEFLOW_SOURCE_DIR to an explicitly reviewed extracted WeFlow 6.3.2 source directory")
        snapshot=Path(source).resolve(strict=True)
        cls.main=(snapshot / "dist-electron/main.js").read_bytes()
        cls.package=(snapshot / "package.json").read_bytes()
        if hashlib.sha256(cls.main).hexdigest()!=prepare632.EXPECTED_MAIN:
            raise AssertionError("Recorded code snapshot hash changed")

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="wf632-mock-",dir=prepare632.WORKSPACE)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root / "source"
        (self.source / "dist-electron").mkdir(parents=True)
        (self.source / "package.json").write_bytes(self.package)
        (self.source / "dist-electron/main.js").write_bytes(self.main)
        self.output=self.root / "output"

    def test_independent_exact_copy_idempotent_and_source_unchanged(self):
        manifest=prepare632.prepare(self.source,self.output)
        modified=(self.output / "modified/dist-electron/main.js").read_bytes()
        self.assertEqual(hashlib.sha256(modified).hexdigest(),manifest["output_main_sha256"])
        extension=(HERE / "weflow632_extension.js").read_bytes().decode("utf8")
        expected=self.main.decode("utf8").replace(prepare632.ROUTE_OLD,prepare632.ROUTE_NEW,1)
        expected+="\n;(()=>{\n"+extension+"\n__wf632InstallIdentityExtension({chatService:X,wcdbService:B,httpService:hM,messagePushService:_M,crypto:o,fs:c,path:t,SaxesParser:require(\"saxes\").SaxesParser});\n})();\n"
        self.assertEqual(modified,expected.encode("utf8"))
        self.assertEqual(prepare632.prepare(self.source,self.output),manifest)
        self.assertEqual((self.source / "dist-electron/main.js").read_bytes(),self.main)
        self.assertEqual((self.source / "package.json").read_bytes(),self.package)
        self.assertEqual(sorted(str(p.relative_to(self.output)).replace("\\","/") for p in self.output.rglob("*") if p.is_file()),
                         ["extension.js","modified/dist-electron/main.js","operations.json"])

    def test_wrong_version_and_hash_refuse_before_output_creation(self):
        package=json.loads(self.package);package["version"]="6.3.3"
        (self.source / "package.json").write_text(json.dumps(package),encoding="utf8")
        with self.assertRaisesRegex(ValueError,"6.3.2"):
            prepare632.prepare(self.source,self.output)
        self.assertFalse(self.output.exists())
        (self.source / "package.json").write_bytes(self.package)
        (self.source / "dist-electron/main.js").write_bytes(self.main+b"\n")
        with self.assertRaisesRegex(ValueError,"SHA-256"):
            prepare632.prepare(self.source,self.output)
        self.assertFalse(self.output.exists())

    def test_source_overlap_and_workspace_root_refused(self):
        for output in (self.source,self.source / "modified",self.root,prepare632.WORKSPACE):
            with self.assertRaises(ValueError):prepare632.prepare(self.source,output)
        self.assertEqual((self.source / "dist-electron/main.js").read_bytes(),self.main)

    def test_external_output_refused(self):
        with tempfile.TemporaryDirectory(prefix="wf632-external-mock-") as external:
            output=Path(external) / "should-not-exist"
            with self.assertRaisesRegex(ValueError,"workspace"):
                prepare632.prepare(self.source,output)
            self.assertFalse(output.exists())

    def test_existing_unrelated_work_refused(self):
        self.output.mkdir();sentinel=self.output / "my-work.txt";sentinel.write_text("preserve",encoding="utf8")
        with self.assertRaisesRegex(ValueError,"other work"):
            prepare632.prepare(self.source,self.output)
        self.assertEqual(sentinel.read_text(encoding="utf8"),"preserve")
        self.assertFalse((self.output / "modified").exists())

    def test_existing_output_modified_after_preparation_refused(self):
        prepare632.prepare(self.source,self.output)
        target=self.output / "modified/dist-electron/main.js";target.write_bytes(b"user edit")
        with self.assertRaisesRegex(ValueError,"other work"):
            prepare632.prepare(self.source,self.output)
        self.assertEqual(target.read_bytes(),b"user edit")

    def test_other_files_in_prepared_output_are_not_silently_accepted(self):
        prepare632.prepare(self.source,self.output)
        sentinel=self.output / "new-user-work.txt";sentinel.write_text("preserve",encoding="utf8")
        with self.assertRaisesRegex(ValueError,"other work"):
            prepare632.prepare(self.source,self.output)
        self.assertEqual(sentinel.read_text(encoding="utf8"),"preserve")

if __name__=="__main__":unittest.main()
