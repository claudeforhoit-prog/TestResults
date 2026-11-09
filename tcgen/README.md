# 테스트 실행 명령 생성기 (tcgen)

테스트 케이스 표를 입력하면, 점프 VM 터미널과 ESX root SSH 창에 붙여넣을 crontab 등록 블록을 만든다.
사용자는 명령과 케이스를 화면에 입력하고 [명령어 생성] 을 누른 뒤, 만들어진 블록을 복사해 붙여넣는다.

## 1. 구성

| 파일 | 내용 |
| :-- | :-- |
| `tcgen_app.py` | 화면 |
| `tcgen_core.py` | 시각 계산, crontab 줄, 붙여넣기 블록, 기록 파일 저장 |
| `run_gui.bat` | 실행. `tcgen.exe` 가 있으면 그것을, 없으면 설치된 Python 으로 실행한다 |
| `build_windows.bat` | Windows 실행 파일(`dist\tcgen\tcgen.exe`) 빌드 |
| `tests\` | 자동 시험(`test_core.py`, `test_blocks_shell.py`)과 화면 점검(`gui_smoke.py`) |

Python 표준 라이브러리만 쓴다. 추가 패키지는 필요 없다.

## 2. 설치와 실행

- Python 3.9 이상이 있는 PC: `run_gui.bat` 을 실행한다. `python tcgen_app.py` 와 같다.
- Python 이 없는 PC: Python 과 인터넷이 있는 PC 에서 `build_windows.bat` 을 실행한다. 만들어진 `dist\tcgen` 폴더를 통째로 복사해 `tcgen.exe` 를 실행한다.

생성기 폴더는 `tcgen_app.py` 또는 `tcgen.exe` 가 있는 폴더다. 설정과 명령 목록을 이 폴더에 저장하므로, 화면의 [설정 폴더 열기] 가 이 폴더를 연다.

| 파일 (생성기 폴더 기준) | 내용 |
| :-- | :-- |
| `settings.json` | 테스트 기록 CSV 경로, 로그 폴더, VM 접속 계정, 모니터링 시작 분, 중지 분 |
| `run_commands.csv` | 탭 1 실행 명령 목록 |
| `monitor_commands.csv` | 탭 2 모니터링 명령 목록 |
| `testcases\test_record.csv` | 테스트 기록 CSV 기본 위치 |
| `testcases\testcaselogging-년월일시분초.csv` | 생성할 때마다 만드는 로그 (기본 위치) |
| `backup\년월일시분초\` | [설정 백업] 으로 복사한 설정 파일 3개 |

- 명령 목록은 화면에서만 고친다. 엑셀로 열어 저장하면 DRM 이 파일을 암호화해 생성기가 읽지 못할 수 있다.
- `settings.json` 의 `pvm_suffix`, `svm_suffix` (기본 `ev01`, `ev02`) 는 PVM, SVM 이름 자동 입력에 쓰는 접미사다. 화면에는 없고 이 파일에서만 바꾼다.

## 3. 화면

### 3.1 설정 줄

| 항목 | 내용 |
| :-- | :-- |
| 테스트 기록 CSV [찾기] | 기존 파일을 고르거나 새 파일 이름을 적는다. 생성할 때마다 케이스 하나에 한 행을 추가한다 |
| 로그 폴더 [찾기] | 생성할 때마다 `testcaselogging-년월일시분초.csv` 를 만든다. crontab 줄 하나에 한 행 |
| VM 접속 계정 | 점프 VM 에서 ssh 로 접속할 계정. 비우면 점프 VM 의 현재 계정으로 접속한다 |
| 모니터링 시작 N분 전 | 기본 5. 모니터링 시작 = 잡 시작 N분 전 |
| 모니터링 중지 N분 전 | 기본 10. 자동 중지 = 다음 케이스 잡 시작 N분 전 |
| [설정 저장] | `settings.json` 저장 |
| [설정 백업] | `settings.json`, `run_commands.csv`, `monitor_commands.csv` 를 `backup\년월일시분초\` 에 복사. 복원은 이 파일들을 생성기 폴더에 덮어쓰고 생성기를 다시 실행한다 |
| [설정 폴더 열기] | 생성기 폴더를 연다 |
| [테스트 케이스 폴더 열기] | 로그 폴더(`testcaselogging-*.csv`)를 연다 |

### 3.2 탭 1. 실행 명령

| 열 | 내용 |
| :-- | :-- |
| App명 | 응용 이름 |
| 명령어 명 | 탭 3 에서 고르는 이름. 영문, 숫자, `.` `_` `-` 만 사용, 중복 불가 |
| 잡 조건 설명 | 예: 1 job, 4 jobs 동시. 테스트 기록 CSV 에 함께 기록한다 |
| 경로/실행 명령 | 절대 경로로 입력한다. 이 명령이 crontab 에 한 줄로 그대로 들어간다 |

명령 안에 쓸 수 있는 변수는 두 가지다.

| 변수 | 값 |
| :-- | :-- |
| `<vm이름>` | 명령이 실행되는 VM 의 hostname (`hostname -s`). ESX 명령에서는 ESX 호스트 이름 |
| `<실행시각>` | 명령이 실행된 시각 14자리, KST. 예: `20261109000000` |

예: `/home/tester/bench/run_a.sh >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1`

### 3.3 탭 2. 모니터링 명령

탭 1 의 열에 실행 위치(VM 또는 ESX), 시작 명령, 중지 명령이 더해진다. 어떤 모니터링 명령이든 직접 등록한다.

- 실행 위치가 VM 이면 PVM, SVM 의 crontab 에, ESX 이면 ESX 호스트의 crontab 에 등록한다.
- 중지 명령을 비우면 중지 줄을 만들지 않는다.

### 3.4 탭 3. 테스트 케이스

| 열 | 내용 |
| :-- | :-- |
| 케이스 이름 | 영문, 숫자, `.` `_` `-` 만 사용, 중복 불가 |
| ESX 호스트 | 입력하면 PVM = 호스트+`ev01`, SVM = 호스트+`ev02` 가 자동 입력된다. PVM, SVM 은 직접 고칠 수 있다 |
| PVM 잡, SVM 잡 | 탭 1 의 명령어 명 중에서 고른다. `(없음)` 이면 그 VM 에는 잡 줄을 만들지 않는다 |
| 간격 | 윗줄 잡 시작부터 이 케이스 잡 시작까지의 시간. `12:00` 형식 |
| 잡 시작 (KST) | `2026-11-09 10:00:00` 형식. 분 단위 |
| 모니터링 | 칸을 두 번 누르면 선택 창이 열린다. VM 모니터링은 PVM, SVM, ESX 모니터링은 ESX 를 체크한다. 기본은 모두 선택 안 함 |
| 모니터링 시작 | 잡 시작 5분 전 (자동 계산) |
| 모니터링 중지 | 칸을 두 번 누르면 자동 / 직접 입력 / 중지 없음 중에서 고른다. 자동은 다음 케이스 잡 시작 10분 전이고, 마지막 케이스는 잡 시작 + 간격 - 10분 |

- [+ 케이스 추가]: 선택한 행을 복사해 아래에 넣는다. 간격은 그 행 간격을 가져오고 잡 시작 = 윗줄 잡 시작 + 간격 으로 계산한다. 이름 끝에 `-r2`, `-r3` 을 추가해 중복을 피한다.
- 잡 시작이나 간격을 고치면 아래 행의 잡 시작이 다시 계산된다.
- [위로], [아래로] 는 케이스 순서만 바꾼다. 잡 시작, 간격, 모니터링 중지 설정은 그 행에 남는다.
- [삭제] 는 다른 케이스의 잡 시작을 바꾸지 않는다.
- 탭 3 의 표는 저장하지 않는다. 생성한 케이스는 테스트 기록 CSV 와 로그에 남는다.

### 3.5 탭 4. 생성 결과

[명령어 생성] 을 누르면 입력을 검사하고, 오류가 없으면 기록 파일을 저장한 뒤 블록 세 개를 보여 준다. 오류가 있으면 블록과 기록을 만들지 않는다.

| 블록 | 붙여넣는 곳 | 하는 일 |
| :-- | :-- | :-- |
| A. 점프 VM 최초 1회 설정 | 점프 VM 터미널 (처음 한 번) | VM 비밀번호를 한 번 입력받아 `~/.tcgen/vmpw` 에 저장(권한 600). 자동 로그인용 `~/.tcgen/vmssh`, `~/.tcgen/askpass` 생성 |
| B. VM 등록 블록 | 점프 VM 터미널 (생성할 때마다) | VM 마다 ssh 로 접속해 crontab 에 줄을 등록하고, 등록된 줄을 출력한다 |
| C. ESX 블록 | 그 ESX 의 root SSH 창 | `/var/spool/cron/crontabs/root` 에 줄을 추가하고 `cron.update` 로 crond 에 알린다 (Broadcom KB 323138 절차) |

ESX 가 여러 대이면 C 블록의 호스트 목록에서 하나씩 골라 복사한다.

## 4. 사용 순서

1. 탭 1, 탭 2 에 명령을 등록하고 [저장] 을 누른다. 한 번 저장한 명령은 다음 실행 때 다시 불러온다.
2. 탭 3 에 케이스를 입력하고 [명령어 생성] 을 누른다.
3. 점프 VM 에서 처음 한 번만 A 블록을 붙여넣는다. `VM password:` 가 나오면 VM 비밀번호를 입력한다. A 블록은 다른 블록과 함께 붙여넣지 않는다.
4. 점프 VM 에 B 블록을 붙여넣는다. VM 마다 `---- VM이름`, `== hostname 시각`, 등록된 `# tcgen` 줄이 출력된다.
5. ESX 의 root SSH 창에 C 블록을 붙여넣는다. 마지막에 그 ESX 에 등록된 `# tcgen` 줄이 출력된다.

