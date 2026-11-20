#!/bin/sh
# tcrun 2.14 - tcgen sequence runner. Runs on the jump VM (POSIX sh). tcgen block A installs it in the jump VM work folder
# (setting of tcgen, ~/.tcgen when empty). tcrun uses the folder it is in as its work folder (D below).
#
#   sh <work folder>/tcrun check <plan>       read-only checks: ESX key login, tools, VMs, settings and power, VM login, clocks
#   sh <work folder>/tcrun start <plan>       start "run" in the background for every ESX host of the plan (block S uses this).
#                                             A run of an ESX host that another run still works for waits in the queue
#   sh <work folder>/tcrun run <plan> <esx>   run the cases of one ESX host in the order of the table
#   sh <work folder>/tcrun status [-a]        state of every ESX host of the latest run, of every run that is running or
#                                             queued, and of every run that left crontab lines on the hosts
#   sh <work folder>/tcrun stop [esx]         ask the runners to stop (flag file). Crontab lines that are registered stay:
#                                             their jobs still start. Monitoring that runs stays.
#   sh <work folder>/tcrun cancel [esx]       end every runner now (running or queued), take the crontab lines of its cases
#                                             out (jobs that have not started will not start) and stop monitoring that runs.
#                                             Also removes the lines that an ended run left on the hosts
#   sh <work folder>/tcrun key                print the line that registers the ssh key of this jump VM on an ESX host
#   sh <work folder>/tcrun version
#
# The plan is read with grep, sed and cut only. No text of the plan is given to eval or to a shell on this machine.
# Commands of the plan go byte for byte into crontab lines (quoted here-documents on the remote side).
# VM login: same way as <work folder>/vmssh (password from <work folder>/vmpw through SSH_ASKPASS, setsid).
# ESX login: <work folder>/esxssh (key <work folder>/id_esx, no password).
#
# Every case with a job has a start time in the plan. tcrun registers the job for that time and never changes it.
# The cases of an ESX host are handled in the order of the table:
#   - a case whose settings or VM power differ from the hosts is prepared PREP_LEAD_MIN before its job start: the VMs
#     are shut down, the settings are written, the VMs are powered on (one after another: power on, 1GB page backing
#     complete, ssh login). Then the crontab lines of that case and of the cases after it with the same settings are
#     registered at once.
#   - cases that need no change are registered as soon as tcrun reaches them (the first ones right after block S).
#   - SNAP_LEAD_MIN before the job start of every case: config snapshot (one login per VM, read only).
# The jobs are not looked for: tcrun does not log in to a VM while its job runs. The runner of an ESX host ends 1 min
# after the last job start. The crontab lines stay on the hosts until the next run of that ESX host registers its
# first lines, or until "tcrun cancel".
# Record of the registration: <log folder>/tcrun-<run id>-jobs.csv (case, esx, vm, job_start, crontab).

