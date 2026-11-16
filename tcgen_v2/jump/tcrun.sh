#!/bin/sh
# tcrun 2.3 - tcgen sequence runner. Runs on the jump VM (POSIX sh). tcgen block A installs it as ~/.tcgen/tcrun.
#
#   sh ~/.tcgen/tcrun check <plan>        read-only checks: ESX key login, tools, VMs, settings, VM login, clocks
#   sh ~/.tcgen/tcrun start <plan>        start "run" in the background for every ESX host of the plan (block S uses this)
#   sh ~/.tcgen/tcrun run <plan> <esx>    run the cases of one ESX host in the order of the table
#   sh ~/.tcgen/tcrun status [-a]         state of the latest run and of every run that is still running (-a: all runs)
#   sh ~/.tcgen/tcrun stop [esx]          ask the running runs to stop (flag file). Running jobs are not touched.
#   sh ~/.tcgen/tcrun key                 print the line that registers the ssh key of this jump VM on an ESX host
#   sh ~/.tcgen/tcrun version
#
# The plan is read with grep, sed and cut only. No text of the plan is given to eval or to a shell on this machine.
# Commands of the plan go byte for byte into crontab lines (quoted here-documents on the remote side).
# VM login: same way as ~/.tcgen/vmssh (password from ~/.tcgen/vmpw through SSH_ASKPASS, setsid).
# ESX login: ~/.tcgen/esxssh (key ~/.tcgen/id_esx, no password).

TCRUN_VERSION=2.3
D=$HOME/.tcgen
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
  grep -E "^(AV|PAT|MS|JOB|PROC|MX) $SEQ " "$PLAN" | cut -d' ' -f3 | grep -q -v '^[A-Za-z0-9._][A-Za-z0-9._-]*$' && return 1
  grep "^PROC $SEQ " "$PLAN" | grep -q "'" && return 1
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
  [ "$POLL" -ge 1 ] || POLL=60
  [ "$GONE" -ge 1 ] || GONE=3
  [ "$PON_WAIT" -ge 1 ] || PON_WAIT=90
  RD=$D/runs/$RUNID
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
first() { head -n 1 "$1" 2>/dev/null | cut -c1-200; }

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
  printf '%s,%s,%s,%s\r\n' "$(clines RPTA)" "$R_JOB" "$R_END" "$(clines RPTB)" >> "$RPT"
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

