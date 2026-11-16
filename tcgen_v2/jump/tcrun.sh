#!/bin/sh
# tcrun 2.6 - tcgen sequence runner. Runs on the jump VM (POSIX sh). tcgen block A installs it in the jump VM work folder
# (setting of tcgen, ~/.tcgen when empty). tcrun uses the folder it is in as its work folder (D below).
#
#   sh <work folder>/tcrun check <plan>       read-only checks: ESX key login, tools, VMs, settings and power, VM login, clocks
#   sh <work folder>/tcrun start <plan>       start "run" in the background for every ESX host of the plan (block S uses this)
#   sh <work folder>/tcrun run <plan> <esx>   run the cases of one ESX host in the order of the table
#   sh <work folder>/tcrun status [-a]        state of every ESX host of the latest run and of every run that is still running
#   sh <work folder>/tcrun stop [esx]         ask the running runs to stop (flag file). Running jobs are not touched.
#   sh <work folder>/tcrun cancel [esx]       end the latest run now, take its crontab lines out, stop its monitoring
#   sh <work folder>/tcrun key                print the line that registers the ssh key of this jump VM on an ESX host
#   sh <work folder>/tcrun version
#
# The plan is read with grep, sed and cut only. No text of the plan is given to eval or to a shell on this machine.
# Commands of the plan go byte for byte into crontab lines (quoted here-documents on the remote side).
# VM login: same way as <work folder>/vmssh (password from <work folder>/vmpw through SSH_ASKPASS, setsid).
# ESX login: <work folder>/esxssh (key <work folder>/id_esx, no password).
#
# One case: wait for the preparation time (cases with a start time: PREP_LEAD_MIN before it), settings, VM power
# (VMs marked off are shut down, the others are powered on one after another: power on, 1GB page backing complete,
# ssh login), crontab lines, end of the job, monitoring stop. A job starts SETTLE_MIN or more after the last VM that
# this case powered on was ready. Every job start on this jump VM is SPACING_MIN or more away from every other one.

TCRUN_VERSION=2.6
case "$0" in        # the work folder is the folder of this file
  */tcrun) D=$(cd "${0%/tcrun}" 2>/dev/null && pwd) ;;
  tcrun) D=$(pwd) ;;
  *) D= ;;
esac
[ -n "$D" ] && [ -f "$D/tcrun" ] || D=$HOME/.tcgen
MD=$D/minutes        # job start times claimed by the runners on this jump VM (see claim_start)
LC_ALL=C
export LC_ALL
umask 077

say() { printf '%s\n' "$*"; }
now() { date +%s; }
fmt() { date -d "@$1" "+$2" 2>/dev/null || date -r "$1" "+$2"; }
fmtu() { date -u -d "@$1" "+$2" 2>/dev/null || date -u -r "$1" "+$2"; }
stamp() { date '+%Y-%m-%d %H:%M:%S'; }
tstr() { fmt "$1" '%Y-%m-%d %H:%M:%S'; }
sha() { (sha256sum "$1" 2>/dev/null || shasum -a 256 "$1" 2>/dev/null) | cut -c1-64; }
bre() { printf '%s' "$1" | sed 's/[].[*^$\\]/\\&/g'; }

# ---------------------------------------------------------------- plan
hget() { grep "^$1 " "$PLAN" | head -n 1 | cut -d' ' -f2-; }
sget() {
  SG=$(grep "^SET $1 " "$PLAN" | head -n 1 | cut -d' ' -f3)
  case "$SG" in ""|*[!0-9]*) SG=$2 ;; esac
  say "$SG"
}
cseqs() { grep "^CASE [0-9]* $(bre "$1") " "$PLAN" | cut -d' ' -f2; }
cname() { grep "^CASE $1 " "$PLAN" | head -n 1 | cut -d' ' -f4; }
clines() { grep "^$1 $SEQ " "$PLAN" | cut -d' ' -f3-; }
vlines() { grep "^$1 $SEQ $(bre "$2") " "$PLAN" | cut -d' ' -f4-; }
has() { grep -q "^$1 $SEQ " "$PLAN"; }
vhas() { grep -q "^$1 $SEQ $(bre "$2") " "$PLAN"; }
target() { grep "^VM $SEQ $(bre "$1") " "$PLAN" | head -n 1 | cut -d' ' -f4; }
chains() { grep '^CHAIN ' "$PLAN" | cut -d' ' -f2; }
chfile() { if [ "$1" = "-" ]; then say noesx; else say "$1"; fi; }
esxaddr() {
  EA=$(grep "^ADDR $(bre "$1") " "$PLAN" | head -n 1 | cut -d' ' -f3)
  say "${EA:-$1}"
}

names_ok() {        # the names of case $SEQ are safe to put on a command line (the generator already checks them)
  grep "^CASE $SEQ " "$PLAN" | grep -q -v '^CASE [0-9]* [A-Za-z0-9._-][A-Za-z0-9._-]* [A-Za-z0-9._-][A-Za-z0-9._-]*$' && return 1
  grep "^VM $SEQ " "$PLAN" | cut -d' ' -f3- | grep -q -v '^[A-Za-z0-9._][A-Za-z0-9._-]* [A-Za-z0-9._][A-Za-z0-9._@-]*$' && return 1
  grep -E "^(AV|PAT|MS|JOB|PROC|MX|OFF) $SEQ " "$PLAN" | cut -d' ' -f3 | grep -q -v '^[A-Za-z0-9._][A-Za-z0-9._-]*$' && return 1
  for NO_V in $(grep "^OFF $SEQ " "$PLAN" | cut -d' ' -f3); do
    grep -q "^VM $SEQ $(bre "$NO_V") " "$PLAN" || return 1
  done
  grep "^PROC $SEQ " "$PLAN" | grep -q "'" && return 1
  grep "^START $SEQ " "$PLAN" | grep -q -v '^START [0-9]* [0-9][0-9]* [0-9][0-9: -]*$' && return 1
  return 0
}
addr_ok() {
  case "$1" in ""|-*|*[!A-Za-z0-9._:-]*) return 1 ;; esac
  return 0
}

load_plan() {
  PLAN=$1
  [ -n "$PLAN" ] && [ -f "$PLAN" ] || { say "tcrun: plan file not found: $PLAN"; exit 2; }
  [ "$(head -n 1 "$PLAN")" = "TCGEN-PLAN 2" ] || { say "tcrun: not a tcgen plan of format 2: $PLAN"; exit 2; }
  PVER=$(hget VER)
  RUNID=$(hget RUNID)
  case "$RUNID" in ""|*[!A-Za-z0-9._-]*) say "tcrun: bad RUNID in the plan"; exit 2 ;; esac
  LOGDIR=$(hget LOGDIR)
  case "$LOGDIR" in
    "~/"*) LOGDIR="$HOME/${LOGDIR#??}" ;;
    "~"|"") LOGDIR="$HOME/tcgen_log" ;;
  esac
  LEAD=$(sget MON_LEAD_MIN 5)
  POLL=$(sget POLL_SEC 60)
  GONE=$(sget GONE_COUNT 3)
  START_WAIT=$(sget START_WAIT_MIN 10)
  PON_WAIT=$(sget POWERON_WAIT_MIN 90)
  BOOT_WAIT=$(sget BOOT_WAIT_MIN 30)
  SHUT_WAIT=$(sget SHUTDOWN_WAIT_MIN 10)
  SETTLE=$(sget SETTLE_MIN 10)
  GAP=$(sget GAP_MIN 0)
  FIRST=$(sget FIRST_CHECK_SEC 60)
  SPACING=$(sget SPACING_MIN 5)
  PREP=$(sget PREP_LEAD_MIN 60)
  [ "$FIRST" -ge 1 ] || FIRST=60
  [ "$SPACING" -ge 1 ] || SPACING=1
  [ "$POLL" -ge 1 ] || POLL=60
  [ "$GONE" -ge 1 ] || GONE=3
  [ "$PON_WAIT" -ge 1 ] || PON_WAIT=90
  RD=$D/runs/$RUNID
}

# ---------------------------------------------------------------- job start times (SPACING_MIN apart on this jump VM)
# Every job start is SPACING_MIN min or more away from every other job start on this jump VM: the runners of all ESX
# hosts, and of other runs, claim the start time of a job before they register it.
# A claim is the folder $MD/<epoch of the minute> with the file "who" (run, ESX host, case) and, once the crontab
# lines of the job are registered, the file "reg". The folder $MD/.lock lets one runner at a time compare and claim.
# Claims and the lock are deleted file by file (rm_claim, unlock_md), never with a recursive delete.
rm_claim() {        # rm_claim <epoch>: delete one claim (its files "who" and "reg", then the empty folder)
  case "$1" in ""|*[!0-9]*) return 0 ;; esac
  rm -f "$MD/$1/who" "$MD/$1/reg"
  rmdir "$MD/$1" 2>/dev/null
  return 0
}
unlock_md() {
  rm -f "$MD/.lock/pid"
  rmdir "$MD/.lock" 2>/dev/null
  return 0
}
lock_md() {         # 0 = this process holds $MD/.lock. A lock whose process ended is removed. 1 = not taken within 40 s
  mkdir -p "$MD" 2>/dev/null
  LM_I=0
  until mkdir "$MD/.lock" 2>/dev/null; do
    LM_I=$((LM_I + 1))
    [ "$LM_I" -gt 40 ] && return 1
    LM_P=$(cat "$MD/.lock/pid" 2>/dev/null)
    [ "$LM_P" = "$$" ] && return 0                      # this runner holds it already (a signal came while it compared)
    case "$LM_P" in
      ""|*[!0-9]*) [ "$LM_I" -ge 30 ] && unlock_md ;;      # no pid for 30 s: its process ended before it wrote one
      *) kill -0 "$LM_P" 2>/dev/null || unlock_md ;;
    esac
    sleep 1
  done
  say $$ > "$MD/.lock/pid"
  return 0
}
claim_dead() {      # claim_dead <claim folder>: 0 = the runner that made the claim ended before it registered the job
  [ -f "$1/reg" ] && return 1
  CD_W=$(cat "$1/who" 2>/dev/null)
  CD_RUN=$(say "$CD_W" | sed -n 's/^run \([A-Za-z0-9._-]*\), esx \([A-Za-z0-9._-]*\), case .*$/\1/p')
  CD_ESX=$(say "$CD_W" | sed -n 's/^run \([A-Za-z0-9._-]*\), esx \([A-Za-z0-9._-]*\), case .*$/\2/p')
  [ -n "$CD_RUN" ] && [ -n "$CD_ESX" ] || return 1
  [ -f "$D/runs/$CD_RUN/$CD_ESX.state" ] || return 1     # no state yet: block S claimed it and is starting the runner
  alive "$D/runs/$CD_RUN/$CD_ESX.pid" && return 1
  return 0
}
claim_start() {     # claim_start <epoch> <who>: 0 = the job start <epoch> is claimed for <who> (or was already).
                    # 1 = another job starts less than SPACING min away: CL_NEAR = the latest such start, CL_WHO = its owner.
                    # 2 = the lock could not be taken
  CL_NEAR=; CL_WHO=
  [ "$(cat "$MD/$1/who" 2>/dev/null)" = "$2" ] && return 0
  lock_md || return 2
  CL_N=$(now)
  for CL_D in "$MD"/[0-9]*; do
    [ -d "$CL_D" ] || continue
    CL_T=$(basename "$CL_D")
    case "$CL_T" in *[!0-9]*) continue ;; esac
    CL_W=$(cat "$CL_D/who" 2>/dev/null)
    [ "$CL_W" = "$2" ] && continue                      # another claim of the same case (the start time of the plan)
    CL_X=$((CL_T - $1))
    [ "$CL_X" -lt 0 ] && CL_X=$((0 - CL_X))
    [ "$CL_X" -lt $((SPACING * 60)) ] || continue
    if [ "$CL_T" -gt "$CL_N" ] && claim_dead "$CL_D"; then rm_claim "$CL_T"; continue; fi
    if [ -z "$CL_NEAR" ] || [ "$CL_T" -gt "$CL_NEAR" ]; then CL_NEAR=$CL_T; CL_WHO=$CL_W; fi
  done
  if [ -z "$CL_NEAR" ]; then
    mkdir "$MD/$1" 2>/dev/null
    say "$2" > "$MD/$1/who"
  fi
  unlock_md
  [ -z "$CL_NEAR" ] && return 0
  return 1
}
release_start() {   # release_start <epoch> <who>: deletes the claim only when <who> holds it
  [ "$(cat "$MD/$1/who" 2>/dev/null)" = "$2" ] && rm_claim "$1"
  return 0
}
release_mine() {    # release_mine <ESX host>: the run ends. Gives back the claims of this run and ESX host whose job
                    # was never registered (start times of cases that did not run). Registered jobs keep their claim.
  [ -d "$MD" ] || return 0
  RM_L=; lock_md && RM_L=1
  for RM_D in "$MD"/[0-9]*; do
    [ -d "$RM_D" ] || continue
    [ -f "$RM_D/reg" ] && continue
    case "$(cat "$RM_D/who" 2>/dev/null)" in "run $RUNID, esx $1, "*) rm_claim "$(basename "$RM_D")" ;; esac
  done
  [ -n "$RM_L" ] && unlock_md
  return 0
}
old_claims() {      # forget claims of job starts that passed more than a day ago
  [ -d "$MD" ] || return 0
  OM_N=$(now); OM_L=; lock_md && OM_L=1
  for OM_D in "$MD"/[0-9]*; do
    OM_T=$(basename "$OM_D")
    case "$OM_T" in ""|*[!0-9]*) continue ;; esac
    [ "$OM_T" -lt $((OM_N - 86400)) ] && rm_claim "$OM_T"
  done
  [ -n "$OM_L" ] && unlock_md
  return 0
}
claim_plan_starts() {   # claim_plan_starts <ESX host of the plan>: claims the start times written in the plan (START lines)
  CP_F=$(chfile "$1")
  for SEQ in $(cseqs "$1"); do
    CP_AT=$(clines START | head -n 1 | cut -d' ' -f1)
    case "$CP_AT" in ""|*[!0-9]*) continue ;; esac
    claim_start $((CP_AT / 60 * 60)) "run $RUNID, esx $CP_F, case $(cname "$SEQ")"
    case $? in
      0) ;;
      1) say "WARN the start time $(clines START | head -n 1 | cut -d' ' -f2-) of case $(cname "$SEQ") is less than $SPACING min from the job start $(tstr "$CL_NEAR") of $CL_WHO. The job of case $(cname "$SEQ") will start $SPACING min after that job or later." ;;
      *) say "WARN the start time of case $(cname "$SEQ") could not be claimed: $MD/.lock could not be taken" ;;
    esac
  done
}

