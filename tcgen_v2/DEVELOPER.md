# 개발 지침 (이 생성기를 고치거나 다시 만드는 사람과 AI 용)

이 문서는 테스트 실행 명령 생성기(tcgen) v2 를 고치는 사람과 AI 코딩 도구가 맨 먼저 읽는 문서다.
사용 방법은 `README.md` 에 있다. 이 문서는 구조, 요청 없이 고치면 안 되는 설계 규칙, plan 형식, 시험 방법, 고치는 절차를 적는다.

## 1. AI 에게 시킬 때

```
DEVELOPER.md 와 README.md 를 먼저 읽어라.
3절의 설계 규칙은 사용자가 정한 것이므로 요청 없이 바꾸지 마라.
jump/ 의 파일을 고쳤으면 python tools/make_payload.py 를 실행해라.
고친 뒤 python -m unittest discover -s tests -v 가 통과하는지 확인하고, 규칙을 바꿨으면 DEVELOPER.md 와 README.md 와 시험을 같이 고쳐라.
```

## 2. 구조

### 2.1 파일

| 파일 | 하는 일 |
| :-- | :-- |
| `tcgen_core.py` | 화면과 무관한 계산 전부. 앞부분은 v1.4 에서 이어지는 코드(시각 계산, crontab 줄, 블록 A, B, C, D, 기록 CSV, 리포트용 CSV, 데이터 폴더)이고, 뒷부분의 '설정 프로파일' 과 '순서 실행' 절이 v2 에서 더한 코드다. 버전은 `VERSION` |
| `tcgen_app.py` | 화면(tkinter). 탭 1 실행 명령, 탭 2 모니터링 명령, 탭 3 설정 프로파일, 탭 4 테스트 케이스, 탭 5 생성 결과. 진행 방식에 따라 `apply_mode` 가 열과 칸을 보이거나 숨긴다 |
| `jump/tcrun.sh` | 점프 VM 의 진행 스크립트(POSIX sh). 설치되면 `~/.tcgen/tcrun` |
| `jump/tcgen_apply.py` | ESX 호스트에서 실행하는 설정 적용 스크립트(Python 3.5 문법, 표준 라이브러리만) |
| `tcgen_payload.py` | 위 두 파일의 내용을 담은 모듈. `tools/make_payload.py` 가 만든다. 직접 고치지 않는다. exe 로 빌드해도 두 파일의 내용이 함께 들어가게 하려고 모듈로 둔다 |
| `tools/make_payload.py` | `jump/` 의 두 파일을 `tcgen_payload.py` 로 옮긴다. `--check` 는 같은지만 확인한다 |
| `run_gui.bat`, `build_windows.bat` | 실행, Windows 실행 파일 빌드(PyInstaller). ASCII, CR+LF 로 저장한다 |
| `tests/` | 6절 |

Python 표준 라이브러리만 쓴다. 추가 패키지를 넣지 않는다. PC 쪽 코드(`tcgen_core.py`, `tcgen_app.py`, `tcgen_payload.py`)는 Python 3.8 에서 동작해야 한다.

### 2.2 세 곳에서 실행되는 코드

| 실행되는 곳 | 코드 | 하는 일 |
| :-- | :-- | :-- |
| PC | `tcgen_core.py`, `tcgen_app.py` | 입력을 검증하고 블록과 plan 을 만든다. ssh 를 호출하지 않는다 |
| 점프 VM | `~/.tcgen/tcrun` | plan 을 읽어 ESX 호스트별로 케이스를 순서대로 진행한다. VM 에는 `vmssh` 와 같은 방법(비밀번호는 `vmpw`, `SSH_ASKPASS`, `setsid`)으로, ESX 에는 `esxssh`(키)로 접속한다 |
| ESX 호스트 | `/tmp/tcgen/tcgen_apply.py` | 설정 비교와 적용, VM 종료와 켜기, 이전 값으로 복원. `tcrun` 이 호출할 때마다 전송해 저장한다 |