TCRUN_VERSION=2.14
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
  grep -E "^(AV|PAT|MS|JOB|MX|OFF) $SEQ " "$PLAN" | cut -d' ' -f3 | grep -q -v '^[A-Za-z0-9._][A-Za-z0-9._-]*$' && return 1
  for NO_V in $(grep "^OFF $SEQ " "$PLAN" | cut -d' ' -f3); do
    grep -q "^VM $SEQ $(bre "$NO_V") " "$PLAN" || return 1
  done
  grep "^START $SEQ " "$PLAN" | grep -q -v '^START [0-9]* [0-9][0-9]* [0-9][0-9: -]*$' && return 1
  grep -E "^(MSTOP|APPLYAT) $SEQ " "$PLAN" | grep -q -v -E '^(MSTOP|APPLYAT) [0-9]* [0-9][0-9]* [0-9][0-9: -]*$' && return 1
  grep "^SEG $SEQ " "$PLAN" | grep -q -v '^SEG [0-9]* [01]$' && return 1
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
  PON_WAIT=$(sget POWERON_WAIT_MIN 90)
  BOOT_WAIT=$(sget BOOT_WAIT_MIN 30)
  SHUT_WAIT=$(sget SHUTDOWN_WAIT_MIN 10)
  SNAPLEAD=$(sget SNAP_LEAD_MIN 10)
  SPACING=$(sget SPACING_MIN 5)
  PREP=$(sget PREP_LEAD_MIN 30)
  [ "$SPACING" -ge 1 ] || SPACING=1
  [ "$PON_WAIT" -ge 1 ] || PON_WAIT=90
  [ "$PREP" -ge 1 ] || PREP=30
  [ "$SNAPLEAD" -ge 1 ] || SNAPLEAD=10
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
                    # 1 = another job starts less than SPACING min away: CL_NEAR = the latest such start, CL_WHO = its owner
                    #     (also when <who> holds the claim already: claim_keep records a start time that is kept).
                    # 2 = the lock could not be taken
  CL_NEAR=; CL_WHO=
  if ! lock_md; then
    [ "$(cat "$MD/$1/who" 2>/dev/null)" = "$2" ] && return 0
    return 2
  fi
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
  if [ -z "$CL_NEAR" ] && [ "$(cat "$MD/$1/who" 2>/dev/null)" != "$2" ]; then
    mkdir "$MD/$1" 2>/dev/null
    say "$2" > "$MD/$1/who"
  fi
  unlock_md
  [ -z "$CL_NEAR" ] && return 0
  return 1
}
claim_keep() {      # claim_keep <epoch> <who>: the start time is used although another job starts near it (the start
                    # times of the plan are not changed): record it, so that the check of a later run sees it.
                    # Nothing is done when another runner holds exactly that minute
  [ -d "$MD/$1" ] && return 0
  lock_md || return 0
  if mkdir "$MD/$1" 2>/dev/null; then say "$2" > "$MD/$1/who"; fi
  unlock_md
  return 0
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
      1) say "WARN the start time $(clines START | head -n 1 | cut -d' ' -f2-) of case $(cname "$SEQ") is less than $SPACING min from the job start $(tstr "$CL_NEAR") of $CL_WHO. The start time of the plan is kept: check the two start times."
         claim_keep $((CP_AT / 60 * 60)) "run $RUNID, esx $CP_F, case $(cname "$SEQ")" ;;
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
guard() {           # guard <pid> <seconds> [<epoch>]: wait for the pid; end it when it runs longer than <seconds>, or when
                    # the clock of this machine reaches <epoch> (a login that must end before a start time).
                    # 124 = ended by the time limit.
                    # G_STOP=1: end it too when the stop flag of this runner appears. 125 = ended by a stop request
  G_P=$1
  rm -f "$W/timedout" "$W/stopwait"
  (
    G_I=0
    while [ "$G_I" -lt "$2" ]; do
      sleep 1
      kill -0 "$G_P" 2>/dev/null || exit 0
      if [ -n "$G_STOP" ] && [ -n "$STOPF" ] && [ -f "$STOPF" ]; then : > "$W/stopwait"; break; fi
      if [ -n "$3" ] && [ "$(now)" -ge "$3" ]; then break; fi
      G_I=$((G_I + 1))
    done
    [ -f "$W/stopwait" ] || : > "$W/timedout"
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
  [ -f "$W/stopwait" ] && G_R=125
  G_P=
  return $G_R
}
vmrun() {           # vmrun <ssh target> <script> <output> [seconds [epoch]]: run the script with sh on a VM (guard: time limit)
  $SETSID ssh $SO "$1" sh -s < "$2" > "$3" 2>&1 &
  guard $! "${4:-90}" "$5"
}
esxrun() {          # esxrun <script> <output> [seconds [epoch]]: run the script with sh on the ESX host of this chain
  sh "$D/esxssh" "$EADDR" sh -s < "$1" > "$2" 2>&1 &
  guard $! "${3:-120}" "$4"
}
tval() { grep "^$1 " "$2" 2>/dev/null | tail -n 1 | cut -d' ' -f2-; }
numv() { case "$1" in ""|*[!0-9]*) say "$2" ;; *) say "$1" ;; esac; }     # numv <value> <default>: the value when it is a number
first() {           # first <file>: the line that tells why it failed: an error line of ssh when there is one, else the first line
  FR=$(grep -i -E 'denied|refused|timed out|unreachable|no route|resolve|not known|verification failed|too many|reset by|closed by|broken pipe|quota|no space' "$1" 2>/dev/null | head -n 1)
  [ -n "$FR" ] || FR=$(grep -v -e '^ *$' -e '^TCGEN-HAD ' "$1" 2>/dev/null | head -n 1)
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
  MP_F=$(gcmd_file)
  if [ -n "$MP_F" ]; then say "if [ -r '$MP_F' ]; then echo 'TCGEN-GCMD yes'; else echo 'TCGEN-GCMD no'; fi" >> "$1"; fi
}
gcmd() {            # the command that the plan wants to run on every VM at the config snapshot (GCMD line; empty: none)
  grep '^GCMD ' "$PLAN" 2>/dev/null | head -n 1 | cut -d' ' -f2-
}
gcmd_file() {       # the first word of that command that is an absolute path (the script it runs), when it is a plain path
  GF_R=
  set -f              # the words of the command are not file name patterns of this machine
  for GF_W in $(gcmd); do
    case "$GF_W" in
      /*[!A-Za-z0-9._/+-]*) ;;
      /*) [ -n "$GF_R" ] || GF_R=$GF_W ;;
    esac
  done
  set +f
  [ -n "$GF_R" ] && say "$GF_R"
  return 0
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
mk_vmcron() {       # mk_vmcron <vm> <out> [TAG:epoch ...]: replace the lines of this case on the VM by the lines of the tags.
                    # The script prints "TCGEN-HAD <n>": how many lines of the case the VM had before.
                    # CRON_DL (epoch): the VM does not take the lines when its clock has reached that time (TCGEN-CRON late)
  MC_VM=$1; MC_OUT=$2
  shift 2
  {
    say 'N=$HOME/.tcgen_cron.new; P=$HOME/.tcgen_cron.pat'
    say "cat > \$P << 'TCGEN_EOF_PAT'"
    vlines PAT "$MC_VM"
    say 'TCGEN_EOF_PAT'
    say 'echo "TCGEN-HAD `crontab -l 2>/dev/null | grep -c -f $P`"'
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
    [ -n "$CRON_DL" ] && say "[ \`date +%s\` -lt $CRON_DL ] || { echo 'TCGEN-CRON late'; rm -f \$N \$P; exit 1; }"
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
    say 'echo "TCGEN-HAD `grep -c -f $P $CT`"'
    say 'grep -v -f $P $CT > $N'
    for ME_A in "$@"; do
      ME_TAG=${ME_A%%:*}; ME_T=${ME_A#*:}
      has "$ME_TAG" || continue
      ME_F=$(fmtu "$ME_T" '%M %H %d %m')
      say "while IFS= read -r L; do printf '%s * %s\\n' '$ME_F' \"\$L\"; done >> \$N << 'TCGEN_EOF_LINES'"
      clines "$ME_TAG"
      say 'TCGEN_EOF_LINES'
    done
    [ -n "$CRON_DL" ] && say "[ \`date +%s\` -lt $CRON_DL ] || { echo 'TCGEN-CRON late'; rm -f \$N \$P; exit 1; }"
    say 'chmod u+w $CT'
    say 'if cat $N > $CT && /bin/echo root >> $CD/cron.update; then echo "TCGEN-CRON ok"; else echo "TCGEN-CRON failed (cannot write $CT)"; fi'
    say 'rm -f $N $P'
    say 'echo "TCGEN-CLOCK `date +%s`"'
    say "grep ' # tcgen ' \$CT | sed 's/^/TCGEN-LINE /'"
  } > "$ME_OUT"
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
      check|prepare|snapshot)
        say "\$PY \"\$T/tcgen_apply.py\" $* << 'TCGEN_EOF_SPEC'"
        cat "${MA_SPEC:-$W/spec}"
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

# ---------------------------------------------------------------- VCF Operations (optional)
# When the plan has OPS lines, tcrun writes at the config snapshot of every case:
#   - the VM settings that the snapshot read on the ESX host (the rows "vm advanced config": the keys of config_snapshot
#     without sched.mem.pin, and the vmx keys of the settings profiles of this run) as properties
#     "Custom Configuration|VM|<key>" of the VirtualMachine object that has the name of the VM
#   - the host options of the case (AH lines) as "Custom Configuration|Host|<Section.Option>" of the HostSystem object
#     that has the name of the ESX host (or <name>.<domain>, or the address of the plan)
# The REST calls are those of guest_config_push: token (suite-api/api/auth/token/acquire), object by name
# (api/resources), properties (api/resources/<id>/properties). The guest OS settings are not written here: the command
# at the snapshot (guest_config_push) writes them from inside the guest.
# The password is in the plan as OPS ENC (openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -base64 -A) with its key
# OPS KEY, the way guest_config_push keeps it. It is decrypted only for the login; the key and the request body are
# files of the work folder (mode 600) that are removed right after. The password and the token are not logged.
# A failure is a warning in the log and a row of the snapshot csv: the case goes on.
ops_on() { grep -q '^OPS HOST ' "$PLAN"; }
ops_get() { grep "^OPS $1 " "$PLAN" | head -n 1 | cut -d' ' -f3-; }
ops_ok() {          # the OPS lines of the plan are safe to use (the generator already checks them)
  ops_get HOST | grep -q '^[A-Za-z0-9][A-Za-z0-9._:/-]*$' || return 1
  ops_get USER | LC_ALL=C grep -q '^[!-~][!-~]*$' || return 1
  ops_get ENC | grep -q '^[A-Za-z0-9+/=][A-Za-z0-9+/=]*$' || return 1
  ops_get KEY | grep -q '^[A-Za-z0-9][A-Za-z0-9]*$' || return 1
  return 0
}
ops_esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
ops_tool() {        # OPS_PY = a python of this jump VM that reads the JSON answers, else OPS_JQ = 1 (jq). 1 = neither
  OPS_PY=; OPS_JQ=
  for OT_C in python3 /usr/libexec/platform-python python; do
    if command -v "$OT_C" >/dev/null 2>&1; then OPS_PY=$OT_C; return 0; fi
  done
  if command -v jq >/dev/null 2>&1; then OPS_JQ=1; return 0; fi
  return 1
}
ops_token() {       # stdin: the answer of the token call. Prints the token
  if [ -n "$OPS_PY" ]; then
    "$OPS_PY" -c 'import sys, json
try:
    print(json.load(sys.stdin).get("token", ""))
except Exception:
    print("")'
  else
    jq -r '.token // ""' 2>/dev/null
  fi
}
ops_pick() {        # ops_pick <name> <0|1>: stdin = answer of the resources call. Prints the identifier of the object
                    # with exactly that name (1: or with a name that starts with "<name>.")
  if [ -n "$OPS_PY" ]; then
    "$OPS_PY" -c 'import sys, json
name, pre = sys.argv[1].lower(), sys.argv[2] == "1"
try:
    rows = json.load(sys.stdin).get("resourceList") or []
except Exception:
    rows = []
for r in rows:
    n = ((r.get("resourceKey") or {}).get("name") or "").lower()
    if n == name or (pre and n.startswith(name + ".")):
        print(r.get("identifier", ""))
        break' "$1" "$2"
  else
    jq -r --arg n "$1" --arg p "$2" '[.resourceList[]? | select((.resourceKey.name | ascii_downcase) as $x | $x == ($n | ascii_downcase) or ($p == "1" and ($x | startswith(($n | ascii_downcase) + "."))))][0].identifier // ""' 2>/dev/null
  fi
}
ops_login() {       # 0 = logged in: OPS_BASE and OPS_TLS are set, and $W/ops.cfg holds the token for curl -K (the token is
                    # not put on a command line, where other accounts of the jump VM could read it). 1 = not: $OPS_WHY
  OPS_WHY=
  rm -f "$W/ops.cfg"
  ops_ok || { OPS_WHY="the plan holds a VCF Operations value that tcrun does not accept. Generate again in tcgen"; return 1; }
  ops_tool || { OPS_WHY="python3 or jq is needed on the jump VM to read the answers of VCF Operations"; return 1; }
  for OL_C in curl openssl; do
    command -v $OL_C >/dev/null 2>&1 || { OPS_WHY="command not found on the jump VM: $OL_C"; return 1; }
  done
  OL_H=$(ops_get HOST)
  case "$OL_H" in
    http://*|https://*) OPS_BASE=${OL_H%/} ;;
    *) OPS_BASE="https://$OL_H" ;;
  esac
  OPS_TLS=-k
  [ "$(ops_get TLS)" = 1 ] && OPS_TLS=
  ops_get KEY > "$W/ops.key"
  OL_P=$(ops_get ENC | openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -pass "file:$W/ops.key" -base64 -A 2>/dev/null)
  rm -f "$W/ops.key"
  if [ -z "$OL_P" ]; then
    OPS_WHY="the password of VCF Operations could not be decrypted (openssl 1.1.1 or later is needed). Type the password again in tcgen and generate again"
    return 1
  fi
  printf '{"username":"%s","password":"%s","authSource":"LOCAL"}' "$(ops_esc "$(ops_get USER)")" "$(ops_esc "$OL_P")" > "$W/ops.body"
  OL_P=
  curl -s $OPS_TLS --max-time 15 -X POST "$OPS_BASE/suite-api/api/auth/token/acquire" -H 'Content-Type: application/json' \
    -H 'Accept: application/json' --data-binary "@$W/ops.body" > "$W/ops.out" 2>&1
  OL_R=$?
  rm -f "$W/ops.body"
  OL_T=$(ops_token < "$W/ops.out")
  if [ -z "$OL_T" ]; then
    OPS_WHY="login to VCF Operations $OL_H failed (account $(ops_get USER), curl exit $OL_R: $(head -c 160 "$W/ops.out" | LC_ALL=C tr -cd '\40-\176'))"
    rm -f "$W/ops.out"
    return 1
  fi
  rm -f "$W/ops.out"                             # the answer holds the token
  case "$OL_T" in
    *[!A-Za-z0-9:._=+/-]*) OPS_WHY="VCF Operations $OL_H gave a token with characters that tcrun does not accept"; OL_T=; return 1 ;;
  esac
  for OL_S in OpsToken vRealizeOpsToken; do
    printf 'header = "Authorization: %s %s"\n' "$OL_S" "$OL_T" > "$W/ops.cfg"
    OL_C=$(curl -s $OPS_TLS -K "$W/ops.cfg" -o /dev/null -w '%{http_code}' --max-time 15 -G "$OPS_BASE/suite-api/api/resources" \
      --data-urlencode "pageSize=1" --data-urlencode "_no_links=true" -H 'Accept: application/json' 2>/dev/null)
    if [ "$OL_C" = 200 ]; then OL_T=; return 0; fi
  done
  OL_T=
  rm -f "$W/ops.cfg"
  OPS_WHY="VCF Operations $OL_H did not accept its token (HTTP $OL_C)"
  return 1
}
ops_find() {        # ops_find <VirtualMachine|HostSystem> <name>: prints the identifier of that object ("" when there is none)
  curl -s $OPS_TLS --max-time 20 -G "$OPS_BASE/suite-api/api/resources" --data-urlencode "adapterKindKey=VMWARE" \
    --data-urlencode "resourceKind=$1" --data-urlencode "name=$2" --data-urlencode "_no_links=true" \
    -K "$W/ops.cfg" -H 'Accept: application/json' > "$W/ops.out" 2>&1
  OF_I=$(ops_pick "$2" 0 < "$W/ops.out")
  if [ -z "$OF_I" ] && [ "$1" = HostSystem ]; then      # a host is often registered with its full name
    curl -s $OPS_TLS --max-time 20 -G "$OPS_BASE/suite-api/api/resources" --data-urlencode "adapterKindKey=VMWARE" \
      --data-urlencode "resourceKind=$1" --data-urlencode "regex=^$(bre "$2")\\..*" --data-urlencode "_no_links=true" \
      -K "$W/ops.cfg" -H 'Accept: application/json' > "$W/ops.out" 2>&1
    OF_I=$(ops_pick "$2" 1 < "$W/ops.out")
  fi
  say "$OF_I"
}
ops_put() {         # ops_put <identifier> <property root> <file of "key<TAB>value" lines>: 0 = written, OPS_N = how many
  OPS_N=$(grep -c '[^ ]' "$3")
  awk -F'\t' -v root="$2" -v ts="$(( $(now) * 1000 ))" '
    function esc(s) { gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
    BEGIN { printf "{\"property-content\":[" }
    $1 != "" { printf "%s{\"statKey\":\"%s|%s\",\"timestamps\":[%s],\"values\":[\"%s\"]}", (n++ ? "," : ""), esc(root), esc($1), ts, esc($2) }
    END { printf "]}" }' "$3" > "$W/ops.body"
  OP_C=$(curl -s $OPS_TLS -o "$W/ops.out" -w '%{http_code}' --max-time 30 -X POST "$OPS_BASE/suite-api/api/resources/$1/properties" \
    -K "$W/ops.cfg" -H 'Content-Type: application/json' -H 'Accept: application/json' --data-binary "@$W/ops.body" 2>/dev/null)
  rm -f "$W/ops.body"
  case "$OP_C" in 200|201|204) return 0 ;; esac
  OPS_WHY="HTTP ${OP_C:-none}: $(head -c 160 "$W/ops.out" 2>/dev/null | LC_ALL=C tr -cd '\40-\176')"
  return 1
}
ops_snapshot() {    # write the VM settings in $W/snap.rows and the host options of case $SEQ to VCF Operations.
                    # Adds one row per VM to $W/snap.rows ("operations write") and sets SN_P (VMs written), SN_W
  SN_P=0
  if ! ops_login; then
    SN_W="$SN_W VCF Operations: nothing written ($OPS_WHY);"
    for OS_VM in $VMS; do printf '%s\tadv\toperations write\tfailed (no login)\n' "$OS_VM" >> "$W/snap.rows"; done
    return 0
  fi
  OS_NONE=$(cfgl none '(none)')
  OS_PIN=$(grep -i -c '^key sched\.mem\.pin$' "$W/snap.spec" 2>/dev/null)
  for OS_VM in $VMS; do
    awk -F'\t' -v vm="$OS_VM" -v none="$OS_NONE" -v pin="${OS_PIN:-0}" '
      $1 == vm && $2 == "adv" && $3 != "operations write" && (pin > 0 || tolower($3) != "sched.mem.pin") { print $3 "\t" ($4 == "" ? none : $4) }' \
      "$W/snap.rows" > "$W/ops.props"
    if [ ! -s "$W/ops.props" ]; then
      printf '%s\tadv\toperations write\tnot written (no VM settings were read)\n' "$OS_VM" >> "$W/snap.rows"
      continue
    fi
    OS_ID=$(ops_find VirtualMachine "$OS_VM")
    if [ -z "$OS_ID" ]; then
      SN_W="$SN_W VCF Operations has no VirtualMachine object named $OS_VM;"
      printf '%s\tadv\toperations write\tnot written (no VirtualMachine object with this name)\n' "$OS_VM" >> "$W/snap.rows"
      continue
    fi
    if ops_put "$OS_ID" "Custom Configuration|VM" "$W/ops.props"; then
      SN_P=$((SN_P + 1))
      log "config snapshot: VCF Operations: vm $OS_VM: $OPS_N VM setting(s) written (Custom Configuration|VM)"
      printf '%s\tadv\toperations write\tok: %s properties\n' "$OS_VM" "$OPS_N" >> "$W/snap.rows"
    else
      SN_W="$SN_W VCF Operations: properties of vm $OS_VM not written ($OPS_WHY);"
      printf '%s\tadv\toperations write\tfailed (%s)\n' "$OS_VM" "$(say "$OPS_WHY" | tr '\t' ' ' | cut -c1-80)" >> "$W/snap.rows"
    fi
  done
  if has AH; then
    # the host options of the case: the values that the ESX host has now (read by the snapshot, TCGEN-HOPT lines)
    grep '^TCGEN-HOPT ' "$W/snap.out" 2>/dev/null | cut -d' ' -f2- |
      awk '{ i = index($0, "|"); k = substr($0, 1, i - 1); v = substr($0, i + 1); sub(/^\//, "", k); gsub(/\//, ".", k); print k "\t" v }' > "$W/ops.props"
    OS_NH=$(clines AH | grep -c '[^ ]')
    OS_NR=$(grep -c '[^ ]' "$W/ops.props")
    [ "$OS_NR" -lt "$OS_NH" ] && SN_W="$SN_W $((OS_NH - OS_NR)) of $OS_NH host option(s) of the case not read on esx $CHF and not written to VCF Operations;"
    if [ "$OS_NR" -gt 0 ]; then
      OS_ID=$(ops_find HostSystem "$CH")
      if [ -z "$OS_ID" ] && [ "$EADDR" != "$CH" ]; then OS_ID=$(ops_find HostSystem "$EADDR"); fi
      if [ -z "$OS_ID" ]; then
        SN_W="$SN_W VCF Operations has no HostSystem object named $CH;"
      elif ops_put "$OS_ID" "Custom Configuration|Host" "$W/ops.props"; then
        log "config snapshot: VCF Operations: esx $CHF: $OPS_N host option(s) written (Custom Configuration|Host)"
      else
        SN_W="$SN_W VCF Operations: properties of esx $CHF not written ($OPS_WHY);"
      fi
    fi
  fi
  rm -f "$W/ops.out" "$W/ops.props" "$W/ops.cfg"
  return 0
}

# ---------------------------------------------------------------- configuration snapshot (config snapshot)
# Before the job of every case tcrun records the configuration of the VMs of the case: the VM settings (.vmx values,
# read on the ESX host by "tcgen_apply.py snapshot") and the guest OS settings (read on each VM of the case that is
# powered on; the items of guest_config_push). One csv per case in the log folder, laid out like the VM table of
# config_snapshot: columns group, item, one column per VM. The labels of the csv come from the plan (CFGL lines;
# tcrun itself is ASCII only).
# When: SNAP_LEAD_MIN minutes before the job start of every case (10 by default; user decision 2026-11-18). The
# crontab lines of the case are registered before that, so the snapshot does not move a start time. When the job
# start is less than 1 min away, the snapshot is not taken (no login to a VM while its job runs).
# A case without a job (settings only) has no snapshot.
# Read only. A snapshot that cannot be taken is a warning in the log: the case goes on.
# The guest OS settings are those at the time of the snapshot. What a job command changes when it starts is not in it.
# Command at the snapshot (GCMD line of the plan, optional): tcrun runs it on every VM of the case that is powered on,
# after the guest OS settings were read (one more login). It is meant for the script that records the guest settings
# in VCF Operations (guest_config_push). Its result is one row of the csv ("snapshot command") and one line of the log.
# A command that fails is a warning: the case goes on. The command is not started on a VM later than 2 min before the
# job start (SN_DL); that is a warning too.
cfgl() {            # cfgl <code> <text when the plan has no such label>
  CL_T=$(grep "^CFGL $1 " "$PLAN" | head -n 1 | cut -d' ' -f3-)
  say "${CL_T:-$2}"
}
mk_guestcfg() {     # mk_guestcfg <out>: script that prints the guest OS settings of a VM as "TCGEN-G <item>|<value>"
  cat > "$1" << 'TCRUN_EOF_GUESTCFG'
R=${TCGEN_SYSROOT:-}
T=$R/sys/kernel/mm/transparent_hugepage
br() { sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$1" 2>/dev/null | head -n 1; }
rd() { [ -r "$1" ] && tr -d '\n' < "$1" 2>/dev/null; }
echo "TCGEN-G THP enabled|`br $T/enabled`"
echo "TCGEN-G THP defrag|`br $T/defrag`"
echo "TCGEN-G khugepaged scan_sleep_millisecs|`rd $T/khugepaged/scan_sleep_millisecs`"
echo "TCGEN-G khugepaged pages_to_scan|`rd $T/khugepaged/pages_to_scan`"
echo "TCGEN-G kernel.numa_balancing|`rd $R/proc/sys/kernel/numa_balancing`"
echo "TCGEN-G hugepages 2MB nr_hugepages|`rd $R/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages`"
echo "TCGEN-G hugepages 1GB nr_hugepages|`rd $R/sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages`"
echo "TCGEN-G default_hugepagesz (cmdline)|`sed -n 's/.*default_hugepagesz=\([^ ]*\).*/\1/p' $R/proc/cmdline 2>/dev/null | head -n 1`"
echo "TCGEN-G kernel|`uname -r 2>/dev/null`"
echo "TCGEN-G Collected At|`date '+%Y-%m-%d %H:%M:%S %Z'`"
echo "TCGEN-G-END ok"
TCRUN_EOF_GUESTCFG
}
snapshot() {        # snapshot: write the config snapshot of the VMs of this case ($SN_WHY says why it is taken)
  STEP=snapshot; setstate running "recording the configuration of the VMs (config snapshot)"
  # VMs that this run has just powered on: read 60 s after the last one was ready, so that the guest has applied the
  # settings of its boot (tuned, rc scripts). Not when that would be later than 2 min before the job start
  if [ -n "$T_ON" ] && [ "$(now)" -lt $((T_ON + 60)) ] && [ $((T_ON + 60)) -lt $((AT / 60 * 60 - 120)) ]; then
    log "config snapshot: taken at $(tstr $((T_ON + 60))), 60 s after the last VM was ready (the guest applies the settings of its boot)"
    wait_until $((T_ON + 60)) || return 0
  fi
  SN_F=$LOGDIR/tcrun-$RUNID-$CHF-config-$SEQ-$CNAME.csv
  SN_T=$(stamp); SN_W=; SN_V=0; SN_G=0; SN_P=
  : > "$W/snap.rows"
  # the command at the snapshot is not started later than 2 min before the job start
  SN_DL=$((AT / 60 * 60 - 120))
  if [ "$CH" != "-" ]; then          # VM settings: read on the ESX host
    {
      for SN_VM in $VMS; do say "vm $SN_VM"; done
      grep '^AV [0-9]* ' "$PLAN" | cut -d' ' -f4- | sed 's/=.*//' | sort -u | sed 's/^/key /'
      if ops_on; then clines AH | sed 's/=.*//' | sed 's/^/host /'; fi      # read for VCF Operations (ops_snapshot)
    } > "$W/snap.spec"
    MA_SPEC=$W/snap.spec
    mk_apply "$W/snap.sh" snapshot
    MA_SPEC=
    esxrun "$W/snap.sh" "$W/snap.out" 180
    SN_X=$?
    if [ "$(tval TCGEN-RESULT "$W/snap.out" | cut -d' ' -f1)" = snapshot ]; then
      grep '^TCGEN-CFG ' "$W/snap.out" | cut -d' ' -f2- |
        awk -F'|' '{ v = $4; for (i = 5; i <= NF; i++) v = v "|" $i; print $1 "\t" $2 "\t" $3 "\t" v }' >> "$W/snap.rows"
      SN_V=$(grep -c '^TCGEN-CFG [^|]*|basic|cpu|' "$W/snap.out")
      for SN_VM in $VMS; do
        grep -q "^TCGEN-CFG $(bre "$SN_VM")|basic|cpu|" "$W/snap.out" || SN_W="$SN_W VM settings of $SN_VM not read ($(grep "^vm $(bre "$SN_VM"): " "$W/snap.out" | head -n 1 | cut -c1-160));"
      done
    else
      SN_R=$(tval TCGEN-RESULT "$W/snap.out")
      SN_W="$SN_W VM settings not read on the ESX host (${SN_R:-ssh exit $SN_X: $(first "$W/snap.out")});"
    fi
    if ops_on; then ops_snapshot; fi
  fi
  mk_guestcfg "$W/gcfg.sh"           # guest OS settings: read on each VM that is powered on
  SN_C=$(gcmd); SN_O=0
  if [ -n "$SN_C" ]; then
    { say "( $SN_C ) < /dev/null"; say 'echo "TCGEN-GCMD-RC $?"'; } > "$W/gcmd.sh"
  fi
  for SN_VM in $ONVMS; do
    vmrun "$(target "$SN_VM")" "$W/gcfg.sh" "$W/gcfg.out" 60
    SN_X=$?
    if grep -q '^TCGEN-G-END ' "$W/gcfg.out"; then
      grep '^TCGEN-G ' "$W/gcfg.out" | cut -d' ' -f2- |
        awk -F'|' -v vm="$SN_VM" '{ v = $2; for (i = 3; i <= NF; i++) v = v "|" $i; print vm "\tguest\t" $1 "\t" v }' >> "$W/snap.rows"
      SN_G=$((SN_G + 1))
    else
      SN_W="$SN_W guest OS settings of $SN_VM not read (ssh exit $SN_X: $(first "$W/gcfg.out"));"
    fi
    [ -n "$SN_C" ] || continue
    if [ -n "$SN_DL" ] && [ "$(now)" -gt "$SN_DL" ]; then
      SN_W="$SN_W command on vm $SN_VM not run (no time left before the job start);"
      printf '%s\tguest\tsnapshot command\tnot run (no time left before the job start)\n' "$SN_VM" >> "$W/snap.rows"
      continue
    fi
    # command at the snapshot (records the guest settings in VCF Operations): its last line goes into the log and the csv
    vmrun "$(target "$SN_VM")" "$W/gcmd.sh" "$W/gcmd.out" 150
    SN_X=$?
    SN_RC=$(tval TCGEN-GCMD-RC "$W/gcmd.out")
    SN_L=$(grep -v '^TCGEN-GCMD-RC ' "$W/gcmd.out" | grep -v '^ *$' | tail -n 1 | LC_ALL=C tr -cd '\11\40-\176' | tr '\t' ' ' | cut -c1-160)
    if [ "$SN_RC" = 0 ]; then
      SN_O=$((SN_O + 1))
      log "config snapshot: command on vm $SN_VM: ok${SN_L:+ ($SN_L)}"
      printf '%s\tguest\tsnapshot command\tok%s\n' "$SN_VM" "${SN_L:+: $SN_L}" >> "$W/snap.rows"
    else
      case "$SN_RC" in ""|*[!0-9]*) SN_E="ssh exit $SN_X" ;; *) SN_E="exit $SN_RC" ;; esac
      SN_W="$SN_W command on vm $SN_VM failed ($SN_E${SN_L:+: $SN_L});"
      printf '%s\tguest\tsnapshot command\tfailed (%s)%s\n' "$SN_VM" "$SN_E" "${SN_L:+: $SN_L}" >> "$W/snap.rows"
    fi
  done
  {
    printf '\357\273\277'
    tr -d '\r' < "$W/snap.rows" | awk -F'\t' -v vms="$(say $VMS)" -v now="$SN_T" -v h1="$(cfgl h1 group)" -v h2="$(cfgl h2 item)" \
      -v ltime="$(cfgl time 'collected at')" -v lbasic="$(cfgl basic 'VM basic information')" -v ladv="$(cfgl adv 'vm advanced config')" \
      -v lguest="$(cfgl guest 'Guest OS')" -v laff="$(cfgl aff 'CPU affinity')" -v resx="$(cfgl ref_esx 'time of the values (ESX host)')" \
      -v rguest="$(cfgl ref_guest 'time of the values (guest)')" -v none="$(cfgl none '(none)')" '
      function q(s) { gsub(/"/, "\"\"", s); return "\"" s "\"" }
      BEGIN { n = split(vms, vm, " "); ng = split("basic adv guest aff", g, " ")
              lab["basic"] = lbasic; lab["adv"] = ladv; lab["guest"] = lguest; lab["aff"] = laff }
      NF >= 3 {
        if ($2 == "guest" && $3 == "Collected At") { gt[$1] = $4; next }
        k = $2 SUBSEP $3
        if (!(k in seen)) { seen[k] = 1; cnt[$2]++; item[$2, cnt[$2]] = $3 }
        val[$1, $2, $3] = $4; got[$1, $2] = 1
      }
      END {
        line = q(h1) "," q(h2); for (i = 1; i <= n; i++) line = line "," q(vm[i]); printf "%s\r\n", line
        line = q(ltime) "," q(""); for (i = 1; i <= n; i++) line = line "," q(now); printf "%s\r\n", line
        for (x = 1; x <= ng; x++) {
          G = g[x]; if (!cnt[G]) continue
          line = q(lab[G]) "," q(G == "guest" ? rguest : resx)
          for (i = 1; i <= n; i++) {
            if (G == "guest") t = ((vm[i] in gt) && gt[vm[i]] != "") ? gt[vm[i]] : none
            else t = ((vm[i], G) in got) ? now : none
            line = line "," q(t)
          }
          printf "%s\r\n", line
          for (y = 1; y <= cnt[G]; y++) {
            it = item[G, y]; line = q("") "," q(it)
            for (i = 1; i <= n; i++) { v = ((vm[i], G, it) in val) ? val[vm[i], G, it] : ""; line = line "," q(v == "" ? none : v) }
            printf "%s\r\n", line
          }
        }
      }'
  } > "$SN_F.new" 2>> "$LOG"
  if [ -s "$SN_F.new" ] && mv -f "$SN_F.new" "$SN_F"; then
    log "config snapshot ($SN_WHY): $SN_F  (VM settings of $SN_V VM(s), guest OS settings of $SN_G VM(s)${SN_P:+, VM settings written to VCF Operations for $SN_P VM(s)}${SN_C:+, command at the snapshot ok on $SN_O VM(s)})"
  else
    rm -f "$SN_F.new"
    SN_W="$SN_W the csv could not be written: $SN_F;"
  fi
  [ -n "$SN_W" ] && log "WARN config snapshot of case $CNAME is not complete:$SN_W The case goes on."
  return 0
}