# ---------------------------------------------------------------- log and state
log() { printf '%s %s\n' "$(stamp)" "$*" >> "$LOG"; }
logf() {            # logf <fixed prefix> <file>: copy a file into the log, one log line per line
  [ -s "$2" ] && sed "s/^/$(stamp) $1/" "$2" >> "$LOG"
  return 0
}
setstate() {
  {
    say "state=$1"
    say "time=$(stamp)"
    say "case=$CNAME"
    say "pos=$IDX/$TOTAL"
    say "step=$STEP"
    say "text=$2"
  } > "$ST.new"
  mv -f "$ST.new" "$ST"
}
alive() {           # alive <pid file>: the runner that wrote it is still running
  [ -f "$1" ] || return 1
  AL_P=$(cat "$1" 2>/dev/null)
  case "$AL_P" in ""|*[!0-9]*) return 1 ;; esac
  kill -0 "$AL_P" 2>/dev/null || return 1
  AL_A=$(ps -p "$AL_P" -o args= 2>/dev/null)
  [ -z "$AL_A" ] && return 0
  case "$AL_A" in *tcrun*) return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------- ssh
ssh_setup() {
  SO="-x -o ConnectTimeout=10 -o NumberOfPasswordPrompts=1 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
  # LogLevel=ERROR: no login banner and no 'Permanently added' notes in the outputs, only errors. known_hosts in the work folder
  SO="$SO -o LogLevel=ERROR -o UserKnownHostsFile=$D/known_hosts"
  if ssh -G -o StrictHostKeyChecking=accept-new localhost >/dev/null 2>&1; then
    SO="$SO -o StrictHostKeyChecking=accept-new"
  else
    SO="$SO -o StrictHostKeyChecking=no"
  fi
  SSH_ASKPASS="$D/askpass"; DISPLAY="${DISPLAY:-tcgen:0}"; SSH_ASKPASS_REQUIRE=force
  export SSH_ASKPASS DISPLAY SSH_ASKPASS_REQUIRE
  if setsid -w true >/dev/null 2>&1; then SETSID="setsid -w"; else SETSID=setsid; fi
  APPLY_SHA=$(sha "$D/tcgen_apply.py")
}
guard() {           # guard <pid> <seconds>: wait for the pid; end it when it runs longer. 124 = ended by the time limit
  G_P=$1
  rm -f "$W/timedout"
  (
    G_I=0
    while [ "$G_I" -lt "$2" ]; do
      sleep 1
      kill -0 "$G_P" 2>/dev/null || exit 0
      G_I=$((G_I + 1))
    done
    : > "$W/timedout"
    kill "$G_P" 2>/dev/null
    sleep 2
    kill -9 "$G_P" 2>/dev/null
  ) > /dev/null 2>&1 &
  G_W=$!
  wait "$G_P"
  G_R=$?
  kill "$G_W" 2>/dev/null
  wait "$G_W" 2>/dev/null
  [ -f "$W/timedout" ] && G_R=124
  return $G_R
}
vmrun() {           # vmrun <ssh target> <script> <output> [seconds]: run the script with sh on a VM
  $SETSID ssh $SO "$1" sh -s < "$2" > "$3" 2>&1 &
  guard $! "${4:-90}"
}
esxrun() {          # esxrun <script> <output> [seconds]: run the script with sh on the ESX host of this chain
  sh "$D/esxssh" "$EADDR" sh -s < "$1" > "$2" 2>&1 &
  guard $! "${3:-120}"
}
tval() { grep "^$1 " "$2" 2>/dev/null | tail -n 1 | cut -d' ' -f2-; }
first() {           # first <file>: the line that tells why it failed: an error line of ssh when there is one, else the first line
  FR=$(grep -i -E 'denied|refused|timed out|unreachable|no route|resolve|not known|verification failed|too many|reset by|closed by|broken pipe|quota|no space' "$1" 2>/dev/null | head -n 1)
  [ -n "$FR" ] || FR=$(grep -v '^ *$' "$1" 2>/dev/null | head -n 1)
  say "$FR" | cut -c1-200
}
can_write() {       # can_write <folder>: 0 = 1 MB can be written there now (free disk space and disk quota)
  mkdir -p "$1" 2>/dev/null
  CW_F=$1/.tcgen_write_test.$$
  CW_N=0
  if dd if=/dev/zero of="$CW_F" bs=1024 count=1024 2>/dev/null; then CW_N=$(wc -c < "$CW_F" 2>/dev/null | tr -d ' '); fi
  rm -f "$CW_F"
  [ "$CW_N" = 1048576 ]
}
free_mb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print int($4 / 1024) }'; }

# ---------------------------------------------------------------- remote scripts
mk_probe() {
  cat > "$1" << 'EOF'
echo "TCGEN-CLOCK `date +%s`"
if command -v pgrep >/dev/null 2>&1; then echo "TCGEN-PGREP yes"; else echo "TCGEN-PGREP no"; fi
if command -v crontab >/dev/null 2>&1; then echo "TCGEN-CRONTAB yes"; else echo "TCGEN-CRONTAB no"; fi
echo "TCGEN-NAME `hostname 2>/dev/null`"
echo "TCGEN-LINES `crontab -l 2>/dev/null | grep -c ' # tcgen '`"
EOF
}
mk_esxprobe() {
  cat > "$1" << 'EOF'
echo "TCGEN-CLOCK `date +%s`"
echo "TCGEN-NAME `hostname 2>/dev/null`"
for X in python3 vim-cmd esxcli; do
  if command -v $X >/dev/null 2>&1; then echo "TCGEN-TOOL $X yes"; else echo "TCGEN-TOOL $X no"; fi
done
CD=${TCGEN_CRON_DIR:-/var/spool/cron/crontabs}
if [ -f "$CD/root" ]; then echo "TCGEN-CRONFILE yes"; else echo "TCGEN-CRONFILE no"; fi
EOF
}
mk_vmcron() {       # mk_vmcron <vm> <out> [TAG:epoch ...]: replace the lines of this case on the VM by the lines of the tags
  MC_VM=$1; MC_OUT=$2
  shift 2
  {
    say 'N=$HOME/.tcgen_cron.new; P=$HOME/.tcgen_cron.pat'
    say "cat > \$P << 'TCGEN_EOF_PAT'"
    vlines PAT "$MC_VM"
    say 'TCGEN_EOF_PAT'
    say 'crontab -l 2>/dev/null | grep -v -f $P > $N'
    for MC_A in "$@"; do
      MC_TAG=${MC_A%%:*}; MC_T=${MC_A#*:}
      vhas "$MC_TAG" "$MC_VM" || continue
      say "F=\`date -d @$MC_T '+%M %H %d %m' 2>/dev/null\` || F=\`date -r $MC_T '+%M %H %d %m'\`"
      say 'case "$F" in [0-9][0-9]" "[0-9][0-9]" "[0-9][0-9]" "[0-9][0-9]) ;; *) echo "TCGEN-CRON failed (date gave: $F)"; rm -f $N $P; exit 1 ;; esac'
      say "while IFS= read -r L; do printf '%s * %s\\n' \"\$F\" \"\$L\"; done >> \$N << 'TCGEN_EOF_LINES'"
      vlines "$MC_TAG" "$MC_VM"
      say 'TCGEN_EOF_LINES'
    done
    say 'if crontab $N; then rm -f $N $P; echo "TCGEN-CRON ok"; else echo "TCGEN-CRON failed (crontab did not take the file, kept $N)"; fi'
    say 'echo "TCGEN-CLOCK `date +%s`"'
    say "crontab -l 2>/dev/null | grep ' # tcgen ' | sed 's/^/TCGEN-LINE /'"
  } > "$MC_OUT"
}
mk_esxcron() {      # mk_esxcron <out> [TAG:epoch ...]: same for the root crontab of the ESX host (Broadcom KB 323138)
  ME_OUT=$1
  shift
  {
    say 'T=${TCGEN_TMP:-/tmp/tcgen}; CD=${TCGEN_CRON_DIR:-/var/spool/cron/crontabs}'
    say 'mkdir -p "$T"; CT=$CD/root; N=$T/cron.new; P=$T/cron.pat'
    say '[ -f "$CT" ] || { echo "TCGEN-CRON failed (no $CT)"; exit 1; }'
    say 'cp "$CT" "$T/root.crontab.bak"'
    say "cat > \$P << 'TCGEN_EOF_PAT'"
    clines EPAT
    say 'TCGEN_EOF_PAT'
    say 'grep -v -f $P $CT > $N'
    for ME_A in "$@"; do
      ME_TAG=${ME_A%%:*}; ME_T=${ME_A#*:}
      has "$ME_TAG" || continue
      ME_F=$(fmtu "$ME_T" '%M %H %d %m')
      say "while IFS= read -r L; do printf '%s * %s\\n' '$ME_F' \"\$L\"; done >> \$N << 'TCGEN_EOF_LINES'"
      clines "$ME_TAG"
      say 'TCGEN_EOF_LINES'
    done
    say 'chmod u+w $CT'
    say 'if cat $N > $CT && /bin/echo root >> $CD/cron.update; then echo "TCGEN-CRON ok"; else echo "TCGEN-CRON failed (cannot write $CT)"; fi'
    say 'rm -f $N $P'
    say 'echo "TCGEN-CLOCK `date +%s`"'
    say "grep ' # tcgen ' \$CT | sed 's/^/TCGEN-LINE /'"
  } > "$ME_OUT"
}
mk_poll() {         # mk_poll <vm> <out>: count the processes whose command line matches the pattern of the plan
  {
    say 'if command -v pgrep >/dev/null 2>&1; then'
    printf '%s' "  O=\`pgrep -f -- '"
    vlines PROC "$1" | head -n 1 | tr -d '\n'
    say "'\`; R=\$?"
    say "  C=\`printf '%s\\n' \"\$O\" | grep -c .\`"
    say '  if [ $R -eq 0 ]; then echo "TCGEN-PROC $C"; elif [ $R -eq 1 ]; then echo "TCGEN-PROC 0"; else echo "TCGEN-PROC error $R"; fi'
    say 'else'
    say '  echo "TCGEN-PROC nopgrep"'
    say 'fi'
  } > "$2"
}
mk_spec() {         # settings of this case for tcgen_apply.py
  {
    say "opt shutdown_wait $((SHUT_WAIT * 60))"
    for MS_VM in $VMS; do say "vm $MS_VM"; done
    for MS_VM in $OFFVMS; do say "off $MS_VM"; done
    clines AH | sed 's/^/host /'
    clines AV | sed 's/^/set /'
  } > "$W/spec"
}
mk_apply() {        # mk_apply <out> <tcgen_apply.py arguments>: upload tcgen_apply.py to the ESX host and run it
  MA_OUT=$1
  shift
  {
    say 'T=${TCGEN_TMP:-/tmp/tcgen}'
    say 'mkdir -p "$T" || { echo "TCGEN-RESULT error cannot create $T"; exit 1; }'
    say "cat > \"\$T/tcgen_apply.py.new\" << 'TCGEN_EOF_APPLY_PY'"
    cat "$D/tcgen_apply.py"
    say 'TCGEN_EOF_APPLY_PY'
    say 'S=`sha256sum "$T/tcgen_apply.py.new" 2>/dev/null | cut -c1-64`'
    say "E='$APPLY_SHA'"
    say 'if [ -n "$S" ] && [ -n "$E" ] && [ "$S" != "$E" ]; then echo "TCGEN-RESULT error tcgen_apply.py arrived damaged on the ESX host (sha256)"; exit 1; fi'
    say 'mv -f "$T/tcgen_apply.py.new" "$T/tcgen_apply.py"'
    say 'PY=python3; command -v python3 >/dev/null 2>&1 || PY=python'
    case "$1" in
      check|prepare)
        say "\$PY \"\$T/tcgen_apply.py\" $* << 'TCGEN_EOF_SPEC'"
        cat "$W/spec"
        say 'TCGEN_EOF_SPEC'
        ;;
      *)
        say "\$PY \"\$T/tcgen_apply.py\" $* < /dev/null"
        ;;
    esac
  } > "$MA_OUT"
}