VM 에는 파일을 만들지 않는다. `tcrun` 이 ssh 표준 입력으로 셸 글을 보내 crontab 을 고치고 프로세스를 찾는다.

### 2.3 순서 실행의 자료 흐름

1. `build_seq` 가 입력을 검증하고 케이스별 내용(등록할 명령 글, 설정 항목, 프로세스 이름 패턴)을 만든다.
2. `plan_text` 가 그것을 plan(5절)으로 쓴다. `plan_view` 는 같은 내용을 사람이 읽는 글로 쓴다.
3. `seq_check_block`, `seq_run_block` 이 plan 을 gzip 뒤 base64 로 블록에 넣는다. 블록은 plan 을 `~/.tcgen/runs/<실행 ID>/plan` 에 풀고 sha256 을 확인한 뒤 `tcrun check` 또는 `tcrun start` 를 부른다.
4. `tcrun start` 가 ESX 호스트마다 `setsid sh tcrun run <plan> <호스트>` 를 백그라운드로 시작한다.
5. `tcrun run` 은 케이스마다 `apply_settings`, `register`, `wait_job`, `stop_mon` 을 차례로 부르고 로그 폴더에 기록한다.

`tcrun` 과 `tcgen_apply.py` 가 주고받는 줄은 `TCGEN-` 로 시작한다. `TCGEN-RESULT <낱말>` 이 결과다(`nochange`, `changed`, `wouldchange`, `poweredon`, `rolledback`, `nothing`, `error <이유>`). VM 과 ESX 에서 보낸 셸 글의 답도 같은 방식이다(`TCGEN-CLOCK`, `TCGEN-CRON`, `TCGEN-PROC`, `TCGEN-LINE`).

## 3. 설계 규칙 (사용자가 정한 것. 요청 없이 바꾸지 않는다)

### 3.1 동작 방식 (v1.4 에서 이어지는 규칙)

- PC 의 생성기는 글자만 만든다. PC 에서 ssh 를 호출하지 않는다. 사용자가 블록을 복사해 붙여넣는다.
- 엑셀 파일을 입력이나 저장 형식으로 쓰지 않는다(문서 보안 프로그램이 암호화한다). CSV 와 JSON 만 쓴다.
- 등록한 명령은 고치지 않고 crontab 한 줄로 그대로 등록한다. 시작용, 중지용 실행 파일을 VM 에 만들지 않는다.
- 자동으로 추가하는 것은 세 가지뿐이다: 변수를 쓴 명령 앞의 `TC_HOST=…; TC_START=…;`, VM 줄의 `%` 를 `\%` 로, 줄 끝 표식 ` # tcgen <케이스> <호스트>`.
- ESX 줄의 시각은 UTC 다. ESX 줄은 `%` 를 고치지 않는다.
- 붙여넣기 블록은 ASCII 만 쓰고 csh, tcsh, bash 공통이어야 한다: here-document 끝 줄은 따옴표 없이 쓰고 본문의 `\`, `$`, `` ` `` 앞에 역슬래시. `!` 와 탭을 쓰지 않는다. 줄 끝을 역슬래시로 끝내지 않는다. CR+LF 로 붙여넣어도 동작해야 한다.
- VM 비밀번호는 점프 VM 의 `~/.tcgen/vmpw`(권한 600)에만 둔다. 생성기 파일, CSV, 블록, plan 에 비밀번호를 넣지 않는다. ESX 비밀번호는 저장하지 않는다.
- 블록 D(VM crontab 전체 삭제)는 삭제 전에 VM 에 백업 파일을 남기고, 복사 전에 확인을 받는다. ESX 는 대상이 아니다.

### 3.2 판 구분 (사용자 2026-11-16 "이전 버전하고 구분되도록 저장")

