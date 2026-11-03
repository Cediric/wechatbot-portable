"""Capability probe and exact-build WeFlow setup for a new local machine."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGINAL_ASAR_SHA = "3c20bfd34b57ceab7523a410b45dbda05529b21cd301681c1b0fce9dc31b09cd"
EXE_SHA = "0e55aea3120d8c6c487af633542db9d79760b5ac90a1ecd9ae56219c890349e2"
ORIGINAL_MAIN_SHA = "4d504f70c1c29041b3730df30a7aeac20fc213ce53d8d2585eb862c2f8ec36ec"
LEGACY_MODIFIED_MAIN_SHA = "74c96b1fb106dc2b6683d5ef87b4201851b316b53449c47fe2a9688a0ab86d2e"
LEGACY_MODIFIED_ASAR_SHA = "361314d9f7ded19e91ee310add9fec3035c5c66300704c5dbbeffcbd5192dedd"
MODIFIED_MAIN_SHA = "4e8861ca3e78e91595c36a8bb583887804cb06fb632ca3e8641e7377a1a8f6dc"
MODIFIED_ASAR_SHA = "0a77290733a255731502d702207b9af80b44234d68d728260c0f9353a44687f3"

def sha(data): return hashlib.sha256(data).hexdigest()

def asar_block_hashes(raw, block_size):
    """Electron ASAR integrity: empty files still contain the empty SHA-256 block.

    Nonempty exact block multiples have no extra trailing empty block. Use the
    same canonical rule for validation and regenerated main.js metadata.
    """
    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size <= 0:
        raise ValueError("ASAR 数据块大小无效")
    return [sha(raw[i:i+block_size]) for i in range(0, len(raw), block_size)] or [sha(b"")]

def entries(tree, prefix=""):
    for name, entry in tree.get("files", {}).items():
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("ASAR 文件名无效")
        path = prefix + name
        if "files" in entry: yield from entries(entry, path + "/")
        else: yield path, entry

def unpack_asar(data):
    if len(data) < 16: raise ValueError("ASAR 头不完整")
    size_size, header_size, payload_size, json_size = struct.unpack_from("<4I", data)
    if size_size != 4 or header_size < 8 or payload_size + 4 != header_size or json_size > payload_size-4:
        raise ValueError("ASAR 头无效")
    if header_size > 32*1024*1024 or 8+header_size > len(data): raise ValueError("ASAR 头边界无效")
    tree = json.loads(data[16:16+json_size].decode("utf-8"))
    base = 8+header_size
    packed = []
    for name, entry in entries(tree):
        if "link" in entry or entry.get("unpacked"): continue
        offset, size = int(entry["offset"]), int(entry["size"])
        if offset < 0 or size < 0 or base+offset+size > len(data): raise ValueError("ASAR 文件越界")
        raw = data[base+offset:base+offset+size]
        integrity = entry.get("integrity", {})
        if integrity:
            if integrity.get("algorithm") != "SHA256" or integrity.get("hash") != sha(raw): raise ValueError("ASAR 文件完整性校验失败")
            block = integrity.get("blockSize", 0)
            if integrity.get("blocks") != asar_block_hashes(raw, block): raise ValueError("ASAR 数据块校验失败")
        packed.append((offset, name, entry, raw))
    packed.sort(key=lambda x:x[0])
    position = 0
    for offset, _, _, raw in packed:
        if offset != position: raise ValueError("ASAR 存在间隙或重叠")
        position += len(raw)
    if base+position != len(data): raise ValueError("ASAR 尾部数据无效")
    return tree, packed

def patch_asar(data: bytes, extension_root: Path, *, known_owned_hashes=()):
    if sha(data) not in {ORIGINAL_ASAR_SHA, LEGACY_MODIFIED_ASAR_SHA, *known_owned_hashes}:
        raise ValueError("仅支持核验过的原始或本项目旧版 WeFlow 6.3.2 ASAR；未知构建不自动修改。")
    tree, packed = unpack_asar(data)
    files = {name:raw for _,name,_,raw in packed}
    package = json.loads(files["package.json"])
    if package.get("name") != "weflow" or package.get("version") != "6.3.2" or package.get("main") != "dist-electron/main.js": raise ValueError("WeFlow 版本不匹配")
    main = files["dist-electron/main.js"]
    spec = importlib.util.spec_from_file_location("wechatbot_weflow_prepare", extension_root / "prepare_weflow_632.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    text = main.decode()
    if sha(main) == LEGACY_MODIFIED_MAIN_SHA:
        # Strip only the exact pinned old extension, then prove the recovered
        # upstream source byte-for-byte before building the new extension.
        marker = text.rfind('\n;(()=>{\n')
        text = text[:marker] if marker >= 0 else ""
        if text.count(prepare.ROUTE_NEW) != 1:
            raise ValueError("WeFlow 旧版扩展路由不匹配")
        text = text.replace(prepare.ROUTE_NEW, prepare.ROUTE_OLD, 1)
    if sha(text.encode()) != ORIGINAL_MAIN_SHA:
        raise ValueError("WeFlow main.js 不匹配原始核验构建")
    if text.count(prepare.ROUTE_OLD) != 1: raise ValueError("WeFlow 路由不匹配")
    extension = (extension_root / "weflow632_extension.js").read_text(encoding="utf-8")
    append = '\n;(()=>{\n'+extension+'\n__wf632InstallIdentityExtension({chatService:X,wcdbService:B,httpService:hM,messagePushService:_M,crypto:o,fs:c,path:t,SaxesParser:require("saxes").SaxesParser});\n})();\n'
    modified = (text.replace(prepare.ROUTE_OLD, prepare.ROUTE_NEW, 1)+append).encode()
    if sha(modified) != MODIFIED_MAIN_SHA: raise ValueError("本包 WeFlow 扩展不匹配已验收版本")
    payloads = []
    position = 0
    for _, name, entry, original in packed:
        raw = modified if name == "dist-electron/main.js" else original
        entry["offset"], entry["size"] = str(position), len(raw)
        if name == "dist-electron/main.js" and entry.get("integrity"):
            block = entry["integrity"]["blockSize"]
            entry["integrity"].update({"hash":sha(raw), "blocks":asar_block_hashes(raw, block)})
        payloads.append(raw); position += len(raw)
    header_json = json.dumps(tree,ensure_ascii=False,separators=(",",":")).encode()
    padded = header_json + b"\0"*((-len(header_json))%4)
    result = struct.pack("<4I",4,len(padded)+8,len(padded)+4,len(header_json))+padded+b"".join(payloads)
    _, result_entries = unpack_asar(result)
    actual = {name:raw for _,name,_,raw in result_entries}
    if actual.keys() != files.keys() or any(actual[k] != v for k,v in files.items() if k != "dist-electron/main.js"):
        raise ValueError("WeFlow 重包产生了非目标文件变化")
    return result

def api_probe(base, token, *, open_request=None):
    # The verified 6.3.2 service rejects a blank token. safe:/lock: are disk
    # storage envelopes, never HTTP credentials.
    if not isinstance(token, str) or not token.strip() or token.startswith(("safe:", "lock:")):
        return False
    open_request = open_request or urllib.request.urlopen
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password: raise ValueError("WeFlow 地址无效")
    request = urllib.request.Request(base.rstrip("/")+"/api/v1/identity-snapshot",headers={"Authorization":"Bearer "+token,"X-Access-Token":token})
    # Installed WeFlow uses access_token query, keep it out of logs/errors.
    request.full_url += "?" + urllib.parse.urlencode({"access_token":token})
    try:
        with open_request(request,timeout=4) as response:
            body = response.read(16*1024*1024+1)
            if len(body)>16*1024*1024: return False
            data=json.loads(body)
            if not isinstance(data, dict): return False
            payload=data.get("data", data)
            caps = payload.get("capabilities") if isinstance(payload, dict) else None
            ready = (isinstance(payload,dict) and payload.get("fresh") is True
                    and payload.get("version",payload.get("contractVersion")) == 1
                    and isinstance(caps,dict) and type(caps.get("snsMediaKey")) is int and caps["snsMediaKey"] == 1)
        if ready:
            # The extension declares GET for this route. HEAD verifies routing
            # without querying the SNS database or reading a post.
            details = urllib.request.Request(base.rstrip("/") + "/api/v1/sns-identity-details?" +
                urllib.parse.urlencode({"access_token": token}), headers=request.headers, method="HEAD")
            try:
                with open_request(details, timeout=4):
                    return False
            except urllib.error.HTTPError as error:
                with error:
                    return error.code == 405 and error.headers.get("Allow") == "GET"
        return False
    except (OSError, ValueError, urllib.error.URLError): return False

def _contact_usernames(base, token, *, stock=False):
    """Read native username fields, never the displayed name or profile alias."""
    endpoint = "/api/v1/contacts" if stock else "/api/v1/identity-snapshot"
    request = urllib.request.Request(base.rstrip("/")+endpoint,
        headers={"Authorization":"Bearer "+token,"X-Access-Token":token,"Cache-Control":"no-cache"})
    query = {"access_token": token}
    if stock:
        query["limit"] = 10000
    request.full_url += "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            if response.status != 200:
                return None
            body = response.read(16*1024*1024+1)
            if len(body) > 16*1024*1024:
                return None
            data = json.loads(body)
            payload = data.get("data", data) if isinstance(data, dict) else None
            if not isinstance(payload, dict) or payload.get("success") is not True:
                return None
            if not stock and (type(payload.get("version")) is not int or payload["version"] != 1
                    or payload.get("fresh") is not True or not isinstance(payload.get("capabilities"), dict)
                    or type(payload["capabilities"].get("freshIdentitySnapshot")) is not int
                    or payload["capabilities"]["freshIdentitySnapshot"] != 1):
                return None
            stamp = payload.get("snapshotAt")
            if not stock and (type(stamp) not in (int, float) or not -15000 <= time.time()*1000-stamp <= 15000):
                return None
            rows = payload.get("contacts")
            if not isinstance(rows, list) or (stock and (payload.get("count") != len(rows) or len(rows) >= query["limit"])):
                return None
            names = [row.get("username") for row in rows if isinstance(row, dict)]
            if (len(names) != len(rows) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,256}", name)
                    or name in {"unknown", "null", "None", "@chatroom"} for name in names)
                    or len(names) != len(set(names))):
                return None
            return names
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return None

def _canonical_selected_account(username, current, base, token, *, usernames=None):
    """Require a native contact and selected directory before removing a suffix."""
    suffix = re.fullmatch(r"(wxid_[A-Za-z0-9]+)_([0-9a-fA-F]{4})", username)
    if suffix is None:
        return username  # Preserve the verified legacy path without another HTTP call.
    names = _contact_usernames(base, token) if usernames is None else usernames
    if names is None:
        return None
    if names.count(username) == 1:
        return username  # A genuine native ID can itself end with four hex characters.
    candidate = suffix.group(1)
    if username in names or names.count(candidate) != 1:
        return None
    try:
        db_path = current.get("dbPath")
        if not isinstance(db_path, str) or not db_path or not Path(db_path).is_absolute():
            return None
        base_path = Path(db_path).resolve(strict=True)
        account = (base_path / username).resolve(strict=True)
        if not account.is_dir() or account.parent != base_path or account.name != username:
            return None
    except (OSError, ValueError, TypeError):
        return None
    return candidate

def ps_command(argv, *, capture=True, timeout=35):
    system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
    ps = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # The child is Windows PowerShell 5.1 even when its caller runs PowerShell 7.
    env = dict(os.environ, PSModulePath=str(ps.parent / "Modules"))
    return subprocess.run([str(ps),"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",*argv],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,stderr=subprocess.PIPE if capture else None,timeout=timeout,
        text=True,encoding="utf-8",errors="replace",env=env,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))

def discover_executable(runtime=None):
    # Get-Process Path returns the actual executable; no assumptions about usernames/drives.
    result=ps_command(["-Command", """
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$paths = @(Get-Process WeFlow -ErrorAction SilentlyContinue | ForEach-Object { $_.Path } | Where-Object { $_ } | Select-Object -Unique)
if (-not $paths.Count) {
    foreach ($folder in 'Programs','CommonPrograms') {
        $link = Join-Path ([Environment]::GetFolderPath($folder)) 'WeFlow.lnk'
        if (Test-Path -LiteralPath $link -PathType Leaf) {
            $target = (New-Object -ComObject WScript.Shell).CreateShortcut($link).TargetPath
            if ($target -and [IO.Path]::GetFileName($target) -eq 'WeFlow.exe' -and (Test-Path -LiteralPath $target -PathType Leaf)) {
                $paths += $target
            }
        }
    }
}
$paths | Select-Object -Unique | ConvertTo-Json -Compress
"""])
    paths=json.loads(result.stdout.strip() or "[]")
    paths=[paths] if isinstance(paths,str) else paths
    if len(paths)==1: return Path(paths[0]).resolve()
    if len(paths)>1: raise ValueError("检测到多个不同 WeFlow 安装，请先打开需要接入的实例。")
    candidates=[]
    if runtime is not None:
        from portable_bootstrap import load_json
        cached = load_json(runtime / "weflow-installation.json").get("executable")
        if isinstance(cached, str) and Path(cached).is_absolute() and Path(cached).name.lower() == "weflow.exe":
            candidates.append(Path(cached))
    candidates.append(Path(os.environ.get("LOCALAPPDATA",""))/"Programs"/"WeFlow"/"WeFlow.exe")
    return next((p.resolve() for p in candidates if p.is_file()),None)

def _sync_selected_account(bridge_config, base, token, *, current=None, sync_token=False, runtime=None, usernames=None):
    """Fill or correct the selected directory ID through fresh native ID evidence.

    This is WeFlow's saved selection, not runtime account attestation. A directory
    is supporting evidence only; nicknames, Moments owners and caches are not IDs.
    """
    from portable_bootstrap import atomic_json, load_json
    from weflow_secret import WeFlowSecretError, resolve_http_token
    if not isinstance(token, str) or not token.strip() or token.startswith(("safe:", "lock:")):
        return False
    settings = load_json(bridge_config)
    configured_base = settings.get("weflow_base_url", "http://127.0.0.1:5031")
    if configured_base.rstrip("/") != base.rstrip("/"):
        return False
    changed = sync_token and settings.get("access_token", "") != token
    if sync_token:
        settings["access_token"] = token
    app_data = os.environ.get("APPDATA", "")
    local_selection = bool(app_data and Path(app_data).is_absolute())
    app_config = Path(app_data) / "weflow" / "WeFlow-config.json" if local_selection else None
    if current is None:
        current = {}
        if local_selection:
            try:
                current = load_json(app_config)
            except (OSError, ValueError):
                current = {}
    filled = False
    try:
        parsed = urllib.parse.urlsplit(base)
        port = current.get("httpApiPort")
        username = current.get("myWxid")
        if (local_selection and settings.get("bot_wxid") in (None, "", username) and current.get("httpApiEnabled") is True
                and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path in {"", "/"} and type(port) is int
                and port == (parsed.port or 80) and settings.get("access_token", "") == token
                and isinstance(token, str) and bool(token)
                and resolve_http_token(current.get("httpApiToken", ""), app_config, runtime=runtime) == token.strip()
                and isinstance(username, str) and 0 < len(username) <= 256
                and re.fullmatch(r"[A-Za-z0-9_.@:-]+", username)
                and not username.endswith("@chatroom") and not username.startswith("gh_")
                and username not in {"unknown", "null", "None", "filehelper", "fmessage", "wxid_xxx",
                                     "weixin", "newsapp", "medianote", "floatbottle", "qqmail", "qmessage", "tmessage", "qqsync"}):
            canonical = _canonical_selected_account(username, current, base, token, usernames=usernames)
            if canonical is None:
                print("本机 WeFlow 所选账号编号尚未能与新鲜身份快照对应；已保留账号配置，请核对目录编号与真实微信 ID。")
            elif settings.get("bot_wxid") != canonical:
                settings["bot_wxid"] = canonical
                filled = True
    except WeFlowSecretError as error:
        print("本机 WeFlow HTTP 令牌同步失败（" + error.reason + "）；已保留现有账号与连接配置。")
        return False
    except (ValueError, TypeError):
        pass
    if changed or filled:
        atomic_json(bridge_config, settings)
    if filled:
        print("已同步 WeFlow 所选账号编号，请确认微信登录同一账号。")
    return filled

def _rotate_weflow_backups(runtime: Path):
    """Keep two successful installer backups, including a restorable stock ASAR."""
    root = runtime.resolve() / "weflow-backups"
    if root.resolve() != root or not root.is_dir():
        return
    backups = []
    for folder in sorted(root.iterdir(), reverse=True):
        if not re.fullmatch(r"[0-9]{19}", folder.name) or not folder.is_dir() or folder.resolve() != folder:
            continue
        files = set(folder.iterdir())
        names = {f.name for f in files}
        if names not in ({"result.json", "WeFlow-config.json"}, {"result.json", "WeFlow-config.json", "app.asar"}):
            continue
        if any(not f.is_file() or f.resolve() != f for f in files):
            continue
        try:
            result = json.loads((folder / "result.json").read_text(encoding="utf-8-sig"))
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("identity_api_verified") is True:
            backups.append((folder, files, result.get("original_asar_sha256") == ORIGINAL_ASAR_SHA and "app.asar" in names))
    if len(backups) <= 2:
        return
    keep = {backups[0][0]}
    for folder, _, stock in backups:
        if stock:
            with (folder / "app.asar").open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == ORIGINAL_ASAR_SHA:
                    keep.add(folder)
                    break
    if len(keep) < 2:
        keep.add(backups[1][0])
    for folder, files, _ in backups:
        if folder not in keep:
            for file in files:
                file.unlink()
            folder.rmdir()


def ensure_weflow(bridge_config: Path, runtime: Path):
    from portable_bootstrap import atomic_json, load_json
    from weflow_secret import WeFlowSecretError, resolve_http_token
    settings=load_json(bridge_config)
    base=settings.get("weflow_base_url","http://127.0.0.1:5031")
    token=settings.get("access_token","")
    if api_probe(base,token):
        _sync_selected_account(bridge_config, base, token, runtime=runtime)
        print("WeFlow 身份同步接口已就绪。")
        return True
    parsed=urllib.parse.urlsplit(base)
    if parsed.hostname not in {"127.0.0.1","localhost"} or parsed.scheme!="http" or (parsed.port or 80)!=5031:
        print("WeFlow 自定义地址尚未就绪；请在面板检查对应服务。已保留自定义连接。")
        return False
    app_data=os.environ.get("APPDATA", "")
    if not app_data or not Path(app_data).is_absolute():
        print("未找到本机 WeFlow 配置，请在面板填写当前账号编号与连接配置。控制面板仍会启动。")
        return False
    executable=discover_executable(runtime)
    app_config=Path(app_data)/"weflow"/"WeFlow-config.json"
    if executable is None or not app_config.is_file():
        print("请安装并打开登录 WeFlow，在 WeFlow 填写微信数据库所需配置；控制面板仍会启动。")
        return False
    current=load_json(app_config)
    stored_current_token=current.get("httpApiToken","")
    try:
        current_token=resolve_http_token(stored_current_token, app_config, runtime=runtime)
    except WeFlowSecretError as error:
        print("本机 WeFlow HTTP 令牌同步失败（" + error.reason + "）；未更改 WeFlow 或桥接凭证，请在 WeFlow API 设置中核对令牌。")
        return False
    if current.get("httpApiEnabled") and current_token and api_probe(base,current_token):
        _sync_selected_account(bridge_config, base, current_token, current=current, sync_token=True, runtime=runtime)
        print("已同步本机 WeFlow 密钥（密钥不显示）。")
        return True
    if sha(executable.read_bytes()) != EXE_SHA:
        print("WeFlow 构建尚未核验，已停止自动适配。控制面板可启动；请按兼容说明安装核验版本。")
        return False
    asar=executable.parent/"resources"/"app.asar"
    original=asar.read_bytes()
    active_hash=sha(original)
    owned_hashes=set()
    state=load_json(runtime/"weflow-owned-extension.json")
    if state.get("original_asar_sha256")==ORIGINAL_ASAR_SHA and state.get("exe_sha256")==EXE_SHA:
        recorded=state.get("active_asar_sha256","")
        if isinstance(recorded,str) and len(recorded)==64:owned_hashes.add(recorded)
    if active_hash not in {ORIGINAL_ASAR_SHA,MODIFIED_ASAR_SHA,LEGACY_MODIFIED_ASAR_SHA,*owned_hashes}:
        print("WeFlow 完整程序包不匹配核验构建，已停止自动适配。")
        return False
    _, packed=unpack_asar(original)
    files={name:raw for _,name,_,raw in packed}
    main_hash=sha(files.get("dist-electron/main.js",b""))
    if main_hash not in {ORIGINAL_MAIN_SHA,MODIFIED_MAIN_SHA,LEGACY_MODIFIED_MAIN_SHA}:
        print("WeFlow 程序包版本不匹配，已停止自动适配。")
        return False
    # A tray-only close leaves the process running. Keep its verified location
    # so retrying after a manual exit also works for custom installations.
    atomic_json(runtime / "weflow-installation.json", {"executable": str(executable)})
    modified=patch_asar(original,bridge_config.parents[1]/"docs"/"upstream", known_owned_hashes=owned_hashes) if main_hash!=MODIFIED_MAIN_SHA else None
    lifecycle=Path(__file__).with_name("weflow_lifecycle.ps1")
    print("WeFlow 适配：若窗口缩至托盘，请右键托盘图标选择“退出”，安装器会重新打开 WeFlow。", flush=True)
    wait_seconds = 35 if current.get("windowCloseBehavior") == "quit" else 20
    result=ps_command(["-File",str(lifecycle),"-Action","stop","-Executable",str(executable),
                       "-WaitSeconds",str(wait_seconds)], timeout=wait_seconds+15)
    if result.returncode:
        if current.get("httpApiEnabled") and current_token:
            usernames = _contact_usernames(base, current_token, stock=True)
            if usernames:
                _sync_selected_account(bridge_config, base, current_token, current=current,
                                       sync_token=True, runtime=runtime, usernames=usernames)
        if result.returncode == 10:
            print("WeFlow 进程仍在运行。请从托盘退出 WeFlow，再双击一键启动。", flush=True)
        else:
            reason = (result.stderr or "").strip()
            print(f"WeFlow 退出检查失败（退出码 {result.returncode}）：{reason or '系统未返回错误说明'}", flush=True)
        return False
    backup=runtime/"weflow-backups"/(str(time.time_ns()))
    asar_replaced = config_replaced = False
    try:
        if sha(executable.read_bytes())!=EXE_SHA or sha(asar.read_bytes())!=sha(original):
            raise ValueError("WeFlow 退出时发生更新，停止旧版本适配")
        # Electron can save preferences on exit. Back up its final on-disk state.
        current=load_json(app_config)
        stored_current_token=current.get("httpApiToken","")
        current_token=resolve_http_token(stored_current_token, app_config, runtime=runtime)
        token=current_token or secrets.token_urlsafe(32)
        backup.mkdir(parents=True)
        shutil.copyfile(app_config,backup/"WeFlow-config.json")
        if modified is not None:
            shutil.copyfile(asar,backup/"app.asar")
            temp=asar.with_name("app.asar.wechatbot-new")
            temp.write_bytes(modified); os.replace(temp,asar)
            asar_replaced = True
            atomic_json(runtime/"weflow-owned-extension.json",{"original_asar_sha256":ORIGINAL_ASAR_SHA,"exe_sha256":EXE_SHA,"active_asar_sha256":sha(modified)})
        atomic_json(app_config, {**current, "httpApiEnabled": True, "httpApiPort": 5031,
                                 "httpApiToken": stored_current_token if current_token else token})
        config_replaced = True
        settings["access_token"]=token;atomic_json(bridge_config,settings)
        result=ps_command(["-File",str(lifecycle),"-Action","start","-Executable",str(executable)])
        if result.returncode:
            reason = (result.stderr or "").strip() or "系统未返回错误说明"
            raise RuntimeError(f"WeFlow 启动失败（退出码 {result.returncode}）：{reason}")
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if api_probe(base,token):
                _sync_selected_account(bridge_config, base, token, runtime=runtime)
                atomic_json(backup/"result.json",{"installed":modified is not None,"original_asar_sha256":sha(original),"active_asar_sha256":sha(asar.read_bytes()),"identity_api_verified":True})
                try:
                    _rotate_weflow_backups(runtime)
                except OSError:
                    print("旧 WeFlow 备份暂未清理。")
                print("WeFlow 原生身份接口只读验收通过。")
                return True
            time.sleep(.5)
        print("WeFlow 已启动，身份接口尚未就绪；请检查数据库解密配置。备份已保留。")
        return False
    except (OSError, ValueError, RuntimeError) as error:
        # Restore only when app is stopped; never overwrite a running application.
        if ps_command(["-File",str(lifecycle),"-Action","is-stopped","-Executable",str(executable)]).returncode==0:
            if asar_replaced: shutil.copyfile(backup/"app.asar",asar)
            if config_replaced: atomic_json(app_config,current)
            ps_command(["-File",str(lifecycle),"-Action","start","-Executable",str(executable)])
        raise RuntimeError(f"WeFlow 自动适配未完成：{error}") from error