같은 블록을 다시 붙여넣어도 줄이 늘지 않는다. 케이스를 고쳐 다시 생성하면, 블록에 들어 있는 케이스의 기존 줄을 지우고 새 줄로 바꾼다.

## 5. crontab 에 들어가는 줄

명령 하나가 crontab 한 줄이 된다. 줄 끝의 `# tcgen 케이스이름 호스트` 는 다시 붙여넣을 때 기존 줄을 찾는 표식이다.

```
55 23 19 11 * TC_HOST=`hostname -s`; TC_START=`date +\%Y\%m\%d\%H\%M\%S`; /home/tester/mon/start_mon.sh >> /home/tester/log/mon-${TC_HOST}-${TC_START}.log # tcgen TC-base-a esx01ev01
00 00 20 11 * TC_HOST=`hostname -s`; TC_START=`date +\%Y\%m\%d\%H\%M\%S`; /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1 # tcgen TC-base-a esx01ev01
50 11 20 11 * /home/tester/mon/stop_mon.sh # tcgen TC-base-a esx01ev01
```

- 변수를 쓴 명령만 앞에 `TC_HOST=...;`, `TC_START=...;` 가 추가된다. 등록한 명령은 바꾸지 않는다.
- cron 은 홈 폴더에서 명령을 실행한다. 블록을 붙여넣은 폴더와 관계없다.
- VM 의 cron(cronie)은 명령 안의 `%` 를 줄바꿈으로 처리하므로 VM 줄은 `%` 를 `\%` 로 바꿔 넣는다. cron 이 실행할 때 `\%` 를 `%` 로 바꾼다.
- ESX 의 crond(busybox)는 명령을 바꾸지 않고 실행하므로 ESX 줄은 입력한 그대로 넣는다.
- ESX 시계는 UTC 다. ESX 줄의 시각은 KST - 9시간으로 바꿔 넣는다. 로그 CSV 의 `crontab 시각 기준` 열에 KST 또는 UTC 를 적는다.
- cron 줄에는 연도가 없다. 지난 시각을 넣으면 내년 같은 날짜에 실행되므로 생성할 때 경고한다.

