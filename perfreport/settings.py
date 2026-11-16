"""창 화면에서 사용자가 바꾸기 전까지 유지할 값: 입력 경로, 기준선 목록(baselines), 케이스 정보 파일마다의 생성 체크 · 설명 · 기준선(case_state), 종료 기준 VM.

저장 위치: Windows 는 %APPDATA%\\perfreport\\settings.json, 그 밖에는 ~/.config/perfreport/settings.json.
"""
import json
import os


def path():
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'perfreport', 'settings.json')


def load():
    try:
        with open(path(), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(values):
    """값을 더해 저장한다. 임시 파일에 쓴 뒤 바꿔치기해서 쓰는 도중 끊겨도 기존 파일이 남는다.
    기존 파일을 읽지 못하면(깨진 파일) 덮어쓰기 전에 .bad 로 보관한다."""
    p = path()
    d = load()
    try:
        if not d and os.path.isfile(p) and os.path.getsize(p) > 2:
            try:
                with open(p, encoding='utf-8') as f:
                    json.load(f)
            except ValueError:
                os.replace(p, p + '.bad')
        d.update(values)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        return False