- v2 는 v1.x 와 이름이 다르다: 실행 파일 `tcgen2`, 데이터 폴더 `tcgen2_data`, 설정 폴더 `tcgen2`. v1 의 `tcgen_data` 와 설정 폴더 `tcgen` 은 읽기만 한다(`import_from_v1`).
- 점프 VM 의 `~/.tcgen/vmssh`, `askpass`, `setpw` 는 v1.4 와 글자 하나까지 같게 둔다. v1.4 를 다시 쓸 때 그대로 쓰게 하기 위해서다(`test_v14_compat.py` 가 확인).
- 시각 지정 방식은 v1.4 와 같은 결과를 낸다. 같은 입력이면 블록 A, B, C, D 가 바이트 단위로 같다. `tcgen_core.py` 의 v1.4 부분(`build_entries`, `vm_block`, `esx_block`, `wipe_block`, `setup_block`)을 고치면 이 규칙이 깨진다.

### 3.3 순서 실행 (사용자 2026-11-15, 2026-11-16 지시)

- 케이스는 ESX 호스트별로 표의 순서대로 진행한다. 호스트끼리는 서로 기다리지 않는다.
- VM 은 표의 순서대로 하나씩 켠다. 앞 VM 에 ssh 로그인이 되는 것을 확인한 뒤 다음 VM 을 켠다.
- 한 케이스에서 잡이 있는 모든 VM 은 같은 분에 잡을 시작한다.
- 모니터링이 있으면 잡은 모니터링 시작 5분 뒤에 시작한다(설정값 `mon_lead_min`).
- 기본 프로파일은 `tcgen_core.py` 의 `DEFAULT_PROFILES` 에 있다. 데이터 폴더에 `profiles.json` 이 없을 때만 자동으로 넣고(`load_profiles(base, seed=True)`), [기본 프로파일 추가] 는 표에 없는 이름만 추가한다. 사용자가 고친 행을 덮어쓰지 않는다. 이름이나 내용을 바꾸면 `README.md` 7.4절의 표와 `tests/test_seq.py` 를 같이 고친다.
- VM 을 다시 켠 뒤에는 최소 N분(설정값 `seq_settle_min`, 기본 10)이 지난 뒤에 잡을 시작한다. 기준 시각은 마지막 VM 의 ssh 로그인 확인 시각이다. VM 을 다시 켜지 않은 경우에는 이 대기를 넣지 않는다.
- 잡 종료는 프로세스 이름으로 판정한다: 잡이 있는 모든 VM 에서 한 번 이상 보인 뒤 연속 N회 없음. 확인하지 못한 것(ssh 실패, pgrep 없음)을 없음으로 세지 않는다. 판정할 수 없으면 다음 케이스로 넘어가지 않고 멈춘다.
- 설정은 값이 이미 다른 값으로 들어 있어도 오류 없이 적용한다: vmx 에서 같은 키(대소문자 무시)의 줄을 모두 지우고 한 줄을 추가한다. 값이 모두 같으면 VM 을 끄지 않는다.
- CPU affinity 같은 값을 계산하지 않는다. 사용자가 키와 값을 직접 넣는다.
- 재부팅해야 적용되는 호스트 설정은 다루지 않는다.
- 점프 VM 에서 ESX 로는 ssh 키로 접속한다. 로그는 사용자가 화면에 적은 점프 VM 폴더에 남긴다.
- 멈추거나 stop 을 받았을 때 실행 중인 잡을 건드리지 않는다.

### 3.4 순서 실행의 구현 규칙 (이 판을 만들 때 정한 것. 고치려면 시험을 함께 고친다)

