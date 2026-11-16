# -*- coding: utf-8 -*-
"""
make_v14_fixture.py - v1.4 의 생성 결과를 tests/fixtures/v14_blocks.json 에 저장한다.

v2 의 시각 지정 방식이 v1.4 와 같은 결과를 내는지 확인하는 기준 자료다(tests/test_v14_compat.py 가 읽는다).
v1.4 소스(tcgen_core.py, VERSION 1.4)가 있는 폴더를 인자로 준다. 인자가 없으면 이 폴더 옆의 ../tcgen 을 쓴다.

    python tests/make_v14_fixture.py [v1.4 폴더]

v1.4 는 바뀌지 않으므로 이 스크립트는 입력(scenarios)을 늘릴 때만 다시 실행한다.
"""
import copy
import datetime as dt
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "v14_blocks.json")
NOW = "2026-11-09 15:30:12"

RUN = [
    {"app": "AppA", "name": "job_a", "desc": "1 job",
     "cmd": "/home/tester/bench/run_a.sh -o x%y >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1"},
    {"app": "AppB", "name": "job_b4", "desc": "4 jobs", "cmd": "/home/tester/bench/run_b.sh -n 4 -d \"`date +%H`\""},
    {"app": "AppC", "name": "job.c-1", "desc": "quotes", "cmd": "cd /data && ./run 'a b' \"$HOME/x\" \\$1 > out.log 2>&1 &"},
]
MON = [
    {"app": "mon", "name": "guest_perf", "desc": "", "where": "VM",
     "start": "/home/tester/mon/start_mon.sh >> /home/tester/log/mon-<vm이름>-<실행시각>.log",
     "stop": "/home/tester/mon/stop_mon.sh"},
    {"app": "mon", "name": "host_mon", "desc": "", "where": "ESX",
     "start": "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start <vm이름> <실행시각> '%H'",
     "stop": "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh stop"},
    {"app": "mon", "name": "guest_gpu", "desc": "GPU", "where": "VM", "start": "/home/tester/mon/gpu_mon.sh", "stop": ""},
]


def vm(name, job="(없음)", monitors=(), end_ref=False):
    return {"name": name, "job": job, "monitors": list(monitors), "end_ref": end_ref}


def case(name, esx, vms, start="", interval="12:00", esx_monitors=(), stop_mode="auto", stop_time="", applied=False,
         desc="", group="", end_time=""):
    return {"applied": applied, "name": name, "esx": esx, "vms": vms, "esx_monitors": list(esx_monitors),
            "interval": interval, "start": start, "stop_mode": stop_mode, "stop_time": stop_time,
            "desc": desc, "group": group, "end_time": end_time}


def settings(**over):
    s = {"record_csv": "", "log_dir": "", "vm_user": "", "mon_lead_min": 5, "stop_lead_min": 10, "vm_prefix": "ev",
         "default_vms": 2}
    s.update(over)
    return s