## 6. 확인과 삭제

```
crontab -l | grep ' # tcgen '                                   # VM: 등록된 줄 확인
crontab -l | grep -v ' # tcgen TC-base-a ' | crontab -          # VM: 케이스 TC-base-a 줄 삭제
grep ' # tcgen ' /var/spool/cron/crontabs/root                  # ESX: 등록된 줄 확인
```

ESX 에서 줄을 지운 뒤에는 `/bin/echo root >> /var/spool/cron/crontabs/cron.update` 를 실행한다.

## 7. 제한과 주의

- A, B 블록은 csh(tcsh) 용이다. 점프 VM 의 로그인 셸이 bash 이면 here-document 끝 줄 형식이 달라 그대로 쓸 수 없다.
- 명령에 `!`, 탭, 한글을 쓸 수 없다(한글은 변수 이름에만). csh 가 붙여넣는 글의 `!` 를 이전 명령 불러오기로, 탭을 자동 완성으로 처리하기 때문이다.
- 이름(케이스, 호스트, 명령어 명)은 영문, 숫자, `.` `_` `-` 만 쓴다.
- ESX 를 재부팅하면 crontab 변경이 지워진다. 재부팅 뒤에는 C 블록을 다시 붙여넣는다.
- VM 비밀번호는 점프 VM 의 `~/.tcgen/vmpw` 에만 저장한다(본인만 읽기). 생성기 파일, CSV, 블록에는 비밀번호가 들어가지 않는다. ESX 비밀번호는 저장하지 않는다.
- 비밀번호를 바꾸면 A 블록을 다시 붙여넣는다.

## 8. 시험

```
python -m unittest discover -s tests -v
```

- `test_core.py`: 시각 계산, crontab 줄, 오류 검사, 기록 파일.
- `test_blocks_shell.py`: tcsh 가 있는 Linux, macOS 에서만 실행된다. 대화형 tcsh 에 A, B 블록을 붙여넣듯이 보내 가짜 ssh, crontab 으로 등록 결과를 확인하고, cronie 와 busybox crond 처럼 줄을 실행해 변수와 `%` 처리를 확인한다. Windows 에서는 건너뛴다.
- `gui_smoke.py`: 화면을 띄워 버튼과 선택 창을 코드로 눌러 본다. 수동 실행: `python tests\gui_smoke.py`
- 실제 점프 VM, VM, ESX 에서의 동작은 아직 확인하지 않았다.