# ---------------------------------------------------------------- step 1: settings
power_on() {        # power_on <vm>: power on through the ESX host, then wait until ssh login to the VM works
  STEP=poweron; setstate running "power on $1"
  mk_apply "$W/pon.sh" poweron "$1" $((PON_WAIT * 60))
  esxrun "$W/pon.sh" "$W/pon.out" $((PON_WAIT * 60 + 300))
  logf '    | ' "$W/pon.out"
  if [ "$(tval TCGEN-RESULT "$W/pon.out" | cut -d' ' -f1)" != poweredon ]; then
    WHY="power on of vm $1 failed: $(tval TCGEN-RESULT "$W/pon.out")"
    return 1
  fi
  PO_T=$(target "$1"); PO_S=$(now); PO_E=$((PO_S + BOOT_WAIT * 60))
  say 'echo TCGEN-UP' > "$W/up.sh"
  setstate running "vm $1 is powered on. waiting for ssh login"
  while :; do
    vmrun "$PO_T" "$W/up.sh" "$W/up.out" 40
    if grep -q '^TCGEN-UP' "$W/up.out"; then
      log "vm $1: ssh login works $(($(now) - PO_S)) s after power on"
      return 0
    fi
    if [ "$(now)" -ge "$PO_E" ]; then
      WHY="vm $1 did not accept ssh login within $BOOT_WAIT min after power on ($(first "$W/up.out"))"
      return 1
    fi
    sleep 10
  done
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
apply_settings() {
  STEP=apply; setstate running "comparing and applying settings on $CHF"
  TAG=$RUNID-$SEQ
  mk_spec
  log "settings wanted by this case:"
  logf '    | ' "$W/spec"
  mk_apply "$W/apply.sh" prepare "$TAG"
  esxrun "$W/apply.sh" "$W/apply.out" $((SHUT_WAIT * 60 + 900))
  AP_R=$?
  logf '    | ' "$W/apply.out"
  AP_W=$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f1)
  case "$AP_W" in
    nochange)
      R_APPLY=nochange
      log "settings: every value is already as wanted. The VMs are not touched."
      return 0
      ;;
    changed)
      R_APPLY=changed
      ;;
    *)
      AP_T=$(tval TCGEN-RESULT "$W/apply.out")
      [ -n "$AP_T" ] || AP_T="no answer from the ESX host (ssh exit $AP_R: $(first "$W/apply.out"))"
      log "settings: not applied: $AP_T"
      apply_undo
      pause "settings of case $CNAME were not applied: $AP_T"
      return 1
      ;;
  esac
  log "settings: applied. Powering on the VMs one after another: $(say $VMS)"
  for AP_VM in $VMS; do
    if ! power_on "$AP_VM"; then
      log "$WHY"
      AP_T=$WHY
      apply_undo
      pause "$AP_T. The settings of case $CNAME were put back."
      return 1
    fi
  done
  AP_ON=$(now)
  for AP_VM in $VMS; do say "$AP_ON" > "$W/on.$AP_VM"; done
  log "settings: done, every VM of the case accepts ssh login. Power on finished at $(tstr "$AP_ON")"
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
    if [ "$HASMON" = 1 ]; then TMON=$RG_T; TJOB=$((RG_T + RG_L)); else TMON=; TJOB=$RG_T; fi
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
      pause "$WHY. The lines of case $CNAME were removed, nothing was started."
      return 1
    fi
    RG_N=$(($(now) + OFF)); RG_F=${TMON:-$TJOB}
    [ "$RG_N" -le $((RG_F - 15)) ] && break
    # too late: cron may not run a line that arrives less than 15 s before its minute. Take every line out again,
    # so that the case never starts on only some of its hosts.
    log "WARN registering ended $((RG_F - RG_N)) s before the first start time $(tstr "$RG_F"). Removing the lines of this case."
    unregister
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
  [ -n "$TMON" ] && R_MON=$(tstr "$TMON")
  R_JOB=$(tstr "$TJOB")
  if [ -n "$TMON" ]; then
    log "times: monitoring start $R_MON, job start $R_JOB ($LEAD min later, every VM in the same minute)"
  else
    log "times: job start $R_JOB (every VM in the same minute, no monitoring)"
  fi
  return 0
}