- `tcrun` 은 plan 의 글을 `eval` 하지 않고 이 장비의 셸에 넘기지 않는다. `grep`, `sed`, `cut` 으로 꺼내 파일에 쓰고, 원격에서는 따옴표로 감싼 here-document 로 넘긴다(`test_tcrun_does_not_eval_the_plan`).
- `tcrun` 과 `tcgen_apply.py` 에 시험용 코드를 넣지 않는다. 시험은 가짜 `date`, `sleep` 과 경로 환경 변수(`TCGEN_TMP`, `TCGEN_CRON_DIR`, `TCGEN_VMFS_ROOT`, `TCGEN_POLL_SEC`)만 쓴다. 이 변수들은 실제 장비에서는 설정되지 않는다.
- `tcrun` 은 POSIX sh 로 쓴다. `local`, 배열, `[[ ]]` 를 쓰지 않는다. dash 에서도 실행한다.
- `tcgen_apply.py` 는 Python 3.5 문법으로 쓴다. f-string, 변수 주석, `subprocess.run` 을 쓰지 않는다.
- `tcgen_apply.py` 는 무엇이든 바꾸기 전에 대상을 모두 확인한다(VM 을 찾고, vmx 를 읽고, 호스트 옵션을 읽는다). 하나라도 안 되면 아무것도 바꾸지 않는다.
- crontab 등록은 v1.4 의 B 블록, C 블록과 같은 방법이다: 그 케이스의 표식 줄을 지우고 새 줄을 넣는다.
- 등록 시각은 등록을 마친 시점에 15초 이상 남아 있어야 한다. 모자라면 그 케이스의 줄을 모두 지우고, 여유를 두 배로 늘려 다시 등록한다(세 번까지). 지우는 동안 시작 시각이 지났으면 일부 호스트에서 시작됐을 수 있으므로 다시 등록하지 않고 멈춘다.
- 일부 VM 에만 줄이 들어간 채로 멈추지 않는다. 등록 중에 실패하면 그 케이스의 줄을 모두 지운다.
- 모니터링 중지 줄도 같은 여유를 확인한다. 모자라면 더 뒤 시각으로 다시 등록한다(세 번까지).
- `tcrun` 은 plan 의 이름(케이스, VM, ssh 대상, ESX 주소)에 명령줄에서 뜻이 달라지는 글자가 없는지 다시 확인한다(`names_ok`, `addr_ok`). 생성기의 검증과 같은 규칙이다.

### 3.5 테스트 케이스 표

- 케이스 행 아래 VM 행. VM 마다 잡, 모니터링, 설정 프로파일을 지정한다. VM 역할 구분(이름)은 두지 않는다.
- VM 이름 자동 입력 = ESX 호스트 + `vm_prefix` + 두 자리 번호.
- 시각 지정: 모니터링 시작 = 잡 시작 전 설정 분, 중지 = 자동, 직접 입력, 중지 없음 가운데 하나. 새 행은 윗줄 값을 복사하고 시작 시각은 윗줄 + 간격.
- '적용' 을 체크한 케이스는 생성에서 빠진다. 시각 지정에서는 시각 계산에 남는다.
- 리포트용 CSV 에서 비교 기준 케이스를 정하지 않는다. [리포트용 CSV 저장] 은 고른 케이스를 탭 4 의 표 순서대로 저장한다(사용자 결정 2026-11-16. v1.4 의 `비교 기준 (첫 줄)` 칸은 v2.2 에서 뺐다). 비교 기준은 성능 리포트 생성기의 `기준 케이스` 칸에서 정한다.
- 리포트용 CSV 의 머리글(`REPORT_HEADER`)은 성능 리포트 생성기의 케이스 정보 파일 형식과 같아야 한다. 한쪽을 고치면 다른 쪽도 같이 고친다. 순서 실행의 리포트용 CSV 는 `tcrun` 이 plan 의 `RPTH`, `RPTA`, `RPTB` 줄로 만든다.

### 3.6 저장

- 설정, 명령 목록, 설정 프로파일, 케이스 표는 값이 달라질 때마다 자동 저장한다. 저장 버튼을 두지 않는다.
- 데이터는 생성기 폴더가 아니라 데이터 폴더에 둔다. 다시 빌드해도 데이터가 지워지지 않아야 한다.
- 읽지 못한 데이터 파일은 지우지 않고 이름을 바꿔 보관한다. 형식을 고치면 이전 형식을 읽어 변환하고 원본을 `backup` 에 남긴다.
- 새 값은 선택 항목으로 더한다. v1.4 가 만든 `settings.json`, `run_commands.csv`, `test_cases.json` 을 그대로 읽을 수 있어야 한다.