# ---------------------------------------------------------------- waiting
nap() {             # nap <seconds>: 1 = the stop flag appeared
  NP_L=$1
  while [ "$NP_L" -gt 0 ]; do
    [ -f "$STOPF" ] && return 1
    if [ "$NP_L" -gt 5 ]; then sleep 5; NP_L=$((NP_L - 5)); else sleep "$NP_L"; NP_L=0; fi
  done
  [ -f "$STOPF" ] && return 1
  return 0
}
wait_until() {      # wait_until <epoch of this machine> [nostop]: 1 = the stop flag appeared
  while :; do
    WU_N=$(now)
    [ "$WU_N" -ge "$1" ] && return 0
    [ -z "$2" ] && [ -f "$STOPF" ] && return 1
    WU_L=$(($1 - WU_N))
    [ "$WU_L" -gt 5 ] && WU_L=5
    sleep "$WU_L"
  done
}

# ---------------------------------------------------------------- results
write_result() {
  [ -n "$R_DONE" ] && return 0
  R_DONE=1
  WR_S=$(printf '%s' "$R_STATUS" | tr '"' "'")
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"%s"\n' "$CNAME" "$CHF" "$R_APPLY" "$R_BEGIN" "$R_VMSON" "$R_MON" "$R_JOB" \
    "$R_SEEN" "$R_END" "$R_STOP" "$(stamp)" "$WR_S" >> "$RES"
}
write_report() {    # case file of the performance report generator: UTF-8 with BOM, real job start and end
  if [ ! -s "$RPT" ]; then
    printf '\357\273\277' > "$RPT"
    printf '%s\r\n' "$(hget RPTH)" >> "$RPT"
  fi
  WR_END=$R_END
  [ "$POLL" -gt 120 ] && WR_END=          # the end is known to within POLL s only: the report generator detects it
  printf '%s,%s,%s,%s\r\n' "$(clines RPTA)" "$R_JOB" "$WR_END" "$(clines RPTB)" >> "$RPT"
}
# pause and stopped end the run. cmd_run writes the state file as its last step, after the last log lines,
# so that whoever sees the state "paused", "stopped" or "done" finds the complete log.
pause() { log "PAUSED: $1"; R_STATUS="paused: $1"; write_result; HOW=paused; HOW_TEXT=$1; }
stopped() { log "STOPPED on request: $1"; R_STATUS="stopped: $1"; write_result; HOW=stopped; HOW_TEXT=$1; }
hint_stop() {
  has EMX || grep -q "^MX $SEQ " "$PLAN" || return 0
  log "monitoring stop commands of this case (crontab form: \\% stands for %). Run them by hand when the monitoring must end:"
  grep "^MX $SEQ " "$PLAN" | cut -d' ' -f3- | sed "s/^/$(stamp)     | vm /" >> "$LOG"
  clines EMX | sed "s/^/$(stamp)     | esx $CHF /" >> "$LOG"
}

# ---------------------------------------------------------------- step 1: settings and VM power
lpage_wait() {      # lpage_wait <vm>: wait until the 1GB page backing of the VM is complete: the 1GB page backings reach
                    # memSize / 1024, no increase in 6 readings = stopped. VMs without 1GB pages: no wait
  setstate running "vm $1: waiting for the 1GB page backing"
  mk_apply "$W/lpw.sh" lpagewait "$1" $((PON_WAIT * 60))
  esxrun "$W/lpw.sh" "$W/lpw.out" $((PON_WAIT * 60 + 300))
  LW_R=$(tval TCGEN-RESULT "$W/lpw.out")
  LW_N=$(say "$LW_R" | cut -d' ' -f3); LW_T=$(say "$LW_R" | cut -d' ' -f4); LW_F=$(say "$LW_R" | cut -d' ' -f5)
  LW_S=error
  [ "$(say "$LW_R" | cut -d' ' -f1)" = lpage ] && LW_S=$(say "$LW_R" | cut -d' ' -f2)
  case "$LW_S" in
    complete)
      grep '^TCGEN-LPAGE [0-9]' "$W/lpw.out" | sed "s/^TCGEN-LPAGE /$(stamp)     | 1GB page backings, target, failed attempts: /" >> "$LOG"
      log "vm $1: 1GB page backing complete ($LW_N of $LW_T 1GB pages, failed attempts $LW_F)"
      return 0 ;;
    off)
      log "vm $1: 1GB pages are not set for this VM (sched.mem.lpage.enable1GPage). No backing to wait for."
      return 0 ;;
    nonode)
      log "WARN vm $1: the 1GB page backing could not be read (no vsish node /memory/lpage/vmLpage for the VM). Not waited for."
      return 0 ;;
  esac
  logf '    | ' "$W/lpw.out"
  LW_FAIL=1
  case "$LW_S" in
    failed) WHY="vm $1: the 1GB page backing stopped at $LW_N of $LW_T 1GB pages with $LW_F failed attempts. The ESX host has no free contiguous 1GB memory. Power the VM off, free memory on the host (power off other VMs or reboot the host) and run again" ;;
    stalled) WHY="vm $1: the 1GB page backing does not increase at $LW_N of $LW_T 1GB pages (no failed attempts). Check the memory reservation of the VM (sched.mem.prealloc, sched.mem.min), power it off and run again" ;;
    timeout) WHY="vm $1: the 1GB page backing was not complete within $PON_WAIT min ($LW_N of $LW_T 1GB pages)" ;;
    poweredoff) LW_FAIL=; WHY="vm $1 was powered off while its 1GB page backing was read" ;;
    *) LW_FAIL=; WHY="vm $1: the 1GB page backing could not be read: ${LW_R:-no answer from the ESX host ($(first "$W/lpw.out"))}" ;;
  esac
  return 1
}
power_on() {        # power_on <vm>: power on through the ESX host when off, wait for the 1GB page backing, then for ssh login.
                    # PO_NEW=1: this call powered the VM on, PO_READY = when it was ready. LW_FAIL=1: the backing failed
  PO_NEW=0; LW_FAIL=
  STEP=poweron; setstate running "power on $1"
  PO_S=$(now)
  mk_apply "$W/pon.sh" poweron "$1" $((PON_WAIT * 60))
  esxrun "$W/pon.sh" "$W/pon.out" $((PON_WAIT * 60 + 300))
  PO_X=$?
  PO_R=$(tval TCGEN-RESULT "$W/pon.out")
  case "$PO_R" in
    poweredon*) PO_NEW=1; logf '    | ' "$W/pon.out" ;;
    alreadyon*) log "vm $1: powered on already" ;;
    *)
      logf '    | ' "$W/pon.out"
      WHY="power on of vm $1 failed: ${PO_R:-no answer from the ESX host (ssh exit $PO_X: $(first "$W/pon.out"))}"
      return 1 ;;
  esac
  lpage_wait "$1" || return 1
  PO_T=$(target "$1"); PO_E=$(($(now) + BOOT_WAIT * 60))
  say 'echo TCGEN-UP' > "$W/up.sh"
  setstate running "vm $1 is powered on. waiting for ssh login"
  while :; do
    vmrun "$PO_T" "$W/up.sh" "$W/up.out" 40
    grep -q '^TCGEN-UP' "$W/up.out" && break
    if grep -q -i -E 'permission denied|too many authentication failures|host key verification failed' "$W/up.out"; then
      WHY="vm $1: ssh login was refused ($(first "$W/up.out")). Not tried again, so that the account is not locked. Check the VM password (sh $D/setpw) and the VM account"
      return 1
    fi
    if [ "$(now)" -ge "$PO_E" ]; then
      if [ "$PO_NEW" = 1 ]; then
        WHY="vm $1 did not accept ssh login within $BOOT_WAIT min after power on ($(first "$W/up.out"))"
      else
        WHY="vm $1 is powered on, but ssh login did not work for $BOOT_WAIT min ($(first "$W/up.out"))"
      fi
      return 1
    fi
    sleep 10
  done
  if [ "$PO_NEW" = 1 ]; then
    PO_READY=$(now)
    say "$PO_READY" > "$W/on.$1"
    log "vm $1: ssh login works, ready $((PO_READY - PO_S)) s after the power on started (power on, 1GB page backing, ssh login)"
  else
    log "vm $1: ssh login works"
  fi
  return 0
}
apply_undo() {      # put the settings back, then power on (in table order) the VMs that were on before
  STEP=rollback; setstate running "putting the settings back on $CHF"
  mk_apply "$W/rb.sh" rollback "$TAG"
  esxrun "$W/rb.sh" "$W/rb.out" $((SHUT_WAIT * 60 + 900))
  logf '    | ' "$W/rb.out"
  log "rollback: $(tval TCGEN-RESULT "$W/rb.out")"
  for AU_VM in $VMS; do
    grep -q "^TCGEN-WASON $(bre "$AU_VM")\$" "$W/rb.out" || continue
    power_on "$AU_VM" || log "WARN $WHY"
  done
  R_APPLY=rolledback
}
apply_settings() {  # compare the settings of this case and change those that differ (the VMs of the case are shut down)
  STEP=apply; setstate running "comparing and applying settings on $CHF"
  TAG=$RUNID-$SEQ
  mk_spec
  log "settings wanted by this case:"
  logf '    | ' "$W/spec"
  mk_apply "$W/apply.sh" prepare "$TAG"
  esxrun "$W/apply.sh" "$W/apply.out" $((SHUT_WAIT * 60 + 900))
  AP_R=$?
  logf '    | ' "$W/apply.out"
  case "$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f1)" in
    nochange)
      R_APPLY=nochange
      log "settings: every value is already as wanted. The VMs are not shut down for the settings."
      return 0 ;;
    changed)
      R_APPLY=changed
      log "settings: applied. The VMs of this case are powered off now."
      return 0 ;;
  esac
  AP_T=$(tval TCGEN-RESULT "$W/apply.out")
  [ -n "$AP_T" ] || AP_T="no answer from the ESX host (ssh exit $AP_R: $(first "$W/apply.out"))"
  log "settings: not applied: $AP_T"
  apply_undo
  pause "settings of case $CNAME were not applied: $AP_T"
  return 1
}
power_fail() {      # power_fail <reason>: a VM could not be powered as wanted. Settings that this case changed are put back,
                    # except when the 1GB page backing failed: then the settings and the VMs stay as they are, to look at
  PF_T=$1; PF_L=$LW_FAIL
  log "$PF_T"
  if [ "$R_APPLY" = changed ] && [ "$PF_L" != 1 ]; then
    apply_undo
    pause "$PF_T. The settings of case $CNAME were put back."
  elif [ "$R_APPLY" = changed ]; then
    pause "$PF_T. The settings of case $CNAME stay applied."
  else
    pause "$PF_T."
  fi
}
power_vms() {       # VM power as the case wants it: VMs marked off are shut down first, then the other VMs are powered on
                    # one after another in table order (each one: power on, 1GB page backing complete, ssh login)
  PV_NEW=
  for PV_VM in $OFFVMS; do
    STEP=poweroff; setstate running "powering off vm $PV_VM (the case wants it off)"
    mk_apply "$W/poff.sh" poweroff "$PV_VM" $((SHUT_WAIT * 60))
    esxrun "$W/poff.sh" "$W/poff.out" $((SHUT_WAIT * 60 + 300))
    PV_X=$?
    PV_R=$(tval TCGEN-RESULT "$W/poff.out")
    case "$PV_R" in
      alreadyoff*) log "vm $PV_VM: powered off already (the case wants it off)" ;;
      poweredoff*) logf '    | ' "$W/poff.out"; log "vm $PV_VM: shut down (the case wants it off)" ;;
      *)
        logf '    | ' "$W/poff.out"
        LW_FAIL=
        power_fail "power off of vm $PV_VM failed: ${PV_R:-no answer from the ESX host (ssh exit $PV_X: $(first "$W/poff.out"))}"
        return 1 ;;
    esac
  done
  [ -n "$ONVMS" ] || return 0
  log "VM power: the VMs of this case are powered on one after another when off (power on, 1GB page backing, ssh login):$ONVMS"
  for PV_VM in $ONVMS; do
    if ! power_on "$PV_VM"; then
      power_fail "$WHY"
      return 1
    fi
    [ "$PO_NEW" = 1 ] && PV_NEW="$PV_NEW $PV_VM"
  done
  if [ -n "$PV_NEW" ]; then
    log "VM power: done. Powered on by this case:$PV_NEW. The last one was ready at $(tstr "$PO_READY")"
  else
    log "VM power: done. Every VM was on already"
  fi
  return 0
}

