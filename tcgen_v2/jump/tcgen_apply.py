# -*- coding: utf-8 -*-
"""
tcgen_apply.py 2.2 - apply ESXi host advanced options and VM configuration (.vmx) keys for one test case.

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

spec (one item per line, ASCII):
  opt shutdown_wait <seconds>      how long to wait for the guest shutdown before a hard power off (default 600)
  vm <name>                        every VM of the case, in power-on order
  host </Section/Option>=<value>   ESXi advanced option (esxcli system settings advanced)
  set <vm> <key>=<value>           .vmx key of that VM. An empty value removes the key.

Commands used (Broadcom KB): vim-cmd vmsvc/getallvms, get.summary, power.getstate, power.shutdown, power.off,
power.on, reload (KB 343248, 320877, 345049, 308360); esxcli system settings advanced list/set (KB 310338).

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

VERSION = "2.2"
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
    spec = {"vms": [], "host": [], "set": {}, "opt": {"shutdown_wait": 600}}
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
    if changes:
        result("wouldchange", "%d value(s) differ" % changes)
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
        result("poweredon")
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
        else:
            out("usage: tcgen_apply.py check | prepare <tag> | poweron <vm> [seconds] | rollback <tag>   (spec on stdin)")
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
