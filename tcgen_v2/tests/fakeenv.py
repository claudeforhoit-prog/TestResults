# -*- coding: utf-8 -*-
"""
fakeenv.py - 순서 실행 시험용 가짜 환경 (시험 코드에서만 쓴다. 생성기와 점프 VM 스크립트는 이 파일을 모른다)

시험 폴더(/tmp 아래) 하나에 점프 VM, VM, ESX 호스트를 폴더로 만들고, 실제 명령 대신 가짜 명령을 PATH 앞에 둔다.

  fakebin/ssh       VM 대상: v1.4 시험과 같은 확인(제어 터미널 없음, askpass 비밀번호) 뒤 vms/<VM>/ 을 홈으로 원격 명령 실행.
                    ESX 대상(-i 키, -l root): esx/<호스트>/authorized 가 있을 때만 접속. 경로 환경 변수를 그 폴더로 맞춘다.
  fakebin/setsid    util-linux setsid 처럼 동작(-w 지원)
  fakebin/crontab   VM 의 crontab (파일 vms/<VM>/crontab.txt)
  fakebin/pgrep     vms/<VM>/proc/ 의 파일을 프로세스로 본다 (파일 이름 = pid, 내용 = 명령줄)
  fakebin/date, sleep   가상 시계. clock 파일의 배속으로 시간이 빨리 간다. tcrun 은 date 와 sleep 만으로 시간을 다루므로
                    tcrun 에는 시험용 코드가 없다.
  fakebin/vim-cmd, esxcli   Broadcom KB 에 나온 출력 형식을 흉내 낸다. 상태는 esx/<호스트>/vms.json, adv.json.
  fakebin/hostname  FAKE_HOST 값을 출력
  fakebin/ssh-keygen  키 파일 두 개를 만든다

CronSim 은 VM 의 crontab.txt 와 ESX 의 cron/root 에 등록된 줄을 가상 시계의 그 분에 실행한다.
VM 은 cronie 처럼 % 를 처리하고, ESX 는 busybox crond 처럼 cron.update 가 있을 때만 crontab 을 다시 읽는다.
"""
import csv
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import tcgen_core as core  # noqa: E402

PERL = shutil.which("perl")
HAVE = bool(os.name == "posix" and PERL and shutil.which("sh") and shutil.which("awk"))
TEST_PW = "test-pw!'$x \"q"
KST = dt.timezone(dt.timedelta(hours=9))
SPEED = float(os.environ.get("TCGEN_TEST_SPEED", "30"))

FAKE_SSH = r"""#!/bin/sh
KEY=; LOGIN=; QUIET=
while [ $# -gt 0 ]; do
  case "$1" in
    -G) echo "stricthostkeychecking accept-new"; exit 0 ;;
    -i) KEY=$2; shift 2 ;;
    -l) LOGIN=$2; shift 2 ;;
    -o) [ "$2" = LogLevel=ERROR ] && QUIET=1; shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
t=$1; shift
h=${t#*@}
if [ -n "$KEY" ] || [ "$LOGIN" = root ]; then
  [ -f "$KEY" ] || { echo "fake ssh: no key file '$KEY'" >&2; exit 255; }
  [ "$LOGIN" = root ] || { echo "fake ssh: ESX login must be root" >&2; exit 255; }
  n=`grep "^$h " "$FAKE_ROOT/esx_addr" 2>/dev/null | cut -d' ' -f2`
  [ -n "$n" ] && h=$n
  d=$FAKE_ROOT/esx/$h
  [ -d "$d" ] || { echo "ssh: Could not resolve hostname $h: Name or service not known" >&2; exit 255; }
  [ -f "$d/authorized" ] || { echo "root@$h: Permission denied (publickey,keyboard-interactive)." >&2; exit 255; }
  echo "esx $h $*" >> "$FAKE_ROOT/ssh_calls.txt"
  HOME=$d; TCGEN_TMP=$d/tmp; TCGEN_CRON_DIR=$d/cron; TCGEN_VMFS_ROOT=$d/vmfs; TCGEN_POLL_SEC=0.05
  TCGEN_LPAGE_POLL_SEC=`cat "$FAKE_ROOT/lpage_poll"`
  FAKE_ESX_HOME=$d; FAKE_HOST=$h; TZ=UTC; FAKE_CLOCK_OFFSET=`cat "$d/clock_offset" 2>/dev/null`
  export HOME TCGEN_TMP TCGEN_CRON_DIR TCGEN_VMFS_ROOT TCGEN_POLL_SEC TCGEN_LPAGE_POLL_SEC FAKE_ESX_HOME FAKE_HOST TZ FAKE_CLOCK_OFFSET
  cd "$d" || exit 1
  [ $# -gt 0 ] || exit 0
  exec "$@"
fi
if ( : < /dev/tty ) 2>/dev/null; then echo "fake ssh: controlling tty present" >&2; exit 254; fi
[ -t 0 ] && { echo "fake ssh: stdin is a tty" >&2; exit 253; }
[ -n "$SSH_ASKPASS" ] || { echo "fake ssh: SSH_ASKPASS not set" >&2; exit 255; }
pw=`"$SSH_ASKPASS" "$t's password: "`
[ "$pw" = "$FAKE_PW" ] || { echo "fake ssh: Permission denied" >&2; exit 255; }
d=$FAKE_ROOT/vms/$h
[ -d "$d" ] || { echo "ssh: Could not resolve hostname $h: Name or service not known" >&2; exit 255; }
if [ -f "$d/down" ]; then
  u=`cat "$d/down"`
  if [ -n "$u" ] && [ "`date +%s`" -ge "$u" ]; then rm -f "$d/down"; else echo "ssh: connect to host $h port 22: Connection refused" >&2; exit 255; fi
fi
if [ -s "$d/ssh_fail" ]; then
  n=`cat "$d/ssh_fail"`
  if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$d/ssh_fail"; echo "ssh: connect to host $h port 22: Connection timed out" >&2; exit 255; fi
fi
# login banner of sshd (Banner): the ssh client prints it unless LogLevel is ERROR or lower
[ -f "$FAKE_ROOT/banner" ] && [ -z "$QUIET" ] && cat "$FAKE_ROOT/banner" >&2
if [ -f "$d/deny" ]; then echo "deny $t" >> "$FAKE_ROOT/ssh_calls.txt"; echo "$t: Permission denied (publickey,password)." >&2; exit 255; fi
echo "vm $t" >> "$FAKE_ROOT/ssh_calls.txt"
# login_slow: the next login to this VM takes that many seconds before the command runs (a login that is ended while it
# waits runs nothing on the VM)
if [ -s "$d/login_slow" ]; then w=`cat "$d/login_slow"`; rm -f "$d/login_slow"; sleep "$w"; fi
HOME=$d; FAKE_CRONTAB=$d/crontab.txt; FAKE_HOST=$h; FAKE_CLOCK_OFFSET=`cat "$d/clock_offset" 2>/dev/null`
TCGEN_SYSROOT=$d/root
export HOME FAKE_CRONTAB FAKE_HOST FAKE_CLOCK_OFFSET TCGEN_SYSROOT
cd "$d" || exit 1
exec "$@"
"""