def scenarios():
    out = []
    a = [case("TC-a", "esx01", [vm("esx01ev01", "job_a", ["guest_perf"]), vm("esx01ev02", "(없음)", ["guest_perf"])],
              start="2026-11-20 00:00:00", esx_monitors=["host_mon"]),
         case("TC-b4", "esx01", [vm("esx01ev01", "job_a", ["guest_perf"]), vm("esx01ev02", "job_b4", ["guest_perf"])],
              start="2026-11-20 12:00:00", esx_monitors=["host_mon"]),
         case("TC-b4-r2", "esx01", [vm("esx01ev01", "job_a", ["guest_perf"]), vm("esx01ev02", "job_b4", ["guest_perf"])],
              start="2026-11-21 00:00:00", esx_monitors=["host_mon"], stop_mode="none")]
    out.append({"name": "three_cases", "cases": a, "settings": settings()})
    b = [case("multi.1", "esx01", [vm("esx01ev01", "job_a", ["guest_perf", "guest_gpu"], True), vm("esx01ev02", "job_b4"),
                                   vm("esx01ev03", "job.c-1", ["guest_gpu"]), vm("esx01ev04")],
              start="2026-12-01 09:30:00", interval="8", esx_monitors=["host_mon"], stop_mode="manual",
              stop_time="2026-12-01 17:00:00", desc="설명", group="그룹1"),
         case("multi.2", "esx01", [vm("esx01ev01", "job_b4"), vm("esx01ev02", "job_a", ["guest_perf"])],
              start="2026-12-01 17:30:00", interval="4:30")]
    out.append({"name": "user_and_four_vms", "cases": b, "settings": settings(vm_user="tester", mon_lead_min=3, stop_lead_min=7)})
    c = [case("h1-1", "esx01", [vm("esx01ev01", "job_a", ["guest_perf"])], start="2026-11-25 01:00:00", interval="6:00",
              esx_monitors=["host_mon"]),
         case("h2-1", "esx02", [vm("esx02ev01", "job_a"), vm("esx02ev02", "job_a", ["guest_perf"])],
              start="2026-11-25 07:00:00", interval="6:00", esx_monitors=["host_mon"]),
         case("h1-2", "esx01", [vm("esx01ev01", "job_b4", ["guest_perf"])], start="2026-11-25 13:00:00", interval="6:00",
              applied=True),
         case("h2-2", "esx02", [vm("esx02ev01", "job_b4", ["guest_perf"])], start="2026-11-25 19:00:00", interval="6:00",
              esx_monitors=["host_mon"], stop_mode="none")]
    out.append({"name": "two_esx_hosts_and_applied", "cases": c, "settings": settings()})
    d = [case("jobs-only", "", [vm("vm-a", "job_a"), vm("vm-b", "job.c-1")], start="2026-11-30 22:15:00", interval="")]
    out.append({"name": "jobs_only_no_esx", "cases": d, "settings": settings(vm_user="user1")})
    e = [case("past", "esx01", [vm("esx01ev01", "job_a", ["guest_perf"])], start="2026-11-01 00:00:00", interval="1:00")]
    out.append({"name": "past_time_warning", "cases": e, "settings": settings()})
    f = [case("dup", "esx01", [vm("esx01ev01", "job_a")], start="2026-11-20 00:00:00"),
         case("dup", "esx01", [vm("esx01ev01", "job_a")], start="2026-11-20 12:00:00"),
         case("bad name", "esx01", [vm("esx01ev01", "nojob")], start="2026-11-21 00:00:00")]
    out.append({"name": "errors", "cases": f, "settings": settings()})
    return out


def run_one(core, sc, mode=None):
    st = dict(sc["settings"])
    if mode:
        st["run_mode"] = mode
    st = core.resolve_paths("/nonexistent/tcgen_fixture", st)
    now = dt.datetime.strptime(NOW, "%Y-%m-%d %H:%M:%S")
    cases = [core._clean_case(c) for c in copy.deepcopy(sc["cases"])]
    res = core.generate(cases, copy.deepcopy(RUN), copy.deepcopy(MON), st, now=now, write=False)
    return {"errors": res["errors"], "warnings": res["warnings"], "setup": res["setup"], "vm": res["vm"],
            "esx": res["esx"], "vm_wipe": res["vm_wipe"], "wipe_hosts": res["wipe_hosts"],
            "wipe_conflicts": res["wipe_conflicts"], "lines": [e["line"] for e in res["entries"]],
            "skipped": res["skipped"]}


def load_core(folder):
    """v1.4 의 tcgen_core.py 를 읽어 들인다. v1.4 폴더에는 아무것도 쓰지 않는다(바이트코드 파일도 만들지 않음)."""
    spec = importlib.util.spec_from_file_location("tcgen_core_v14", os.path.join(folder, "tcgen_core.py"))
    mod = importlib.util.module_from_spec(spec)
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
    return mod


def main(argv):
    folder = argv[0] if argv else os.path.join(os.path.dirname(os.path.dirname(HERE)), "tcgen")
    core = load_core(folder)
    if core.VERSION != "1.4":
        raise SystemExit("v1.4 소스가 아님: %s (VERSION %s)" % (folder, core.VERSION))
    data = {"source": "tcgen v1.4 tcgen_core.generate", "now": NOW, "run": RUN, "mon": MON, "scenarios": []}
    for sc in scenarios():
        data["scenarios"].append(dict(sc, expect=run_one(core, sc)))
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with io.open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    print("저장: %s (입력 %d가지)" % (FIXTURE, len(data["scenarios"])))


if __name__ == "__main__":
    main(sys.argv[1:])
