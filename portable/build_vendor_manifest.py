"""Release audit: verify wheels against locks and retain upstream license notices."""
from __future__ import annotations
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

HERE=Path(__file__).resolve().parent
VENDOR=HERE/'vendor'
WHEEL_NOTICES=VENDOR/'licenses'/'wheels'


def write_if_changed(path, data):
    """Retain exact bytes without touching already matching public notices."""
    if path.is_file() and path.read_bytes()==data:
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.notice-'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(data)
        if temporary.read_bytes()!=data:
            raise OSError('Retained license byte verification failed')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):value.update(chunk)
    return value.hexdigest()

def retain(bundle, member, root):
    relative=Path(member)
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe license path')
    target=root/relative
    write_if_changed(target,bundle.read(member))
    return target.relative_to(VENDOR).as_posix()


def wheel_notice_target(wheel, name, version, number, member):
    """Short public paths survive extraction below a deep WeChat download path."""
    label=re.sub(r'[^A-Za-z0-9._-]+','-',name+'-'+version).strip('.-') or 'package'
    bucket=label[:32]+'-'+hashlib.sha256(wheel.name.encode('utf-8')).hexdigest()[:8]
    leaf=re.sub(r'[^A-Za-z0-9._-]+','-',Path(member).name).strip('.-') or 'NOTICE'
    target=WHEEL_NOTICES/bucket/(f'{number:03d}-'+leaf[:24])
    if len(target.relative_to(HERE.parent).as_posix())>=110:
        raise ValueError('Public retained license path exceeds the short-layout bound')
    return target


def previous_wheel_notices():
    """Only paths recorded by our old public manifest are eligible for pruning."""
    path=VENDOR/'vendor-manifest.json'
    if not path.exists():return set()
    previous=json.loads(path.read_text(encoding='utf-8'))
    recorded={value for dependency in previous.get('dependencies',[]) for value in dependency.get('license_files',[])}
    result=set()
    root=WHEEL_NOTICES.resolve()
    for value in recorded:
        target=VENDOR/value
        if not target.resolve().is_relative_to(root):
            raise ValueError('Previous license manifest points outside the owned wheel-notice directory')
        result.add(target)
    return result


def prune_previous_wheel_notices(previous, current):
    root=WHEEL_NOTICES.resolve()
    for target in previous-current:
        if not target.resolve().is_relative_to(root):
            raise ValueError('Refusing to remove a notice outside the owned directory')
        target.unlink(missing_ok=True)
    # Remove empty obsolete folders individually, without recursive deletion.
    for directory in sorted((path for path in WHEEL_NOTICES.rglob('*') if path.is_dir()),key=lambda path:len(path.parts),reverse=True):
        if not directory.resolve().is_relative_to(root):
            raise ValueError('Refusing to prune a directory outside the owned wheel-notice tree')
        if not any(directory.iterdir()):directory.rmdir()