# ---------------------------------------------------------------- results
write_result() {
  [ -n "$R_DONE" ] && return 0
  R_DONE=1
  WR_S=$(printf '%s' "$R_STATUS" | tr '"' "'")
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"%s"\n' "$CNAME" "$CHF" "$R_APPLY" "$R_BEGIN" "$R_VMSON" "$R_MON" "$R_JOB" \
    "$R_SEEN" "$R_END" "$R_STOP" "$(stamp)" "$WR_S" >> "$RES"
}
write_report() {    # case file of the performance report generator: UTF-8 with BOM, job start of the plan
  if [ ! -s "$RPT" ]; then
    printf '\357\273\277' > "$RPT"
    printf '%s\r\n' "$(hget RPTH)" >> "$RPT"
  fi
  # job end: empty. tcrun does not look for the job, the report generator detects the end
  printf '%s,%s,%s,%s\r\n' "$(clines RPTA)" "$R_JOB" "" "$(clines RPTB)" >> "$RPT"
}
# pause and stopped end the run. cmd_run writes the state file as its last step, after the last log lines,
# so that whoever sees the state "paused", "stopped" or "done" finds the complete log.
pause() { log "PAUSED: $1"; R_STATUS="paused: $1"; write_result; HOW=paused; HOW_TEXT=$1; }
stopped() { log "STOPPED on request: $1"; R_STATUS="stopped: $1"; write_result; HOW=stopped; HOW_TEXT=$1; }

# ---------------------------------------------------------------- step 1: settings and VM power
lpage_wait() {      # lpage_wait <vm>: wait until the 1GB page backing of the VM is complete: the 1GB page backings reach
                    # memSize / 1024, no increase in 6 readings = stopped. VMs without 1GB pages: no wait
  setstate running "vm $1: waiting for the 1GB page backing"
  mk_apply "$W/lpw.sh" lpagewait "$1" $((PON_WAIT * 60))
  G_STOP=$PV_STOPPABLE
  esxrun "$W/lpw.sh" "$W/lpw.out" $((PON_WAIT * 60 + 300))
  G_STOP=
  if [ -f "$W/stopwait" ]; then
    LW_FAIL=; PO_STOP=1
    WHY="stop requested while the 1GB page backing of vm $1 was read. The VM stays powered on"
    return 1
  fi
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
                    # PO_NEW=1: this call powered the VM on, PO_READY = when it was ready. LW_FAIL=1: the backing failed.
                    # PV_STOPPABLE=1 (set by power_vms): a stop request ends the waits. PO_STOP=1: ended by a stop request
  PO_NEW=0; LW_FAIL=; PO_STOP=
  STEP=poweron; setstate running "power on $1"
  PO_S=$(now)
  mk_apply "$W/pon.sh" poweron "$1" $((PON_WAIT * 60))
  G_STOP=$PV_STOPPABLE
  esxrun "$W/pon.sh" "$W/pon.out" $((PON_WAIT * 60 + 300))
  PO_X=$?
  G_STOP=
  if [ -f "$W/stopwait" ]; then
    PO_STOP=1
    WHY="stop requested while vm $1 was being powered on. Its power-on task goes on to its end on the ESX host"
    return 1
  fi
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
    if [ -n "$PV_STOPPABLE" ] && [ -f "$STOPF" ]; then
      PO_STOP=1
      WHY="stop requested while tcrun waited for the ssh login of vm $1. The VM stays powered on"
      return 1
    fi
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
    say "$PO_READY" > "$W/chg"
    log "vm $1: ssh login works, ready $((PO_READY - PO_S)) s after the power on started (power on, 1GB page backing, ssh login)"
  else
    log "vm $1: ssh login works"
  fi
  return 0
}
apply_undo() {      # put the settings back, then power on (in table order) the VMs that were on before
  PV_STOPPABLE=       # a rollback is not ended by a stop request
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
      say "$(now)" > "$W/chg"
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
power_stopped() {   # power_stopped <where>: a stop request came while the VMs of the case were powered. tcrun ends here and
                    # puts nothing back (a rollback would shut VMs down again)
  PS_T="while the VMs of case $CNAME were being powered ($1)."
  [ "$R_APPLY" = changed ] && PS_T="$PS_T The settings of the case stay applied."
  stopped "$PS_T VMs that were not powered on yet stay off. No crontab line of the case was registered."
}
power_vms() {       # VM power as the case wants it: VMs marked off are shut down first, then the other VMs are powered on
                    # one after another in table order (each one: power on, 1GB page backing complete, ssh login)
  PV_NEW=; PV_OFFED=; PO_STOP=
  for PV_VM in $OFFVMS; do
    STEP=poweroff; setstate running "powering off vm $PV_VM (the case wants it off)"
    mk_apply "$W/poff.sh" poweroff "$PV_VM" $((SHUT_WAIT * 60))
    esxrun "$W/poff.sh" "$W/poff.out" $((SHUT_WAIT * 60 + 300))
    PV_X=$?
    PV_R=$(tval TCGEN-RESULT "$W/poff.out")
    case "$PV_R" in
      alreadyoff*) log "vm $PV_VM: powered off already (the case wants it off)" ;;
      poweredoff*)
        logf '    | ' "$W/poff.out"; log "vm $PV_VM: shut down (the case wants it off)"; PV_OFFED="$PV_OFFED $PV_VM"
        say "$(now)" > "$W/chg" ;;
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
    if [ -f "$STOPF" ]; then
      power_stopped "stop requested before vm $PV_VM was looked at"
      return 1
    fi
    PV_STOPPABLE=1
    power_on "$PV_VM"
    PV_X=$?
    PV_STOPPABLE=
    if [ "$PV_X" != 0 ]; then
      if [ -n "$PO_STOP" ]; then power_stopped "$WHY"; else power_fail "$WHY"; fi
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