# ---------------------------------------------------------------- step 2 and 3: clocks and crontab lines
last_on() {         # T_ON = when the VMs of this case were last powered on again by this run (empty: never)
  T_ON=; R_VMSON=
  for LO_VM in $VMS; do
    LO_T=$(cat "$W/on.$LO_VM" 2>/dev/null)
    case "$LO_T" in ""|*[!0-9]*) continue ;; esac
    [ -z "$T_ON" ] || [ "$LO_T" -gt "$T_ON" ] && T_ON=$LO_T
  done
  [ -n "$T_ON" ] && R_VMSON=$(tstr "$T_ON")
  return 0
}
probe_clocks() {    # OFF = how far the latest clock is ahead of this machine (>= 0), MINOFF = the smallest lead
  PC_MAX=; PC_MIN=; PC_VMAX=; PC_VMIN=
  mk_probe "$W/probe.sh"
  for PC_VM in $REGVMS; do
    PC_J=$(now)
    vmrun "$(target "$PC_VM")" "$W/probe.sh" "$W/probe.out" 60
    PC_R=$?
    PC_C=$(tval TCGEN-CLOCK "$W/probe.out")
    case "$PC_C" in
      ""|*[!0-9]*) WHY="vm $PC_VM: ssh login failed (exit $PC_R: $(first "$W/probe.out"))"; return 1 ;;
    esac
    if [ "$(tval TCGEN-CRONTAB "$W/probe.out")" != yes ]; then WHY="vm $PC_VM: crontab command not found"; return 1; fi
    if vhas PROC "$PC_VM" && [ "$(tval TCGEN-PGREP "$W/probe.out")" != yes ]; then
      WHY="vm $PC_VM: pgrep not found, the end of the job cannot be detected"
      return 1
    fi
    PC_O=$((PC_C - PC_J))
    [ -z "$PC_MAX" ] || [ "$PC_O" -gt "$PC_MAX" ] && PC_MAX=$PC_O
    [ -z "$PC_MIN" ] || [ "$PC_O" -lt "$PC_MIN" ] && PC_MIN=$PC_O
    [ -z "$PC_VMAX" ] || [ "$PC_O" -gt "$PC_VMAX" ] && PC_VMAX=$PC_O
    [ -z "$PC_VMIN" ] || [ "$PC_O" -lt "$PC_VMIN" ] && PC_VMIN=$PC_O
    log "clock: vm $PC_VM is $PC_O s ahead of the jump VM (negative: behind)"
  done
  if has EMS || has EMX; then
    say 'echo "TCGEN-CLOCK `date +%s`"' > "$W/eclock.sh"
    PC_J=$(now)
    esxrun "$W/eclock.sh" "$W/eclock.out" 60
    PC_R=$?
    PC_C=$(tval TCGEN-CLOCK "$W/eclock.out")
    case "$PC_C" in
      ""|*[!0-9]*) WHY="esx $CHF: ssh key login failed (exit $PC_R: $(first "$W/eclock.out"))"; return 1 ;;
    esac
    PC_O=$((PC_C - PC_J))
    [ -z "$PC_MAX" ] || [ "$PC_O" -gt "$PC_MAX" ] && PC_MAX=$PC_O
    [ -z "$PC_MIN" ] || [ "$PC_O" -lt "$PC_MIN" ] && PC_MIN=$PC_O
    log "clock: esx $CHF is $PC_O s ahead of the jump VM (negative: behind)"
  fi
  OFF=${PC_MAX:-0}; MINOFF=${PC_MIN:-0}
  [ "$OFF" -lt 0 ] && OFF=0
  if [ -n "$PC_VMAX" ] && [ $((PC_VMAX - PC_VMIN)) -gt 30 ]; then
    log "WARN the clocks of the VMs differ by $((PC_VMAX - PC_VMIN)) s. The jobs start that far apart. Check time sync."
  fi
  return 0
}
unregister() {      # remove the lines of this case from every VM and from the ESX host
  for UR_VM in $REGVMS; do
    mk_vmcron "$UR_VM" "$W/unreg.sh"
    vmrun "$(target "$UR_VM")" "$W/unreg.sh" "$W/unreg.out" 60
    if [ "$(tval TCGEN-CRON "$W/unreg.out")" = ok ]; then
      log "removed the lines of this case: vm $UR_VM"
    else
      log "WARN the lines of this case could not be removed on vm $UR_VM ($(first "$W/unreg.out"))"
    fi
  done
  if has EPAT; then
    mk_esxcron "$W/eunreg.sh"
    esxrun "$W/eunreg.sh" "$W/eunreg.out" 60
    if [ "$(tval TCGEN-CRON "$W/eunreg.out")" = ok ]; then
      log "removed the lines of this case: esx $CHF"
    else
      log "WARN the lines of this case could not be removed on esx $CHF ($(first "$W/eunreg.out"))"
    fi
  fi
}
ahead() {           # seconds from now to the earliest start time: 75, and 4 more for every host beyond four
  AH_N=$(say "$REGVMS" | grep -c .)
  has EPAT && AH_N=$((AH_N + 1))
  if [ "$AH_N" -gt 4 ]; then say $((75 + 4 * (AH_N - 4))); else say 75; fi
}
register() {
  STEP=register
  RG_TRY=0
  RG_AHEAD=$(ahead)
  while :; do
    RG_TRY=$((RG_TRY + 1))
    setstate running "reading clocks and registering crontab lines (try $RG_TRY)"
    if ! probe_clocks; then
      [ "$RG_TRY" -gt 1 ] && unregister
      pause "$WHY. Nothing of case $CNAME was started."
      return 1
    fi
    RG_T=$(( ($(now) + OFF + RG_AHEAD + 59) / 60 * 60 ))
    RG_L=0
    [ "$HASMON" = 1 ] && RG_L=$((LEAD * 60))
    if [ -n "$T_ON" ] && [ "$SETTLE" -gt 0 ]; then
      # VMs of this case were powered on again by this run: the job must not start before T_ON + SETTLE min
      RG_S=$(( (T_ON + OFF + SETTLE * 60 - RG_L + 59) / 60 * 60 ))
      if [ "$RG_S" -gt "$RG_T" ]; then
        log "settle: VMs of this case were powered on again (finished $R_VMSON). The job must start $SETTLE min or more after that: first start time moved from $(tstr "$RG_T") to $(tstr "$RG_S")"
        RG_T=$RG_S
      elif [ "$RG_TRY" = 1 ]; then
        log "settle: VMs of this case were powered on again (finished $R_VMSON). The job start is already $SETTLE min or more after that: no extra wait"
      fi
    fi
    if [ -z "$AT" ] && [ -n "$PREV_END" ] && [ "$GAP" -gt 0 ]; then
      # gap: the job must not start before the end of the previous job of this ESX host + GAP min
      RG_G=$(( (PREV_END + OFF + GAP * 60 - RG_L + 59) / 60 * 60 ))
      if [ "$RG_G" -gt "$RG_T" ]; then
        log "gap: the previous job ended $(tstr "$PREV_END"). The next job starts $GAP min or more after that: first start time moved from $(tstr "$RG_T") to $(tstr "$RG_G")"
        RG_T=$RG_G
      elif [ "$RG_TRY" = 1 ]; then
        log "gap: the previous job ended $(tstr "$PREV_END"). The job start is already $GAP min or more after that: no extra wait"
      fi
    fi
    if [ -n "$AT" ]; then
      # start time of the plan: the job starts at that time. When it cannot be met, the job starts as soon as possible.
      RG_A=$((AT / 60 * 60 - RG_L))
      if [ "$RG_A" -ge "$RG_T" ]; then
        [ "$RG_TRY" = 1 ] && log "start time: the plan sets the job start of this case to $ATTEXT"
        RG_T=$RG_A
      elif [ "$RG_TRY" = 1 ]; then
        log "WARN the start time $ATTEXT of the plan cannot be met any more (earliest possible job start $(tstr $((RG_T + RG_L)))). Starting as soon as possible."
      fi
    fi
    if [ "$HASMON" = 1 ]; then TMON=$RG_T; TJOB=$((RG_T + RG_L)); else TMON=; TJOB=$RG_T; fi
    # job starts on this jump VM are SPACING min or more apart (other ESX hosts and other runs too): move after a nearer one
    while :; do
      claim_start "$TJOB" "$ME"
      RG_C=$?
      [ "$RG_C" = 0 ] && break
      if [ "$RG_C" != 1 ]; then
        pause "the job start times on this jump VM could not be compared ($MD/.lock could not be taken). Nothing of case $CNAME was started."
        return 1
      fi
      RG_M=$((CL_NEAR + SPACING * 60))
      log "spacing: the job of $CL_WHO starts $(tstr "$CL_NEAR"), less than $SPACING min from $(tstr "$TJOB"). This job starts at $(tstr "$RG_M")"
      [ -n "$TMON" ] && TMON=$((TMON + RG_M - TJOB))
      TJOB=$RG_M
    done
    if [ -n "$AT" ] && [ $((AT / 60 * 60)) != "$TJOB" ]; then
      release_start $((AT / 60 * 60)) "$ME"          # the start time of the plan is not used: give it back
    fi
    WHY=
    for RG_VM in $REGVMS; do
      mk_vmcron "$RG_VM" "$W/reg.sh" ${TMON:+MS:$TMON} "JOB:$TJOB"
      vmrun "$(target "$RG_VM")" "$W/reg.sh" "$W/reg.out" 60
      RG_R=$?
      if [ "$(tval TCGEN-CRON "$W/reg.out")" != ok ]; then
        WHY="vm $RG_VM: crontab lines were not registered (ssh exit $RG_R: $(first "$W/reg.out"))"
        break
      fi
      log "registered on vm $RG_VM:"
      grep '^TCGEN-LINE ' "$W/reg.out" | cut -d' ' -f2- | sed "s/^/$(stamp)     | /" >> "$LOG"
    done
    if [ -z "$WHY" ] && has EMS; then
      mk_esxcron "$W/ereg.sh" "EMS:$TMON"
      esxrun "$W/ereg.sh" "$W/ereg.out" 60
      RG_R=$?
      if [ "$(tval TCGEN-CRON "$W/ereg.out")" != ok ]; then
        WHY="esx $CHF: crontab lines were not registered (ssh exit $RG_R: $(first "$W/ereg.out"))"
      else
        log "registered on esx $CHF (UTC):"
        grep '^TCGEN-LINE ' "$W/ereg.out" | cut -d' ' -f2- | sed "s/^/$(stamp)     | /" >> "$LOG"
      fi
    fi
    if [ -n "$WHY" ]; then
      unregister
      release_start "$TJOB" "$ME"
      pause "$WHY. The lines of case $CNAME were removed, nothing was started."
      return 1
    fi
    RG_N=$(($(now) + OFF)); RG_F=${TMON:-$TJOB}
    [ "$RG_N" -le $((RG_F - 15)) ] && break
    # too late: cron may not run a line that arrives less than 15 s before its minute. Take every line out again,
    # so that the case never starts on only some of its hosts.
    log "WARN registering ended $((RG_F - RG_N)) s before the first start time $(tstr "$RG_F"). Removing the lines of this case."
    unregister
    release_start "$TJOB" "$ME"
    if [ $(($(now) + OFF)) -ge $((RG_F - 2)) ]; then
      hint_stop
      pause "registering the lines of case $CNAME took too long: the first start time $(tstr "$RG_F") passed before the lines were removed. Monitoring or jobs of this case may have started on some hosts. Check them."
      return 1
    fi
    if [ "$RG_TRY" -ge 3 ]; then
      pause "registering the lines of case $CNAME was too slow 3 times. The lines were removed, nothing was started."
      return 1
    fi
    RG_AHEAD=$((RG_AHEAD * 2))
    log "registering again with a start time $RG_AHEAD s or more ahead"
  done
  : > "$MD/$TJOB/reg" 2>/dev/null                 # the job is registered: its claim stays when the run ends
  say "$SEQ ${TMON:-$TJOB} $TJOB" > "$W/times"     # for cancel: this case has lines on the hosts
  [ -n "$TMON" ] && R_MON=$(tstr "$TMON")
  R_JOB=$(tstr "$TJOB")
  if [ -n "$TMON" ]; then
    log "times: monitoring start $R_MON, job start $R_JOB ($LEAD min later, every VM in the same minute, no other job on this jump VM starts within $SPACING min of it)"
  else
    log "times: job start $R_JOB (every VM in the same minute, no monitoring, no other job on this jump VM starts within $SPACING min of it)"
  fi
  return 0
}