# 게스트 OS 의 설정 파일(구성 스냅샷이 읽는 /sys, /proc 의 파일). VM 폴더의 root/ 아래에 둔다(TCGEN_SYSROOT)
GUEST_FILES = {
    "sys/kernel/mm/transparent_hugepage/enabled": "always [madvise] never\n",
    "sys/kernel/mm/transparent_hugepage/defrag": "always defer defer+madvise [madvise] never\n",
    "sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs": "10000\n",
    "sys/kernel/mm/transparent_hugepage/khugepaged/pages_to_scan": "4096\n",
    "proc/sys/kernel/numa_balancing": "0\n",
    "sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages": "0\n",
    "sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages": "16\n",
    "proc/cmdline": "BOOT_IMAGE=(hd0,gpt2)/vmlinuz root=/dev/mapper/rhel-root ro default_hugepagesz=1G hugepagesz=1G hugepages=16\n",
}

FAKE_SETSID = r"""#!%s
use POSIX ();
my $w = 0;
if (@ARGV && ($ARGV[0] eq "-w" || $ARGV[0] eq "--wait")) { $w = 1; shift @ARGV; }
exit 2 unless @ARGV;
if (POSIX::setsid() < 0) {
  my $pid = fork();
  if ($pid) { if ($w) { waitpid($pid, 0); exit($? >> 8); } exit 0; }
  POSIX::setsid();
}
exec { $ARGV[0] } @ARGV;
exit 127;
""" % PERL

FAKE_CRONTAB = r"""#!/bin/sh
F=${FAKE_CRONTAB:?}
[ -f "$HOME/crontab_broken" ] && { echo "crontab: cannot be used" >&2; exit 1; }
case "$1" in
  -l) if [ -f "$F" ]; then cat "$F"; else echo "no crontab for user" >&2; exit 1; fi ;;
  -r) if [ -f "$F" ]; then rm -f "$F"; else echo "no crontab for user" >&2; exit 1; fi ;;
  -*) echo "crontab: bad option" >&2; exit 1 ;;
  *) awk 'NF && $1 !~ /^#/ && NF < 6 { bad = 1 } END { exit bad }' "$1" || { echo "crontab: errors in crontab file" >&2; exit 1; }
     # crontab_slow: the next crontab command of this VM takes that many seconds (it has read its file, then it hangs)
     T=$F.in.$$; cat "$1" > "$T"
     if [ -s "$HOME/crontab_slow" ]; then w=`cat "$HOME/crontab_slow"`; rm -f "$HOME/crontab_slow"; sleep "$w"; fi
     cat "$T" > "$F"; rm -f "$T" ;;
esac
"""

FAKE_PGREP = r"""#!/bin/sh
[ "$1" = "-f" ] && shift
[ "$1" = "--" ] && shift
[ -f "$HOME/pgrep_error" ] && { echo "pgrep: cannot read /proc" >&2; exit 3; }
found=1
for f in "$HOME"/proc/*; do
  [ -f "$f" ] || continue
  if grep -E -q -- "$1" "$f"; then basename "$f"; found=0; fi
done
exit $found
"""

