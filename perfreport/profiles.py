"""설정 묶음(프로파일): 기준선, 케이스 리포트 옵션, 비교 리포트 양식을 각각 이름을 붙여 파일로 저장한다.

저장 위치: 데이터 폴더(기본 C:\\Users\\<계정>\\perfreport_data, 환경 변수 PERFREPORT_DATA 로 바꿈) 아래
  baselines\\<이름>.json · case_options\\<이름>.json · compare_presets\\<이름>.json
서버가 바뀌면 그 종류의 묶음을 복제해 고쳐 쓴다. 파일을 복사하면 다른 PC 로 옮길 수 있다(내보내기 · 가져오기).
"""
import json
import os
import re
import shutil

KINDS = {'baselines': '기준선 프로파일', 'case_options': '옵션 프로파일', 'compare_presets': '비교 리포트 양식'}
DEFAULT_NAME = '기본'


def data_dir():
    return os.environ.get('PERFREPORT_DATA') or os.path.join(os.path.expanduser('~'), 'perfreport_data')


def _dir(kind):
    if kind not in KINDS:
        raise ValueError(kind)
    return os.path.join(data_dir(), kind)


def clean_name(name):
    """파일 이름으로 쓸 수 있는 이름. 쓸 수 없는 글자는 _ 로 바꾼다."""
    return re.sub(r'[\\/:*?"<>|]+', '_', str(name or '')).strip(' .')


def _path(kind, name):
    return os.path.join(_dir(kind), clean_name(name) + '.json')


def names(kind):
    try:
        return sorted(os.path.splitext(f)[0] for f in os.listdir(_dir(kind)) if f.lower().endswith('.json'))
    except OSError:
        return []


def load(kind, name):
    try:
        with open(_path(kind, name), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(kind, name, data):
    """임시 파일에 쓴 뒤 바꿔치기한다 (쓰는 도중 끊겨도 기존 파일이 남는다)."""
    p = _path(kind, name)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def delete(kind, name):
    try:
        os.remove(_path(kind, name))
        return True
    except OSError:
        return False


def rename(kind, old, new):
    new = clean_name(new)
    if not new or new in names(kind):
        return False
    try:
        os.replace(_path(kind, old), _path(kind, new))
        return True
    except OSError:
        return False


def export_to(kind, name, path):
    shutil.copyfile(_path(kind, name), path)
    return path


def import_from(kind, path):
    """파일을 이 종류의 묶음으로 가져온다. 같은 이름이 있으면 이름 뒤에 번호를 붙인다. 반환: 가져온 이름."""
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError('설정 묶음 파일이 아님')
    base = clean_name(os.path.splitext(os.path.basename(path))[0]) or DEFAULT_NAME
    name, n = base, 2
    while name in names(kind):
        name, n = f'{base} ({n})', n + 1
    save(kind, name, d)
    return name
