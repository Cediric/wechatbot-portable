"""ASAR boundary fixtures use upstream canonical integrity; never install WeFlow."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

PORTABLE = Path(__file__).resolve().parents[1] / 'portable'
sys.path.insert(0, str(PORTABLE))
import weflow_setup as setup


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pack(tree, payload):
    text = json.dumps(tree, ensure_ascii=False, separators=(',', ':')).encode()
    padded = text + b'\0' * ((-len(text)) % 4)
    return struct.pack('<4I', 4, len(padded) + 8, len(padded) + 4, len(text)) + padded + payload


def node(raw, offset=0, block=4, blocks=None):
    if blocks is None:
        blocks = [digest(raw[i:i+block]) for i in range(0, len(raw), block)] if raw else [digest(b'')]
    return {'offset': str(offset), 'size': len(raw), 'integrity': {'algorithm': 'SHA256', 'hash': digest(raw), 'blockSize': block, 'blocks': blocks}}


class AsarBoundaryTests(unittest.TestCase):
    def test_empty_partial_exact_and_multiple_blocks_match_fixed_vectors(self):
        vectors = [(b'', [digest(b'')]), (b'a', [digest(b'a')]), (b'abcd', [digest(b'abcd')]),
                   (b'abcde', [digest(b'abcd'), digest(b'e')]), (b'abcdefgh', [digest(b'abcd'), digest(b'efgh')])]
        for raw, blocks in vectors:
            with self.subTest(size=len(raw)):
                data = pack({'files': {'public.bin': node(raw, blocks=blocks)}}, raw)
                _, packed = setup.unpack_asar(data)
                self.assertEqual(packed[0][3], raw)
                self.assertEqual(setup.asar_block_hashes(raw, 4), blocks)

    def test_missing_wrong_and_extra_blocks_remain_rejected(self):
        for raw, wrong_blocks in ((b'', []), (b'', ['0'*64]), (b'abcd', [digest(b'abcd'), digest(b'')]),
                                  (b'abcde', [digest(b'abcd')]), (b'abcdefgh', [digest(b'abcd'), digest(b'wrong')])):
            with self.subTest(size=len(raw), blocks=wrong_blocks), self.assertRaises(ValueError):
                setup.unpack_asar(pack({'files': {'public.bin': node(raw, blocks=wrong_blocks)}}, raw))

    def test_invalid_block_sizes_and_corrupt_whole_hash_remain_rejected(self):
        for block in (0, -1, True, 1.5, '4', None):
            entry = node(b'abcd'); entry['integrity']['blockSize'] = block
            with self.subTest(block=block), self.assertRaises(ValueError):
                setup.unpack_asar(pack({'files': {'public.bin': entry}}, b'abcd'))
        entry = node(b'abcd'); entry['integrity']['hash'] = digest(b'other')
        with self.assertRaises(ValueError): setup.unpack_asar(pack({'files': {'public.bin': entry}}, b'abcd'))

    def test_patch_preserves_empty_file_and_uses_same_exact_boundary_rule(self):
        before = 'const before=1;'
        after = 'const after=1;'
        extension = ''
        append = '\n;(()=>{\n' + extension + '\n__wf632InstallIdentityExtension({chatService:X,wcdbService:B,httpService:hM,messagePushService:_M,crypto:o,fs:c,path:t,SaxesParser:require("saxes").SaxesParser});\n})();\n'
        modified = (after + append).encode()
        package = json.dumps({'name': 'weflow', 'version': '6.3.2', 'main': 'dist-electron/main.js'}).encode()
        main = before.encode()
        tree = {'files': {'package.json': node(package), 'empty.js': node(b'', len(package)),
                         'dist-electron': {'files': {'main.js': node(main, len(package), block=len(modified))}}}}
        original = pack(tree, package + main)
        with tempfile.TemporaryDirectory(prefix='weflow-asar-fixture-') as directory:
            root = Path(directory)
            (root / 'prepare_weflow_632.py').write_text('ROUTE_OLD = ' + repr(before) + '\nROUTE_NEW = ' + repr(after) + '\n', encoding='utf-8')
            (root / 'weflow632_extension.js').write_text(extension, encoding='utf-8')
            with patch.object(setup, 'ORIGINAL_ASAR_SHA', digest(original)), patch.object(setup, 'ORIGINAL_MAIN_SHA', digest(main)), patch.object(setup, 'MODIFIED_MAIN_SHA', digest(modified)):
                result = setup.patch_asar(original, root)
                with self.assertRaises(ValueError): setup.patch_asar(original + b'unknown-byte', root)
        _, packed = setup.unpack_asar(result)
        files = {name: (entry, raw) for _, name, entry, raw in packed}
        self.assertEqual(files['empty.js'][1], b'')
        self.assertEqual(files['empty.js'][0]['integrity']['blocks'], [digest(b'')])
        self.assertEqual(files['package.json'][1], package)
        self.assertEqual(files['dist-electron/main.js'][1], modified)
        self.assertEqual(files['dist-electron/main.js'][0]['integrity']['blocks'], [digest(modified)])

    def test_pinned_legacy_extension_is_rebuilt_from_the_proven_upstream_main_only(self):
        before, after = 'const before=1;', 'const after=1;'
        installer = '\n__wf632InstallIdentityExtension({chatService:X,wcdbService:B,httpService:hM,messagePushService:_M,crypto:o,fs:c,path:t,SaxesParser:require("saxes").SaxesParser});\n})();\n'
        legacy = (after + '\n;(()=>{\n// old extension' + installer).encode()
        modified = (after + '\n;(()=>{\n// current extension' + installer).encode()
        package = json.dumps({'name':'weflow','version':'6.3.2','main':'dist-electron/main.js'}).encode()
        other = b'non-target-file-must-stay-identical'
        tree = {'files': {'package.json':node(package), 'other.js':node(other,len(package)),
                         'dist-electron': {'files': {'main.js':node(legacy,len(package)+len(other))}}}}
        original = pack(tree, package + other + legacy)
        with tempfile.TemporaryDirectory(prefix='weflow-legacy-fixture-') as directory:
            root=Path(directory)
            (root/'prepare_weflow_632.py').write_text('ROUTE_OLD='+repr(before)+'\nROUTE_NEW='+repr(after)+'\n',encoding='utf-8')
            (root/'weflow632_extension.js').write_text('// current extension',encoding='utf-8')
            with patch.object(setup,'LEGACY_MODIFIED_ASAR_SHA',digest(original)), patch.object(setup,'LEGACY_MODIFIED_MAIN_SHA',digest(legacy)), \
                    patch.object(setup,'ORIGINAL_MAIN_SHA',digest(before.encode())), patch.object(setup,'MODIFIED_MAIN_SHA',digest(modified)):
                result=setup.patch_asar(original,root)
                with self.assertRaises(ValueError):
                    setup.patch_asar(original+b'changed non-target data',root)
        _,packed=setup.unpack_asar(result)
        files={name:raw for _,name,_,raw in packed}
        self.assertEqual(files['other.js'],other)
        self.assertEqual(files['package.json'],package)
        self.assertEqual(files['dist-electron/main.js'],modified)
        self.assertNotIn(b'old extension',files['dist-electron/main.js'])


class BackupRotationTests(unittest.TestCase):
    def test_keeps_latest_and_verified_stock_with_unowned_or_unverified_folders_untouched(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(setup, 'ORIGINAL_ASAR_SHA', digest(b'stock')):
            runtime = Path(temp)
            root = runtime / 'weflow-backups'
            root.mkdir()
            dirs = []
            for n in range(6):
                folder = root / str(1800000000000000000+n)
                folder.mkdir()
                (folder/'WeFlow-config.json').write_text('{}')
                if n < 5:
                    (folder/'result.json').write_text(json.dumps({
                        'identity_api_verified':True, 'original_asar_sha256':setup.ORIGINAL_ASAR_SHA if n in (0,1) else ''}))
                if n in (0,1):
                    (folder/'app.asar').write_bytes(b'stock' if n == 0 else b'corrupt')
                if n == 3:
                    (folder/'user.txt').write_text('preserve')
                dirs.append(folder)
            setup._rotate_weflow_backups(runtime)
            self.assertEqual(set(root.iterdir()), {dirs[i] for i in (0,3,4,5)})


if __name__ == '__main__':
    unittest.main()