FAKE_DATE = r"""#!%s
use Time::HiRes qw(time);
use POSIX qw(strftime floor);
open(my $f, "<", "$ENV{FAKE_ROOT}/clock") or die "no clock";
my ($r0, $v0, $sp) = split(" ", <$f>);
close($f);
my $now = floor($v0 + (time() - $r0) * $sp + ($ENV{FAKE_CLOCK_OFFSET} || 0));
my ($utc, $t, $fmt) = (0, undef, undef);
while (@ARGV) {
  my $a = shift @ARGV;
  if ($a eq "-u") { $utc = 1; }
  elsif ($a eq "-d") { my $v = shift @ARGV; ($v =~ s/^@//) or exit 1; $t = $v; }
  elsif ($a eq "-r") { $t = shift @ARGV; }
  elsif ($a =~ /^\+(.*)$/s) { $fmt = $1; }
  else { print STDERR "fake date: bad argument $a\n"; exit 1; }
}
$t = $now unless defined $t;
$fmt = "%%a %%b %%e %%H:%%M:%%S %%Z %%Y" unless defined $fmt;
if ($fmt eq "%%s") { print "$t\n"; exit 0; }
my @tm = $utc ? gmtime($t) : localtime($t);
print strftime($fmt, @tm), "\n";
""" % PERL

FAKE_SLEEP = r"""#!%s
open(my $f, "<", "$ENV{FAKE_ROOT}/clock") or die "no clock";
my ($r0, $v0, $sp) = split(" ", <$f>);
close($f);
select(undef, undef, undef, $ARGV[0] / $sp);
""" % PERL

FAKE_HOSTNAME = """#!/bin/sh
echo "${FAKE_HOST:-jump}"
"""

FAKE_KEYGEN = r"""#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -f) F=$2; shift ;; esac
  shift
done
[ -n "$F" ] || exit 1
echo "-----BEGIN FAKE PRIVATE KEY-----" > "$F"
echo "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQfakekey0123456789+/abc tester@jump" > "$F.pub"
"""

JOB_SH = r"""#!/bin/sh
# job.sh <process name> <seconds> : a fake job. While it runs, $HOME/proc/<pid> holds its command line.
mkdir -p "$HOME/proc"
echo "start $1 `date +%s`" >> "$HOME/job.log"
echo "/opt/sim/$1 --run case" > "$HOME/proc/$$"
sleep "$2"
rm -f "$HOME/proc/$$"
echo "end $1 `date +%s`" >> "$HOME/job.log"
"""

MON_SH = r"""#!/bin/sh
# mon.sh <start|stop> <tag> : a fake monitoring command
echo "$1 $2 `date +%s`" >> "$HOME/mon.log"
"""

# 구성 스냅샷 때 VM 마다 실행하는 명령의 가짜(게스트 구성을 VCF Operations 에 기록하는 guest_config_push 를 대신한다).
# 실행할 때마다 $HOME/gpush.log 에 한 줄을 적는다. $HOME/gpush_fail 이 있으면 실제 스크립트처럼 오류 줄을 내고 1 로 끝난다.
# $HOME/gpush_slow 에 초를 적어 두면 그만큼 걸린다(가상 시계의 초).
GPUSH_SH = r"""#!/bin/sh
echo "[guest_config_push] VM object: $FAKE_HOST   host: $FAKE_HOST"
echo "  THP enabled                        = (없음)"
if [ -s "$HOME/gpush_slow" ]; then sleep "`cat "$HOME/gpush_slow"`"; fi
if [ -f "$HOME/gpush_fail" ]; then
  echo "[ERROR] property write failed HTTP 500: 오류 internal error" >&2
  exit 1
fi
echo "push `date +%s`" >> "$HOME/gpush.log"
echo "[OPS] $FAKE_HOST Guest properties 10 recorded `date '+%Y-%m-%d %H:%M:%S'` KST"
"""