# ---------------------------------------------------------------- step 4: end of the job
poll_all() {
  ALLSEEN=1; ALLGONE=1; PSUM=; NOTSEEN=
  for PA_VM in $JOBVMS; do
    mk_poll "$PA_VM" "$W/poll.sh"
    vmrun "$(target "$PA_VM")" "$W/poll.sh" "$W/poll.out" 60
    PA_R=$?
    PA_N=$(tval TCGEN-PROC "$W/poll.out" | tr -d ' ')
    case "$PA_N" in ""|*[!0-9]*) PA_N=unknown ;; esac
    PA_S=$(cat "$W/seen.$PA_VM" 2>/dev/null)
    PA_Z=$(cat "$W/zero.$PA_VM" 2>/dev/null)
    PA_L=$(cat "$W/last.$PA_VM" 2>/dev/null)
    : "${PA_Z:=0}"
    if [ "$PA_N" = unknown ]; then
      PA_Z=0
      [ "$PA_L" = unknown ] || log "poll: vm $PA_VM unknown (ssh exit $PA_R: $(first "$W/poll.out")). Not counted as ended."
    elif [ "$PA_N" -gt 0 ]; then
      if [ -z "$PA_S" ]; then
        PA_S=$(now)
        say "$PA_S" > "$W/seen.$PA_VM"
        log "poll: vm $PA_VM process seen for the first time ($PA_N)"
      elif [ "$PA_L" != "$PA_N" ]; then
        log "poll: vm $PA_VM $PA_N process(es)"
      fi
      PA_Z=0
    elif [ -n "$PA_S" ]; then
      PA_Z=$((PA_Z + 1))
      [ "$PA_Z" = 1 ] && now > "$W/gone.$PA_VM"
      [ "$PA_Z" -le "$GONE" ] && log "poll: vm $PA_VM no process ($PA_Z of $GONE in a row)"
    fi
    say "$PA_Z" > "$W/zero.$PA_VM"
    say "$PA_N" > "$W/last.$PA_VM"
    if [ -z "$PA_S" ]; then ALLSEEN=0; NOTSEEN="$NOTSEEN $PA_VM"; fi
    [ "$PA_Z" -ge "$GONE" ] || ALLGONE=0
    PSUM="$PSUM $PA_VM=$PA_N"
  done
}
wait_job() {
  STEP=wait-start; setstate running "lines registered. job start $R_JOB"
  WJ_FIRST=$((TJOB - MINOFF + 20))
  if ! wait_until "$WJ_FIRST"; then
    WJ_N=$(($(now) + OFF))
    if [ "$WJ_N" -lt $((TJOB - 5)) ]; then
      log "stop requested before the job start: removing the lines of this case"
      unregister
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
  STEP=wait-job
  WJ_FAST=$((WJ_FIRST + 180))
  WJ_SW=$((TJOB - MINOFF + START_WAIT * 60))
  WJ_MAX=$(clines MAXWAIT | head -n 1)
  case "$WJ_MAX" in ""|*[!0-9]*) WJ_MAX=0 ;; esac
  [ "$WJ_MAX" -gt 0 ] && WJ_MAX=$((TJOB - MINOFF + WJ_MAX * 60))
  while :; do
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
    setstate running "waiting for the job to end. processes:$PSUM"
    if [ "$WJ_N" -lt "$WJ_FAST" ]; then WJ_I=15; else WJ_I=$POLL; fi
    if ! nap "$WJ_I"; then
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
  TMON=; TJOB=; T_ON=; OFF=0; MINOFF=0; WHY=
  rm -f "$W"/seen.* "$W"/zero.* "$W"/gone.* "$W"/last.*
  if ! names_ok; then
    VMS=; REGVMS=; JOBVMS=
    pause "the plan holds a name that tcrun does not accept in case number $SEQ. Generate again in tcgen. Nothing was done."
    return 1
  fi
  VMS=$(grep "^VM $SEQ " "$PLAN" | cut -d' ' -f3)
  REGVMS=$(grep "^PAT $SEQ " "$PLAN" | cut -d' ' -f3)
  JOBVMS=$(grep "^PROC $SEQ " "$PLAN" | cut -d' ' -f3)
  HASMON=0
  if grep -q "^MS $SEQ " "$PLAN" || has EMS; then HASMON=1; fi
  log "==== case $CNAME ($IDX of $TOTAL) begin. VMs: $(say $VMS)"
  setstate running "case started"
  if has AH || has AV; then
    apply_settings || return 1
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
  say $$ > "$PIDF"
  CNAME=; IDX=0; STEP=start; HOW=; HOW_TEXT=
  trap 'rm -f "$PIDF"' 0
  trap 'log "ended by a signal"; setstate stopped "ended by a signal"; exit 1' 2 15
  trap '' 1
  EADDR=$(esxaddr "$CH")
  if [ "$CH" != "-" ] && ! addr_ok "$EADDR"; then say "tcrun: bad ESX address in the plan for $CH"; exit 2; fi
  ssh_setup
  [ -s "$RES" ] || say "case,esx,apply,begin,vms_on,monitor_start,job_start,first_seen,job_end,monitor_stop,end,status" > "$RES"
  log "tcrun $TCRUN_VERSION run $RUNID, esx $CHF ($EADDR), $TOTAL case(s), pid $$"
  log "settings: job starts $LEAD min after the monitoring start, poll every $POLL s, ended after $GONE polls without the process, start wait $START_WAIT min, power on wait $PON_WAIT min, boot wait $BOOT_WAIT min, shutdown wait $SHUT_WAIT min, job $SETTLE min or more after VMs were powered on again"
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
  CS_N=0; CS_L=
  for CS_CH in $(chains); do
    CS_F=$(chfile "$CS_CH")
    if [ -f "$RD/$CS_F.state" ] || [ -f "$RD/$CS_F.pid" ]; then
      say "==== $CS_F: run $RUNID was already started on this jump VM. Not started again. Generate again in tcgen to run again."
      continue
    fi
    CS_O=$(running_for "$CS_F")
    if [ -n "$CS_O" ]; then
      say "==== $CS_F: run $CS_O is still running for this ESX host. Not started. (sh ~/.tcgen/tcrun status)"
      continue
    fi
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
    fi
  done
  say "status: sh ~/.tcgen/tcrun status       stop: sh ~/.tcgen/tcrun stop"
  say "files in $LOGDIR : tcrun-$RUNID-<esx>.log, -result.csv, -perfreport_cases.csv"
}
cmd_status() {
  CT_ALL=0
  [ "$1" = "-a" ] && CT_ALL=1
  [ -d "$D/runs" ] || { say "no run on this jump VM yet"; return 0; }
  CT_LAST=
  for CT_D in $(ls "$D/runs" 2>/dev/null | sort); do
    for CT_F in "$D/runs/$CT_D"/*.state; do
      [ -f "$CT_F" ] && CT_LAST=$CT_D
      break
    done
  done
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
  [ "$CT_N" -gt 0 ] || say "no run on this jump VM yet"
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
}
cmd_key() {
  [ -s "$D/id_esx.pub" ] || { say "no key file $D/id_esx.pub. Paste block A of tcgen first."; return 1; }
  say "Paste this ONE line into the root SSH session of each ESX host (once per host):"
  say ""
  say "echo '$(cat "$D/id_esx.pub")' >> /etc/ssh/keys-root/authorized_keys"
  say ""
  say "Then check from this jump VM. It must print the host name without asking for a password:"
  say "sh ~/.tcgen/esxssh <esx host> hostname"
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
  [ -s "$D/vmpw" ] || ck_bad "the VM password is not saved: sh ~/.tcgen/setpw"
  for CB_X in ssh setsid; do
    command -v $CB_X >/dev/null 2>&1 || ck_bad "command not found on the jump VM: $CB_X"
  done
  if [ -d "$LOGDIR" ] && [ -w "$LOGDIR" ]; then ck_ok "log folder $LOGDIR"; else ck_bad "the log folder cannot be written: $LOGDIR"; fi
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
        if has AH || has AV || has EMS || has EMX; then CB_ESX=1; fi
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
          ck_bad "esx $CHF ($EADDR): ssh key login failed (exit $CB_R: $(first "$W/eprobe.out")). Register the key: sh ~/.tcgen/tcrun key"
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
    elif [ "$CB_ESX" = 0 ]; then
      say "  (no settings and no ESX monitoring in the cases of this host: no ESX login needed)"
    fi
    for SEQ in $(cseqs "$CH"); do
      CNAME=$(cname "$SEQ")
      if ! names_ok; then
        ck_bad "case number $SEQ: the plan holds a name that tcrun does not accept. Generate again in tcgen."
        continue
      fi
      VMS=$(grep "^VM $SEQ " "$PLAN" | cut -d' ' -f3)
      say "-- case $CNAME   VMs: $(say $VMS)"
      if has AH || has AV; then
        if [ "$CB_UP" = 1 ]; then
          mk_spec
          mk_apply "$W/apply.sh" check
          esxrun "$W/apply.sh" "$W/apply.out" 300
          sed 's/^/     /' "$W/apply.out"
          case "$(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f1)" in
            nochange) ck_ok "settings: every value is already as wanted. The VMs will not be restarted." ;;
            wouldchange) ck_ok "settings: $(tval TCGEN-RESULT "$W/apply.out" | cut -d' ' -f2-). The VMs of this case will be shut down and powered on." ;;
            *) ck_bad "settings: $(tval TCGEN-RESULT "$W/apply.out")" ;;
          esac
        else
          ck_bad "settings of case $CNAME not checked (no ESX login)"
        fi
      fi
      for CB_VM in $VMS; do ck_vm "$CB_VM"; done
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
  key) cmd_key ;;
  version) say "$TCRUN_VERSION" ;;
  *)
    say "tcrun $TCRUN_VERSION (tcgen sequence runner)"
    say "usage: sh ~/.tcgen/tcrun check <plan> | start <plan> | run <plan> <esx> | status [-a] | stop [esx] | key | version"
    exit 2
    ;;
esac