# ---------------------------------------------------------------- step 4: end of the job
poll_all() {
  ALLSEEN=1; ALLGONE=1; ALLZERO=1; PSUM=; NOTSEEN=
  for PA_VM in $JOBVMS; do
    mk_poll "$PA_VM" "$W/poll.sh"
    vmrun "$(target "$PA_VM")" "$W/poll.sh" "$W/poll.out" 60
    PA_R=$?
    PA_N=$(tval TCGEN-PROC "$W/poll.out" | tr -d ' ')
    case "$PA_N" in ""|*[!0-9]*) PA_N=unknown ;; esac
    PA_S=$(cat "$W/seen.$PA_VM" 2>/dev/null)
    PA_Z=$(cat "$W/zero.$PA_VM" 2>/dev/null)
    : "${PA_Z:=0}"
    if [ "$PA_N" = unknown ]; then
      PA_Z=0
      PA_T="unknown (ssh exit $PA_R: $(first "$W/poll.out")). Not counted as ended."
    elif [ "$PA_N" -gt 0 ]; then
      PA_T="$PA_N process(es) running"
      if [ -z "$PA_S" ]; then
        PA_S=$(now)
        say "$PA_S" > "$W/seen.$PA_VM"
        PA_T="$PA_T (seen for the first time)"
      fi
      PA_Z=0
    elif [ -n "$PA_S" ]; then
      PA_Z=$((PA_Z + 1))
      [ "$PA_Z" = 1 ] && now > "$W/gone.$PA_VM"
      PA_T="no process ($PA_Z of $GONE in a row)"
    else
      PA_T="no process yet (not seen since the job start)"
    fi
    # one line for every login to a VM: the time of the line is when the check of that VM ended (clock of the jump VM)
    log "check $WJ_K (case $CNAME) vm $PA_VM: $PA_T"
    say "$PA_Z" > "$W/zero.$PA_VM"
    if [ -z "$PA_S" ]; then ALLSEEN=0; NOTSEEN="$NOTSEEN $PA_VM"; fi
    [ "$PA_Z" -ge "$GONE" ] || ALLGONE=0
    [ "$PA_N" = 0 ] && [ -n "$PA_S" ] || ALLZERO=0
    PSUM="$PSUM $PA_VM=$PA_N"
  done
}
wait_job() {
  STEP=wait-start; setstate running "lines registered. job start $R_JOB"
  # While a job runs, the VMs are logged in to as rarely as possible (a login is load on the VM under test):
  #   once FIRST s after the job start (is the process there?), then every POLL s.
  #   Not seen on every VM at the first check: once more at the end of the start wait.
  #   No process on any VM any more: checked again after a short time, until GONE checks in a row agree.
  #   No job is running then, so these checks put no load on a measurement.
  WJ_FIRST=$((TJOB - MINOFF + FIRST))
  if ! wait_until "$WJ_FIRST"; then
    WJ_N=$(($(now) + OFF))
    if [ "$WJ_N" -lt $((TJOB - 5)) ]; then
      log "stop requested before the job start: removing the lines of this case"
      unregister
      : > "$W/times"
      release_start "$TJOB" "$ME"
      if [ "$HASMON" = 1 ] && [ "$WJ_N" -ge $((TMON - 30)) ]; then
        log "the monitoring of this case has started or is about to start: stopping it"
        stop_mon_core
      fi
      stopped "before the job of case $CNAME started. Its lines were removed."
    else
      hint_stop
      stopped "just after the job of case $CNAME started. The job and the monitoring keep running."
    fi
    return 1
  fi
  STEP=wait-job; setstate running "checking the job processes (check 1)"
  WJ_CONF=60
  [ "$POLL" -lt "$WJ_CONF" ] && WJ_CONF=$POLL
  WJ_NS=; WJ_K=0
  WJ_SW=$((TJOB - MINOFF + START_WAIT * 60))
  WJ_MAX=$(clines MAXWAIT | head -n 1)
  case "$WJ_MAX" in ""|*[!0-9]*) WJ_MAX=0 ;; esac
  [ "$WJ_MAX" -gt 0 ] && WJ_MAX=$((TJOB - MINOFF + WJ_MAX * 60))
  while :; do
    WJ_K=$((WJ_K + 1))
    poll_all
    WJ_N=$(now)
    if [ "$ALLSEEN" = 1 ] && [ -z "$R_SEEN" ]; then
      R_SEEN=$(stamp)
      log "the process is seen on every VM with a job"
    fi
    [ "$ALLSEEN" = 1 ] && [ "$ALLGONE" = 1 ] && break
    if [ "$ALLSEEN" = 0 ] && [ "$WJ_N" -ge "$WJ_SW" ]; then
      hint_stop
      pause "the process was never seen within $START_WAIT min after the job start on:$NOTSEEN. Check the process name and the job. Nothing was stopped."
      return 1
    fi
    if [ "$WJ_MAX" -gt 0 ] && [ "$WJ_N" -ge "$WJ_MAX" ]; then
      hint_stop
      pause "the job of case $CNAME did not end within the maximum wait ($(clines MAXWAIT | head -n 1) min). The job and the monitoring keep running."
      return 1
    fi
    if [ "$ALLSEEN" = 0 ]; then
      WJ_I=$((WJ_SW - WJ_N))
      [ "$WJ_I" -gt "$POLL" ] && WJ_I=$POLL
      if [ -z "$WJ_NS" ]; then
        WJ_NS=1
        log "the process is not seen yet on:$NOTSEEN. It must be seen within $START_WAIT min after the job start."
      fi
    elif [ "$ALLZERO" = 1 ]; then
      WJ_I=$WJ_CONF
    else
      WJ_I=$POLL
    fi
    if [ "$WJ_MAX" -gt 0 ] && [ $((WJ_N + WJ_I)) -gt "$WJ_MAX" ]; then WJ_I=$((WJ_MAX - WJ_N)); fi
    [ "$WJ_I" -lt 1 ] && WJ_I=1
    log "check $WJ_K (case $CNAME) done. next check $(tstr $((WJ_N + WJ_I)))"
    setstate running "waiting for the job to end. processes:$PSUM. next check $(tstr $((WJ_N + WJ_I)))"
    if ! wait_until $((WJ_N + WJ_I)); then          # waits for that time on the clock, so that the checks do not drift
      hint_stop
      stopped "while the job of case $CNAME was running. The job and the monitoring keep running."
      return 1
    fi
  done
  WJ_E=0
  for WJ_VM in $JOBVMS; do
    WJ_G=$(cat "$W/gone.$WJ_VM" 2>/dev/null)
    [ -n "$WJ_G" ] && [ "$WJ_G" -gt "$WJ_E" ] && WJ_E=$WJ_G
    log "vm $WJ_VM: process first seen $(tstr "$(cat "$W/seen.$WJ_VM")"), gone since $(tstr "${WJ_G:-0}")"
  done
  R_END=$(tstr "$WJ_E")
  log "job ended: $R_END (no process $GONE times in a row on every VM with a job)"
  if [ "$POLL" -gt 120 ]; then
    log "the end time is the first check without the process. The job ended within the $POLL s before it (check interval). The report case file leaves the job end empty."
  fi
  return 0
}