FAKE_VIMCMD = r'''#!%s
# fake vim-cmd: output formats follow Broadcom KB 343248 (getallvms, reload) and KB 320877 (power.*)
import json, os, sys, time

home = os.environ["FAKE_ESX_HOME"]
root = os.environ["FAKE_ROOT"]
path = os.path.join(home, "vms.json")


def vnow():
    with open(os.path.join(root, "clock")) as f:
        r0, v0, sp = [float(x) for x in f.read().split()]
    return v0 + (time.time() - r0) * sp


def load():
    with open(path) as f:
        return json.load(f)


def save(d):
    with open(path + ".tmp", "w") as f:
        json.dump(d, f, indent=1)
    os.rename(path + ".tmp", path)


def fault(kind, msg):
    sys.stdout.write("(vim.fault.%%s) {\n   faultCause = (vmodl.MethodFault) null,\n   faultMessage = <unset>\n   msg = \"%%s\"\n}\n" %% (kind, msg))


def mark_down(vm, up_at=""):
    d = os.path.join(root, "vms", vm["name"])
    if os.path.isdir(d):
        with open(os.path.join(d, "down"), "w") as f:
            f.write(str(up_at))


def settle(vm):
    if vm["state"] == "shutting" and vnow() >= vm.get("off_at", 0):
        vm["state"] = "off"
        mark_down(vm)
        return True
    return False


args = sys.argv[1:]
with open(os.path.join(home, "calls.log"), "a") as f:
    f.write("vim-cmd %%s\n" %% " ".join(args))
d = load()
vms = d["vms"]
if any([settle(v) for v in vms]):
    save(d)
cmd = args[0] if args else ""
if cmd == "vmsvc/getallvms":
    sys.stdout.write("Vmid       Name                 File                                 Guest OS          Version   Annotation\n")
    for v in vms:
        sys.stdout.write("%%-6s %%-18s [%%s] %%s   %%-18s %%-9s %%s\n" %% (v["id"], v["name"], v["ds"], v["file"], "rhel8_64Guest", "vmx-19", v.get("note", "")))
        for extra in v.get("note_more", []):
            sys.stdout.write(extra + "\n")
    sys.exit(0)
vm = next((v for v in vms if len(args) > 1 and v["id"] == args[1]), None)
if vm is None:
    fault("NotFound", "Unable to find a VM corresponding to \\\"%%s\\\"" %% (args[1] if len(args) > 1 else ""))
    sys.exit(1)
if cmd == "vmsvc/get.summary":
    if vm.get("no_summary"):
        fault("SystemError", "A general system error occurred")
        sys.exit(1)
    sys.stdout.write("Listsummary:\n\n(vim.vm.Summary) {\n   vm = 'vim.VirtualMachine:%%s',\n   runtime = (vim.vm.RuntimeInfo) {\n"
                     "      powerState = \"poweredOn\",\n   },\n   config = (vim.vm.Summary.ConfigSummary) {\n      name = \"%%s\",\n"
                     "      template = false,\n      vmPathName = \"[%%s] %%s\",\n      memorySizeMB = 16384,\n   },\n}\n"
                     %% (vm["id"], vm["name"], vm["ds"], vm["file"]))
elif cmd == "vmsvc/power.getstate":
    word = {"on": "Powered on", "shutting": "Powered on", "off": "Powered off", "suspended": "Suspended"}[vm["state"]]
    sys.stdout.write("Retrieved runtime info\n%%s\n" %% word)
elif cmd == "vmsvc/power.shutdown":
    how = vm.get("shutdown", "ok")
    if vm["state"] != "on":
        fault("InvalidPowerState", "The attempted operation cannot be performed in the current state (Powered off).")
        sys.exit(1)
    if how == "fault":
        fault("ToolsUnavailable", "Cannot complete operation because VMware Tools is not running in this virtual machine.")
        sys.exit(1)
    if how == "fault0":            # fault text with exit status 0
        fault("ToolsUnavailable", "Cannot complete operation because VMware Tools is not running in this virtual machine.")
        sys.exit(0)
    if how == "ok":
        vm["state"] = "shutting"
        vm["off_at"] = vnow() + float(vm.get("shutdown_sec", 20))
        save(d)
    # "ignore": the guest takes the request and never shuts down
elif cmd == "vmsvc/power.off":
    sys.stdout.write("Powering off VM:\n")
    if vm.get("poweroff") == "fail":
        fault("GenericVmConfigFault", "Failed to power off VM")
        sys.exit(1)
    vm["state"] = "off"
    save(d)
    mark_down(vm)
elif cmd == "vmsvc/power.on":
    sys.stdout.write("Powering on VM:\n")
    if vm.get("poweron") == "fail":
        fault("GenericVmConfigFault", "Failed to power on VM. Module 'MonitorLoop' power on failed.")
        sys.exit(1)
    if vm.get("poweron") == "slow":        # the task takes poweron_sec (real seconds). A vim-cmd that is ended before that leaves the VM off
        sys.stdout.flush()
        time.sleep(float(vm.get("poweron_sec", 5)))
        d = load()
        vm = next(v for v in d["vms"] if v["id"] == args[1])
    vm["state"] = "on"
    vm["on_at"] = vnow()
    save(d)
    mark_down(vm, int(vnow() + float(vm.get("boot_sec", 60))))
    if vm.get("poweron") == "hang":        # the VM is powered on, but vim-cmd does not return
        sys.stdout.flush()
        time.sleep(600)
elif cmd == "vmsvc/reload":
    if vm.get("reload") == "fail":
        fault("SystemError", "A general system error occurred: reload failed")
        sys.exit(1)
else:
    sys.stdout.write("fake vim-cmd: unknown command %%s\n" %% cmd)
    sys.exit(1)
sys.exit(0)
''' % sys.executable

FAKE_ESXCLI = r'''#!%s
# fake esxcli system settings advanced list/set: output format follows Broadcom KB 310338
import json, os, sys

home = os.environ["FAKE_ESX_HOME"]
path = os.path.join(home, "adv.json")
args = sys.argv[1:]
with open(os.path.join(home, "calls.log"), "a") as f:
    f.write("esxcli %%s\n" %% " ".join(args))
if args[:3] == ["vm", "process", "list"]:          # running VMs: World ID, VMX Cartel ID, Display Name (KB 1014165 form)
    with open(os.path.join(home, "vms.json")) as f:
        vms = json.load(f)["vms"]
    for v in vms:
        if v["state"] in ("on", "shutting"):
            w = 100000 + int(v["id"])
            sys.stdout.write("%%s\n   World ID: %%d\n   Process ID: 0\n   VMX Cartel ID: %%d\n   UUID: 42 0a\n   Display Name: %%s\n"
                             "   Config File: /vmfs/volumes/%%s/%%s\n\n" %% (v["name"], w, w - 1, v["name"], v["ds"], v["file"]))
    sys.exit(0)
if args[:3] != ["system", "settings", "advanced"] or len(args) < 6 or args[3] not in ("list", "set") or args[4] != "-o":
    sys.stderr.write("Error: Unknown command or namespace %%s\n" %% " ".join(args))
    sys.exit(1)
with open(path) as f:
    adv = json.load(f)
opt = adv.get(args[5])
if opt is None:
    sys.stderr.write("Unable to find option %%s\n" %% args[5].rsplit("/", 1)[-1])
    sys.exit(1)
if args[3] == "list":
    integer = opt["type"] == "integer"
    sys.stdout.write("   Path: %%s\n   Type: %%s\n   Int Value: %%s\n   Default Int Value: %%s\n   Min Value: %%s\n   Max Value: %%s\n"
                     "   String Value: %%s\n   Default String Value: %%s\n   Valid Characters: %%s\n   Description: %%s\n" %% (
                         args[5], opt["type"], opt["value"] if integer else 0, opt.get("default", 0) if integer else 0,
                         opt.get("min", 0), opt.get("max", 0), "" if integer else opt["value"],
                         "" if integer else opt.get("default", ""), "" if integer else "*", opt.get("desc", "fake option")))
    sys.exit(0)
if len(args) != 8 or args[6] not in ("-i", "-s", "--int-value", "--string-value"):
    sys.stderr.write("Error: Invalid option\n")
    sys.exit(1)
flag, value = args[6], args[7]
if opt["type"] == "integer":
    if flag not in ("-i", "--int-value"):
        sys.stderr.write("Unable to set option: an integer value is required\n")
        sys.exit(1)
    if not (int(opt.get("min", -10 ** 9)) <= int(value) <= int(opt.get("max", 10 ** 9))):
        sys.stderr.write("Unable to set option. Invalid value %%s: out of range\n" %% value)
        sys.exit(1)
    opt["value"] = str(int(value))
else:
    if flag not in ("-s", "--string-value"):
        sys.stderr.write("Unable to set option: a string value is required\n")
        sys.exit(1)
    opt["value"] = value
if opt.get("set") == "ignore":          # the host takes the command and keeps the old value
    sys.exit(0)
with open(path + ".tmp", "w") as f:
    json.dump(adv, f, indent=1)
os.rename(path + ".tmp", path)
sys.exit(0)
''' % sys.executable

