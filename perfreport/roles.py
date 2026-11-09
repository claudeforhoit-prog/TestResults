"""지표 역할 지정: 성능평가방법론의 지표 역할 5종(metrics.ROLES)을 지표마다 하나씩 정한다.

리포트를 만들 때마다 창 화면(또는 역할 파일)으로 정한다. 지정이 하나도 없으면 기본 리포트 양식으로 만든다.

역할 파일(metric_roles.csv, UTF-8 BOM): 열 '지표, 역할, 순서'. 순서는 같은 역할 안의 출력 순서(1부터).
"""
import csv
import io

from . import metrics as M

ROLES = M.ROLES
COLUMNS = ('지표', '역할', '순서')


def _read_rows(path):
    for enc in ('utf-8-sig', 'cp949'):
        try:
            with io.open(path, encoding=enc, newline='') as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise SystemExit(f'역할 파일의 문자 인코딩을 읽지 못함: {path}')


def load(path):
    """역할 파일 → {지표: (역할, 순서)}. 역할 이름이 5종이 아니면 오류."""
    rows = _read_rows(path)
    if rows and not {'지표', '역할'} <= set(rows[0]):
        raise SystemExit(f'역할 파일에 열이 없음: 지표, 역할 (있는 열: {", ".join(rows[0])})')
    out, errs = {}, []
    for i, r in enumerate(rows, start=2):
        m, role = (r.get('지표') or '').strip(), (r.get('역할') or '').strip()
        if not m or not role:
            continue
        if role not in ROLES:
            errs.append(f'{i}행 {m}: 역할 이름이 다름 "{role}" (쓸 수 있는 이름: {", ".join(ROLES)})')
            continue
        try:
            order = int(float(r.get('순서') or 0))
        except ValueError:
            order = 0
        out[m] = (role, order or 10_000 + i)
    if errs:
        raise SystemExit('역할 파일 오류\n  ' + '\n  '.join(errs))
    return normalize(out)


def save(path, roles):
    roles = normalize(roles)
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for role in ROLES:
            for m in members(roles, role):
                w.writerow([m, role, roles[m][1]])
    return path


def members(roles, role):
    return [m for m, _ in sorted(((m, o) for m, (r, o) in roles.items() if r == role), key=lambda x: x[1])]


def normalize(roles):
    """역할마다 순서를 1부터 다시 매긴다."""
    out = {}
    for role in ROLES:
        for i, m in enumerate(members(roles, role), start=1):
            out[m] = (role, i)
    return out


def restrict(roles, names):
    """데이터에 있는 지표만 남긴다. 반환: (남긴 역할, 데이터에 없어 뺀 지표 목록)."""
    names = set(names)
    kept = {m: v for m, v in roles.items() if m in names}
    return normalize(kept), sorted(set(roles) - names)


def summary(roles):
    roles = roles or {}
    parts = [f'{r} {len(members(roles, r))}개' for r in ROLES if members(roles, r)]
    return ', '.join(parts) if parts else '지정 없음 (기본 리포트 양식)'


def display_order(names, roles):
    """역할 지정 지표(역할 순서 → 역할 안 순서) 다음에 미지정 지표(분류 순서)."""
    names = list(dict.fromkeys(names))
    first = [m for r in ROLES for m in members(roles, r) if m in names]
    rest = [m for m in M.flat(names) if m not in roles]
    return first + rest


class RoleModel:
    """창 화면의 지표 역할 지정 상태. Tk 와 무관하게 시험할 수 있게 상태와 동작만 둔다."""

    def __init__(self, names, roles=None):
        self.names = list(dict.fromkeys(names))
        self.cat = {m: M.category(m) for m in self.names}
        self.roles = restrict(roles or {}, self.names)[0]
        self.checked = set()
        self.query = ''

    def rows(self):
        """(지표, 분류, 역할, 순서, 체크 여부): 화면 표시 순서, 검색어 적용."""
        q = self.query.strip().lower()
        out = []
        for m in display_order(self.names, self.roles):
            if q and q not in m.lower() and q not in self.cat[m].lower() and q not in self.roles.get(m, ('',))[0].lower():
                continue
            r, o = self.roles.get(m, ('', ''))
            out.append((m, self.cat[m], r, o, m in self.checked))
        return out

    def toggle(self, m):
        self.checked.symmetric_difference_update({m})

    def check_visible(self, on=True):
        vis = {r[0] for r in self.rows()}
        if on:
            self.checked |= vis
        else:
            self.checked -= vis

    def clear_checks(self):
        self.checked.clear()

    def assign(self, role):
        """체크한 지표를 그 역할 끝 순서에 넣는다(이미 그 역할이면 그대로)."""
        if role not in ROLES:
            raise ValueError(role)
        end = len(members(self.roles, role))
        for m in display_order([m for m in self.names if m in self.checked], {}):
            if self.roles.get(m, ('',))[0] != role:
                end += 1
                self.roles[m] = (role, end + 0.5)
        self.roles = normalize(self.roles)

    def unassign(self):
        for m in list(self.checked):
            self.roles.pop(m, None)
        self.roles = normalize(self.roles)

    def move(self, delta):
        """체크한 지표를 같은 역할 안에서 한 칸 위(-1) 또는 아래(+1)로 옮긴다."""
        for role in ROLES:
            seq = members(self.roles, role)
            idx = range(1, len(seq)) if delta < 0 else range(len(seq) - 2, -1, -1)
            for i in idx:
                j = i + (-1 if delta < 0 else 1)
                if seq[i] in self.checked and seq[j] not in self.checked:
                    seq[i], seq[j] = seq[j], seq[i]
            for k, m in enumerate(seq, start=1):
                self.roles[m] = (role, k)

    def load(self, path):
        roles = load(path)
        self.roles, missing = restrict(roles, self.names)
        return missing

    def save(self, path):
        return save(path, self.roles)