# ---------------------------------------------------------------- step 5: stop the monitoring, remove the lines
stop_mon_core() {   # sets SM_BAD to the hosts where the stop command could not be registered
  SM_BAD=
  if grep -q "^MX $SEQ " "$PLAN" || has EMX; then
    STEP=stop-monitoring; setstate running "registering the monitoring stop"
    SM_TRY=0
    SM_AHEAD=$(ahead)
    while :; do
      SM_TRY=$((SM_TRY + 1)); SM_BAD=
      TSTOP=$(( ($(now) + OFF + SM_AHEAD + 59) / 60 * 60 ))
      for SM_VM in $REGVMS; do
        vhas MX "$SM_VM" || continue
        mk_vmcron "$SM_VM" "$W/stop.sh" "MX:$TSTOP"
        vmrun "$(target "$SM_VM")" "$W/stop.sh" "$W/stop.out" 60
        if [ "$(tval TCGEN-CRON "$W/stop.out")" = ok ]; then
          log "monitoring stop registered on vm $SM_VM:"
          grep '^TCGEN-LINE ' "$W/stop.out" | cut -d' ' -f2- | sed "s/^/$(stamp)     | /" >> "$LOG"
        else
          SM_BAD="$SM_BAD $SM_VM"
          log "WARN monitoring stop was not registered on vm $SM_VM ($(first "$W/stop.out"))"
        fi
      done
      if has EMX; then
        mk_esxcron "$W/estop.sh" "EMX:$TSTOP"
        esxrun "$W/estop.sh" "$W/estop.out" 60
        if [ "$(tval TCGEN-CRON "$W/estop.out")" = ok ]; then
          log "monitoring stop registered on esx $CHF (UTC):"
          grep '^TCGEN-LINE ' "$W/estop.out" | cut -d' ' -f2- | sed "s/^/$(stamp)     | /" >> "$LOG"
        else
          SM_BAD="$SM_BAD $CHF"
          log "WARN monitoring stop was not registered on esx $CHF ($(first "$W/estop.out"))"
        fi
      fi
      SM_N=$(($(now) + OFF))
      [ "$SM_N" -le $((TSTOP - 15)) ] && break
      if [ "$SM_TRY" -ge 3 ]; then
        SM_BAD="$SM_BAD (registered too late 3 times)"
        break
      fi
      SM_AHEAD=$((SM_AHEAD * 2))
      log "WARN the monitoring stop was registered only $((TSTOP - SM_N)) s before its time $(tstr "$TSTOP"). Registering it again $SM_AHEAD s or more ahead."
    done
    R_STOP=$(tstr "$TSTOP")
    setstate running "monitoring stop at $R_STOP"
    wait_until $((TSTOP - MINOFF + 20)) nostop
    log "monitoring stop time $R_STOP has passed"
  fi
  STEP=remove-lines; setstate running "removing the lines of the case"
  unregister
  : > "$W/times"
}
stop_mon() {
  stop_mon_core
  if [ -n "$SM_BAD" ]; then
    hint_stop
    pause "the monitoring stop of case $CNAME could not be registered on:$SM_BAD. Stop it by hand."
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- one case, one chain
run_case() {
  STEP=begin
  R_DONE=; R_APPLY=none; R_BEGIN=$(stamp); R_VMSON=; R_MON=; R_JOB=; R_SEEN=; R_END=; R_STOP=; R_STATUS=
  ME="run $RUNID, esx $CHF, case $CNAME"          # owner of the job start time of this case (claim_start)
  TMON=; TJOB=; T_ON=; OFF=0; MINOFF=0; WHY=
  rm -f "$W"/seen.* "$W"/zero.* "$W"/gone.*
  if ! names_ok; then
    VMS=; OFFVMS=; ONVMS=; REGVMS=; JOBVMS=
    pause "the plan holds a name that tcrun does not accept in case number $SEQ. Generate again in tcgen. Nothing was done."
    return 1
  fi
  VMS=$(grep "^VM $SEQ " "$PLAN" | cut -d' ' -f3)
  OFFVMS=$(grep "^OFF $SEQ " "$PLAN" | cut -d' ' -f3)
  ONVMS=
  for RC_VM in $VMS; do
    grep -q "^OFF $SEQ $(bre "$RC_VM")\$" "$PLAN" || ONVMS="$ONVMS $RC_VM"
  done
  REGVMS=$(grep "^PAT $SEQ " "$PLAN" | cut -d' ' -f3)
  JOBVMS=$(grep "^PROC $SEQ " "$PLAN" | cut -d' ' -f3)
  HASMON=0
  if grep -q "^MS $SEQ " "$PLAN" || has EMS; then HASMON=1; fi
  AT=$(clines START | head -n 1 | cut -d' ' -f1)
  case "$AT" in ""|*[!0-9]*) AT= ;; esac
  ATTEXT=$(clines START | head -n 1 | cut -d' ' -f2-)
  log "==== case $CNAME ($IDX of $TOTAL) begin. VMs: $(say $VMS)${OFFVMS:+ (powered off for this case: $(say $OFFVMS))}"
  setstate running "case started"
  if [ -n "$AT" ] && [ "$PREP" -gt 0 ] && [ "$(now)" -lt $((AT - PREP * 60)) ]; then
    STEP=wait-prepare
    log "start time $ATTEXT: the preparation (settings, VM power) starts $PREP min before it, at $(tstr $((AT - PREP * 60)))"
    setstate running "waiting until $(tstr $((AT - PREP * 60))) to prepare the case (job start $ATTEXT)"
    if ! wait_until $((AT - PREP * 60)); then
      stopped "before case $CNAME was prepared"
      return 1
    fi
  fi
  if has AH || has AV; then
    apply_settings || return 1
  fi
  if [ "$CH" != "-" ] && [ -n "$VMS" ]; then
    power_vms || return 1
  fi
  last_on
  if [ -f "$STOPF" ]; then
    stopped "before the lines of case $CNAME were registered"
    return 1
  fi
  if [ -z "$JOBVMS" ]; then
    R_STATUS="done (settings only)"; write_result
    log "==== case $CNAME done (settings only, no job)"
    return 0
  fi
  register || return 1
  wait_job || return 1
  PREV_END=$WJ_E
  stop_mon || return 1
  R_STATUS=done; write_result; write_report
  log "==== case $CNAME done. job $R_JOB to $R_END"
  return 0
}

cmd_run() {
  load_plan "$1"
  CH=$2
  [ -n "$CH" ] || { say "usage: tcrun run <plan> <esx>"; exit 2; }
  case "$CH" in -?*|*[!A-Za-z0-9._-]*) say "tcrun: bad ESX host name: $CH"; exit 2 ;; esac
  if [ "$PVER" != "$TCRUN_VERSION" ]; then
    say "tcrun $TCRUN_VERSION cannot run a plan made by tcgen $PVER. Paste block A of that tcgen again."
    exit 3
  fi
  CHF=$(chfile "$CH")
  mkdir -p "$RD" "$LOGDIR" || exit 2
  W=$RD/$CHF.work
  rm -rf "$W"
  mkdir -p "$W" || exit 2
  ST=$RD/$CHF.state; STOPF=$RD/$CHF.stop; PIDF=$RD/$CHF.pid
  LOG=$LOGDIR/tcrun-$RUNID-$CHF.log
  RES=$LOGDIR/tcrun-$RUNID-$CHF-result.csv
  RPT=$LOGDIR/tcrun-$RUNID-$CHF-perfreport_cases.csv
  SEQS=$(cseqs "$CH")
  TOTAL=$(say "$SEQS" | grep -c .)
  [ "$TOTAL" -gt 0 ] || { say "tcrun: the plan has no case for $CH"; exit 2; }
  : >> "$LOG" || { say "tcrun: cannot write the log: $LOG"; exit 2; }
  for CR_X in "$RD" "$LOGDIR"; do
    can_write "$CR_X" || { say "tcrun: 1 MB cannot be written in $CR_X (disk full or disk quota, $(free_mb "$CR_X") MB free on the disk)"; exit 2; }
  done
  say $$ > "$PIDF"
  CNAME=; IDX=0; STEP=start; HOW=; HOW_TEXT=; PREV_END=
  trap 'rm -f "$PIDF"' 0
  trap 'log "ended by a signal"; release_mine "$CHF"; setstate stopped "ended by a signal"; exit 1' 2 15
  trap '' 1
  EADDR=$(esxaddr "$CH")
  if [ "$CH" != "-" ] && ! addr_ok "$EADDR"; then say "tcrun: bad ESX address in the plan for $CH"; exit 2; fi
  ssh_setup
  [ -s "$RES" ] || say "case,esx,apply,begin,vms_on,monitor_start,job_start,first_seen,job_end,monitor_stop,end,status" > "$RES"
  log "tcrun $TCRUN_VERSION run $RUNID, esx $CHF ($EADDR), $TOTAL case(s), pid $$"
  old_claims
  claim_plan_starts "$CH" | while IFS= read -r CR_LINE; do log "$CR_LINE"; done
  log "settings: job starts $LEAD min after the monitoring start, first check $FIRST s after the job start and then every $POLL s, ended after $GONE checks without the process, start wait $START_WAIT min, power on wait $PON_WAIT min, boot wait $BOOT_WAIT min, shutdown wait $SHUT_WAIT min, job $SETTLE min or more after the last VM that the case powered on was ready, $GAP min or more between the end of a job and the start of the next job (cases without a start time), job starts $SPACING min or more apart on this jump VM, preparation $PREP min before a start time of the plan"
  setstate running "started"
  for SEQ in $SEQS; do
    IDX=$((IDX + 1))
    CNAME=$(cname "$SEQ")
    if [ -f "$STOPF" ]; then
      R_DONE=1
      stopped "before case $CNAME"
      break
    fi
    run_case || break
  done
  release_mine "$CHF"
  if [ -z "$HOW" ]; then
    STEP=end
    log "run finished: all $TOTAL case(s) done"
    setstate done "all $TOTAL case(s) finished"
    return 0
  fi
  CR_I=0; CR_REST=
  for CR_S in $SEQS; do
    CR_I=$((CR_I + 1))
    [ "$CR_I" -ge "$IDX" ] && CR_REST="$CR_REST $(cname "$CR_S")"
  done
  log "run ended ($HOW) at case $CNAME. Not finished:$CR_REST"
  log "to go on: fix the cause, tick the finished cases as applied in tcgen, generate again and paste block S"
  setstate "$HOW" "$HOW_TEXT"
  return 1
}