# ---------------------------------------------------------------- VM power state, clocks, removing lines
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
unregister() {      # remove the lines of this case from the VMs and from the ESX host.
                    # UR_VMS: the VMs to do, in that order (empty: every VM of the case, "-": no VM). UR_NOESX=1: not the
                    # ESX host.
                    # 0 = removed everywhere. 1 = not: UR_BAD lists the VMs (and "@esx") where the lines may still be.
                    # $W/unreg.clock gets a line "<vm or @esx> <clock of that host when its lines were removed> <how many
                    # lines of the case it had>" for every host that answered
  UR_BAD=
  : > "$W/unreg.clock"
  for UR_VM in ${UR_VMS:-$REGVMS}; do
    [ "$UR_VM" = "-" ] && continue
    mk_vmcron "$UR_VM" "$W/unreg.sh"
    vmrun "$(target "$UR_VM")" "$W/unreg.sh" "$W/unreg.out" 60
    if [ "$(tval TCGEN-CRON "$W/unreg.out")" = ok ]; then
      log "removed the lines of this case: vm $UR_VM"
      say "$UR_VM $(numv "$(tval TCGEN-CLOCK "$W/unreg.out")" 0) $(numv "$(tval TCGEN-HAD "$W/unreg.out")" 1)" >> "$W/unreg.clock"
    else
      UR_BAD="$UR_BAD $UR_VM"
      log "WARN the lines of this case could not be removed on vm $UR_VM ($(first "$W/unreg.out"))"
    fi
  done
  if [ -z "$UR_NOESX" ] && has EPAT; then
    mk_esxcron "$W/eunreg.sh"
    esxrun "$W/eunreg.sh" "$W/eunreg.out" 60
    if [ "$(tval TCGEN-CRON "$W/eunreg.out")" = ok ]; then
      log "removed the lines of this case: esx $CHF"
      say "@esx $(numv "$(tval TCGEN-CLOCK "$W/eunreg.out")" 0) $(numv "$(tval TCGEN-HAD "$W/eunreg.out")" 1)" >> "$W/unreg.clock"
    else
      UR_BAD="$UR_BAD @esx"
      log "WARN the lines of this case could not be removed on esx $CHF ($(first "$W/eunreg.out"))"
    fi
  fi
  [ -z "$UR_BAD" ]
}