def main():
    manifest=json.loads((HERE/'runtime-manifest.json').read_text(encoding='utf8'))
    locks='\n'.join((HERE/f'requirements-{name}.lock').read_text(encoding='utf8') for name in ('bridge','astrbot'))
    hashes=set(re.findall(r'--hash=sha256:([0-9a-f]{64})',locks))
    previous=previous_wheel_notices()
    dependencies=[]
    notice_index=[]
    current=set()
    for wheel in sorted((VENDOR/'wheels').glob('*.whl')):
        sha=digest(wheel)
        if sha not in hashes:raise ValueError('Wheel absent from locked hashes: '+wheel.name)
        with zipfile.ZipFile(wheel) as bundle:
            names=bundle.namelist()
            info=[n for n in names if n.endswith('.dist-info/METADATA')]
            if len(info)!=1:raise ValueError('Invalid wheel metadata')
            meta=BytesParser().parsebytes(bundle.read(info[0]))
            notices=[]
            sources=[]
            selected=[]
            for name in names:
                leaf=Path(name).name.lower()
                if not name.endswith('/') and ('.dist-info/licenses/' in name or leaf.startswith(('license','copying','notice','copyright')) and Path(leaf).suffix in ('','.txt','.md','.rst')):
                    selected.append(name)
            for number,member in enumerate(sorted(selected),1):
                relative=Path(member)
                if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe upstream wheel notice path')
                target=wheel_notice_target(wheel,meta['Name'],meta['Version'],number,member)
                data=bundle.read(member)
                write_if_changed(target,data)
                if target.read_bytes()!=data:raise ValueError('Retained wheel notice bytes changed')
                current.add(target)
                path=target.relative_to(VENDOR).as_posix()
                notices.append(path)
                source={'path':path,'member':member,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                sources.append(source)
                notice_index.append(dict(source,wheel=wheel.name,wheel_sha256=sha))
            dependencies.append({'wheel':wheel.name,'name':meta['Name'],'version':meta['Version'],'sha256':sha,
                'bytes':wheel.stat().st_size,'license_expression':meta.get('License-Expression') or meta.get('License') or '',
                'license_classifiers':[x for x in meta.get_all('Classifier',[]) if x.startswith('License ::')],
                'license_files':notices,'license_sources':sources,'upstream':f'https://pypi.org/project/{meta["Name"]}/{meta["Version"]}/'})
    for name,expected,label in ((manifest['python_archive'],manifest['python_archive_sha256'],'python'),
        (manifest['ffmpeg_archive'],manifest['ffmpeg_sha256'],'ffmpeg'),
        ('uv-x86_64-pc-windows-msvc.zip',manifest['uv_sha256'],'uv'),
        (manifest['astrbot_wheel'],manifest['astrbot_sha256'],'astrbot')):
        archive=VENDOR/name
        if digest(archive)!=expected:raise ValueError('Runtime archive hash mismatch: '+name)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                leaf=Path(member).name.lower()
                if not member.endswith('/') and leaf.startswith(('license','copying','notice','readme')) and Path(leaf).suffix in ('','.txt','.md','.rst'):
                    retain(bundle,member,VENDOR/'licenses'/label)
    index_path=WHEEL_NOTICES/'index.json'
    write_if_changed(index_path,json.dumps({'schema':1,'layout':'short-name-version-and-numbered-leaf',
        'note':'Paths are relative to vendor/. Original ZIP member names and original wheel digests are retained for traceability.',
        'notices':notice_index},ensure_ascii=False,indent=2).encode('utf-8'))
    prune_previous_wheel_notices(previous,current)
    artifacts=[]
    for path in sorted(VENDOR.rglob('*')):
        if path.is_file() and path.name!='vendor-manifest.json':
            artifacts.append({'path':path.relative_to(VENDOR).as_posix(),'bytes':path.stat().st_size,'sha256':digest(path)})
    data={'schema':1,'target':'Windows x86_64 / Python 3.12','runtime_versions':{'uv':manifest['uv_version'],'python':manifest['python_request'],'astrbot':manifest['astrbot_version'],'ffmpeg':manifest['ffmpeg_version']},
        'wheel_count':len(dependencies),'artifact_count':len(artifacts),'total_bytes':sum(a['bytes'] for a in artifacts),
        'dependencies':dependencies,'artifacts':artifacts,
        'python_source':{'url':manifest['python_original_url'],'sha256':manifest['python_original_sha256'],'note':manifest['python_repack_note']},
        'license_index':'licenses/wheels/index.json',
        'source_notices':'Licenses remain inside each original wheel/archive and are additionally retained under licenses/. Short wheel-notice paths map to original wheel members in licenses/wheels/index.json.'}
    write_if_changed(VENDOR/'vendor-manifest.json',json.dumps(data,ensure_ascii=False,indent=2).encode('utf8'))
    print(json.dumps({'wheels':len(dependencies),'artifacts':len(artifacts),'vendor_bytes':data['total_bytes'],'manifest_sha256':digest(VENDOR/'vendor-manifest.json')}))

if __name__=='__main__':main()