# ---------------------------------------------------------------- start, status, stop, key
running_for() {     # print the RUNID of a run that is still running for this ESX host
  for RF in "$D"/runs/*/"$1".pid; do
    [ -f "$RF" ] || continue
    if alive "$RF"; then
      basename "$(dirname "$RF")"
      return 0
    fi
  done
  return 1
}
cmd_start() {
  load_plan "$1"
  if [ "$PVER" != "$TCRUN_VERSION" ]; then
    say "==== tcrun $TCRUN_VERSION, plan made by tcgen $PVER: paste block A again. Nothing started."
    exit 3
  fi
  for CS_X in askpass esxssh tcgen_apply.py; do
    [ -f "$D/$CS_X" ] || { say "==== missing $D/$CS_X: paste block A again. Nothing started."; exit 2; }
  done
  mkdir -p "$RD" "$LOGDIR" 2>/dev/null
  if [ ! -d "$LOGDIR" ] || [ ! -w "$LOGDIR" ]; then
    say "==== the log folder cannot be written: $LOGDIR. Nothing started."
    exit 2
  fi
  for CS_X in "$RD" "$LOGDIR"; do
    if ! can_write "$CS_X"; then
      say "==== 1 MB cannot be written in $CS_X (disk full or disk quota of the account, $(free_mb "$CS_X") MB free on the disk). Nothing started."
      say "     Set the jump VM work folder or log folder in tcgen to a folder with free space, paste block A (work folder) and generate again."
      exit 2
    fi
  done
  CS_N=0; CS_L=
  old_claims
  for CS_CH in $(chains); do
    CS_F=$(chfile "$CS_CH")
    if [ -f "$RD/$CS_F.state" ] || [ -f "$RD/$CS_F.pid" ]; then
      say "==== $CS_F: run $RUNID was already started on this jump VM. Not started again. Generate again in tcgen to run again."
      continue
    fi
    CS_O=$(running_for "$CS_F")
    if [ -n "$CS_O" ]; then
      say "==== $CS_F: run $CS_O is still running for this ESX host. Not started. (sh $D/tcrun status)"
      continue
    fi
    claim_plan_starts "$CS_CH" | sed "s/^/==== $CS_F: /"
    setsid sh "$D/tcrun" run "$PLAN" "$CS_CH" > "$RD/$CS_F.out" 2>&1 < /dev/null &
    CS_N=$((CS_N + 1)); CS_L="$CS_L $CS_F:$!"
  done
  # wait until every runner has written its state file (at most 30 s. a runner that ended at once is not waited for)
  CS_I=0
  while [ "$CS_N" -gt 0 ] && [ "$CS_I" -lt 30 ]; do
    CS_W=0
    for CS_X in $CS_L; do
      CS_F=${CS_X%%:*}
      [ -f "$RD/$CS_F.state" ] && continue
      if [ "$CS_I" -ge 3 ] && [ ! -f "$RD/$CS_F.pid" ] && ! kill -0 "${CS_X#*:}" 2>/dev/null; then continue; fi
      CS_W=1
    done
    [ "$CS_W" = 0 ] && break
    sleep 1
    CS_I=$((CS_I + 1))
  done
  for CS_X in $CS_L; do
    CS_F=${CS_X%%:*}
    if [ -f "$RD/$CS_F.state" ]; then
      say "started: run $RUNID esx $CS_F   log: $LOGDIR/tcrun-$RUNID-$CS_F.log"
    else
      say "==== $CS_F: the runner did not start:"
      sed 's/^/     /' "$RD/$CS_F.out" 2>/dev/null
      release_mine "$CS_F"
    fi
  done
  say "status: sh $D/tcrun status       stop: sh $D/tcrun stop       cancel: sh $D/tcrun cancel"
  say "files in $LOGDIR : tcrun-$RUNID-<esx>.log, -result.csv, -perfreport_cases.csv"
}
cmd_status() {
  CT_ALL=0
  [ "$1" = "-a" ] && CT_ALL=1
  [ -d "$D/runs" ] || { say "no run on this jump VM yet"; return 0; }
  latest_run
  CT_LAST=$LR
  CT_N=0
  for CT_D in $(ls "$D/runs" 2>/dev/null | sort); do
    for CT_F in "$D/runs/$CT_D"/*.state; do
      [ -f "$CT_F" ] || continue
      CT_X=$(basename "$CT_F" .state)
      CT_S=$(grep '^state=' "$CT_F" | cut -d= -f2-)
      CT_LIVE=0
      alive "$D/runs/$CT_D/$CT_X.pid" && CT_LIVE=1
      if [ "$CT_S" = running ] && [ "$CT_LIVE" = 0 ]; then
        CT_S="died (the runner is not running any more. see $D/runs/$CT_D/$CT_X.out)"
      fi
      [ "$CT_ALL" = 1 ] || [ "$CT_D" = "$CT_LAST" ] || [ "$CT_LIVE" = 1 ] || continue
      CT_N=$((CT_N + 1))
      say "== run $CT_D  esx $CT_X : $CT_S"
      grep -v '^state=' "$CT_F" | sed 's/^/   /'
      if [ -f "$D/runs/$CT_D/plan" ]; then
        PLAN=$D/runs/$CT_D/plan
        CT_L=$(hget LOGDIR)
        case "$CT_L" in
          "~/"*) CT_L="$HOME/${CT_L#??}" ;;
          "~"|"") CT_L="$HOME/tcgen_log" ;;
        esac
        say "   log=$CT_L/tcrun-$CT_D-$CT_X.log"
        tail -n 3 "$CT_L/tcrun-$CT_D-$CT_X.log" 2>/dev/null | sed 's/^/   > /'
      fi
    done
  done
  if [ -n "$CT_LAST" ] && [ -f "$D/runs/$CT_LAST/plan" ]; then
    PLAN=$D/runs/$CT_LAST/plan
    for CT_C in $(chains); do                  # ESX hosts of the latest run whose runner wrote no state
      CT_X=$(chfile "$CT_C")
      [ -f "$D/runs/$CT_LAST/$CT_X.state" ] && continue
      CT_N=$((CT_N + 1))
      say "== run $CT_LAST  esx $CT_X : not started (no state file. A full disk or an error at the start: see below)"
      tail -n 5 "$D/runs/$CT_LAST/$CT_X.out" 2>/dev/null | sed 's/^/   > /'
    done
  fi
  [ "$CT_N" -gt 0 ] || say "no run on this jump VM yet"
  [ "$CT_N" -gt 0 ] && say "stop: sh $D/tcrun stop [esx]   cancel: sh $D/tcrun cancel [esx]"
  return 0
}
cmd_stop() {
  CP_N=0
  for CP_F in "$D"/runs/*/*.pid; do
    [ -f "$CP_F" ] || continue
    alive "$CP_F" || continue
    CP_X=$(basename "$CP_F" .pid)
    [ -z "$1" ] || [ "$1" = "$CP_X" ] || continue
    CP_D=$(dirname "$CP_F")
    : > "$CP_D/$CP_X.stop"
    say "stop requested: run $(basename "$CP_D") esx $CP_X. The runner ends at its next check. Running jobs are not touched."
    CP_N=$((CP_N + 1))
  done
  [ "$CP_N" -gt 0 ] || say "no running tcrun${1:+ for $1}"
  latest_run
  if [ -n "$LR" ] && [ -f "$D/runs/$LR/plan" ]; then
    PLAN=$D/runs/$LR/plan
    for CP_C in $(chains); do
      CP_X=$(chfile "$CP_C")
      [ -z "$1" ] || [ "$1" = "$CP_X" ] || continue
      alive "$D/runs/$LR/$CP_X.pid" && continue
      CP_S=$(grep '^state=' "$D/runs/$LR/$CP_X.state" 2>/dev/null | cut -d= -f2-)
      CP_T=$(grep '^text=' "$D/runs/$LR/$CP_X.state" 2>/dev/null | cut -d= -f2-)
      say "run $LR esx $CP_X: not running (${CP_S:-not started}${CP_T:+: $CP_T}). To take its crontab lines out: sh $D/tcrun cancel $CP_X"
    done
  fi
  return 0
}
cmd_key() {
  [ -s "$D/id_esx.pub" ] || { say "no key file $D/id_esx.pub. Paste block A of tcgen first."; return 1; }
  say "Paste this ONE line into the root SSH session of each ESX host (once per host):"
  say ""
  say "echo '$(cat "$D/id_esx.pub")' >> /etc/ssh/keys-root/authorized_keys"
  say ""
  say "Then check from this jump VM. It must print the host name without asking for a password:"
  say "sh $D/esxssh <esx host> hostname"
}