# ---------------------------------------------------------------- cases, parts and the registration record
# Every case with a job has a start time in the plan (START). tcrun never changes it.
# A "part" is a case whose settings or VM power may differ from the hosts (SEG 1: the first case of an ESX host, and a
# case whose settings, VM power or VMs differ from the case before it) together with the cases after it that want the
# same (SEG 0). The crontab lines of every case of a part are registered at once, right after the settings and the VM
# power of its first case are as wanted. Between the cases of a part tcrun logs in to the VMs only for the config
# snapshot (SNAP_LEAD_MIN before every job start).
# A part whose settings or VM power must change is prepared PREP_LEAD_MIN before the job start of its first case:
# the VMs are shut down, the settings are written, the VMs are powered on again, then the lines are registered.
# A part that needs no change is registered as soon as tcrun reaches it (the first part: right after block S).
ctx() {             # ctx <case number>: load the values of that case
  SEQ=$1
  CNAME=$(cname "$SEQ")
  VMS=$(grep "^VM $SEQ " "$PLAN" | cut -d' ' -f3)
  OFFVMS=$(grep "^OFF $SEQ " "$PLAN" | cut -d' ' -f3)
  ONVMS=
  for CX_VM in $VMS; do
    grep -q "^OFF $SEQ $(bre "$CX_VM")\$" "$PLAN" || ONVMS="$ONVMS $CX_VM"
  done
  REGVMS=$(grep "^PAT $SEQ " "$PLAN" | cut -d' ' -f3)
  JOBVMS=$(grep "^JOB $SEQ " "$PLAN" | cut -d' ' -f3)
  HASMON=0
  if grep -q "^MS $SEQ " "$PLAN" || has EMS; then HASMON=1; fi
  AT=$(clines START | head -n 1 | cut -d' ' -f1)
  case "$AT" in ""|*[!0-9]*) AT= ;; esac
  ATTEXT=$(clines START | head -n 1 | cut -d' ' -f2-)
  MSTOP=$(clines MSTOP | head -n 1 | cut -d' ' -f1)
  case "$MSTOP" in ""|*[!0-9]*) MSTOP= ;; esac
  APPLYAT=$(clines APPLYAT | head -n 1 | cut -d' ' -f1)
  case "$APPLYAT" in ""|*[!0-9]*) APPLYAT= ;; esac
  SEG=$(clines SEG | head -n 1)
  [ "$SEG" = 0 ] || SEG=1
}
seg_list() {        # seg_list <case number>: that case and the cases after it up to the next case with SEG 1
  SL_ON=
  for SL_S in $SEQS; do
    if [ "$SL_S" = "$1" ]; then SL_ON=1; say "$SL_S"; continue; fi
    [ -n "$SL_ON" ] || continue
    [ "$(grep "^SEG $SL_S " "$PLAN" | head -n 1 | cut -d' ' -f3)" = 0 ] || break
    say "$SL_S"
  done
}
part_last() {       # part_last <case number>: the latest job start (epoch) of the cases of the part that starts at that
                    # case ("" when none of them has a job)
  PL_M=
  for PL_S in $(seg_list "$1"); do
    PL_T=$(grep "^START $PL_S " "$PLAN" | head -n 1 | cut -d' ' -f3)
    case "$PL_T" in ""|*[!0-9]*) continue ;; esac
    if [ -z "$PL_M" ] || [ "$PL_T" -gt "$PL_M" ]; then PL_M=$PL_T; fi
  done
  say "$PL_M"
}
jobs_head() {       # the registration record of the run: one row for every VM with a job of every case
  [ -s "$JOBS" ] || say "case,esx,vm,job_start,crontab" > "$JOBS"
}
jobs_rows() {       # jobs_rows <text>: one row for every VM of case $CNAME that has a job.
                    # $W/jobs.note may hold a line "<vm> <note>" for a VM: "<note>" is added to its text, "=<text>" is
                    # written instead of it, "-" writes no row for that VM. The file is emptied
  for JR_VM in $JOBVMS; do
    JR_N=$(grep "^$(bre "$JR_VM") " "$W/jobs.note" 2>/dev/null | head -n 1 | cut -d' ' -f2-)
    case "$JR_N" in
      "-") continue ;;
      "="*) JR_T=${JR_N#=} ;;
      "") JR_T=$1 ;;
      *) JR_T="$1. $JR_N" ;;
    esac
    JR_T=$(printf '%s' "$JR_T" | tr '"' "'" | tr -d '\r\n')
    printf '%s,%s,%s,%s,"%s"\n' "$CNAME" "$CHF" "$JR_VM" "$ATTEXT" "$JR_T" >> "$JOBS"
  done
  rm -f "$W/jobs.note"
}
need_change() {     # 0 = the settings or the VM power of case $SEQ differ from what the hosts have now (NC_T says what).
                    # 1 = everything is as wanted, or the case has nothing on an ESX host. 2 = could not be read ($WHY)
  [ "$CH" != "-" ] || return 1
  if ! has AH && ! has AV && [ -z "$VMS" ]; then return 1; fi
  STEP=compare; setstate running "comparing the settings and the VM power with the ESX host"
  mk_spec
  mk_apply "$W/apply.sh" check
  esxrun "$W/apply.sh" "$W/apply.out" 300
  NC_R=$?
  NC_T=$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f2-)
  case "$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f1)" in
    nochange) return 1 ;;
    wouldchange) return 0 ;;
  esac
  if [ "$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f1)" = error ]; then
    WHY="esx $CHF: the settings of case $CNAME cannot be applied: $NC_T"
  else
    WHY="esx $CHF: the settings and the VM power of case $CNAME could not be read (ssh exit $NC_R: $(first "$W/apply.out"))"
  fi
  return 2
}
register_case() {   # register the crontab lines of case $SEQ (ctx loaded) at the times of the plan. The times are not changed.
                    # 0 = registered on every host. 1 = not registered: $WHY says why. Lines that a host already had are
                    # removed again; $W/jobs.note says for which VM that was not possible or was done after the first
                    # start time (rows of the registration record)
  WHY=; RG_MLATE=
  TJOB=$((AT / 60 * 60)); TMON=
  [ "$HASMON" = 1 ] && TMON=$((TJOB - LEAD * 60))
  RG_N=$(now)
  if [ "$RG_N" -ge $((TJOB - 60)) ]; then
    WHY="its job start time $(tstr "$TJOB") has passed or is less than 1 min away"
    return 1
  fi
  if [ -n "$TMON" ] && [ "$RG_N" -ge $((TMON - 60)) ]; then
    # the monitoring start time has passed or is less than 1 min away: the monitoring starts at the first minute that
    # is 1 min or more away (never later than the job start). The job start is not changed
    RG_MLATE=$(tstr "$TMON")
    TMON=$(( (RG_N + 119) / 60 * 60 ))
    [ "$TMON" -gt "$TJOB" ] && TMON=$TJOB
    log "WARN case $CNAME: the monitoring start time $RG_MLATE has passed or is less than 1 min away. The monitoring starts at $(tstr "$TMON"). The job start $ATTEXT is kept."
  fi
  RG_F=${TMON:-$TJOB}
  claim_start "$TJOB" "$ME"
  if [ $? = 1 ]; then
    log "WARN the job of $CL_WHO starts $(tstr "$CL_NEAR"), less than $SPACING min from the job start $ATTEXT of case $CNAME. The start time of the plan is kept."
    claim_keep "$TJOB" "$ME"
  fi
  # The lines of a case are on every host 15 s before its first start time, or on none:
  #  - every login gets only the time that is left until then, and a host does not take the lines when its own clock
  #    has reached that time (CRON_DL)
  #  - when one host fails, the lines are removed again on the hosts that have them, first on those that answered in time
  # The case is listed in $W/times before the first login: when the runner is ended here, cancel removes its lines.
  say "$SEQ $RG_F $TJOB" >> "$W/times"
  CRON_DL=$((RG_F - 15))
  RG_HAVE=; RG_SLOW=; RG_UNK=; RG_ZONE=
  for RG_VM in $REGVMS; do
    RG_L=$((RG_F - 15 - $(now)))
    if [ "$RG_L" -lt 5 ]; then
      WHY="vm $RG_VM: no time was left to register its lines 15 s before the first start time $(tstr "$RG_F")"
      break
    fi
    [ "$RG_L" -gt 60 ] && RG_L=60
    mk_vmcron "$RG_VM" "$W/reg.sh" ${TMON:+MS:$TMON} "JOB:$TJOB" ${MSTOP:+MX:$MSTOP}
    vmrun "$(target "$RG_VM")" "$W/reg.sh" "$W/reg.out" "$RG_L" $((RG_F - 15))
    RG_R=$?
    RG_C=$(tval TCGEN-CRON "$W/reg.out")
    if [ "$RG_C" = ok ]; then
      RG_K=$(numv "$(tval TCGEN-CLOCK "$W/reg.out")" "$(now)")
      if [ "$RG_K" -ge $((RG_F - 15)) ]; then
        WHY="vm $RG_VM: the lines arrived less than 15 s before the first start time $(tstr "$RG_F") (clock of the VM)"
        RG_SLOW=$RG_VM
        break
      fi
      RG_HAVE="$RG_HAVE $RG_VM"
      continue
    fi
    case "$RG_C" in
      late*)                                     # the VM did not take the lines
        WHY="vm $RG_VM: the lines arrived less than 15 s before the first start time $(tstr "$RG_F") (clock of the VM)" ;;
      "")                                        # no result line: the script did not run, or it did not end
        # the script had started when its output holds the TCGEN-HAD line: the VM may still take the lines (RG_ZONE)
        grep -q '^TCGEN-HAD ' "$W/reg.out" 2>/dev/null && RG_ZONE=$RG_VM
        if [ "$RG_R" = 124 ]; then
          WHY="vm $RG_VM: no answer within the $RG_L s that were left to register the lines 15 s before the first start time $(tstr "$RG_F")"
          RG_UNK=$RG_VM
        else
          WHY="vm $RG_VM: the crontab lines were not registered (ssh exit $RG_R: $(first "$W/reg.out"))"
          RG_UNK=$RG_ZONE
        fi ;;
      *)                                         # the crontab of the VM was not changed
        WHY="vm $RG_VM: the crontab lines were not registered (ssh exit $RG_R: $(first "$W/reg.out"))" ;;
    esac
    break
  done
  RG_EHAVE=; RG_EUNK=; RG_EZONE=
  if [ -z "$WHY" ] && has EMS; then
    RG_L=$((RG_F - 15 - $(now)))
    if [ "$RG_L" -lt 5 ]; then
      WHY="esx $CHF: no time was left to register its lines 15 s before the first start time $(tstr "$RG_F")"
    else
      [ "$RG_L" -gt 60 ] && RG_L=60
      mk_esxcron "$W/ereg.sh" "EMS:$TMON" ${MSTOP:+EMX:$MSTOP}
      esxrun "$W/ereg.sh" "$W/ereg.out" "$RG_L" $((RG_F - 15))
      RG_R=$?
      RG_C=$(tval TCGEN-CRON "$W/ereg.out")
      case "$RG_C" in
        ok) RG_EHAVE=1 ;;
        late*) WHY="esx $CHF: the lines arrived less than 15 s before the first start time $(tstr "$RG_F") (clock of the ESX host)" ;;
        "")
          grep -q '^TCGEN-HAD ' "$W/ereg.out" 2>/dev/null && RG_EZONE=1
          if [ "$RG_R" = 124 ]; then
            WHY="esx $CHF: no answer within the $RG_L s that were left to register the lines 15 s before the first start time $(tstr "$RG_F")"
            RG_EUNK=1
          else
            WHY="esx $CHF: the crontab lines were not registered (ssh exit $RG_R: $(first "$W/ereg.out"))"
            RG_EUNK=$RG_EZONE
          fi ;;
        *) WHY="esx $CHF: the crontab lines were not registered (ssh exit $RG_R: $(first "$W/ereg.out"))" ;;
      esac
    fi
  fi
  CRON_DL=
  if [ -z "$WHY" ]; then
    # the job is registered: its claim stays when the run ends
    [ "$(cat "$MD/$TJOB/who" 2>/dev/null)" = "$ME" ] && : > "$MD/$TJOB/reg" 2>/dev/null
    say "$SEQ" >> "$W/reg.ok"
    return 0
  fi
  # not registered. The lines are removed again: first on the VMs that took them in time, then on the VM that took
  # them late, last on the VM that did not answer (it is not known whether that one has the lines)
  UR_VMS=$(say $RG_HAVE $RG_SLOW $RG_UNK)
  UR_NOESX=1
  if [ -n "$RG_EHAVE" ] || [ -n "$RG_EUNK" ]; then UR_NOESX=; fi
  UR_BAD=
  : > "$W/unreg.clock"
  if [ -n "$UR_VMS" ] || [ -z "$UR_NOESX" ]; then
    [ -n "$UR_VMS" ] || UR_VMS=-
    unregister
  fi
  UR_VMS=; UR_NOESX=
  # what is known about every host that was asked:
  #  - the lines could not be removed (UR_BAD): they may still be there
  #  - it had the lines at the first start time (RG_HALF): what was to start then may have started there
  #  - it gave no result while it was taking the lines, and had none when tcrun removed them (RG_LATER): they may
  #    still arrive
  RG_HALF=; RG_LATER=
  rm -f "$W/jobs.note"
  for RG_X in $RG_HAVE $RG_SLOW $RG_UNK @esx; do
    case " $UR_BAD " in
      *" $RG_X "*)
        [ "$RG_X" = "@esx" ] && continue
        if [ "$RG_X" = "$RG_UNK" ]; then
          say "$RG_X It is not known whether the lines are on this VM: it did not answer. Run sh $D/tcrun cancel $CHF" >> "$W/jobs.note"
        else
          say "$RG_X The lines could NOT be removed on this VM again: what they start still starts. Run sh $D/tcrun cancel $CHF" >> "$W/jobs.note"
        fi
        continue ;;
    esac
    RG_K=$(grep "^$(bre "$RG_X") " "$W/unreg.clock" 2>/dev/null | tail -n 1 | cut -d' ' -f2)
    RG_H=$(grep "^$(bre "$RG_X") " "$W/unreg.clock" 2>/dev/null | tail -n 1 | cut -d' ' -f3)
    [ -n "$RG_K" ] || continue
    if [ "$RG_H" = 0 ]; then
      if [ "$RG_X" = "@esx" ]; then
        [ -n "$RG_EZONE" ] && RG_LATER="$RG_LATER @esx"
      elif [ "$RG_X" = "$RG_ZONE" ]; then
        RG_LATER="$RG_LATER $RG_X"
        say "$RG_X This VM gave no answer while it was taking the lines, and it had none when tcrun removed them at $(tstr "$RG_K") (clock of the VM): they may still arrive. Look at its crontab, or run sh $D/tcrun cancel $CHF" >> "$W/jobs.note"
      fi
      continue
    fi
    [ "$RG_K" -ge "$RG_F" ] || continue
    RG_HALF="$RG_HALF $RG_X"
    [ "$RG_X" = "@esx" ] && continue
    say "$RG_X The lines were removed on this VM at $(tstr "$RG_K") (clock of the VM), after the first start time: what was to start then may have started on this VM" >> "$W/jobs.note"
  done
  if [ -n "$RG_HALF" ]; then
    log "WARN case $CNAME is not registered, but its lines were still on$(say "$RG_HALF" | sed "s/@esx/esx $CHF/") at the first start time $(tstr "$RG_F"): what was to start then may have started there (see $JOBS)"
    if [ "$HASMON" = 1 ]; then
      CM_ONLY=$RG_HALF
      cancel_mon_stop
      CM_ONLY=
    fi
  fi
  grep -v "^$SEQ " "$W/times" > "$W/times.new"
  cat "$W/times.new" > "$W/times"
  rm -f "$W/times.new"
  RG_KEEP=$(say $UR_BAD $RG_LATER)
  if [ -n "$RG_KEEP" ]; then
    # lines may be on a host, or may still arrive there: the case stays listed, so that cancel and the next run remove
    # them. Its start time stays recorded: the job may still start there
    say "$SEQ $RG_F $TJOB $(say "$RG_KEEP" | tr ' ' ',')" >> "$W/times"
    [ "$(cat "$MD/$TJOB/who" 2>/dev/null)" = "$ME" ] && : > "$MD/$TJOB/reg" 2>/dev/null
    [ -n "$UR_BAD" ] && log "WARN case $CNAME is not registered, but its lines could not be removed on:$(say "$UR_BAD" | sed "s/@esx/esx $CHF/"). What they start still starts there. Remove them: sh $D/tcrun cancel $CHF"
    [ -n "$RG_LATER" ] && log "WARN case $CNAME is not registered, but$(say "$RG_LATER" | sed "s/@esx/esx $CHF/") gave no answer while it was taking the lines: they may still arrive there. Look at its crontab, or run sh $D/tcrun cancel $CHF"
  else
    release_start "$TJOB" "$ME"
  fi
  return 1
}
register_part() {   # register the lines of every case of the part that starts at case $SEQ, at once. Every case gets its
                    # rows in the registration record. The values of the first case are loaded again at the end.
                    # RP_NOW = the case whose lines are being registered (for on_signal)
  RP_FIRST=$SEQ; RP_N=0; RP_OK=0; RP_CHG=$CHG_AT; RP_NOW=
  STEP=register; setstate running "registering the crontab lines of case $CNAME and of the cases after it with the same settings"
  for RP_S in $(seg_list "$RP_FIRST"); do
    ctx "$RP_S"
    ME="run $RUNID, esx $CHF, case $CNAME"
    say "$RP_S" >> "$W/jobs.done"
    [ -n "$JOBVMS" ] || continue
    RP_N=$((RP_N + 1))
    if [ -z "$AT" ]; then
      NREGBAD=$((NREGBAD + 1))
      log "WARN not registered: case $CNAME has no start time in the plan"
      jobs_rows "not registered (no start time in the plan)"
      continue
    fi
    RP_NOW=$RP_S
    if register_case; then
      RP_NOW=
      RP_OK=$((RP_OK + 1))
      RP_NOTE=
      if [ -n "$RP_CHG" ] && [ $((TJOB - RP_CHG)) -lt $((PREP * 60 - 90)) ]; then
        RP_NOTE="settings changed $(( (TJOB - RP_CHG) / 60 )) min before the job start. The setting is $PREP min"
      fi
      if [ -n "$RG_MLATE" ]; then
        RP_NOTE="${RP_NOTE:+$RP_NOTE. }monitoring starts $(( (TJOB - TMON) / 60 )) min before the job start. The setting is $LEAD min"
      fi
      [ -n "$RP_NOTE" ] && RP_NOTE=" ($RP_NOTE)"
      log "registered: case $CNAME, job start $ATTEXT${TMON:+, monitoring start $(tstr "$TMON")}${MSTOP:+, monitoring stop $(tstr "$MSTOP")}, VMs: $(say $REGVMS)$RP_NOTE"
      jobs_rows "registered$RP_NOTE"
    else
      RP_NOW=
      NREGBAD=$((NREGBAD + 1))
      log "WARN not registered: case $CNAME, job start $ATTEXT: $WHY"
      jobs_rows "not registered ($WHY)"
    fi
  done
  NREG=$((NREG + RP_OK))
  ctx "$RP_FIRST"
  ME="run $RUNID, esx $CHF, case $CNAME"
  log "registration: $RP_OK of $RP_N case(s) of this part are in crontab now. Record: $JOBS"
}
jobs_rest() {       # the run ends before every case was registered: the record gets rows for the cases that were not reached
                    # (once: a case that has its rows is not written again)
  JT_KEEP=$SEQ; JT_C=$CNAME
  for JT_S in $SEQS; do
    grep -qx "$JT_S" "$W/jobs.done" 2>/dev/null && continue
    say "$JT_S" >> "$W/jobs.done"
    SEQ=$JT_S
    CNAME=$(cname "$SEQ")
    JOBVMS=$(grep "^JOB $SEQ " "$PLAN" | cut -d' ' -f3)
    ATTEXT=$(clines START | head -n 1 | cut -d' ' -f2-)
    jobs_rows "not registered (the run ended as $HOW: $HOW_TEXT)"
  done
  if [ -n "$JT_KEEP" ]; then ctx "$JT_KEEP"; else CNAME=$JT_C; fi
}
run_case() {
  STEP=begin
  R_DONE=; R_APPLY=none; R_BEGIN=$(stamp); R_VMSON=; R_MON=; R_JOB=; R_SEEN=; R_END=; R_STOP=; R_STATUS=
  TMON=; TJOB=; T_ON=; OFF=0; WHY=; PV_NEW=; PV_OFFED=; CHG_AT=
  if ! names_ok; then
    VMS=; OFFVMS=; ONVMS=; REGVMS=; JOBVMS=
    pause "the plan holds a name that tcrun does not accept in case number $SEQ. Generate again in tcgen. Nothing was done."
    return 1
  fi
  ctx "$SEQ"
  ME="run $RUNID, esx $CHF, case $CNAME"          # owner of the job start time of this case (claim_start)
  log "==== case $CNAME ($IDX of $TOTAL) begin. VMs: $(say $VMS)${OFFVMS:+ (powered off for this case: $(say $OFFVMS))}${AT:+. Job start $ATTEXT}"
  setstate running "case started"
  if [ -n "$JOBVMS" ] && [ -z "$AT" ]; then
    pause "the plan has no start time for case $CNAME. Generate again in tcgen. Nothing of this case was done."
    return 1
  fi
  if [ "$SEG" = 1 ]; then
    RC_CHG=; RC_SKIP=
    need_change
    RC_N=$?
    if [ "$RC_N" = 2 ]; then
      pause "$WHY. Nothing of case $CNAME was done."
      return 1
    fi
    [ "$RC_N" = 0 ] && RC_CHG=1
    # when: a case with a job PREP min before its job start. A case without a job at the time of the plan (APPLYAT)
    RC_W=
    [ -n "$AT" ] && RC_W=$((AT / 60 * 60 - PREP * 60))
    [ -z "$JOBVMS" ] && RC_W=$APPLYAT
    RC_WT=
    [ -n "$RC_W" ] && RC_WT=" at $(tstr "$RC_W")"
    [ -n "$RC_W" ] && [ -n "$AT" ] && RC_WT="$RC_WT ($PREP min before the job start $ATTEXT)"
    [ "$IDX" = 1 ] || TAKE=
    if [ -n "$RC_CHG" ]; then
      # no VM is shut down for jobs that cannot start any more: the next case with other settings is compared again
      RC_LAST=$(part_last "$SEQ")
      if [ -n "$RC_LAST" ] && [ "$(now)" -ge $((RC_LAST / 60 * 60 - 60)) ]; then
        log "WARN settings and VM power: $NC_T. Nothing is changed: the job start of every case with these settings has passed or is less than 1 min away"
        RC_CHG=; RC_SKIP=1
      fi
    fi
    if [ -n "$RC_CHG" ]; then
      if [ -n "$RC_W" ] && [ "$(now)" -lt "$RC_W" ]; then
        log "settings and VM power: $NC_T. They are changed$RC_WT"
        STEP=wait-prepare
        setstate running "waiting until $(tstr "$RC_W") to change the settings and the VM power for case $CNAME${AT:+ (job start $ATTEXT)}"
        if ! wait_until "$RC_W"; then
          stopped "before the settings of case $CNAME were changed. Nothing of this case was done."
          return 1
        fi
      elif [ -n "$AT" ] && [ "$(now)" -lt $((AT / 60 * 60 - 60)) ]; then
        log "WARN settings and VM power: $NC_T. The time to change them ($(tstr "$RC_W"), $PREP min before the job start $ATTEXT) has passed: they are changed now, $(( (AT / 60 * 60 - $(now)) / 60 )) min before the job start. The start time is kept."
      elif [ -n "$AT" ]; then
        log "WARN settings and VM power: $NC_T. The job start $ATTEXT of case $CNAME has passed or is less than 1 min away: they are changed now for the cases after it with the same settings."
      else
        log "settings and VM power: $NC_T. They are changed now"
      fi
      [ -n "$TAKE" ] && take_over
      CHG_AT=$(now)
      if has AH || has AV; then
        apply_settings || return 1
        if [ -f "$STOPF" ]; then
          if [ "$R_APPLY" = changed ]; then
            stopped "after the settings of case $CNAME were applied. Its VMs are powered off and were not powered on again."
          else
            stopped "after the settings of case $CNAME were compared (nothing was changed)"
          fi
          return 1
        fi
      fi
      if [ "$CH" != "-" ] && [ -n "$VMS" ]; then
        power_vms || return 1
      fi
      last_on
    else
      [ -n "$RC_SKIP" ] || log "settings and VM power: already as wanted. Nothing is changed for case $CNAME"
      # lines that an earlier run left: only those of cases that have the name of a case of this run are removed now
      # (this run registers lines with the same mark). The others are removed PREP min before the first job start
      if [ -n "$TAKE" ]; then
        take_over names
        TAKE=$(open_runs done)
      fi
    fi
    if [ -f "$STOPF" ]; then
      stopped "before the lines of case $CNAME were registered"
      return 1
    fi
    register_part
    if [ -n "$TAKE" ]; then
      if [ -n "$RC_W" ] && [ "$(now)" -lt "$RC_W" ]; then
        STEP=wait-cleanup
        setstate running "waiting until $(tstr "$RC_W") to remove the crontab lines that run(s) $(say $TAKE) left${AT:+ ($PREP min before the job start $ATTEXT of case $CNAME)}"
        if ! wait_until "$RC_W"; then
          stopped "before the crontab lines that run(s) $(say $TAKE) left were removed. The crontab lines of this run stay registered: its jobs start at their times. To remove them: sh $D/tcrun cancel $CHF"
          return 1
        fi
      fi
      take_over
    fi
  fi
  if [ -z "$JOBVMS" ]; then
    R_STATUS="done (settings only)"; write_result
    log "==== case $CNAME done (settings only, no job)"
    return 0
  fi
  TJOB=$((AT / 60 * 60))
  R_JOB=$(tstr "$TJOB")
  if [ "$HASMON" = 1 ]; then                      # the monitoring start that was registered (times: case, first start, job start)
    RC_M=$(grep "^$SEQ " "$W/times" 2>/dev/null | head -n 1 | cut -d' ' -f2)
    case "$RC_M" in ""|*[!0-9]*) RC_M=$((TJOB - LEAD * 60)) ;; esac
    R_MON=$(tstr "$RC_M")
  fi
  [ -n "$MSTOP" ] && R_STOP=$(tstr "$MSTOP")
  if ! grep -qx "$SEQ" "$W/reg.ok" 2>/dev/null; then
    R_JOB=; R_MON=; R_STOP=
    R_STATUS="not registered"; write_result
    log "==== case $CNAME done: NOT registered (see the WARN line above and $JOBS)"
    return 0
  fi
  # config snapshot: SNAPLEAD min before the job start of every case (user decision 2026-11-18)
  if [ -n "$VMS" ]; then
    RC_S=$((TJOB - SNAPLEAD * 60))
    if [ "$(now)" -lt "$RC_S" ]; then
      STEP=wait-snapshot
      setstate running "waiting until $(tstr "$RC_S") to take the config snapshot of case $CNAME ($SNAPLEAD min before the job start $ATTEXT)"
      if ! wait_until "$RC_S"; then
        stopped "before the config snapshot of case $CNAME. Its crontab lines stay registered: the job starts at $ATTEXT. To remove them: sh $D/tcrun cancel $CHF"
        return 1
      fi
    fi
    if [ "$(now)" -ge $((TJOB - 60)) ]; then
      log "WARN config snapshot of case $CNAME not taken: the job start $ATTEXT is less than 1 min away or has passed (no login to the VMs while a job runs)"
    else
      SN_WHY="before the job of this case"
      [ "$R_APPLY" = changed ] && SN_WHY="$SN_WHY. Settings changed by this case"
      [ -n "$PV_NEW" ] && SN_WHY="$SN_WHY. Powered on by this case:$PV_NEW"
      [ -n "$PV_OFFED" ] && SN_WHY="$SN_WHY. Powered off by this case:$PV_OFFED"
      snapshot
    fi
  fi
  R_STATUS=registered; write_result; write_report
  log "==== case $CNAME done. job start $R_JOB (crontab)"
  return 0
}