### 3.7 화면

- 테마는 clam, 색은 `COLORS` 사전 한 곳에서 정한다. 선택한 탭은 파란 바탕 흰 굵은 글씨.
- 쓸 수 없는 글자(`!`, 탭, 한글 등)는 생성 전에 오류로 알린다.
- 표에서 숨긴 열이 있을 때는 `EditableTable.set_visible` 을 쓴다. 누른 좌표의 열 순번은 화면의 순번이므로 `_double` 이 `columns` 의 순번으로 바꾼다.

## 4. 점프 VM 의 파일과 tcrun

| 파일 | 만드는 곳 | 비고 |
| :-- | :-- | :-- |
| `askpass`, `vmssh`, `setpw` | `VMSSH_LINES`, `SETPW_LINES` | v1.4 와 같다. 고치지 않는다 |
| `esxssh`, `postsetup` | `ESXSSH_LINES`, `POSTSETUP_LINES` | 짧은 파일. 블록에 `printf` 줄로 넣는다 |
| `tcrun`, `tcgen_apply.py` | `tcgen_payload.py` | 긴 파일. gzip 뒤 base64 로 넣고 설치 뒤 sha256 을 확인한다 |

`tcrun` 의 하위 명령은 `check <plan>`, `start <plan>`, `run <plan> <esx>`, `status [-a]`, `stop [esx]`, `key`, `version` 이다. `start` 는 S 블록이 쓴다.

실행 자료는 `~/.tcgen/runs/<실행 ID>/` 에 둔다.

| 파일 | 내용 |
| :-- | :-- |
| `plan` | 블록이 푼 plan |
| `<호스트>.pid` | 진행 프로세스의 pid. 끝나면 지운다 |
| `<호스트>.state` | `state=`, `time=`, `case=`, `pos=`, `step=`, `text=` 줄. `tcrun status` 가 읽는다 |
| `<호스트>.stop` | `tcrun stop` 이 만드는 깃발 파일 |
| `<호스트>.out` | 진행 프로세스의 표준 출력(정상일 때는 비어 있다) |
| `<호스트>.work/` | 원격에 보낸 셸 글과 받은 출력, VM 별 확인 상태(`seen.<VM>`, `zero.<VM>`, `gone.<VM>`), VM 켜기 완료 시각(`on.<VM>`) |

호스트 자리의 이름은 ESX 호스트 이름이고, ESX 호스트를 적지 않은 케이스는 `noesx` 다.

시각 계산은 `register` 에 있다. 가장 늦은 시계 = 점프 VM 시계 + `OFF`(VM 과 ESX 시계가 점프 VM 보다 앞선 초의 최댓값). 첫 시작 시각 = (가장 늦은 시계 + 여유)를 분으로 올림. 여유는 75초이고, 줄을 등록할 호스트가 넷을 넘으면 한 대마다 4초를 더한다(`ahead`). VM 을 다시 켠 경우 = max(그 값, (VM 켜기 완료 시각 + `OFF` + N분 - 모니터링 간격)을 분으로 올림). VM 의 crontab 시각 칸은 VM 에서 `date -d @<초>` 로, ESX 의 시각 칸은 점프 VM 에서 UTC 로 만든다.

## 5. plan 형식

plan 은 UTF-8 글 파일이고 줄 단위다. 첫 낱말이 종류, 둘째 낱말이 케이스 번호(세 자리)다. 낱말 사이는 빈칸 하나다. `tcrun` 은 `grep "^<종류> <번호> "` 와 `cut -d' ' -f<N>-` 로 읽는다. 줄의 나머지는 글자 그대로 쓴다.

머리 부분은 다음과 같다.