# ---------------------------------------------------------------- cancel
latest_run() {      # LR = the latest run on this jump VM that has a state file (the run that status shows first)
  LR=
  for LR_D in $(ls "$D/runs" 2>/dev/null | sort); do
    for LR_F in "$D/runs/$LR_D"/*.state; do
      [ -f "$LR_F" ] && LR=$LR_D
      break
    done
  done
}
cancel_mon_stop() { # run the monitoring stop commands of case $SEQ now, once (VM lines: \% of the crontab form stands for %)
  for CM_VM in $REGVMS; do
    vhas MX "$CM_VM" || continue
    vlines MX "$CM_VM" | sed 's/\\%/%/g' > "$W/mx.sh"
    vmrun "$(target "$CM_VM")" "$W/mx.sh" "$W/mx.out" 120
    CM_R=$?
    log "cancel: monitoring stop commands run on vm $CM_VM (exit $CM_R)"
    logf '    | ' "$W/mx.out"
    say "   monitoring stop: vm $CM_VM (exit $CM_R)"
  done
  if has EMX; then
    clines EMX > "$W/emx.sh"
    esxrun "$W/emx.sh" "$W/emx.out" 120
    CM_R=$?
    log "cancel: monitoring stop commands run on esx $CHF (exit $CM_R)"
    logf '    | ' "$W/emx.out"
    say "   monitoring stop: esx $CHF (exit $CM_R)"
  fi
}
cancel_chain() {    # cancel run $RUNID for the ESX host $CH
  ST=$RD/$CHF.state; PIDF=$RD/$CHF.pid; STOPF=$RD/$CHF.stop
  LOG=$LOGDIR/tcrun-$RUNID-$CHF.log; RES=$LOGDIR/tcrun-$RUNID-$CHF-result.csv
  EADDR=$(esxaddr "$CH")
  CN_S=$(grep '^state=' "$ST" 2>/dev/null | cut -d= -f2-)
  CNAME=$(grep '^case=' "$ST" 2>/dev/null | cut -d= -f2-)
  STEP=$(grep '^step=' "$ST" 2>/dev/null | cut -d= -f2-)
  IDX=$(grep '^pos=' "$ST" 2>/dev/null | cut -d= -f2- | cut -d/ -f1); TOTAL=$(cseqs "$CH" | grep -c .)
  case "$CN_S" in
    done) say "== esx $CHF: run $RUNID is done. Nothing to cancel."; return 0 ;;
    cancelled) say "== esx $CHF: run $RUNID was cancelled already."; return 0 ;;
  esac
  if [ "$CH" != "-" ] && ! addr_ok "$EADDR"; then say "== esx $CHF: bad ESX address in the plan. Not cancelled."; return 1; fi
  say "== esx $CHF: cancelling run $RUNID (state ${CN_S:-none}, case ${CNAME:-none}, step ${STEP:-none})"
  W=$RD/$CHF.cancel.$$
  mkdir -p "$W" || { say "   cannot create $W"; return 1; }
  if alive "$PIDF"; then
    CN_P=$(cat "$PIDF")
    : > "$STOPF"
    kill -s TERM -- "-$CN_P" 2>/dev/null || kill -s TERM "$CN_P" 2>/dev/null
    CN_I=0
    while alive "$PIDF" && [ "$CN_I" -lt 10 ]; do sleep 1; CN_I=$((CN_I + 1)); done
    if alive "$PIDF"; then kill -s KILL -- "-$CN_P" 2>/dev/null || kill -s KILL "$CN_P" 2>/dev/null; fi
    say "   the runner (pid $CN_P) was ended at step ${STEP:-?}"
    case "$STEP" in
      apply|poweron|poweroff|rollback)
        say "   WARN it was changing settings or VM power on the ESX host. That task goes on to its end on the host: check the VMs" ;;
    esac
  fi
  log "CANCEL requested (sh tcrun cancel). state was ${CN_S:-none}, case ${CNAME:-none}, step ${STEP:-none}"
  # the case whose crontab lines may be on the hosts: <work>/times is written when its lines are registered
  # and removed when they are removed
  SEQ=$(cut -d' ' -f1 "$RD/$CHF.work/times" 2>/dev/null)
  CN_MON=$(cut -d' ' -f2 "$RD/$CHF.work/times" 2>/dev/null)
  case "$SEQ" in ""|*[!0-9]*) SEQ= ;; esac
  case "$CN_MON" in *[!0-9]*) CN_MON= ;; esac
  if [ -n "$SEQ" ] && names_ok; then
    CNAME=$(cname "$SEQ")
    REGVMS=$(grep "^PAT $SEQ " "$PLAN" | cut -d' ' -f3)
    JOBVMS=$(grep "^PROC $SEQ " "$PLAN" | cut -d' ' -f3)
    OFF=0
    unregister
    say "   crontab lines of case $CNAME removed (see the log for each host)"
    if [ -n "$CN_MON" ] && [ "$(now)" -ge $((CN_MON - 60)) ]; then
      cancel_mon_stop
    fi
    for CN_VM in $JOBVMS; do
      mk_poll "$CN_VM" "$W/poll.sh"
      vmrun "$(target "$CN_VM")" "$W/poll.sh" "$W/poll.out" 60
      CN_J=$(tval TCGEN-PROC "$W/poll.out" | tr -d ' ')
      say "   job on vm $CN_VM: ${CN_J:-unknown} process(es) match '$(vlines PROC "$CN_VM" | head -n 1)'. tcrun does not end jobs."
      log "cancel: job on vm $CN_VM: ${CN_J:-unknown} process(es) still match the process name"
    done
    : > "$RD/$CHF.work/times"
  else
    say "   no crontab lines of this run are registered on the hosts"
  fi
  # job start times of this run and ESX host that have not come yet
  CN_N=$(now)
  for CN_D in "$MD"/[0-9]*; do
    [ -d "$CN_D" ] || continue
    CN_T=$(basename "$CN_D")
    case "$CN_T" in *[!0-9]*) continue ;; esac
    [ "$CN_T" -gt "$CN_N" ] || continue
    case "$(cat "$CN_D/who" 2>/dev/null)" in "run $RUNID, esx $CHF, "*) rm_claim "$CN_T" ;; esac
  done
  [ -n "$CNAME" ] && printf '%s,%s,,,,,,,,,%s,"%s"\n' "$CNAME" "$CHF" "$(stamp)" "cancelled" >> "$RES"
  log "CANCELLED. Settings that this run applied stay as they are."
  setstate cancelled "cancelled at $(stamp) by sh tcrun cancel. Settings that the run applied stay as they are"
  say "   cancelled. Settings that this run applied stay as they are."
}
cmd_cancel() {      # cancel [esx]: end the latest run (all its ESX hosts or one) and take its crontab lines out
  latest_run
  [ -n "$LR" ] && [ -f "$D/runs/$LR/plan" ] || { say "no run to cancel on this jump VM"; return 0; }
  load_plan "$D/runs/$LR/plan"
  ssh_setup
  CN_K=0
  for CH in $(chains); do
    CHF=$(chfile "$CH")
    [ -z "$1" ] || [ "$1" = "$CHF" ] || continue
    CN_K=$((CN_K + 1))
    cancel_chain
  done
  [ "$CN_K" -gt 0 ] || say "run $RUNID has no ESX host $1"
}

# ---------------------------------------------------------------- check (reads only)
ck_bad() { BAD=$((BAD + 1)); say "  [PROBLEM] $*"; : > "$W/bad.$BAD"; }
ck_ok() { say "  [ok] $*"; }
ck_vm() {           # login, clock, pgrep and crontab of one VM (once per ssh target)
  CV_T=$(target "$1")
  if grep -q "^$(bre "$CV_T")\$" "$W/done.vm" 2>/dev/null; then return 0; fi
  say "$CV_T" >> "$W/done.vm"
  CV_J=$(now)
  vmrun "$CV_T" "$W/probe.sh" "$W/probe.out" 60
  CV_R=$?
  CV_C=$(tval TCGEN-CLOCK "$W/probe.out")
  case "$CV_C" in
    ""|*[!0-9]*) ck_bad "vm $1 ($CV_T): ssh login failed (exit $CV_R: $(first "$W/probe.out"))"; return 0 ;;
  esac
  ck_ok "vm $1 ($CV_T): ssh login works, clock $((CV_C - CV_J)) s ahead of the jump VM, tcgen lines in crontab: $(tval TCGEN-LINES "$W/probe.out")"
  say "$((CV_C - CV_J))" >> "$W/clocks"
  [ "$(tval TCGEN-CRONTAB "$W/probe.out")" = yes ] || ck_bad "vm $1: crontab command not found"
  if vhas PROC "$1" && [ "$(tval TCGEN-PGREP "$W/probe.out")" != yes ]; then
    ck_bad "vm $1: pgrep not found, the end of the job cannot be detected"
  fi
}
check_body() {
  BAD=0
  say "tcrun $TCRUN_VERSION check, run $RUNID, $(stamp). Nothing is changed by this check."
  [ "$PVER" = "$TCRUN_VERSION" ] || ck_bad "the plan was made by tcgen $PVER, tcrun is $TCRUN_VERSION: paste block A again"
  for CB_X in askpass vmssh setpw esxssh tcgen_apply.py; do
    [ -f "$D/$CB_X" ] || ck_bad "missing $D/$CB_X: paste block A again"
  done
  [ -s "$D/vmpw" ] || ck_bad "the VM password is not saved: sh $D/setpw"
  for CB_X in ssh setsid; do
    command -v $CB_X >/dev/null 2>&1 || ck_bad "command not found on the jump VM: $CB_X"
  done
  for CB_X in "$D" "$LOGDIR"; do
    if can_write "$CB_X"; then
      ck_ok "folder $CB_X: 1 MB can be written, $(free_mb "$CB_X") MB free on the disk"
    else
      ck_bad "folder $CB_X: 1 MB cannot be written (disk full or disk quota of the account, $(free_mb "$CB_X") MB free on the disk). Set the jump VM work folder or log folder in tcgen to a folder with free space"
    fi
  done
  ssh_setup
  mk_probe "$W/probe.sh"
  : > "$W/done.vm"; : > "$W/clocks"
  for CH in $(chains); do
    CHF=$(chfile "$CH"); EADDR=$(esxaddr "$CH")
    say ""
    say "== ESX host $CHF"
    CB_ESX=0
    if [ "$CH" != "-" ]; then
      for SEQ in $(cseqs "$CH"); do
        names_ok || continue                     # a case with names that tcrun does not accept is reported below, no login
        if has AH || has AV || has EMS || has EMX || has VM; then CB_ESX=1; fi
      done
    fi
    CB_UP=0
    if [ "$CB_ESX" = 1 ] && ! addr_ok "$EADDR"; then
      ck_bad "esx $CHF: the plan holds an address that tcrun does not accept"
      CB_ESX=2
    fi
    if [ "$CB_ESX" = 1 ]; then
      mk_esxprobe "$W/eprobe.sh"
      CB_J=$(now)
      esxrun "$W/eprobe.sh" "$W/eprobe.out" 60
      CB_R=$?
      CB_C=$(tval TCGEN-CLOCK "$W/eprobe.out")
      case "$CB_C" in
        ""|*[!0-9]*)
          ck_bad "esx $CHF ($EADDR): ssh key login failed (exit $CB_R: $(first "$W/eprobe.out")). Register the key: sh $D/tcrun key"
          ;;
        *)
          CB_UP=1
          ck_ok "esx $CHF ($EADDR): ssh key login works, host name $(tval TCGEN-NAME "$W/eprobe.out"), clock $((CB_C - CB_J)) s ahead of the jump VM"
          for CB_X in python3 vim-cmd esxcli; do
            if grep -q "^TCGEN-TOOL $CB_X yes" "$W/eprobe.out"; then ck_ok "esx $CHF: $CB_X found"; else ck_bad "esx $CHF: $CB_X not found"; fi
          done
          grep -q '^TCGEN-CRONFILE yes' "$W/eprobe.out" || ck_bad "esx $CHF: no root crontab file"
          ;;
      esac
    elif [ "$CB_ESX" = 0 ] && [ "$CH" = "-" ]; then
      say "  (cases without an ESX host: no ESX login. The power of their VMs is not handled: the VMs must be on)"
    elif [ "$CB_ESX" = 0 ]; then
      say "  (no VMs, no settings and no ESX monitoring in the cases of this host: no ESX login needed)"
    fi
    for SEQ in $(cseqs "$CH"); do
      CNAME=$(cname "$SEQ")
      if ! names_ok; then
        ck_bad "case number $SEQ: the plan holds a name that tcrun does not accept. Generate again in tcgen."
        continue
      fi
      VMS=$(grep "^VM $SEQ " "$PLAN" | cut -d' ' -f3)
      say "-- case $CNAME   VMs: $(say $VMS)"
      CB_AT=$(clines START | head -n 1 | cut -d' ' -f1)
      case "$CB_AT" in ""|*[!0-9]*) CB_AT= ;; esac
      if [ -n "$CB_AT" ]; then
        if [ "$CB_AT" -le "$(now)" ]; then
          ck_bad "case $CNAME: the start time $(clines START | head -n 1 | cut -d' ' -f2-) has already passed. Change it in tcgen and generate again."
        else
          ck_ok "start time: job at $(clines START | head -n 1 | cut -d' ' -f2-) ($(( (CB_AT - $(now)) / 60 )) min from now). Preparation (settings, VM power) from $PREP min before it."
          CB_M=$((CB_AT / 60 * 60))
          for CB_D in "$MD"/[0-9]*; do
            [ -d "$CB_D" ] || continue
            CB_T=$(basename "$CB_D")
            case "$CB_T" in *[!0-9]*) continue ;; esac
            CB_W=$(cat "$CB_D/who" 2>/dev/null)
            case "$CB_W" in "run $RUNID, "*) continue ;; esac
            CB_X=$((CB_T - CB_M))
            [ "$CB_X" -lt 0 ] && CB_X=$((0 - CB_X))
            [ "$CB_X" -lt $((SPACING * 60)) ] || continue
            [ "$CB_T" -gt "$(now)" ] && claim_dead "$CB_D" && continue
            ck_bad "case $CNAME: the start time $(clines START | head -n 1 | cut -d' ' -f2-) is less than $SPACING min from the job start $(tstr "$CB_T") of ${CB_W:-another runner}. Job starts on this jump VM are $SPACING min or more apart: change the start time or leave it empty."
          done
        fi
      fi
      CB_PW=0
      if [ "$CH" != "-" ] && { [ -n "$VMS" ] || has AH || has AV; }; then
        if [ "$CB_UP" = 1 ]; then
          OFFVMS=$(grep "^OFF $SEQ " "$PLAN" | cut -d' ' -f3)
          mk_spec
          mk_apply "$W/apply.sh" check
          esxrun "$W/apply.sh" "$W/apply.out" 300
          sed 's/^/     /' "$W/apply.out"
          case "$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f1)" in
            nochange) ck_ok "settings and VM power: already as wanted. The VMs will not be shut down or powered on." ;;
            wouldchange) CB_PW=1; ck_ok "settings and VM power: $(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f2-). The run changes them before the job (when a value differs, the VMs of the case are shut down first)." ;;
            *) ck_bad "settings and VM power: $(tval TCGEN-RESULT "$W/apply.out")" ;;
          esac
        else
          ck_bad "settings and VM power of case $CNAME not checked (no ESX login)"
        fi
      fi
      for CB_VM in $VMS; do
        if grep -q "^OFF $SEQ $(bre "$CB_VM")\$" "$PLAN"; then
          ck_ok "vm $CB_VM: the case wants it powered off. No login."
          continue
        fi
        if [ "$CB_PW" = 1 ] && grep "^TCGEN-POWER $(bre "$CB_VM") " "$W/apply.out" | grep -q ' now=off '; then
          ck_ok "vm $CB_VM: powered off now. The run powers it on and then waits for its ssh login."
          continue
        fi
        ck_vm "$CB_VM"
      done
    done
  done
  if [ -s "$W/clocks" ]; then
    CB_MIN=$(sort -n "$W/clocks" | head -n 1); CB_MAX=$(sort -n "$W/clocks" | tail -n 1)
    if [ $((CB_MAX - CB_MIN)) -gt 30 ]; then
      ck_bad "the clocks of the VMs differ by $((CB_MAX - CB_MIN)) s. Jobs of one case would start that far apart. Check time sync."
    fi
  fi
  say ""
  if [ "$BAD" = 0 ]; then say "CHECK RESULT: OK"; else say "CHECK RESULT: $BAD problem(s). Fix them before block S."; fi
}
cmd_check() {
  load_plan "$1"
  mkdir -p "$RD" "$LOGDIR" 2>/dev/null
  W=$RD/check.work
  rm -rf "$W"
  mkdir -p "$W" || exit 2
  CK_LOG=$LOGDIR/tcrun-$RUNID-check.log
  if [ -d "$LOGDIR" ] && [ -w "$LOGDIR" ]; then
    check_body 2>&1 | tee "$CK_LOG"
    say "saved: $CK_LOG"
  else
    check_body 2>&1
  fi
}

case "$1" in
  run) cmd_run "$2" "$3" ;;
  start) cmd_start "$2" ;;
  check) cmd_check "$2" ;;
  status) cmd_status "$2" ;;
  stop) cmd_stop "$2" ;;
  cancel) cmd_cancel "$2" ;;
  key) cmd_key ;;
  version) say "$TCRUN_VERSION" ;;
  *)
    say "tcrun $TCRUN_VERSION (tcgen sequence runner)"
    say "usage: sh $D/tcrun check <plan> | start <plan> | run <plan> <esx> | status [-a] | stop [esx] | cancel [esx] | key | version"
    exit 2
    ;;
esac