on_signal() {       # the runner is ended by a signal (sh tcrun cancel sends TERM). The login that runs is ended, and the
                    # registration record gets rows for the cases that were not registered
  [ -n "$G_P" ] && kill "$G_P" 2>/dev/null
  log "ended by a signal"
  OS_C=$CNAME
  HOW=stopped; HOW_TEXT="ended by a signal"
  if [ -f "$RD/$CHF.cancelling" ]; then HOW=cancelled; HOW_TEXT="sh tcrun cancel"; fi
  if [ -n "$JOBS" ] && [ -n "$SEQS" ]; then
    rm -f "$W/jobs.note"
    if [ -n "$RP_NOW" ]; then
      SEQ=$RP_NOW; RP_NOW=
      CNAME=$(cname "$SEQ")
      JOBVMS=$(grep "^JOB $SEQ " "$PLAN" | cut -d' ' -f3)
      ATTEXT=$(clines START | head -n 1 | cut -d' ' -f2-)
      jobs_rows "not registered (the run ended as $HOW: $HOW_TEXT, while the lines of this case were being registered)"
    fi
    SEQ=
    jobs_rest
  fi
  CNAME=$OS_C
  release_mine "$CHF"
  setstate stopped "ended by a signal"
  exit 1
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
  JOBS=$LOGDIR/tcrun-$RUNID-jobs.csv
  SEQS=$(cseqs "$CH")
  TOTAL=$(say "$SEQS" | grep -c .)
  [ "$TOTAL" -gt 0 ] || { say "tcrun: the plan has no case for $CH"; exit 2; }
  : >> "$LOG" || { say "tcrun: cannot write the log: $LOG"; exit 2; }
  for CR_X in "$RD" "$LOGDIR"; do
    can_write "$CR_X" || { say "tcrun: 1 MB cannot be written in $CR_X (disk full or disk quota, $(free_mb "$CR_X") MB free on the disk)"; exit 2; }
  done
  # one runner at a time works for an ESX host: under the lock, note the runners of this ESX host that are alive now
  # (this run waits for them in the queue), then write the pid file of this runner
  QWAIT=
  lock_md; CR_LK=$?
  for CR_P in "$D"/runs/*/"$CHF".pid; do
    [ "$CR_P" = "$PIDF" ] && continue
    alive "$CR_P" || continue
    CR_P=${CR_P%/*}
    QWAIT="$QWAIT ${CR_P##*/}"
  done
  say $$ > "$PIDF"
  [ "$CR_LK" = 0 ] && unlock_md
  CNAME=; IDX=0; STEP=start; HOW=; HOW_TEXT=; TAKE=; R_DONE=; NREG=0; NREGBAD=0; CR_ALL=
  trap 'rm -f "$PIDF"' 0
  trap 'on_signal' 2 15
  trap '' 1
  EADDR=$(esxaddr "$CH")
  if [ "$CH" != "-" ] && ! addr_ok "$EADDR"; then say "tcrun: bad ESX address in the plan for $CH"; exit 2; fi
  ssh_setup
  [ -s "$RES" ] || say "case,esx,apply,begin,vms_on,monitor_start,job_start,first_seen,job_end,monitor_stop,end,status" > "$RES"
  jobs_head
  log "tcrun $TCRUN_VERSION run $RUNID, esx $CHF ($EADDR), $TOTAL case(s), pid $$"
  old_claims
  claim_plan_starts "$CH" | while IFS= read -r CR_LINE; do log "$CR_LINE"; done
  CR_GO=1
  if [ -n "$QWAIT" ]; then
    queue_wait || CR_GO=
  fi
  if [ -n "$CR_GO" ]; then
    CR_B=$(host_block)
    if [ -n "$CR_B" ]; then
      R_DONE=1
      pause "$CR_B. Nothing of this run was started."
      CR_GO=
    fi
  fi
  if [ -n "$CR_GO" ]; then
    TAKE=$(open_runs done)
    [ -n "$TAKE" ] && log "run(s) $(say $TAKE) left crontab lines on the hosts of this ESX host. They are removed $PREP min before the first job start of this run"
  fi
  log "settings: start times of the plan are not changed. Settings and VM power of a case that differs from the hosts are changed $PREP min before its job start, config snapshot $SNAPLEAD min before every job start, monitoring start $LEAD min before the job start, power on wait $PON_WAIT min, boot wait $BOOT_WAIT min, shutdown wait $SHUT_WAIT min"
  [ -n "$CR_GO" ] && setstate running "started"
  for SEQ in ${CR_GO:+$SEQS}; do
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
    # the runner stays until 1 min after the last job start: a run that is queued behind it removes its lines then
    CR_LAST=$(cut -d' ' -f3 "$W/times" 2>/dev/null | sort -n | tail -n 1)
    case "$CR_LAST" in ""|*[!0-9]*) CR_LAST= ;; esac
    if [ -n "$CR_LAST" ] && [ "$(now)" -lt $((CR_LAST + 60)) ]; then
      STEP=wait-last-job
      setstate running "every case is handled. Waiting until 1 min after the last job start ($(tstr "$CR_LAST")), then this runner ends"
      if ! wait_until $((CR_LAST + 60)); then
        CR_ALL=1
        stopped "while the runner waited for the last job start. The crontab lines stay registered: the jobs start at their times. To remove them: sh $D/tcrun cancel $CHF"
      fi
    fi
  fi
  if [ -z "$HOW" ]; then
    STEP=end
    log "run finished: $TOTAL case(s), $NREG job case(s) registered, $NREGBAD not registered"
    CR_T="all $TOTAL case(s) finished: $NREG job case(s) registered"
    [ "$NREGBAD" -gt 0 ] && CR_T="$CR_T, $NREGBAD NOT registered (see $JOBS)"
    setstate done "$CR_T. The crontab lines stay on the hosts until the next run of this ESX host registers its first lines, or sh $D/tcrun cancel $CHF"
    return 0
  fi
  jobs_rest
  CR_I=0; CR_REST=
  for CR_S in $SEQS; do
    CR_I=$((CR_I + 1))
    [ "$CR_I" -ge "$IDX" ] && CR_REST="$CR_REST $(cname "$CR_S")"
  done
  if [ -n "$CR_ALL" ]; then
    log "run ended ($HOW) after every case was handled: $NREG job case(s) registered, $NREGBAD not registered (see $JOBS)"
  elif [ "$IDX" = 0 ]; then
    log "run ended ($HOW) before its first case. Not finished:$CR_REST"
    log "the cases that are registered start at their times (see $JOBS). To go on with the others: sh $D/tcrun cancel $CHF (removes the lines that are left), fix the cause, generate again in tcgen and paste block S"
  else
    log "run ended ($HOW) at case $CNAME. Not finished:$CR_REST"
    log "the cases that are registered start at their times (see $JOBS). To go on with the others: sh $D/tcrun cancel $CHF (removes the lines that are left), fix the cause, generate again in tcgen and paste block S"
  fi
  setstate "$HOW" "$HOW_TEXT"
  return 1
}