| 줄 | 내용 |
| :-- | :-- |
| `TCGEN-PLAN 2` | 형식 번호. 첫 줄 |
| `VER <판>` | plan 을 만든 생성기의 판. `tcrun` 의 판과 다르면 시작하지 않는다 |
| `RUNID <실행 ID>` | `년월일-시분초`. 로그 파일 이름과 실행 폴더 이름에 쓴다 |
| `LOGDIR <경로>` | 점프 VM 로그 폴더. `~/` 로 시작하면 홈 폴더 |
| `GEN <시각>` | 생성 시각(참고용) |
| `SET <이름> <정수>` | `MON_LEAD_MIN`, `SETTLE_MIN`, `POLL_SEC`, `GONE_COUNT`, `START_WAIT_MIN`, `POWERON_WAIT_MIN`, `BOOT_WAIT_MIN`, `SHUTDOWN_WAIT_MIN`. plan 에 없는 이름은 `tcrun` 의 기본값을 쓴다 |
| `ADDR <ESX 호스트> <주소>` | ESX 접속 주소. 없으면 호스트 이름으로 접속 |
| `CHAIN <ESX 호스트>` | 진행 하나. ESX 호스트를 적지 않은 케이스는 `-` |
| `RPTH <CSV 한 줄>` | 리포트용 CSV 의 머리글 |

케이스 부분은 `CASE` 줄로 시작하고 `END` 줄로 끝난다.

| 줄 | 내용 |
| :-- | :-- |
| `CASE <번호> <ESX 호스트 또는 -> <케이스 이름>` | 케이스 시작. plan 의 순서가 실행 순서 |
| `MAXWAIT <번호> <분>` | 최대 대기. 0 이면 제한 없음 |
| `RPTA <번호> <CSV 조각>` | 리포트용 CSV 한 행의 앞부분: 테스트 그룹, 케이스명, 설명, 호스트 이름 |
| `RPTB <번호> <CSV 조각>` | 뒷부분: VM, 종료 기준 VM. `tcrun` 은 `RPTA,잡 시작,잡 종료,RPTB` 로 한 행을 쓴다 |
| `VM <번호> <VM 이름> <ssh 대상>` | 그 케이스의 VM. 순서가 켜는 순서. ssh 대상은 `계정@VM` 또는 `VM` |
| `AH <번호> </구역/옵션>=<값>` | ESX 호스트 옵션 |
| `AV <번호> <VM 이름> <키>=<값>` | vmx 키. 값이 비면 삭제 |
| `PAT <번호> <VM 이름> <정규식>` | 그 VM 에서 이 케이스의 줄을 찾는 `grep` 패턴(표식 + `$`) |
| `MS <번호> <VM 이름> <명령 글>` | 모니터링 시작. 명령 글 = `render_command` 결과 + 표식. 시각 칸은 `tcrun` 이 추가 |
| `JOB <번호> <VM 이름> <명령 글>` | 잡 |
| `PROC <번호> <VM 이름> <정규식>` | `pgrep -f` 에 넘길 패턴. 프로세스 이름의 정규식 특수 문자를 PC 에서 역슬래시 처리한 것(`proc_pattern`) |
| `MX <번호> <VM 이름> <명령 글>` | 모니터링 중지 |
| `EPAT <번호> <정규식>`, `EMS <번호> <명령 글>`, `EMX <번호> <명령 글>` | ESX 호스트의 패턴, 모니터링 시작, 모니터링 중지 |
| `END <번호>` | 케이스 끝 |

- 케이스에 `PROC` 줄이 없으면 잡이 없는 케이스다(설정만 적용).
- `MS` 나 `EMS` 줄이 있으면 모니터링이 있는 케이스다.
- 형식을 고치면 `PLAN_FORMAT`, `tcrun` 의 첫 줄 확인, 이 절, `test_plan_text` 를 함께 고친다. `tcrun` 과 생성기는 `VER` 이 같을 때만 함께 동작하므로 이전 형식을 읽을 필요는 없다.

## 6. 시험

```
python -m unittest discover -s tests -v
```