ESX_DEFAULT_CRON = """#min hour day mon dow command
1    1    *   *   *   /sbin/tmpwatch.py
1    *    *   *   *   /sbin/auto-backup.sh
0    *    *   *   *   /usr/lib/vmware/vmksummary/log-heartbeat.py
"""

VMX_TEMPLATE = """.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "19"
displayName = "%(name)s"
numvcpus = "8"
memSize = "16384"
sched.cpu.affinity = "all"
numa.nodeAffinity = "0"
guestOS = "rhel8-64"
"""


def vmx_1g(name, mem_mb=16384):
    """1GB page 를 켠 VM 의 .vmx (sched.mem.lpage.enable1GPage = TRUE, 목표 backing 수 = mem_mb // 1024)."""
    return (VMX_TEMPLATE % {"name": name}).replace('memSize = "16384"', 'memSize = "%d"' % mem_mb) + \
        'sched.mem.lpage.enable1GPage = "TRUE"\n'


FAKE_VSISH = r'''#!%s
# fake vsish -e ls|get /memory/lpage/vmLpage/<world id>: 1GB page backings grow with the virtual clock after power on
import json, os, sys, time

home, root = os.environ["FAKE_ESX_HOME"], os.environ["FAKE_ROOT"]
args = sys.argv[1:]
with open(os.path.join(home, "calls.log"), "a") as f:
    f.write("vsish %%s\n" %% " ".join(args))
with open(os.path.join(root, "clock")) as f:
    r0, v0, sp = [float(x) for x in f.read().split()]
vnow = v0 + (time.time() - r0) * sp
with open(os.path.join(home, "vms.json")) as f:
    vms = json.load(f)["vms"]
live = dict((str(100000 + int(v["id"])), v) for v in vms if v["state"] in ("on", "shutting") and v.get("lpage_node", True))
if len(args) == 3 and args[:2] == ["-e", "ls"] and args[2].rstrip("/") == "/memory/lpage/vmLpage":
    for w in sorted(live):
        sys.stdout.write(w + "/\n")
    sys.exit(0)
if len(args) == 3 and args[:2] == ["-e", "get"] and args[2].startswith("/memory/lpage/vmLpage/"):
    v = live.get(args[2].rsplit("/", 1)[-1])
    if v is None:
        sys.stderr.write("VSI node (195887110)(bad3)/memory/lpage/vmLpage/x: Not found\n")
        sys.exit(1)
    cur = int(max(0.0, vnow - float(v.get("on_at", 0))) * float(v.get("lpage_rate", 4)))
    if v.get("lpage_cap") is not None:
        cur = min(cur, int(v["lpage_cap"]))
    sys.stdout.write("VM lpage info {\n   Current number of 2MB page backings:12\n   Current number of 1GB page backings:%%d\n"
                     "   1GB page backing attempts failed:%%d\n}\n" %% (cur, int(v.get("lpage_failed", 0))))
    sys.exit(0)
sys.stderr.write("fake vsish: unknown arguments %%s\n" %% " ".join(args))
sys.exit(1)
''' % sys.executable


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