# ---------------------------------------------------------------- runs of the same ESX host: queue, lines left on the hosts
# One runner at a time works for an ESX host. A run that is started while another run still works for the same ESX host
# waits in the queue and starts when that run has ended as done.
# <run>/<host>.work/times holds one line "<case number> <first start time> <job start>" for every case whose crontab
# lines are on the hosts, from the registration until the lines are removed (by the next run or by cancel).
# A fourth field lists the hosts where the lines could not be removed ("vm1,vm2,@esx"): only those still have them.
run_end() {         # run_end <run id> <ESX host of the plan>: when the runner of that run for the ESX host ends: 1 min
                    # after its latest job start (epoch; "" when its plan has no job start for the host)
  RE_M=
  for RE_S in $(grep "^CASE [0-9]* $(bre "$2") " "$D/runs/$1/plan" 2>/dev/null | cut -d' ' -f2); do
    RE_T=$(grep "^START $RE_S " "$D/runs/$1/plan" 2>/dev/null | head -n 1 | cut -d' ' -f3)
    case "$RE_T" in ""|*[!0-9]*) continue ;; esac
    if [ -z "$RE_M" ] || [ "$RE_T" -gt "$RE_M" ]; then RE_M=$RE_T; fi
  done
  [ -n "$RE_M" ] && say $((RE_M / 60 * 60 + 60))
  return 0
}
state_of() {        # state_of <run dir> <host file name>: state of that runner: running, queued, done, paused, stopped, cancelled,
                    # died (the state says running or queued, but the runner is gone) or none
  SO_S=$(grep '^state=' "$1/$2.state" 2>/dev/null | cut -d= -f2-)
  case "$SO_S" in
    running|queued) alive "$1/$2.pid" || SO_S=died ;;
    "") SO_S=none ;;
  esac
  say "$SO_S"
}
open_case_name() {  # open_case_name <run dir> <host file name>: the cases of that run whose crontab lines are on the hosts:
                    # the name of the first one and how many more ("" when there is none)
  OC_N=0; OC_FIRST=
  for OC_Q in $(cut -d' ' -f1 "$1/$2.work/times" 2>/dev/null); do
    case "$OC_Q" in ""|*[!0-9]*) continue ;; esac
    OC_N=$((OC_N + 1))
    [ -n "$OC_FIRST" ] && continue
    OC_FIRST=$(grep "^CASE $OC_Q " "$1/plan" 2>/dev/null | head -n 1 | cut -d' ' -f4)
    [ -n "$OC_FIRST" ] || OC_FIRST="number $OC_Q"
  done
  [ "$OC_N" -gt 0 ] || return 0
  if [ "$OC_N" = 1 ]; then say "$OC_FIRST"; else say "$OC_FIRST and $((OC_N - 1)) more"; fi
}
open_runs() {       # open_runs [done]: run ids (other than this run) whose runner for ESX host $CHF has ended with a case open.
                    # "done": only those that ended as done (a case that was not checked and has no end time)
  for OR_F in "$D"/runs/*/"$CHF".work/times; do
    [ -s "$OR_F" ] || continue
    OR_D=${OR_F%/*}; OR_D=${OR_D%/*}
    [ "${OR_D##*/}" = "$RUNID" ] && continue
    alive "$OR_D/$CHF.pid" && continue
    if [ "$1" = done ] && [ "$(state_of "$OR_D" "$CHF")" != done ]; then continue; fi
    say "${OR_D##*/}"
  done
}
host_block() {      # prints why run $RUNID must not be started for ESX host $CH now (prints nothing when it may start).
                    # Needs PLAN, RUNID, CH, CHF. A run that ended as done may have left lines whose times have passed:
                    # the next run removes them (take_over). A run that ended in another way may have left lines of jobs
                    # that have not started yet: those must be removed first
  for HB_R in $(open_runs); do
    HB_D=$D/runs/$HB_R
    HB_S=$(state_of "$HB_D" "$CHF")
    [ "$HB_S" = done ] && continue
    say "run $HB_R of this ESX host ended as $HB_S and crontab lines of its case(s) are still on the hosts ($(open_case_name "$HB_D" "$CHF")): their jobs may still start. Remove them first: sh $D/tcrun cancel $CHF"
    return 0
  done
  return 0
}
queue_wait() {      # wait until the runners of this ESX host that were alive when this one started ($QWAIT) have ended.
                    # 1 = this run does not start (stop request, or one of them did not end as done)
  STEP=queued
  log "queued: run(s)$QWAIT still work for this ESX host. This run starts when they have ended as done"
  for QW_R in $QWAIT; do
    QW_E=$(run_end "$QW_R" "$CH")
    setstate queued "waiting for run $QW_R of this ESX host to end${QW_E:+ (about $(tstr "$QW_E"), 1 min after its last job start)}"
    while alive "$D/runs/$QW_R/$CHF.pid" && [ ! -f "$STOPF" ]; do
      sleep 5
    done
    if [ -f "$STOPF" ]; then                      # looked at first: a stop request often ends both runners at once
      R_DONE=1
      stopped "while this run was queued behind run $QW_R. Nothing of this run was started."
      return 1
    fi
    QW_S=$(state_of "$D/runs/$QW_R" "$CHF")
    if [ "$QW_S" != done ]; then
      R_DONE=1
      pause "run $QW_R of this ESX host ended as $QW_S, not as done. Nothing of this run was started. Look at that run (sh $D/tcrun status), then generate again in tcgen."
      return 1
    fi
    log "queued: run $QW_R ended as done"
  done
  return 0
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
  JOBS=$LOGDIR/tcrun-$RUNID-jobs.csv
  jobs_head
  old_claims
  for CS_CH in $(chains); do
    CS_F=$(chfile "$CS_CH")
    if [ -f "$RD/$CS_F.state" ] || [ -f "$RD/$CS_F.pid" ]; then
      say "==== $CS_F: run $RUNID was already started on this jump VM. Not started again. Generate again in tcgen to run again."
      continue
    fi
    CH=$CS_CH; CHF=$CS_F
    CS_B=$(host_block)
    if [ -n "$CS_B" ]; then
      say "==== $CS_F: not started. $CS_B"
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
    if [ -f "$RD/$CS_F.state" ] && [ "$(state_of "$RD" "$CS_F")" = queued ]; then
      say "queued:  run $RUNID esx $CS_F   $(grep '^text=' "$RD/$CS_F.state" | cut -d= -f2-). It starts when that has ended as done   log: $LOGDIR/tcrun-$RUNID-$CS_F.log"
    elif [ -f "$RD/$CS_F.state" ]; then
      say "started: run $RUNID esx $CS_F   log: $LOGDIR/tcrun-$RUNID-$CS_F.log"
    else
      say "==== $CS_F: the runner did not start:"
      sed 's/^/     /' "$RD/$CS_F.out" 2>/dev/null
      release_mine "$CS_F"
    fi
  done
  say "status: sh $D/tcrun status       stop: sh $D/tcrun stop       cancel: sh $D/tcrun cancel"
  say "files in $LOGDIR : tcrun-$RUNID-jobs.csv (which jobs are registered in crontab), tcrun-$RUNID-<esx>.log, -result.csv, -perfreport_cases.csv, -config-<case number>-<case>.csv (config snapshot)"
}
cmd_status() {
  CT_ALL=0
  [ "$1" = "-a" ] && CT_ALL=1
  [ -d "$D/runs" ] || { say "no run on this jump VM yet"; return 0; }
  latest_run
  CT_LAST=$LR
  CT_N=0
  for CT_D in $(ls "$D/runs" 2>/dev/null | sort); do
    CT_RUNLIVE=0                                 # a run with a runner that is alive is shown with all its ESX hosts
    for CT_F in "$D/runs/$CT_D"/*.pid; do
      alive "$CT_F" && CT_RUNLIVE=1
    done
    for CT_F in "$D/runs/$CT_D"/*.state; do
      [ -f "$CT_F" ] || continue
      CT_X=$(basename "$CT_F" .state)
      CT_S=$(grep '^state=' "$CT_F" | cut -d= -f2-)
      CT_LIVE=0
      alive "$D/runs/$CT_D/$CT_X.pid" && CT_LIVE=1
      if { [ "$CT_S" = running ] || [ "$CT_S" = queued ]; } && [ "$CT_LIVE" = 0 ]; then
        CT_S="died (the runner is not running any more. see $D/runs/$CT_D/$CT_X.out)"
      fi
      CT_OPEN=
      [ "$CT_LIVE" = 0 ] && CT_OPEN=$(open_case_name "$D/runs/$CT_D" "$CT_X")
      [ "$CT_ALL" = 1 ] || [ "$CT_D" = "$CT_LAST" ] || [ "$CT_RUNLIVE" = 1 ] || [ -n "$CT_OPEN" ] || continue
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
      if [ -n "$CT_OPEN" ]; then
        say "   crontab lines on the hosts: case $CT_OPEN. To remove them: sh $D/tcrun cancel $CT_X"
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
    say "stop requested: run $(basename "$CP_D") esx $CP_X. The runner ends at its next check."
    CP_N=$((CP_N + 1))
  done
  [ "$CP_N" -gt 0 ] || say "no running tcrun${1:+ for $1}"
  [ "$CP_N" -gt 0 ] && say "stop ends the runners only: crontab lines that are registered stay, so those jobs still start at their times, and monitoring that runs stays. To remove the lines: sh $D/tcrun cancel [esx]"
  latest_run
  if [ -n "$LR" ] && [ -f "$D/runs/$LR/plan" ]; then
    PLAN=$D/runs/$LR/plan
    for CP_C in $(chains); do
      CP_X=$(chfile "$CP_C")
      [ -z "$1" ] || [ "$1" = "$CP_X" ] || continue
      alive "$D/runs/$LR/$CP_X.pid" && continue
      CP_S=$(state_of "$D/runs/$LR" "$CP_X")
      [ "$CP_S" = none ] && CP_S=
      CP_T=$(grep '^text=' "$D/runs/$LR/$CP_X.state" 2>/dev/null | cut -d= -f2-)
      [ "$CP_S" = died ] && CP_T="the runner is gone. Its last state: $CP_T"
      CP_O=$(open_case_name "$D/runs/$LR" "$CP_X")
      say "run $LR esx $CP_X: not running (${CP_S:-not started}${CP_T:+: $CP_T})${CP_O:+. Crontab lines of case $CP_O are on the hosts: to remove them: sh $D/tcrun cancel $CP_X}"
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

# ---------------------------------------------------------------- cancel, removing the lines of a run
csay() { [ -n "$CC_SAY" ] && say "$*"; return 0; }
latest_run() {      # LR = the latest run on this jump VM that has a state file (the run that status shows first)
  LR=
  for LR_D in $(ls "$D/runs" 2>/dev/null | sort); do
    for LR_F in "$D/runs/$LR_D"/*.state; do
      [ -f "$LR_F" ] && LR=$LR_D
      break
    done
  done
}
cancel_mon_stop() { # run the monitoring stop commands of case $SEQ now, once (VM lines: \% of the crontab form stands for %).
                    # CM_ONLY: when set, only on these VMs ("@esx": the ESX host)
  for CM_VM in $REGVMS; do
    vhas MX "$CM_VM" || continue
    if [ -n "$CM_ONLY" ]; then
      case " $CM_ONLY " in *" $CM_VM "*) ;; *) continue ;; esac
    fi
    vlines MX "$CM_VM" | sed 's/\\%/%/g' > "$W/mx.sh"
    vmrun "$(target "$CM_VM")" "$W/mx.sh" "$W/mx.out" 120
    CM_R=$?
    log "monitoring stop commands run on vm $CM_VM (exit $CM_R)"
    logf '    | ' "$W/mx.out"
    csay "   monitoring stop: vm $CM_VM (exit $CM_R)"
  done
  if [ -n "$CM_ONLY" ]; then
    case " $CM_ONLY " in *" @esx "*) ;; *) return 0 ;; esac
  fi
  if has EMX; then
    clines EMX > "$W/emx.sh"
    esxrun "$W/emx.sh" "$W/emx.out" 120
    CM_R=$?
    log "monitoring stop commands run on esx $CHF (exit $CM_R)"
    logf '    | ' "$W/emx.out"
    csay "   monitoring stop: esx $CHF (exit $CM_R)"
  fi
  return 0
}
close_case() {      # close_case: remove the crontab lines of every case of the loaded run that are on the hosts for ESX host
                    # $CH. The monitoring of a case is stopped when it has started and no stop was registered for a time
                    # that has passed. CC_SAY=1: print to the terminal too. CC_BY: who closes (for the record).
                    # CC_ONLY: when set, only the cases with one of these names (the others keep their lines).
                    # Needs PLAN, RD, CH, CHF, EADDR, W, LOG. 1 = no case had lines. CC_N = how many cases were closed,
                    # CC_BAD = how many cases still have lines on a host that could not be reached (they stay in the list)
  CC_F=$RD/$CHF.work/times
  CC_N=0; CC_BAD=0; OFF=0
  [ -s "$CC_F" ] || return 1
  cp "$CC_F" "$W/times.close" 2>/dev/null || return 1
  : > "$W/times.keep"
  for CC_Q in $(cut -d' ' -f1 "$W/times.close"); do
    case "$CC_Q" in ""|*[!0-9]*) continue ;; esac
    SEQ=$CC_Q
    names_ok || continue
    ctx "$SEQ"
    if [ -n "$CC_ONLY" ]; then
      case " $CC_ONLY " in
        *" $CNAME "*) ;;
        *) grep "^$CC_Q " "$W/times.close" >> "$W/times.keep"; continue ;;
      esac
    fi
    CC_MON=$(grep "^$CC_Q " "$W/times.close" | head -n 1 | cut -d' ' -f2)
    case "$CC_MON" in ""|*[!0-9]*) CC_MON= ;; esac
    # the fourth field: the hosts where an earlier attempt could not remove the lines (only those are left)
    CC_L=$(grep "^$CC_Q " "$W/times.close" | head -n 1 | cut -d' ' -f4 | tr ',' ' ')
    UR_VMS=; UR_NOESX=
    if [ -n "$CC_L" ]; then
      UR_NOESX=1
      for CC_X in $CC_L; do
        if [ "$CC_X" = "@esx" ]; then UR_NOESX=; else UR_VMS="$UR_VMS $CC_X"; fi
      done
      [ -n "$UR_VMS" ] || UR_VMS=-
    fi
    unregister
    UR_VMS=; UR_NOESX=
    rm -f "$W/jobs.note"
    for CC_X in $JOBVMS; do
      case " $UR_BAD " in
        *" $CC_X "*) say "$CC_X =NOT removed on this VM (${CC_BY:-closed}: the VM could not be reached): its job still starts at $ATTEXT. Run sh $D/tcrun cancel $CHF again" >> "$W/jobs.note" ;;
        *) if [ -n "$CC_L" ]; then case " $CC_L " in *" $CC_X "*) ;; *) say "$CC_X -" >> "$W/jobs.note" ;; esac; fi ;;
      esac
    done
    if [ -n "$UR_BAD" ]; then
      # the lines are still on a host: the case stays in the list, so that cancel can be run again
      say "$CC_Q ${CC_MON:-0} $(grep "^$CC_Q " "$W/times.close" | head -n 1 | cut -d' ' -f3) $(say $UR_BAD | tr ' ' ',')" >> "$W/times.keep"
      CC_BAD=$((CC_BAD + 1))
      csay "   crontab lines of case $CNAME NOT removed on:$(say "$UR_BAD" | sed "s/@esx/esx $CHF/") (job start $ATTEXT). What they start still starts there."
    else
      CC_N=$((CC_N + 1))
      csay "   crontab lines of case $CNAME removed (job start $ATTEXT)"
    fi
    if [ "$HASMON" = 1 ] && [ -n "$CC_MON" ] && [ "$(now)" -ge $((CC_MON - 60)) ]; then
      if [ -z "$MSTOP" ] || [ "$(now)" -lt "$MSTOP" ]; then
        CM_ONLY=$CC_L
        cancel_mon_stop
        CM_ONLY=
      fi
    fi
    if [ -n "$JOBS" ] && [ -n "$AT" ] && [ "$(now)" -lt "$AT" ]; then
      jobs_rows "removed before the job start (${CC_BY:-closed})"
    fi
    rm -f "$W/jobs.note"
  done
  cat "$W/times.keep" > "$CC_F"
  [ $((CC_N + CC_BAD)) -gt 0 ] || return 1
  return 0
}
close_open() {      # close_open <run id> <closed by> [names]: remove the lines that an ended run left on the hosts of this
                    # ESX host (names given: only the lines of the cases with one of these names).
                    # Runs in a subshell with the plan of that run, so that nothing of the calling runner changes
  (
    CO_W=$W; CO_LOG=$LOG
    load_plan "$D/runs/$1/plan"
    W=$CO_W; LOG=$CO_LOG
    JOBS=
    CC_ONLY=$3
    EADDR=$(esxaddr "$CH")
    if [ "$CH" != "-" ] && ! addr_ok "$EADDR"; then
      log "WARN the plan of run $1 holds an ESX address that tcrun does not accept: its lines were not removed"
      exit 0
    fi
    CC_SAY=; CC_BY=$2
    SEQS=$(cseqs "$CH")
    if close_case; then
      [ "$CC_N" -gt 0 ] && log "the crontab lines of $CC_N case(s) of run $1 are removed"
      [ "$CC_BAD" -gt 0 ] && log "WARN the crontab lines of $CC_BAD case(s) of run $1 could not be removed on every host (see the WARN lines above). The next run of this ESX host tries again. sh $D/tcrun cancel $CHF removes them too, and it ends the run that works for this ESX host"
      if [ ! -s "$RD/$CHF.work/times" ]; then
        ST=$RD/$CHF.state
        TOTAL=$(say "$SEQS" | grep -c .); IDX=$TOTAL; STEP=end; CNAME=
        setstate done "all $TOTAL case(s) finished. The crontab lines of the run were removed $(stamp) by $2"
      fi
    fi
  )
}
take_over() {       # take_over [names]: remove the lines that earlier runs of this ESX host left on the hosts (their job
                    # start times have passed: a runner ends 1 min after its last job start).
                    # "names": only the lines of cases that have the name of a case of this run and ESX host (this run
                    # registers lines with the same mark). Without it: every line that is left, and TAKE is emptied
  if [ "$1" = names ]; then
    TO_N=
    for TO_S in $SEQS; do TO_N="$TO_N $(cname "$TO_S")"; done
    for TO_R in $TAKE; do
      close_open "$TO_R" "run $RUNID, which has a case with the same name" "$TO_N"
    done
    return 0
  fi
  for TO_R in $TAKE; do
    [ -s "$D/runs/$TO_R/$CHF.work/times" ] || continue
    log "run $TO_R left crontab lines of its case(s) on the hosts of this ESX host. Removing them now"
    close_open "$TO_R" "run $RUNID"
  done
  TAKE=
}
cancel_chain() {    # cancel the runner of run $RUNID for ESX host $CH (running or queued) and remove the lines of its cases
  ST=$RD/$CHF.state; PIDF=$RD/$CHF.pid; STOPF=$RD/$CHF.stop
  LOG=$LOGDIR/tcrun-$RUNID-$CHF.log; RES=$LOGDIR/tcrun-$RUNID-$CHF-result.csv
  JOBS=$LOGDIR/tcrun-$RUNID-jobs.csv
  EADDR=$(esxaddr "$CH")
  SEQS=$(cseqs "$CH")
  CN_S=$(state_of "$RD" "$CHF")
  CNAME=$(grep '^case=' "$ST" 2>/dev/null | cut -d= -f2-)
  STEP=$(grep '^step=' "$ST" 2>/dev/null | cut -d= -f2-)
  IDX=$(grep '^pos=' "$ST" 2>/dev/null | cut -d= -f2- | cut -d/ -f1); TOTAL=$(say "$SEQS" | grep -c .)
  if [ "$CH" != "-" ] && ! addr_ok "$EADDR"; then say "== run $RUNID esx $CHF: bad ESX address in the plan. Not cancelled."; return 1; fi
  if [ "$CN_S" = done ]; then
    say "== run $RUNID esx $CHF: removing the crontab lines that this run left on the hosts"
  else
    say "== run $RUNID esx $CHF: cancelling (state $CN_S, case ${CNAME:-none}, step ${STEP:-none})"
  fi
  W=$RD/$CHF.cancel.$$
  mkdir -p "$W" || { say "   cannot create $W"; return 1; }
  if alive "$PIDF"; then
    CN_P=$(cat "$PIDF")
    : > "$RD/$CHF.cancelling"                      # tells the runner why it is ended (rows of the registration record)
    : > "$STOPF"
    kill -s TERM -- "-$CN_P" 2>/dev/null || kill -s TERM "$CN_P" 2>/dev/null
    CN_I=0
    while alive "$PIDF" && [ "$CN_I" -lt 30 ]; do sleep 1; CN_I=$((CN_I + 1)); done
    if alive "$PIDF"; then kill -s KILL -- "-$CN_P" 2>/dev/null || kill -s KILL "$CN_P" 2>/dev/null; fi
    rm -f "$RD/$CHF.cancelling"
    say "   the runner (pid $CN_P) was ended at step ${STEP:-?}"
    case "$STEP" in
      apply|poweron|poweroff|rollback)
        say "   WARN it was changing settings or VM power on the ESX host. That task goes on to its end on the host: check the VMs" ;;
    esac
  fi
  log "CANCEL requested (sh tcrun cancel). state was $CN_S, case ${CNAME:-none}, step ${STEP:-none}"
  CC_SAY=1; CC_BY="sh tcrun cancel"; CC_ONLY=
  close_case
  if [ "$CC_BAD" -gt 0 ]; then
    say "   WARN the crontab lines of $CC_BAD case(s) could NOT be removed on every host (see the lines above and $LOG). Run sh $D/tcrun cancel $CHF again when the hosts can be reached."
  fi
  if [ "$CC_N" -gt 0 ]; then
    say "   the crontab lines of $CC_N case(s) were removed: their jobs that have not started will not start. Jobs that run now are not touched."
  elif [ "$CC_BAD" = 0 ]; then
    say "   no crontab lines of this run are registered on the hosts"
  fi
  CC_SAY=
  # job start times of this run and ESX host that have not come yet
  CN_N=$(now)
  for CN_D in "$MD"/[0-9]*; do
    [ -d "$CN_D" ] || continue
    CN_T=$(basename "$CN_D")
    case "$CN_T" in *[!0-9]*) continue ;; esac
    [ "$CN_T" -gt "$CN_N" ] || continue
    case "$(cat "$CN_D/who" 2>/dev/null)" in "run $RUNID, esx $CHF, "*) rm_claim "$CN_T" ;; esac
  done
  rm -f "$W"/* 2>/dev/null
  rmdir "$W" 2>/dev/null
  CN_L="Crontab lines removed"
  [ "$CC_BAD" -gt 0 ] && CN_L="Crontab lines of $CC_BAD case(s) are STILL on a host that could not be reached: run sh $D/tcrun cancel $CHF again"
  if [ "$CN_S" = done ]; then
    IDX=$TOTAL; STEP=end; CNAME=
    if [ "$CC_BAD" -gt 0 ]; then
      log "WARN not every crontab line of this run could be removed (sh tcrun cancel)"
      setstate done "all $TOTAL case(s) finished. $CN_L"
    else
      log "the crontab lines of this run were removed (sh tcrun cancel)"
      setstate done "all $TOTAL case(s) finished. The crontab lines of the run were removed $(stamp) by sh tcrun cancel"
    fi
    say "   done. The run stays done."
    return 0
  fi
  log "CANCELLED. Settings that this run applied stay as they are."
  setstate cancelled "cancelled at $(stamp) by sh tcrun cancel. $CN_L. Settings that the run applied stay as they are"
  say "   cancelled. Settings that this run applied stay as they are."
}
cmd_cancel() {      # cancel [esx]: every runner that is running or queued, and every case that an ended run left open.
                    # The newest run first, so that a queued run is ended before the run it waits for
  CN_K=0
  for CN_RID in $(ls "$D/runs" 2>/dev/null | sort -r); do
    CN_PL=$D/runs/$CN_RID/plan
    [ -f "$CN_PL" ] || continue
    CN_HIT=
    for CN_F in "$D/runs/$CN_RID"/*.pid; do
      alive "$CN_F" && CN_HIT=1
    done
    for CN_F in "$D/runs/$CN_RID"/*.work/times; do
      [ -s "$CN_F" ] && CN_HIT=1
    done
    [ -n "$CN_HIT" ] || continue
    [ "$(head -n 1 "$CN_PL")" = "TCGEN-PLAN 2" ] || continue
    load_plan "$CN_PL"
    [ -n "$SO" ] || ssh_setup
    for CH in $(chains); do
      CHF=$(chfile "$CH")
      [ -z "$1" ] || [ "$1" = "$CHF" ] || continue
      alive "$RD/$CHF.pid" || [ -s "$RD/$CHF.work/times" ] || continue
      CN_K=$((CN_K + 1))
      cancel_chain
    done
  done
  [ "$CN_K" -gt 0 ] || say "nothing to cancel${1:+ for $1}: no run is running or queued, and no crontab lines of a run are on the hosts"
  return 0
}

# ---------------------------------------------------------------- check (reads only)
ck_bad() { BAD=$((BAD + 1)); say "  [PROBLEM] $*"; : > "$W/bad.$BAD"; }
ck_ok() { say "  [ok] $*"; }
ck_vm() {           # login, clock and crontab of one VM (one login per ssh target: $W/done.vm holds "<target> <ok|fail>")
  CV_T=$(target "$1")
  CV_P=$(grep "^$(bre "$CV_T") " "$W/done.vm" 2>/dev/null | head -n 1 | cut -d' ' -f2)
  if [ -z "$CV_P" ]; then
    CV_J=$(now)
    vmrun "$CV_T" "$W/probe.sh" "$W/probe.out" 60
    CV_R=$?
    CV_C=$(tval TCGEN-CLOCK "$W/probe.out")
    case "$CV_C" in
      ""|*[!0-9]*)
        say "$CV_T fail" >> "$W/done.vm"
        ck_bad "vm $1 ($CV_T): ssh login failed (exit $CV_R: $(first "$W/probe.out"))"
        return 0 ;;
    esac
    CV_P=ok
    say "$CV_T $CV_P" >> "$W/done.vm"
    ck_ok "vm $1 ($CV_T): ssh login works, clock $((CV_C - CV_J)) s ahead of the jump VM, tcgen lines in crontab: $(tval TCGEN-LINES "$W/probe.out")"
    say "$((CV_C - CV_J))" >> "$W/clocks"
    [ "$(tval TCGEN-CRONTAB "$W/probe.out")" = yes ] || ck_bad "vm $1: crontab command not found"
    case "$(tval TCGEN-GCMD "$W/probe.out")" in
      yes) ck_ok "vm $1: the file of the command at the config snapshot is there ($(gcmd_file))" ;;
      no) ck_bad "vm $1: the file of the command at the config snapshot is not there or cannot be read: $(gcmd_file). Check the command in the settings area of tcgen" ;;
    esac
  fi
  return 0
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
  ck_ok "start times: the jobs are registered for the start times of the plan. tcrun does not change them"
  ck_ok "settings and VM power: a case that differs from the hosts is changed $PREP min before its job start (VMs shut down, settings written, VMs powered on). Then its lines and the lines of the cases after it with the same settings are registered at once"
  ck_ok "config snapshot: taken $SNAPLEAD min before the job start of every case"
  if [ -n "$(gcmd)" ]; then
    ck_ok "command at the config snapshot (run on every VM of a case that is powered on, before the job): $(gcmd)"
  else
    ck_ok "command at the config snapshot: none (the guest settings are not recorded in VCF Operations by the run)"
  fi
  CB_OPS=
  : > "$W/done.ops"
  if ops_on; then
    if ops_login; then
      CB_OPS=1
      ck_ok "VCF Operations $(ops_get HOST): login works (account $(ops_get USER)). At the config snapshot of every case the VM settings and the host options of the case are written as properties of the VM and host objects"
    else
      ck_bad "VCF Operations: $OPS_WHY"
    fi
  else
    ck_ok "VCF Operations: no address in the settings of tcgen. The VM settings are not written to VCF Operations"
  fi
  for CH in $(chains); do
    CHF=$(chfile "$CH"); EADDR=$(esxaddr "$CH")
    say ""
    say "== ESX host $CHF"
    # other runs of this ESX host: a run that still works here (this run is queued behind it), a case left open
    CB_BLK=$(host_block)
    [ -n "$CB_BLK" ] && ck_bad "esx $CHF: block S would not start this run. $CB_BLK"
    CB_BUSY=$(running_for "$CHF")
    CB_END=
    if [ -n "$CB_BUSY" ]; then
      CB_END=$(run_end "$CB_BUSY" "$CH")
      ck_ok "esx $CHF: run $CB_BUSY still works for this ESX host${CB_END:+ until about $(tstr "$CB_END") (1 min after its last job start)}. Block S puts this run in the queue: its lines are registered when that run has ended as done. This check does not log in to the VMs of this ESX host (a job may run)"
      say "       that run now: case $(grep '^case=' "$D/runs/$CB_BUSY/$CHF.state" 2>/dev/null | cut -d= -f2-), $(grep '^text=' "$D/runs/$CB_BUSY/$CHF.state" 2>/dev/null | cut -d= -f2-)"
    else
      CB_BUSY=$(open_runs done | head -n 1)
      if [ -n "$CB_BUSY" ] && [ -z "$CB_BLK" ]; then
        ck_ok "esx $CHF: crontab lines of run $CB_BUSY are still on the hosts (case $(open_case_name "$D/runs/$CB_BUSY" "$CHF")). This run removes them $PREP min before its first job start. This check does not log in to the VMs of this ESX host (a job may run)"
      elif [ -n "$CB_BLK" ]; then
        CB_BUSY=$(open_runs | head -n 1)
      fi
    fi
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
    CB_IDX=0
    for SEQ in $(cseqs "$CH"); do
      CNAME=$(cname "$SEQ")
      CB_IDX=$((CB_IDX + 1))
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
          ck_ok "start time: job at $(clines START | head -n 1 | cut -d' ' -f2-) ($(( (CB_AT - $(now)) / 60 )) min from now)"
          if [ -n "$CB_END" ] && [ $((CB_AT / 60 * 60)) -lt $((CB_END + 120)) ]; then
            ck_bad "case $CNAME: its job start is less than 2 min after the end of run $CB_BUSY of this ESX host (about $(tstr "$CB_END")). The lines of this run are registered after that end, so this case would not be registered. To run it: sh $D/tcrun cancel $CHF, then generate the cases that are left of that run together with this one."
          fi
          if has MS || has EMS; then
            CB_MS=$(clines MSTOP | head -n 1 | cut -d' ' -f2-)
            ck_ok "monitoring: starts $LEAD min before the job start. Stop: ${CB_MS:-not registered (last case without a maximum wait: the monitoring goes on until the next run or sh $D/tcrun cancel $CHF)}"
          fi
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
            ck_bad "case $CNAME: the start time $(clines START | head -n 1 | cut -d' ' -f2-) is less than $SPACING min from the job start $(tstr "$CB_T") of ${CB_W:-another runner}. Job starts on this jump VM are $SPACING min or more apart: change the start time in tcgen and generate again."
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
            wouldchange)
              CB_PW=1
              ck_ok "settings and VM power: $(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f2-) (compared with the hosts as they are now). The run changes them $PREP min before the job start of the case (when a value differs, the VMs of the case are shut down first)."
              # the first case of the ESX host is the one that is compared with the hosts as the run will find them
              if [ "$CB_IDX" = 1 ] && [ -n "$CB_AT" ] && [ "$CB_AT" -gt "$(now)" ] && [ "$(now)" -ge $((CB_AT / 60 * 60 - PREP * 60)) ]; then
                ck_bad "case $CNAME: its settings or VM power differ from the host, and the time to change them ($(tstr $((CB_AT / 60 * 60 - PREP * 60))), $PREP min before the job start) has passed. Set a later start time in tcgen and generate again. (When block S is pasted as it is, they are changed at once and the job starts at its time.)"
              fi
              ;;
            *) ck_bad "settings and VM power: $(tval TCGEN-RESULT "$W/apply.out")" ;;
          esac
        else
          ck_bad "settings and VM power of case $CNAME not checked (no ESX login)"
        fi
      fi
      if [ -n "$CB_OPS" ] && [ "$CH" != "-" ]; then      # the objects that the properties are written to
        for CB_VM in $VMS; do
          grep -qx "vm $CB_VM" "$W/done.ops" && continue
          say "vm $CB_VM" >> "$W/done.ops"
          if [ -n "$(ops_find VirtualMachine "$CB_VM")" ]; then
            ck_ok "vm $CB_VM: VCF Operations has a VirtualMachine object with this name"
          else
            ck_bad "vm $CB_VM: VCF Operations has no VirtualMachine object with this name. Its VM settings would not be written"
          fi
        done
        if has AH && ! grep -qx "esx $CH" "$W/done.ops"; then
          say "esx $CH" >> "$W/done.ops"
          CB_I=$(ops_find HostSystem "$CH")
          if [ -z "$CB_I" ] && [ "$EADDR" != "$CH" ]; then CB_I=$(ops_find HostSystem "$EADDR"); fi
          if [ -n "$CB_I" ]; then
            ck_ok "esx $CHF: VCF Operations has a HostSystem object for this host"
          else
            ck_bad "esx $CHF: VCF Operations has no HostSystem object named $CH (or $CH.<domain>). The host options would not be written"
          fi
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
        if [ -n "$CB_BUSY" ]; then
          ck_ok "vm $CB_VM: not logged in to by this check (run $CB_BUSY, see above). The run looks at it when it starts."
          continue
        fi
        ck_vm "$CB_VM"
      done
    done
  done
  rm -f "$W/ops.out" "$W/ops.props" "$W/ops.cfg"
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
