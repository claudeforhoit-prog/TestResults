# -*- coding: utf-8 -*-
"""
tcgen_apply.py 2.12 - apply ESXi host advanced options and VM configuration (.vmx) keys for one test case.

Runs ON THE ESXi HOST with the python3 that ships with ESXi. Standard library only, Python 3.5 syntax.
The tcgen sequence runner (tcrun, on the jump VM) uploads this file to /tmp/tcgen on the host and runs it over ssh.
Do not edit it on the host: tcgen block A installs it on the jump VM and tcrun uploads it again on every call.

Usage (the settings come on standard input, see "spec" below):
  python3 tcgen_apply.py check                 read only: show the VMs, the current and the wanted values.
  python3 tcgen_apply.py prepare <tag>         if anything differs: shut the VMs of the case down, set the host
                                               options, replace the .vmx keys, reload. The VMs stay powered off.
  python3 tcgen_apply.py poweron <vm> [secs]   power one VM on and wait until its state is "Powered on".
                                               secs: how long vim-cmd vmsvc/power.on may take (default 5400).
  python3 tcgen_apply.py rollback <tag>        undo what "prepare <tag>" changed (.vmx backups, host options).
  python3 tcgen_apply.py poweroff <vm> [secs]  guest shutdown, power off after [secs] (default 600) when still on.
  python3 tcgen_apply.py lpage <vm>            read the 1GB page backing of a powered-on VM once (vsish /memory/lpage/vmLpage).
  python3 tcgen_apply.py lpagewait <vm> [secs] wait until the 1GB page backing of a powered-on VM is complete (default 5400).
                                               Complete when the number of 1GB page
                                               backings reaches memSize / 1024; no increase in 6 readings (15 s apart) = stopped.
  python3 tcgen_apply.py snapshot              read only: the settings of VMs for the configuration snapshot (config snapshot).
                                               Standard input: "vm <name>" lines, and "key <.vmx key>" lines for more keys.
                                               Prints "TCGEN-CFG <vm>|<group>|<item>|<value>": group basic (power state, cpu,
                                               memory, shares), adv (advanced keys of the .vmx), aff (CPU affinity keys).
                                               An empty value: the .vmx has no such key.

spec (one item per line, ASCII):
  opt shutdown_wait <seconds>      how long to wait for the guest shutdown before a hard power off (default 600)
  vm <name>                        every VM of the case, in power-on order
  host </Section/Option>=<value>   ESXi advanced option (esxcli system settings advanced)
  set <vm> <key>=<value>           .vmx key of that VM. An empty value removes the key.
  off <vm>                         the case wants this VM powered off (check shows it; prepare does not power anything on)

Commands used (Broadcom KB): vim-cmd vmsvc/getallvms, get.summary, power.getstate, power.shutdown, power.off,
power.on, reload (KB 343248, 320877, 345049, 308360); esxcli system settings advanced list/set (KB 310338).
1GB page backing: esxcli vm process list (world ID) and vsish -e get /memory/lpage/vmLpage/<id>
("Current number of 1GB page backings", "1GB page backing attempts failed").

Output: lines for people, plus lines that start with "TCGEN-" for the runner. The last line is always
"TCGEN-RESULT <word> [text]". Exit status 0 = done, 1 = error. The same lines go to <tmp>/apply.log.

Environment variables for local tests only (not set on a real host): TCGEN_VMFS_ROOT, TCGEN_TMP, TCGEN_POLL_SEC.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

VERSION = "2.12"
VMFS_ROOT = os.environ.get("TCGEN_VMFS_ROOT", "/vmfs/volumes")
TMP = os.environ.get("TCGEN_TMP", "/tmp/tcgen")
POLL = float(os.environ.get("TCGEN_POLL_SEC", "5"))
KEEP_BACKUPS = 5
POWERON_WAIT = 5400          # seconds that vim-cmd vmsvc/power.on may take when the caller gives no limit
LINE_RE = re.compile(r'^\s*([^\s=#"]+)\s*=\s*(?:"(.*)"|(.*?))\s*$')
VM_ROW_RE = re.compile(r"^(\d+)\s+(.+?)\s+\[([^\]]+)\]\s+(.+?\.vmx)(?:\s|$)")
LOG = []


class Fail(Exception):
    pass


class NoAnswer(Fail):
    """A command did not end within its time limit."""


def out(line):
    LOG.append(line)
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (IOError, OSError, ValueError):
        pass          # the ssh session is gone: go on, so that the host ends in a state that the log describes


def result(word, text=""):
    out("TCGEN-RESULT %s%s" % (word, (" " + text) if text else ""))


def save_log(argv):
    try:
        path = os.path.join(TMP, "apply.log")
        if os.path.isfile(path) and os.path.getsize(path) > 1000000:
            os.rename(path, path + ".old")
        with open(path, "a") as f:
            f.write("==== %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv[1:])))
            for line in LOG:
                f.write(line + "\n")
    except (IOError, OSError):
        pass


def run(argv, timeout=120):
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise Fail("cannot run %s: %s" % (argv[0], e))
    try:
        data, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise NoAnswer("no answer in %d s: %s" % (timeout, " ".join(argv)))
    return p.returncode, data.decode("utf-8", "replace")


def short(text):
    """One line that says why a command failed: the msg of a vim-cmd fault, else the last line with text."""
    m = re.search(r'msg\s*=\s*"(.*)"', text)
    if m:
        return m.group(1)[:300]
    lines = [x.strip() for x in text.strip().splitlines() if x.strip() not in ("", "{", "}")]
    return (lines[-1] if lines else "")[:300]


def failed(rc, text):
    return rc != 0 or "fault" in text.lower()


# ---------------------------------------------------------------- lock (one tcgen_apply at a time on a host)
def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as e:
        return getattr(e, "errno", 0) == 1          # EPERM: the process exists
    return True


def take_lock():
    if not os.path.isdir(TMP):
        os.makedirs(TMP)
    path = os.path.join(TMP, "lock")
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return path
        except OSError:
            try:
                with open(path) as f:
                    pid = int(f.read().strip() or "0")
            except (IOError, OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and pid_alive(pid):
                raise Fail("another tcgen_apply.py (pid %d) is running on this host. Try again when it has ended." % pid)
            try:
                os.remove(path)
            except OSError:
                pass
    raise Fail("cannot create the lock file %s" % path)


def drop_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- spec
def read_spec(data):
    spec = {"vms": [], "host": [], "set": {}, "off": [], "opt": {"shutdown_wait": 600}}
    for raw in data.decode("ascii", "replace").split("\n"):
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        word, _, rest = line.partition(" ")
        if word == "vm":
            name = rest.strip()
            if not name or name in spec["vms"]:
                raise Fail("spec: empty or repeated vm line: %r" % line)
            spec["vms"].append(name)
            spec["set"].setdefault(name, [])
        elif word == "host":
            key, eq, value = rest.partition("=")
            if not eq or not key.strip() or value == "":
                raise Fail("spec: a host line needs </Section/Option>=<value>: %r" % line)
            spec["host"].append((key.strip(), value))
        elif word == "set":
            name, _, kv = rest.partition(" ")
            key, eq, value = kv.partition("=")
            if not eq or not key.strip() or '"' in value:
                raise Fail("spec: a set line needs <vm> <key>=<value> without double quotes: %r" % line)
            if name not in spec["set"]:
                raise Fail("spec: a set line names a VM that has no vm line: %r" % line)
            spec["set"][name].append((key.strip(), value))
        elif word == "off":
            name = rest.strip()
            if name not in spec["set"]:
                raise Fail("spec: an off line names a VM that has no vm line: %r" % line)
            if name not in spec["off"]:
                spec["off"].append(name)
        elif word == "opt":
            key, _, value = rest.partition(" ")
            try:
                spec["opt"][key] = int(value)
            except ValueError:
                raise Fail("spec: the opt value is not a number: %r" % line)
        else:
            raise Fail("spec: unknown line: %r" % line)
    if not spec["vms"] and not spec["host"]:
        raise Fail("spec: nothing to do (no vm line and no host line)")
    return spec


# ---------------------------------------------------------------- VMs
def all_vms():
    rc, text = run(["vim-cmd", "vmsvc/getallvms"])
    if rc != 0:
        raise Fail("vim-cmd vmsvc/getallvms failed: %s" % short(text))
    rows = []
    for line in text.splitlines():
        m = VM_ROW_RE.match(line)
        if m:
            rows.append({"id": m.group(1), "name": m.group(2).strip(), "ds": m.group(3), "file": m.group(4).strip()})
    return rows


def find_vm(rows, name):
    hits = [r for r in rows if r["name"] == name]
    if not hits:
        raise Fail("VM '%s' is not registered on this host (vim-cmd vmsvc/getallvms). Nothing was changed." % name)
    if len(hits) > 1:
        raise Fail("the VM name '%s' is on %d lines of vim-cmd vmsvc/getallvms. Nothing was changed." % (name, len(hits)))
    return hits[0]


def vmx_path(row):
    rc, text = run(["vim-cmd", "vmsvc/get.summary", row["id"]])
    m = re.search(r'vmPathName\s*=\s*"\[([^\]]+)\]\s*([^"]+)"', text) if rc == 0 else None
    ds, rel = (m.group(1), m.group(2)) if m else (row["ds"], row["file"])
    path = os.path.join(VMFS_ROOT, ds, rel)
    if not os.path.isfile(path):
        raise Fail(".vmx file of VM '%s' not found: %s. Nothing was changed." % (row["name"], path))
    return path


def power_state(vmid):
    rc, text = run(["vim-cmd", "vmsvc/power.getstate", vmid])
    low = text.lower()
    if rc == 0:
        for word, state in (("powered on", "on"), ("powered off", "off"), ("suspended", "suspended")):
            if word in low:
                return state
    raise Fail("cannot read the power state of vmid %s (vim-cmd vmsvc/power.getstate): %s" % (vmid, short(text)))


def wait_state(vmid, want, seconds):
    end = time.time() + seconds
    while True:
        if power_state(vmid) == want:
            return True
        if time.time() >= end:
            return False
        time.sleep(POLL)


def hard_off(name, vmid):
    rc, text = run(["vim-cmd", "vmsvc/power.off", vmid], timeout=300)
    if not wait_state(vmid, "off", 5 if failed(rc, text) else 120):
        raise Fail("VM '%s' did not power off (vim-cmd vmsvc/power.off: %s)" % (name, short(text)))


def shut_down(vms, seconds):
    """vms: [(name, vmid)] that are powered on. Ask every guest to shut down, wait, then power off what is still on."""
    waiting = []
    for name, vmid in vms:
        rc, text = run(["vim-cmd", "vmsvc/power.shutdown", vmid])
        if failed(rc, text):
            out("  %s: guest shutdown is not possible (%s): power off" % (name, short(text)))
            hard_off(name, vmid)
        else:
            out("  %s: guest shutdown requested" % name)
            waiting.append((name, vmid))
    end = time.time() + seconds
    while waiting:
        waiting = [(n, i) for n, i in waiting if power_state(i) != "off"]
        if not waiting or time.time() >= end:
            break
        time.sleep(POLL)
    for name, vmid in waiting:
        out("  %s: still on %d s after the guest shutdown request: power off" % (name, seconds))
        hard_off(name, vmid)
    for name, vmid in vms:
        if power_state(vmid) != "off":
            raise Fail("VM '%s' is not powered off" % name)
        out("  %s: powered off" % name)


# ---------------------------------------------------------------- .vmx
def read_vmx(path):
    with open(path, "rb") as f:
        return f.read().split(b"\n")


def key_of(raw):
    m = LINE_RE.match(raw.decode("latin-1").rstrip("\r"))
    if not m:
        return None, None
    return m.group(1).lower(), (m.group(2) if m.group(2) is not None else m.group(3))


def values_of(lines, key):
    k = key.lower()
    return [v for kk, v in (key_of(x) for x in lines) if kk == k]


def same(values, want):
    if want == "":
        return not values
    return len(values) == 1 and values[0] == want


def show(values):
    if not values:
        return "(none)"
    if len(values) == 1:
        return '"%s"' % values[0]
    return "%d lines: %s" % (len(values), ", ".join('"%s"' % v for v in values))


def edit_vmx(lines, items):
    """Remove every line whose key is a wanted key (any letter case), then add one line per wanted value.
    Every other line stays byte for byte."""
    body = [x for x in lines if x.strip()]
    eol = b"\r" if body and all(x.endswith(b"\r") for x in body) else b""
    for key, want in items:
        k = key.lower()
        lines = [x for x in lines if key_of(x)[0] != k]
        if want != "":
            tail = []
            while lines and lines[-1].strip() == b"":
                tail.append(lines.pop())
            lines.append(('%s = "%s"' % (key, want)).encode("ascii") + eol)
            lines.extend(reversed(tail))
    if not lines or lines[-1] != b"":
        lines.append(b"")
    return lines


def replace_file(path, data):
    tmp = path + ".tcgen-new"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    shutil.copymode(path, tmp)
    os.rename(tmp, path)


def write_vmx(path, lines, stamp):
    backup = "%s.tcgen-%s" % (path, stamp)
    shutil.copy2(path, backup)
    replace_file(path, b"\n".join(lines))
    folder, base = os.path.dirname(path), os.path.basename(path)
    old = sorted(n for n in os.listdir(folder) if re.match(re.escape(base) + r"\.tcgen-\d{14}$", n))
    for n in old[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(folder, n))
        except OSError:
            pass
    return backup


def reload_vm(name, vmid):
    rc, text = run(["vim-cmd", "vmsvc/reload", vmid])
    if failed(rc, text):
        raise Fail("vim-cmd vmsvc/reload failed for VM '%s': %s" % (name, short(text)))


# ---------------------------------------------------------------- host options
def host_get(path):
    rc, text = run(["esxcli", "system", "settings", "advanced", "list", "-o", path])
    if rc != 0:
        raise Fail("host option '%s' cannot be read (esxcli system settings advanced list -o): %s. Nothing was changed."
                   % (path, short(text)))
    typ = re.search(r"^[ \t]*Type:[ \t]*(\S+)", text, re.M)
    if not typ or typ.group(1) not in ("integer", "string"):
        raise Fail("host option '%s': the esxcli output has no known Type line. Nothing was changed." % path)
    name = "Int Value" if typ.group(1) == "integer" else "String Value"
    val = re.search(r"^[ \t]*%s:[ \t]*(.*)$" % name, text, re.M)
    if not val:
        raise Fail("host option '%s': the esxcli output has no '%s' line. Nothing was changed." % (path, name))
    return typ.group(1), val.group(1).strip()


def host_same(typ, cur, want):
    if typ == "integer":
        try:
            return int(cur) == int(want)
        except ValueError:
            return False
    return cur == want


def host_set(path, typ, value):
    flag = "-i" if typ == "integer" else "-s"
    rc, text = run(["esxcli", "system", "settings", "advanced", "set", "-o", path, flag, value])
    if rc != 0:
        raise Fail("host option '%s' cannot be set to '%s' (esxcli system settings advanced set): %s" % (path, value, short(text)))
    now = host_get(path)[1]
    if not host_same(typ, now, value):
        raise Fail("host option '%s' is '%s' after setting '%s'" % (path, now, value))


# ---------------------------------------------------------------- analyse (reads only)
def analyse(spec):
    rows = all_vms() if spec["vms"] else []
    found = [(name, find_vm(rows, name)) for name in spec["vms"]]      # every VM must exist before anything else
    vms = []
    changes = 0
    for name, row in found:
        path = vmx_path(row)
        state = power_state(row["id"])
        lines = read_vmx(path)
        last = {}
        order = []
        for key, want in spec["set"][name]:
            if key.lower() not in last:
                order.append(key.lower())
            last[key.lower()] = (key, want)          # the last line for a key wins
        items = []
        for k in order:
            key, want = last[k]
            cur = values_of(lines, key)
            diff = not same(cur, want)
            changes += 1 if diff else 0
            items.append({"key": key, "want": want, "cur": cur, "diff": diff})
        vms.append({"name": name, "id": row["id"], "vmx": path, "power": state, "items": items})
    host = []
    for path, want in spec["host"]:
        typ, cur = host_get(path)
        if typ == "integer":
            try:
                int(want)
            except ValueError:
                raise Fail("host option '%s' is an integer option, the wanted value is '%s'. Nothing was changed." % (path, want))
        diff = not host_same(typ, cur, want)
        changes += 1 if diff else 0
        host.append({"path": path, "type": typ, "cur": cur, "want": want, "diff": diff})
    for v in vms:
        out("TCGEN-VM %s vmid=%s power=%s vmx=%s" % (v["name"], v["id"], v["power"], v["vmx"]))
        for it in v["items"]:
            out("TCGEN-SET %s %s: %s -> %s  [%s]" % (v["name"], it["key"], show(it["cur"]),
                                                     ('"%s"' % it["want"]) if it["want"] != "" else "(remove)",
                                                     "change" if it["diff"] else "same"))
    for h in host:
        out("TCGEN-HOST %s (%s): %s -> %s  [%s]" % (h["path"], h["type"], h["cur"], h["want"],
                                                   "change" if h["diff"] else "same"))
    bad = [v["name"] for v in vms if v["power"] == "suspended"]
    if bad:
        raise Fail("suspended VM: %s. Resume it or power it off first. Nothing was changed." % ", ".join(bad))
    return vms, host, changes


# ---------------------------------------------------------------- state for rollback
def state_path(tag):
    if not re.match(r"^[A-Za-z0-9._-]+$", tag or ""):
        raise Fail("bad tag: %r" % tag)
    return os.path.join(TMP, "state-%s.json" % tag)


def save_state(tag, state):
    tmp = state_path(tag) + ".new"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.rename(tmp, state_path(tag))


# ---------------------------------------------------------------- commands
def cmd_check(spec):
    vms, host, changes = analyse(spec)
    pon = poff = 0
    for v in vms:
        want = "off" if v["name"] in spec["off"] else "on"
        if want == "on" and v["power"] != "on":
            pon += 1
        if want == "off" and v["power"] != "off":
            poff += 1
        out("TCGEN-POWER %s now=%s want=%s  [%s]" % (v["name"], v["power"], want, "same" if v["power"] == want else "change"))
    parts = (["%d value(s) differ" % changes] if changes else []) + (["%d VM(s) to power on" % pon] if pon else []) + \
        (["%d VM(s) to power off" % poff] if poff else [])
    if parts:
        result("wouldchange", ", ".join(parts))
    else:
        result("nochange")


def cmd_prepare(tag, spec):
    state_path(tag)
    vms, host, changes = analyse(spec)
    if not changes:
        result("nochange")
        return
    stamp = time.strftime("%Y%m%d%H%M%S")
    wait = spec["opt"]["shutdown_wait"]
    state = {"tag": tag, "shutdown_wait": wait, "was_on": [v["name"] for v in vms if v["power"] == "on"],
             "vms": [], "host": []}
    save_state(tag, state)
    if state["was_on"]:
        out("shutting down the VMs of this case: %s" % ", ".join(state["was_on"]))
        shut_down([(v["name"], v["id"]) for v in vms if v["power"] == "on"], wait)
    for h in host:
        if not h["diff"]:
            continue
        state["host"].append({"path": h["path"], "type": h["type"], "old": h["cur"]})
        save_state(tag, state)
        host_set(h["path"], h["type"], h["want"])
        out("TCGEN-DONE host %s: %s -> %s" % (h["path"], h["cur"], h["want"]))
    for v in vms:
        todo = [(it["key"], it["want"]) for it in v["items"] if it["diff"]]
        if not todo:
            continue
        backup = "%s.tcgen-%s" % (v["vmx"], stamp)
        state["vms"].append({"name": v["name"], "id": v["id"], "vmx": v["vmx"], "backup": backup})
        save_state(tag, state)
        write_vmx(v["vmx"], edit_vmx(read_vmx(v["vmx"]), todo), stamp)
        reload_vm(v["name"], v["id"])
        lines = read_vmx(v["vmx"])
        wrong = [k for k, w in todo if not same(values_of(lines, k), w)]
        if wrong:
            raise Fail("VM '%s': the .vmx does not hold the wanted value after writing: %s" % (v["name"], ", ".join(wrong)))
        out("TCGEN-DONE vm %s: %d key(s) written, reloaded. backup %s" % (v["name"], len(todo), backup))
    result("changed", "%d value(s)" % changes)


def cmd_poweron(name, seconds):
    row = find_vm(all_vms(), name)
    if power_state(row["id"]) == "on":
        out("%s: already powered on" % name)
        result("alreadyon")
        return
    begin = time.time()
    try:
        rc, text = run(["vim-cmd", "vmsvc/power.on", row["id"]], timeout=seconds)
    except NoAnswer:
        # vim-cmd only asks hostd for the power-on task and waits for it. Ending vim-cmd does not end the task.
        if power_state(row["id"]) == "on":
            out("%s: powered on (vim-cmd vmsvc/power.on did not return within %d s, the state is 'Powered on')" % (name, seconds))
            result("poweredon")
            return
        raise Fail("VM '%s' is not 'Powered on' %d s after vim-cmd vmsvc/power.on was started. "
                   "The power-on task may still be running on the host." % (name, seconds))
    if failed(rc, text):
        raise Fail("VM '%s' did not power on (vim-cmd vmsvc/power.on): %s" % (name, short(text)))
    if not wait_state(row["id"], "on", 120):
        raise Fail("VM '%s' is not 'Powered on' after vim-cmd vmsvc/power.on" % name)
    out("%s: powered on (vim-cmd vmsvc/power.on took %d s)" % (name, int(time.time() - begin)))
    result("poweredon")


def cmd_poweroff(name, seconds):
    row = find_vm(all_vms(), name)
    state = power_state(row["id"])
    if state == "off":
        out("%s: already powered off" % name)
        result("alreadyoff")
        return
    if state == "suspended":
        raise Fail("VM '%s' is suspended. Resume it or power it off first." % name)
    shut_down([(name, row["id"])], seconds)
    result("poweredoff")


LPAGE = "/memory/lpage/vmLpage"


def lpage_ids(name):
    """World ID and VMX Cartel ID of a running VM (esxcli vm process list), in that order."""
    rc, text = run(["esxcli", "vm", "process", "list"])
    if rc != 0:
        return []
    blocks, cur = [], {}
    for line in text.splitlines():
        if line.strip() and not line[:1].isspace():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        key, sep, value = line.strip().partition(":")
        if sep:
            cur[key.strip()] = value.strip()
    if cur:
        blocks.append(cur)
    for b in blocks:
        if b.get("Display Name") == name:
            return [x for x in (b.get("World ID"), b.get("VMX Cartel ID")) if x and x.isdigit()]
    return []


LPAGE_POLL = float(os.environ.get("TCGEN_LPAGE_POLL_SEC", "15"))   # seconds between two readings
LPAGE_EVERY = 15        # seconds of the time limit that one reading stands for
LPAGE_STALL = 6         # readings in a row without an increase: the backing has stopped
LPAGE_NONODE = 8        # readings in a row without a vsish node (about 2 min): the backing cannot be read


def lpage_read(name):
    """One reading of the 1GB page backing: (state, current, target, failed).
    state: off (1GB pages not set in the .vmx), poweredoff, nonode (no readable vsish node), ok."""
    row = find_vm(all_vms(), name)
    lines = read_vmx(vmx_path(row))
    on = values_of(lines, "sched.mem.lpage.enable1GPage")
    if not (len(on) == 1 and on[0].strip().upper() == "TRUE"):
        return "off", 0, 0, 0
    mem = values_of(lines, "memSize")
    try:
        target = int(mem[-1]) // 1024
    except (IndexError, ValueError):
        raise Fail("VM '%s': memSize of the .vmx is not a number: %s" % (name, show(mem)))
    if power_state(row["id"]) != "on":
        return "poweredoff", 0, target, 0
    rc, listed = run(["vsish", "-e", "ls", LPAGE + "/"])
    have = set(x.strip().strip("/") for x in listed.splitlines()) if rc == 0 else set()
    ids = lpage_ids(name)
    for vid in [i for i in ids if i in have] + [i for i in ids if i not in have]:
        rc, text = run(["vsish", "-e", "get", "%s/%s" % (LPAGE, vid)])
        cur = re.search(r"Current number of 1GB page backings\s*:\s*(\d+)", text, re.I) if rc == 0 else None
        if not cur:
            continue
        failed = re.search(r"1GB page backing attempts failed\s*:\s*(\d+)", text, re.I)
        return "ok", int(cur.group(1)), target, int(failed.group(1)) if failed else 0
    return "nonode", 0, target, 0


def cmd_lpage(name):
    """1GB page backing of a VM: TCGEN-LPAGE <current> <target> <failed>, or off (1GB pages not set), poweredoff, nonode."""
    state, n, target, f = lpage_read(name)
    text = ("%d %d %d" % (n, target, f)) if state == "ok" else state
    out("TCGEN-LPAGE " + text)
    result("lpage", text)


# ---------------------------------------------------------------- configuration snapshot (read only)
# The items and their names are those of the VM table of config_snapshot (VM basic information, vm advanced config,
# CPU affinity). Here they are read from the .vmx file: that is the place where the settings of tcgen (set lines) go.
SNAP_ADV_KEYS = ("sched.mem.lpage.enable1GPage", "sched.mem.pin", "sched.mem.prealloc", "sched.mem.prealloc.pinnedMainMem",
                 "sched.swap.vmxSwapEnabled", "numa.vcpu.preferHT", "numa.vcpu.maxPerVirtualNode",
                 "monitor_control.halt_in_monitor", "monitor.idleLoopSpinBeforeHalt", "monitor.idleLoopMinSpinUS",
                 "monitor_control.disable_pause_loop_exiting", "vpmc.enable")
SNAP_BASIC_KEYS = ("numvcpus", "cpuid.corespersocket", "memsize", "sched.mem.min", "sched.cpu.shares", "sched.mem.shares",
                   "sched.cpu.latencysensitivity")
SNAP_AFF_FIXED = ("sched.cpu.affinity", "numa.nodeAffinity")
SNAP_AFF_RE = re.compile(r"^sched\.(vcpu|node)(\d+)\.affinity$")


def read_snap_spec(data):
    vms, keys = [], []
    for raw in data.decode("ascii", "replace").split("\n"):
        line = raw.rstrip("\r").strip()
        if not line or line.startswith("#"):
            continue
        word, _, rest = line.partition(" ")
        rest = rest.strip()
        if word == "vm" and rest:
            if rest not in vms:
                vms.append(rest)
        elif word == "key" and rest:
            if rest.lower() not in [k.lower() for k in keys]:
                keys.append(rest)
        else:
            raise Fail("snapshot spec: unknown line: %r" % line)
    if not vms:
        raise Fail("snapshot spec: no vm line")
    return vms, keys


def snap_number(values):
    try:
        return int(values[0]) if len(values) == 1 else None
    except ValueError:
        return None


def snap_vm(name, lines, state, keys):
    """The snapshot rows of one VM: a list of (group, item, value). An empty value: the .vmx has no such key."""
    def one(key):
        return " | ".join(values_of(lines, key))

    rows = [("basic", "power state", state)]
    cpu = snap_number(values_of(lines, "numvcpus")) or 1            # no numvcpus line: 1 vCPU
    cps = snap_number(values_of(lines, "cpuid.coresPerSocket")) or 1
    rows.append(("basic", "cpu", str(cpu)))
    rows.append(("basic", "cores per socket / sockets", "%d / %s" % (cps, (cpu // cps) if cpu % cps == 0 else "?")))
    mem = snap_number(values_of(lines, "memSize"))
    if mem is None:
        rows.append(("basic", "memory (GB)", one("memSize")))
    else:
        rows.append(("basic", "memory (GB)", str(mem // 1024) if mem % 1024 == 0 else "%.1f" % (mem / 1024.0)))
    res = one("sched.mem.min")
    rows.append(("basic", "memory reservation", (res + " MB") if res else ""))
    rows.append(("basic", "cpu shares", one("sched.cpu.shares")))
    rows.append(("basic", "mem shares", one("sched.mem.shares")))
    rows.append(("basic", "latency sensitivity", one("sched.cpu.latencySensitivity")))
    skip = set(SNAP_BASIC_KEYS) | set(k.lower() for k in SNAP_AFF_FIXED)
    done = set()
    for key in list(SNAP_ADV_KEYS) + list(keys):
        low = key.lower()
        if low in skip or low in done or SNAP_AFF_RE.match(low):
            continue
        done.add(low)
        rows.append(("adv", key, one(key)))
    found = {}
    for raw in lines:
        k, v = key_of(raw)
        m = SNAP_AFF_RE.match(k) if k else None
        if m:
            found[(0 if m.group(1) == "vcpu" else 1, int(m.group(2)))] = k
    rows.append(("aff", SNAP_AFF_FIXED[0], one(SNAP_AFF_FIXED[0])))
    for _, k in sorted(found.items()):
        rows.append(("aff", k, one(k)))
    rows.append(("aff", SNAP_AFF_FIXED[1], one(SNAP_AFF_FIXED[1])))
    return rows


def cmd_snapshot(vms, keys):
    """Read only: the settings of each VM as lines TCGEN-CFG <vm>|<group>|<item>|<value>."""
    table = all_vms()
    n = 0
    for name in vms:
        try:
            row = find_vm(table, name)
            lines = read_vmx(vmx_path(row))
            state = power_state(row["id"])
        except Fail as e:
            out("vm %s: settings not read: %s" % (name, e))
            out("TCGEN-CFG %s|basic|power state|not read" % name)
            continue
        for group, item, value in snap_vm(name, lines, state, keys):
            out("TCGEN-CFG %s|%s|%s|%s" % (name, group, item, value.replace("\t", " ")))
        n += 1
    result("snapshot", "%d of %d VM(s) read" % (n, len(vms)))


def cmd_lpagewait(name, seconds):
    """Wait until the 1GB page backing of a powered-on VM is complete. Result "lpage <state> [current target failed]":
    complete, off (1GB pages not set: nothing to wait for), nonode, stalled (no increase, no failed attempts),
    failed (no increase and failed attempts > 0: the host has no free contiguous 1GB memory), timeout, poweredoff."""
    tries = max(1, int(seconds) // LPAGE_EVERY)
    prev, stall, nonode, shown = -1, 0, 0, None
    state, n, target, f = "nonode", 0, 0, 0
    begin = time.time()
    for i in range(tries):
        if i:
            time.sleep(LPAGE_POLL)
        state, n, target, f = lpage_read(name)
        if state in ("off", "poweredoff"):
            out("TCGEN-LPAGE " + state)
            result("lpage", state)
            return
        if state == "nonode":
            nonode += 1
            if nonode >= LPAGE_NONODE:
                out("TCGEN-LPAGE nonode")
                result("lpage", "nonode")
                return
            continue
        nonode = 0
        line = "TCGEN-LPAGE %d %d %d" % (n, target, f)
        if line != shown:                       # one line for every change, not for every reading
            out("%s  (%d s)" % (line, int(time.time() - begin)))
            shown = line
        if n >= target:
            result("lpage", "complete %d %d %d" % (n, target, f))
            return
        stall = stall + 1 if (prev >= 0 and n <= prev) else 0
        if stall >= LPAGE_STALL:
            result("lpage", "%s %d %d %d" % ("failed" if f > 0 else "stalled", n, target, f))
            return
        prev = n
    if state == "nonode":
        out("TCGEN-LPAGE nonode")
        result("lpage", "nonode")
        return
    result("lpage", "timeout %d %d %d" % (n, target, f))


def cmd_rollback(tag):
    path = state_path(tag)
    if not os.path.isfile(path):
        result("nothing", "no change is recorded for tag %s" % tag)
        return
    with open(path) as f:
        state = json.load(f)
    for name in state.get("was_on", []):
        out("TCGEN-WASON %s" % name)
    rows = all_vms() if state["vms"] else []
    for v in state["vms"]:
        if not os.path.isfile(v["backup"]):
            continue                              # prepare ended before this .vmx was written
        row = find_vm(rows, v["name"])
        if power_state(row["id"]) != "off":
            out("%s: powering off before the .vmx is put back" % v["name"])
            shut_down([(v["name"], row["id"])], int(state.get("shutdown_wait", 600)))
        with open(v["backup"], "rb") as f:
            data = f.read()
        replace_file(v["vmx"], data)
        reload_vm(v["name"], row["id"])
        out("TCGEN-DONE vm %s: .vmx put back from %s" % (v["name"], v["backup"]))
    for h in state["host"]:
        host_set(h["path"], h["type"], h["old"])
        out("TCGEN-DONE host %s: put back to %s" % (h["path"], h["old"]))
    os.rename(path, path + ".rolledback")
    result("rolledback", "%d VM(s), %d host option(s)" % (len(state["vms"]), len(state["host"])))


def main(argv):
    lock = None
    try:
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (AttributeError, ValueError, OSError):
            pass
        if len(argv) >= 2 and argv[1] == "version":
            sys.stdout.write(VERSION + "\n")
            return 0
        if len(argv) == 2 and argv[1] == "check":
            cmd_check(read_spec(sys.stdin.buffer.read()))
        elif len(argv) == 3 and argv[1] == "prepare":
            spec = read_spec(sys.stdin.buffer.read())
            lock = take_lock()
            cmd_prepare(argv[2], spec)
        elif len(argv) in (3, 4) and argv[1] == "poweron":
            lock = take_lock()
            cmd_poweron(argv[2], int(argv[3]) if len(argv) == 4 else POWERON_WAIT)
        elif len(argv) == 3 and argv[1] == "rollback":
            lock = take_lock()
            cmd_rollback(argv[2])
        elif len(argv) in (3, 4) and argv[1] == "poweroff":
            lock = take_lock()
            cmd_poweroff(argv[2], int(argv[3]) if len(argv) == 4 else 600)
        elif len(argv) == 3 and argv[1] == "lpage":
            cmd_lpage(argv[2])
        elif len(argv) in (3, 4) and argv[1] == "lpagewait":
            cmd_lpagewait(argv[2], int(argv[3]) if len(argv) == 4 else POWERON_WAIT)
        elif len(argv) == 2 and argv[1] == "snapshot":
            cmd_snapshot(*read_snap_spec(sys.stdin.buffer.read()))
        else:
            out("usage: tcgen_apply.py check | prepare <tag> | poweron <vm> [seconds] | poweroff <vm> [seconds] | lpage <vm> | "
                "lpagewait <vm> [seconds] | rollback <tag> | snapshot   (spec on stdin)")
            result("error", "bad arguments")
            return 1
    except Fail as e:
        result("error", str(e))
        return 1
    except Exception as e:          # an unexpected error must still end with a result line for the runner
        result("error", "unexpected %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        if lock:
            drop_lock(lock)
        save_log(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