| 파일 | 내용 |
| :-- | :-- |
| `test_core.py`, `test_blocks_shell.py` | v1.4 에서 옮겨 온 시험. 진행 방식을 시각 지정으로 두고 실행한다 |
| `test_v14_compat.py` | 시각 지정의 블록을 v1.4 의 결과와 바이트 단위로 비교한다. 기준 자료는 `fixtures/v14_blocks.json` 이고 `make_v14_fixture.py` 가 v1.4 소스로 만든다. 옆 폴더(`../tcgen`)에 v1.4 소스가 있으면 그 코드도 직접 실행해 비교한다 |
| `test_seq.py` | 순서 실행의 단위 시험 |
| `test_apply.py` | `tcgen_apply.py` 시험 |
| `test_tcrun.py` | `tcrun` 통합 시험 |
| `test_seq_blocks_shell.py` | 대화형 tcsh, bash 에 블록 A, P, S 를 붙여넣는 시험 |
| `fakeenv.py` | 위 세 시험이 쓰는 가짜 환경 |
| `gui_smoke.py` | 화면 점검(수동 실행). Tk 8.6 이상의 Python 으로 실행한다 |

가짜 환경(`fakeenv.py`)은 시험 폴더 하나에 점프 VM, VM, ESX 호스트를 폴더로 만들고 가짜 명령을 `PATH` 앞에 둔다.

- 가짜 `ssh`: VM 대상은 제어 터미널이 없는지와 askpass 비밀번호를 확인한다. ESX 대상(`-i`, `-l root`)은 키 파일과 `authorized` 파일이 있을 때만 접속한다. VM 이 꺼져 있으면 접속을 거절한다.
- 가짜 `date`, `sleep`: 가상 시계. 시험에서는 시간이 30배 빨리 간다(`TCGEN_TEST_SPEED`). `tcrun` 은 시간을 `date` 와 `sleep` 으로만 다루므로 시험용 코드가 필요 없다.
- `CronSim`: 등록된 crontab 줄을 가상 시계의 그 분에 실행한다. VM 은 cronie 의 `%` 처리를, ESX 는 `cron.update` 가 있을 때만 다시 읽는 동작을 흉내 낸다.
- 가짜 `vim-cmd`, `esxcli`: Broadcom KB 343248, 320877, 310338 의 출력 형식을 따른다. 실제 ESX 의 출력이 이 가짜와 다르면 시험으로는 찾지 못한다. `README.md` 12절의 '실제 장비에서 확인할 것' 을 참조한다.
- dash 시험(`DashTests`): `PATH` 앞에 `sh` 를 dash 로 두어 `tcrun`, VM 과 ESX 에 보내는 글, cron 이 실행하는 줄을 모두 dash 로 실행한다.

화면 글자가 잘리는지는 `gui_smoke.py <그림 폴더>` 로 창을 PNG 로 저장해 확인한다(macOS). 창 크기는 환경 변수 `TCGEN_W`, `TCGEN_H` 로 정한다.

## 7. 고치는 절차

1. 고치기 전에 고칠 파일을 복사해 둔다.
2. 고친다. 동작이 달라지면 `tcgen_core.py` 의 `VERSION`, `jump/tcrun.sh` 의 `TCRUN_VERSION`, `jump/tcgen_apply.py` 의 `VERSION` 을 같은 새 값으로 고치고 `README.md` 를 같이 고친다. 세 값이 다르면 `test_versions_match` 가 실패한다.
3. `jump/` 의 파일을 고쳤으면 `python tools/make_payload.py` 를 실행한다. 실행하지 않으면 `test_payload_is_in_sync_with_sources` 가 실패한다.
4. `python -m unittest discover -s tests -v` 가 전부 통과하는지 본다. 블록이나 `jump/` 를 고쳤으면 tcsh, dash, perl 이 있는 Linux 나 macOS 에서 실행한다.
5. 화면을 고쳤으면 `python tests/gui_smoke.py` 를 실행한다.
6. Windows 실행 파일은 `build_windows.bat` 으로 만든다.
