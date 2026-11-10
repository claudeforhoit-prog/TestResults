"""창 화면에서 사용자가 바꾸기 전까지 유지할 값 (예: TAT 비교 기준값과 그 설명).

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
    d = load()
    d.update(values)
    try:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        with open(path(), 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False