class FakeEnv(object):
    """시험 폴더 하나. 점프 VM 홈(jump), VM 폴더(vms/<이름>), ESX 폴더(esx/<이름>), 가짜 명령(fakebin), 가상 시계(clock)."""

    def __init__(self, speed=None, start="2026-11-20 10:00:20", dash=False, install=True, jdir=False):
        self.speed = float(speed or SPEED)
        self.root = tempfile.mkdtemp(prefix="tcgen_seq_", dir="/tmp")
        r = self.root
        self.fakebin, self.bin, self.jump = os.path.join(r, "fakebin"), os.path.join(r, "bin"), os.path.join(r, "jump")
        self.vms, self.esx, self.log = os.path.join(r, "vms"), os.path.join(r, "esx"), os.path.join(r, "log")
        for d in (self.fakebin, self.bin, self.jump, self.vms, self.esx, self.log):
            os.makedirs(d)
        for name, text in (("ssh", FAKE_SSH), ("setsid", FAKE_SETSID), ("crontab", FAKE_CRONTAB), ("pgrep", FAKE_PGREP),
                           ("date", FAKE_DATE), ("sleep", FAKE_SLEEP), ("hostname", FAKE_HOSTNAME),
                           ("ssh-keygen", FAKE_KEYGEN), ("vim-cmd", FAKE_VIMCMD), ("esxcli", FAKE_ESXCLI), ("vsish", FAKE_VSISH)):
            write_exec(os.path.join(self.fakebin, name), text)
        if dash:
            os.symlink(shutil.which("dash"), os.path.join(self.fakebin, "sh"))     # 모든 sh 호출을 dash 로
        write_exec(os.path.join(self.bin, "job.sh"), JOB_SH)
        write_exec(os.path.join(self.bin, "mon.sh"), MON_SH)
        write_exec(os.path.join(self.bin, "gpush.sh"), GPUSH_SH)
        v0 = dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()
        with open(os.path.join(r, "clock"), "w") as f:
            f.write("%r %r %r\n" % (time.time(), v0, self.speed))
        with open(os.path.join(r, "lpage_poll"), "w") as f:      # 1GB backing 읽기 간격: 가상 시계로 15초
            f.write("%.4f\n" % (15.0 / self.speed))
        self.env = {"HOME": self.jump, "PATH": self.fakebin + ":/usr/bin:/bin", "TERM": "dumb", "FAKE_PW": TEST_PW,
                    "FAKE_ROOT": r, "FAKE_VMS": self.vms, "LANG": "C", "USER": "tester", "TZ": "Asia/Seoul"}
        # tcrun start 의 확인 질문(v2.15)에 줄 답. 시험은 터미널이 없으므로 TCRUN_TEST_TTY 의 파일에서 읽는다(시험 전용)
        self.answer = os.path.join(r, "answer")
        with open(self.answer, "w") as f:
            f.write("yes\n")
        self.env["TCRUN_TEST_TTY"] = self.answer
        self.tcgen = os.path.join(self.jump, ".tcgen")
        self.jdir = None                 # jdir=True: 점프 VM 작업 폴더를 홈 밖(<시험 폴더>/data/tcgen)에 둔다
        if jdir:
            self.tcgen = self.jdir = os.path.join(r, "data", "tcgen")
        self.cron = None
        self.procs = []
        if install:
            self.install_jump()

    # ---- 시계
    def now(self):
        with open(os.path.join(self.root, "clock")) as f:
            r0, v0, sp = [float(x) for x in f.read().split()]
        return v0 + (time.time() - r0) * sp

    # ---- 점프 VM
    def install_jump(self, key=True, password=True):
        """A 블록을 붙여넣은 뒤와 같은 상태를 만든다(파일 내용은 core.jump_files 와 같음)."""
        os.makedirs(os.path.join(self.tcgen, "runs"), exist_ok=True)
        for name, text in core.jump_files(self.jdir or core.DEFAULT_JUMP_DIR):
            with open(os.path.join(self.tcgen, name), "w", newline="\n") as f:
                f.write(text)
            os.chmod(os.path.join(self.tcgen, name), 0o700)
        if password:
            with open(os.path.join(self.tcgen, "vmpw"), "w") as f:
                f.write(TEST_PW + "\n")
        if key:
            subprocess.check_call(["ssh-keygen", "-t", "rsa", "-b", "4096", "-N", "", "-f", os.path.join(self.tcgen, "id_esx")],
                                  env=self.env)

    # ---- VM
    def add_vm(self, name, crontab=None, clock_offset=0):
        d = os.path.join(self.vms, name)
        os.makedirs(os.path.join(d, "proc"))
        if crontab is not None:
            with open(os.path.join(d, "crontab.txt"), "w") as f:
                f.write(crontab)
        if clock_offset:
            with open(os.path.join(d, "clock_offset"), "w") as f:
                f.write(str(clock_offset))
        for rel, text in GUEST_FILES.items():
            self.guest_set(name, rel, text)
        return d

    def guest_set(self, name, rel, text):
        """게스트 OS 의 설정 파일 내용을 정한다(rel: 'sys/kernel/mm/transparent_hugepage/enabled' 처럼 / 없는 경로). None 이면 없앤다."""
        p = os.path.join(self.vms, name, "root", rel)
        if text is None:
            if os.path.exists(p):
                os.remove(p)
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def vm_dir(self, name):
        return os.path.join(self.vms, name)

    def vm_crontab(self, name):
        p = os.path.join(self.vms, name, "crontab.txt")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def vm_file(self, name, fn):
        p = os.path.join(self.vms, name, fn)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [x.split() for x in f.read().splitlines()]

    # ---- ESX
    def add_esx(self, name, vms=(), adv=None, authorized=True, addr=None, vmx=None):
        """vms: [(VM 이름, {vim-cmd 상태 덮어쓰기})]. vmx: {VM 이름: vmx 글}. adv: {옵션 경로: {type, value, ...}}."""
        d = os.path.join(self.esx, name)
        for sub in ("cron", "tmp", "vmfs"):
            os.makedirs(os.path.join(d, sub))
        with open(os.path.join(d, "cron", "root"), "w") as f:
            f.write(ESX_DEFAULT_CRON)
        os.chmod(os.path.join(d, "cron", "root"), 0o444)
        rows = []
        for n, (vm, over) in enumerate(vms):
            row = {"id": str(10 + n), "name": vm, "ds": "datastore1", "file": "%s/%s.vmx" % (vm, vm), "state": "on",
                   "shutdown": "ok", "shutdown_sec": 20, "poweron": "ok", "boot_sec": 60}
            row.update(over or {})
            rows.append(row)
            folder = os.path.join(d, "vmfs", row["ds"], os.path.dirname(row["file"]))
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(d, "vmfs", row["ds"], row["file"]), "wb") as f:
                f.write(((vmx or {}).get(vm) or (VMX_TEMPLATE % {"name": vm})).encode("latin-1"))
            if row["state"] != "on" and os.path.isdir(os.path.join(self.vms, vm)):
                open(os.path.join(self.vms, vm, "down"), "w").close()
        with open(os.path.join(d, "vms.json"), "w") as f:
            json.dump({"vms": rows}, f, indent=1)
        base = {"/Numa/LocalityWeightActionAffinity": {"type": "integer", "value": "130", "default": "130", "min": 0, "max": 200},
                "/Cpu/HTWholeCoreThreshold": {"type": "integer", "value": "800", "default": "800", "min": 0, "max": 100000},
                "/Misc/HostName": {"type": "string", "value": "old-name", "default": ""}}
        base.update(adv or {})
        with open(os.path.join(d, "adv.json"), "w") as f:
            json.dump(base, f, indent=1)
        if authorized:
            open(os.path.join(d, "authorized"), "w").close()
        if addr:
            with open(os.path.join(self.root, "esx_addr"), "a") as f:
                f.write("%s %s\n" % (addr, name))
        return d

    def esx_dir(self, name):
        return os.path.join(self.esx, name)

    def esx_calls(self, name):
        p = os.path.join(self.esx, name, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def esx_vms(self, name):
        with open(os.path.join(self.esx, name, "vms.json")) as f:
            return dict((v["name"], v) for v in json.load(f)["vms"])

    def esx_adv(self, name, path):
        with open(os.path.join(self.esx, name, "adv.json")) as f:
            return json.load(f)[path]["value"]

    def vmx_path(self, esx, vm):
        return os.path.join(self.esx, esx, "vmfs", "datastore1", vm, vm + ".vmx")

    def vmx(self, esx, vm):
        with open(self.vmx_path(esx, vm), "rb") as f:
            return f.read().decode("latin-1")

    def esx_crontab(self, name):
        with open(os.path.join(self.esx, name, "cron", "root")) as f:
            return f.read().splitlines()

    # ---- 명령
    def job_cmd(self, proc, seconds):
        return "sh %s/job.sh %s %d" % (self.bin, proc, seconds)

    def mon_cmd(self, word, tag):
        return "sh %s/mon.sh %s %s" % (self.bin, word, tag)

    def gpush_cmd(self):
        """구성 스냅샷 때 VM 마다 실행할 명령(설정의 seq_guest_cmd 에 넣는다)."""
        return "sh %s/gpush.sh" % self.bin

    def gpush_runs(self, name):
        """VM 에서 그 명령이 성공한 횟수."""
        p = os.path.join(self.vms, name, "gpush.log")
        if not os.path.exists(p):
            return 0
        with open(p) as f:
            return len(f.read().splitlines())

    # ---- plan 과 tcrun
    def settings(self, **over):
        s = dict(core.DEFAULT_SETTINGS)
        # 시험의 기본값: 모니터링 시작 1분 전, 설정 적용 10분 전, 구성 스냅샷 3분 전(가상 시계로 케이스 하나가 십몇 분 안에 끝나게).
        # 모니터링 중지(자동)는 다음 케이스 잡 시작 12분 전: v2.14 까지 시험에 쓰던 시각(설정 적용 10분 + 2분)을 그대로 쓴다.
        # 설정 적용보다 늦은 중지(사용자 기본값 10분과 같은 관계)는 StopRuleTests 가 본다
        s.update(record_csv=os.path.join(self.root, "pc", "rec.csv"), log_dir=os.path.join(self.root, "pc"), run_mode="seq",
                 jump_log_dir=self.log, mon_lead_min=1, seq_boot_wait_min=5, seq_shutdown_wait_min=1,
                 seq_prep_lead_min=10, seq_snap_lead_min=3, stop_lead_min=12)
        if self.jdir:
            s["jump_dir"] = self.jdir
        s.update(over)
        return s

    def put_plan(self, res):
        """생성 결과의 plan 을 실행 블록이 두는 자리(~/.tcgen/runs/<실행 ID>/plan)에 쓴다."""
        d = os.path.join(self.tcgen, "runs", res["run_id"])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "plan")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(res["plan"])
        return p

    def tcrun(self, *args, **kw):
        """tcrun 을 끝날 때까지 실행한다. (종료 코드, 출력) 반환."""
        p = subprocess.run(["sh", os.path.join(self.tcgen, "tcrun")] + list(args), env=self.env, cwd=self.jump,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           start_new_session=True, timeout=kw.get("timeout", 300))
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def tcrun_bg(self, *args):
        p = subprocess.Popen(["sh", os.path.join(self.tcgen, "tcrun")] + list(args), env=self.env, cwd=self.jump,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def state(self, run_id, chain):
        p = os.path.join(self.tcgen, "runs", run_id, chain + ".state")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            return dict(x.split("=", 1) for x in f.read().splitlines() if "=" in x)

    def jobs_rows(self, run_id):
        """등록 기록(<로그 폴더>/tcrun-<실행 ID>-jobs.csv)의 행."""
        p = os.path.join(self.log, "tcrun-%s-jobs.csv" % run_id)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def vm_logins(self):
        """점프 VM 에서 VM 으로 ssh 접속한 횟수(가짜 ssh 가 적는 기록)."""
        p = os.path.join(self.root, "ssh_calls.txt")
        if not os.path.exists(p):
            return 0
        with open(p) as f:
            return len([x for x in f.read().splitlines() if x.startswith("vm ")])

    def log_text(self, run_id, chain, suffix=".log"):
        p = os.path.join(self.log, "tcrun-%s-%s%s" % (run_id, chain, suffix))
        if not os.path.exists(p):
            return ""
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")

    def wait_for(self, cond, seconds=120, what=""):
        end = time.time() + seconds
        while time.time() < end:
            got = cond()
            if got:
                return got
            time.sleep(0.02)
        raise AssertionError("기다리는 상태가 되지 않음: %s" % what)

    def start_cron(self):
        self.cron = CronSim(self)
        self.cron.start()
        return self.cron

    def close(self):
        if self.cron:
            self.cron.stop()
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
            if p.stdout:
                p.stdout.close()
        # 남은 tcrun 진행(start 로 띄운 것)을 끝낸다
        runs = os.path.join(self.tcgen, "runs")
        if os.path.isdir(runs):
            for cur, _, files in os.walk(runs):
                for fn in files:
                    if fn.endswith(".pid"):
                        try:
                            with open(os.path.join(cur, fn)) as f:
                                os.killpg(int(f.read().strip()), signal.SIGKILL)
                        except (OSError, ValueError):
                            pass
        shutil.rmtree(self.root, ignore_errors=True)


def cronie_split(cmd):
    """cronie do_command.c 와 같은 % 처리: \\% -> %, 첫 % 이후는 표준 입력(% -> 줄바꿈)."""
    out, i, stdin = [], 0, None
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and i + 1 < len(cmd) and cmd[i + 1] == "%":
            out.append("%")
            i += 2
            continue
        if ch == "%":
            stdin = cmd[i + 1:].replace("%", "\n")
            break
        out.append(ch)
        i += 1
    return "".join(out), stdin


class CronSim(threading.Thread):
    """등록된 crontab 줄을 가상 시계의 그 분에 실행한다. fired = [(호스트, 분의 시작 epoch, crontab 줄)]."""

    def __init__(self, env):
        threading.Thread.__init__(self)
        self.daemon = True
        self.env = env
        self.fired = []
        self.children = []
        self._stop_evt = threading.Event()
        self._last = {}
        self._esx_cache = {}

    def stop(self):
        self._stop_evt.set()
        self.join(5)
        for p in self.children:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            p.wait()

    def offset(self, d):
        try:
            with open(os.path.join(d, "clock_offset")) as f:
                return float(f.read().strip() or 0)
        except (IOError, OSError, ValueError):
            return 0.0

    def run(self):
        e = self.env
        while not self._stop_evt.is_set():
            now = e.now()
            for kind, base in (("vm", e.vms), ("esx", e.esx)):
                for name in sorted(os.listdir(base)):
                    d = os.path.join(base, name)
                    if not os.path.isdir(d):
                        continue
                    if kind == "esx":
                        self.esx_reload(name, d)
                    minute = int((now + self.offset(d)) // 60)
                    key = (kind, name)
                    if key not in self._last:
                        self._last[key] = minute
                        continue
                    while self._last[key] < minute:
                        self._last[key] += 1
                        self.fire(kind, name, d, self._last[key] * 60)
            time.sleep(0.003)

    def esx_reload(self, name, d):
        upd = os.path.join(d, "cron", "cron.update")
        if name not in self._esx_cache or os.path.exists(upd):
            try:
                with open(os.path.join(d, "cron", "root")) as f:
                    self._esx_cache[name] = f.read().splitlines()
            except (IOError, OSError):
                self._esx_cache[name] = []
            if os.path.exists(upd):
                try:
                    os.rename(upd, upd + ".seen")
                except OSError:
                    pass

    def fire(self, kind, name, d, t):
        if kind == "vm":
            if os.path.exists(os.path.join(d, "down")):
                return
            try:
                with open(os.path.join(d, "crontab.txt")) as f:
                    lines = f.read().splitlines()
            except (IOError, OSError):
                return
            when = dt.datetime.fromtimestamp(t, KST)
        else:
            lines = self._esx_cache.get(name, [])
            when = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        for line in lines:
            parts = line.split(None, 5)
            if len(parts) < 6 or parts[0].startswith("#"):
                continue
            want = (when.minute, when.hour, when.day, when.month)
            if any(f != "*" and (not f.isdigit() or int(f) != w) for f, w in zip(parts[:4], want)):
                continue
            if parts[4] != "*":
                continue
            env = dict(self.env.env)
            env.update(HOME=d, FAKE_HOST=name, FAKE_CLOCK_OFFSET=str(int(self.offset(d))))
            if kind == "vm":
                cmd, _ = cronie_split(parts[5])
                env["FAKE_CRONTAB"] = os.path.join(d, "crontab.txt")
            else:
                cmd = parts[5]
                env["TZ"] = "UTC"
                if "tmpwatch" in cmd or "auto-backup" in cmd or "log-heartbeat" in cmd:
                    continue
            self.fired.append((name, t, line))
            self.children.append(subprocess.Popen(["sh", "-c", cmd], cwd=d, env=env, stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                  start_new_session=True))
