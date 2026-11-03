"""Prepare a reviewable code-only 6.3.2 copy. Never installs, packs ASAR, or changes PE/fuses."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[2]
EXPECTED_MAIN = "4d504f70c1c29041b3730df30a7aeac20fc213ce53d8d2585eb862c2f8ec36ec"
ROUTE_OLD = "else if(i===`/api/v1/contacts`)await this.handleContacts(r,t);"
ROUTE_NEW = ("else if(i===`/api/v1/identity-snapshot`){if(e.method!==`GET`)return this.sendMethodNotAllowed(t,`GET`);await this.handleFreshIdentitySnapshot632(e,t)}"
             "else if(i===`/api/v1/sns-identity-timeline`){if(e.method!==`GET`)return this.sendMethodNotAllowed(t,`GET`);await this.handleSnsIdentityTimeline632(e,r,t)}"
             "else if(i===`/api/v1/sns-identity-schema`){if(e.method!==`GET`)return this.sendMethodNotAllowed(t,`GET`);await this.handleSnsIdentitySchema632(e,r,t)}"
             "else if(i===`/api/v1/sns-identity-details`){if(e.method!==`GET`)return this.sendMethodNotAllowed(t,`GET`);await this.handleSnsIdentityDetails632(e,r,t)}" + ROUTE_OLD)


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def contained(path, root):
    try:
        return os.path.commonpath([canonical(path),canonical(root)]) == canonical(root)
    except ValueError:
        return False


def prepare(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not contained(output, WORKSPACE) or output == WORKSPACE:
        raise ValueError("Output must be an independent directory inside the review workspace")
    if contained(output,source) or contained(source,output):
        raise ValueError("Output must not overlap the read-only source snapshot")
    package = json.loads((source / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != "weflow" or package.get("version") != "6.3.2" or package.get("main") != "dist-electron/main.js":
        raise ValueError("Only the recorded WeFlow 6.3.2 code snapshot is supported")
    main = (source / "dist-electron/main.js").read_bytes()
    if hashlib.sha256(main).hexdigest() != EXPECTED_MAIN:
        raise ValueError("main.js SHA-256 mismatch; refusing to patch another build")
    text = main.decode("utf-8")
    if text.count(ROUTE_OLD) != 1 or "/api/v1/identity-snapshot" in text or "/api/v1/sns-identity-timeline" in text or "/api/v1/sns-identity-schema" in text or "__wf632InstallIdentityExtension" in text:
        raise ValueError("Unexpected route or previously modified bundle")
    for binding in ("X=new class{", "B=new on", "hM=new class n{", "_M=new class{", 'o=require("crypto")',
                    'let t=require("path"),n=e.c(t,1);t=e.c(t);',
                    'let s=require("url"),c=require("fs"),l=e.c(c,1);c=e.c(c);'):
        if text.count(binding) != 1:
            raise ValueError("Bundle binding differs: " + binding)
    extension = (HERE / "weflow632_extension.js").read_bytes().decode("utf-8")
    append = "\n;(()=>{\n" + extension + "\n__wf632InstallIdentityExtension({chatService:X,wcdbService:B,httpService:hM,messagePushService:_M,crypto:o,fs:c,path:t,SaxesParser:require(\"saxes\").SaxesParser});\n})();\n"
    modified = (text.replace(ROUTE_OLD,ROUTE_NEW,1) + append).encode("utf-8")
    manifest = {"version":"6.3.2","input_main_sha256":EXPECTED_MAIN,
        "output_main_sha256":hashlib.sha256(modified).hexdigest(),
        "extension_sha256":hashlib.sha256(extension.encode("utf-8")).hexdigest(),
        "route_old":ROUTE_OLD,"route_new":ROUTE_NEW,
        "write_scope":"independent code copy only; no ASAR/PE/installed modifications",
        "fresh_identity_contract":1,"sns_identity_timeline_contract":1,"sns_identity_schema_contract":1,
        "sns_schema_table_whitelist":["SnsTimeLine"],"sns_db_fixed_relative_path":"db_storage/sns/sns.db","sns_identity_details_contract":1,
        "native_response_deadline_ms":2500,"sns_media_key_contract":1}
    files = {"modified/dist-electron/main.js":modified,
             "extension.js":extension.encode("utf-8"),
             "operations.json":json.dumps(manifest,ensure_ascii=False,indent=2).encode("utf-8")}
    if output.exists() and any(output.iterdir()):
        existing={str(path.relative_to(output)).replace("\\","/") for path in output.rglob("*") if path.is_file()}
        if existing == set(files) and all(contained(output / name,output) and (output / name).is_file() and
               (output / name).read_bytes() == data for name,data in files.items()):
            return manifest
        raise ValueError("Output already contains other work; refusing to replace it")
    for name,data in files.items():
        target = output / name
        if not contained(target,output):
            raise ValueError("Output path escapes the review directory")
        target.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive writes also refuse unexpected pre-existing files and links.
        with target.open("xb") as handle:
            handle.write(data)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=WORKSPACE / "installed_weflow_code")
    parser.add_argument("--output",type=Path,default=WORKSPACE / "WeFlow-6.3.2-sns-details-v5-extension")
    args=parser.parse_args()
    result=prepare(args.source,args.output)
    print(json.dumps({"prepared":str(args.output.resolve()),"main_sha256":result["output_main_sha256"],"installed":False},ensure_ascii=False))


if __name__ == "__main__":
    main()
