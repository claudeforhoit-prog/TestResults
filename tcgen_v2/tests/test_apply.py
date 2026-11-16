# -*- coding: utf-8 -*-
"""
tcgen_apply.py (ESX 호스트에서 실행하는 설정 적용 스크립트) 시험.

가짜 vim-cmd, esxcli(fakeenv.py. 출력 형식은 Broadcom KB 343248, 320877, 310338 의 예시를 따름)와 가짜 vmx 폴더로 실행한다.
실제 ESX 의 출력 형식이 이 가짜와 다르면 이 시험으로는 찾지 못한다(README 의 '실제 장비에서 확인할 것' 참조).
"""
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakeenv  # noqa: E402

APPLY = os.path.join(fakeenv.BASE, "jump", "tcgen_apply.py")
ODD_VMX = (b'.encoding = "UTF-8"\n'
           b'displayName = "esx01ev01"\n'
           b'# a comment line with sched.mem.pin = "X"\n'
           b'sched.mem.pin = "FALSE"\n'
           b'  weird.spacing   =    "keep me"   \n'
           b'annotation = "caf\xe9 | note"\n'
           b'Sched.Mem.Pin = "TRUE"\n'
           b'numa.nodeAffinity = "0"\n'
           b'SCHED.MEM.PIN = "maybe"\n'
           b'sched.mem.pin.extra = "other key"\n'
           b'guestOS = "rhel8-64"\n')


@unittest.skipUnless(fakeenv.HAVE, "perl, sh 가 있는 Linux/macOS 에서만 실행")
class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.e = fakeenv.FakeEnv(install=False)
        for n in ("esx01ev01", "esx01ev02"):
            self.e.add_vm(n)
        self.host = self.e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {})])

    def tearDown(self):
        self.e.close()

    def apply(self, args, spec="", wait=True):
        d = self.host
        env = dict(self.e.env, HOME=d, TCGEN_TMP=os.path.join(d, "tmp"), TCGEN_VMFS_ROOT=os.path.join(d, "vmfs"),
                   TCGEN_POLL_SEC="0.05", FAKE_ESX_HOME=d, FAKE_HOST="esx01", TZ="UTC")
        p = subprocess.Popen([sys.executable, APPLY] + list(args), env=env, cwd=d, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not wait:
            return p
        out, _ = p.communicate(spec.encode("ascii"), timeout=120)
        lines = out.decode("utf-8", "replace").splitlines()
        self.assertTrue(lines and lines[-1].startswith("TCGEN-RESULT "), "마지막 줄은 항상 TCGEN-RESULT: %r" % lines[-3:])
        self.assertEqual(p.returncode, 1 if lines[-1].startswith("TCGEN-RESULT error") else 0, lines)
        return lines[-1][len("TCGEN-RESULT "):], lines

    def calls(self, word=None):
        c = self.e.esx_calls("esx01")
        return [x for x in c if word in x] if word else c

    def changing_calls(self):
        return [x for x in self.calls() if any(w in x for w in ("power.shutdown", "power.off", "power.on", "reload", " set "))]

    def set_vm(self, name, **over):
        import json
        p = os.path.join(self.host, "vms.json")
        with open(p) as f:
            d = json.load(f)
        for v in d["vms"]:
            if v["name"] == name:
                v.update(over)
        with open(p, "w") as f:
            json.dump(d, f)

    def write_vmx(self, vm, data):
        with open(self.e.vmx_path("esx01", vm), "wb") as f:
            f.write(data)

    def read_vmx(self, vm):
        with open(self.e.vmx_path("esx01", vm), "rb") as f:
            return f.read()

    def backups(self, vm):
        d = os.path.dirname(self.e.vmx_path("esx01", vm))
        return sorted(n for n in os.listdir(d) if ".vmx.tcgen-" in n)

    SPEC = "opt shutdown_wait 30\nvm esx01ev01\nvm esx01ev02\n"

    # ---- (b) 값이 이미 같으면 아무것도 바꾸지 않는다
    def test_nochange_touches_nothing(self):
        before = self.read_vmx("esx01ev01")
        res, lines = self.apply(["prepare", "t1"], self.SPEC + 'host /Numa/LocalityWeightActionAffinity=130\n'
                                'set esx01ev01 numa.nodeAffinity=0\nset esx01ev01 NUMVCPUS=8\nset esx01ev02 not.there=\n')
        self.assertEqual(res, "nochange")
        self.assertEqual(self.changing_calls(), [])
        self.assertEqual(self.read_vmx("esx01ev01"), before)
        self.assertEqual(self.backups("esx01ev01"), [])
        self.assertIn('TCGEN-SET esx01ev01 numa.nodeAffinity: "0" -> "0"  [same]', lines)
        self.assertIn("TCGEN-HOST /Numa/LocalityWeightActionAffinity (integer): 130 -> 130  [same]", lines)
        self.assertFalse(os.path.exists(os.path.join(self.host, "tmp", "state-t1.json")))

    def test_check_is_read_only(self):
        spec = self.SPEC + "host /Cpu/HTWholeCoreThreshold=0\nset esx01ev02 sched.mem.pin=TRUE\n"
        res, lines = self.apply(["check"], spec)
        self.assertEqual(res, "wouldchange 2 value(s) differ")
        self.assertEqual(self.changing_calls(), [])
        self.assertTrue(any(x.startswith("TCGEN-VM esx01ev01 vmid=10 power=on vmx=") for x in lines))
        self.assertIn('TCGEN-SET esx01ev02 sched.mem.pin: (none) -> "TRUE"  [change]', lines)
        self.assertEqual(self.apply(["check"], self.SPEC + "set esx01ev01 numa.nodeAffinity=0\n")[0], "nochange")

    # ---- 기본 흐름: 종료, 호스트 옵션, vmx, reload, 꺼진 채로 둠
    def test_prepare_changes_and_leaves_vms_off(self):
        spec = self.SPEC + ("host /Numa/LocalityWeightActionAffinity=0\nhost /Misc/HostName=new name\n"
                            "set esx01ev01 sched.mem.pin=TRUE\nset esx01ev01 numa.nodeAffinity=\n")
        before2 = self.read_vmx("esx01ev02")
        res, lines = self.apply(["prepare", "t1"], spec)
        self.assertEqual(res, "changed 4 value(s)")
        c = self.calls()
        order = [c.index(x) for x in ("vim-cmd vmsvc/power.shutdown 10", "vim-cmd vmsvc/power.shutdown 11",
                                      "esxcli system settings advanced set -o /Numa/LocalityWeightActionAffinity -i 0",
                                      "esxcli system settings advanced set -o /Misc/HostName -s new name",
                                      "vim-cmd vmsvc/reload 10")]
        self.assertEqual(order, sorted(order), "종료, 호스트 옵션, vmx 와 reload 의 순서")
        self.assertEqual(self.calls("power.on"), [])
        self.assertEqual(self.calls("reload 11"), [], "바꾸지 않은 VM 은 reload 하지 않음")
        self.assertEqual([v["state"] for v in self.e.esx_vms("esx01").values()], ["off", "off"])
        self.assertEqual(self.e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        self.assertEqual(self.e.esx_adv("esx01", "/Misc/HostName"), "new name")
        text = self.read_vmx("esx01ev01").decode()
        self.assertTrue(text.endswith('guestOS = "rhel8-64"\nsched.mem.pin = "TRUE"\n'))
        self.assertNotIn("numa.nodeAffinity", text)
        self.assertEqual(self.read_vmx("esx01ev02"), before2, "설정이 없는 VM 의 vmx 는 그대로")
        self.assertEqual(len(self.backups("esx01ev01")), 1)
        self.assertEqual(self.backups("esx01ev02"), [])
        self.assertIn("TCGEN-DONE host /Numa/LocalityWeightActionAffinity: 130 -> 0", lines)
        self.assertEqual(os.stat(self.e.vmx_path("esx01", "esx01ev01")).st_mode & 0o777,
                         os.stat(self.e.vmx_path("esx01", "esx01ev02")).st_mode & 0o777)

    # ---- (c) 같은 키가 다른 값으로 여러 줄, 대소문자 다른 키, 키 삭제
    def test_vmx_same_key_on_many_lines(self):
        self.write_vmx("esx01ev01", ODD_VMX)
        res, lines = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 sched.mem.pin=TRUE\nset esx01ev01 numa.nodeAffinity=\n")
        self.assertEqual(res, "changed 2 value(s)")
        self.assertIn('TCGEN-SET esx01ev01 sched.mem.pin: 3 lines: "FALSE", "TRUE", "maybe" -> "TRUE"  [change]', lines)
        want = (b'.encoding = "UTF-8"\n'
                b'displayName = "esx01ev01"\n'
                b'# a comment line with sched.mem.pin = "X"\n'
                b'  weird.spacing   =    "keep me"   \n'
                b'annotation = "caf\xe9 | note"\n'
                b'sched.mem.pin.extra = "other key"\n'
                b'guestOS = "rhel8-64"\n'
                b'sched.mem.pin = "TRUE"\n')
        self.assertEqual(self.read_vmx("esx01ev01"), want, "목표 키의 줄만 바뀌고 다른 줄은 바이트 그대로")
        d = os.path.dirname(self.e.vmx_path("esx01", "esx01ev01"))
        with open(os.path.join(d, self.backups("esx01ev01")[0]), "rb") as f:
            self.assertEqual(f.read(), ODD_VMX, "백업은 바꾸기 전 내용")
        self.assertEqual([n for n in os.listdir(d) if n.endswith("tcgen-new")], [])
        # 다시 실행하면 변경 없음
        self.assertEqual(self.apply(["prepare", "t2"], self.SPEC + "set esx01ev01 SCHED.mem.PIN=TRUE\nset esx01ev01 numa.nodeAffinity=\n")[0],
                         "nochange")

    def test_vmx_single_wrong_case_key_and_last_spec_line_wins(self):
        self.write_vmx("esx01ev01", b'Numa.NodeAffinity = "1"\r\nguestOS = "rhel8-64"\r\n')
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 numa.nodeAffinity=7\nset esx01ev01 NUMA.nodeaffinity=0,1\n")
        self.assertEqual(res, "changed 1 value(s)")
        self.assertEqual(self.read_vmx("esx01ev01"), b'guestOS = "rhel8-64"\r\nNUMA.nodeaffinity = "0,1"\r\n', "CR+LF 파일은 새 줄도 CR+LF")

    def test_vmx_unquoted_value_and_no_final_newline(self):
        self.write_vmx("esx01ev01", b'a.key = old\nb.key = "2"')
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 a.key=new\n")
        self.assertEqual(res, "changed 1 value(s)")
        self.assertEqual(self.read_vmx("esx01ev01"), b'b.key = "2"\na.key = "new"\n')

    def test_keeps_five_backups(self):
        d = os.path.dirname(self.e.vmx_path("esx01", "esx01ev01"))
        for n in range(1, 8):
            open(os.path.join(d, "esx01ev01.vmx.tcgen-2026010100000%d" % n), "w").close()
        open(os.path.join(d, "esx01ev01.vmx.tcgen-notes"), "w").close()
        self.assertEqual(self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 x.y=1\n")[0], "changed 1 value(s)")
        left = self.backups("esx01ev01")
        self.assertEqual(len(left), 6, left)
        self.assertEqual(left[:4], ["esx01ev01.vmx.tcgen-2026010100000%d" % n for n in (4, 5, 6, 7)])
        self.assertIn("esx01ev01.vmx.tcgen-notes", left, "형식이 다른 이름의 파일은 지우지 않음")

    # ---- (d) 게스트 종료가 안 되면 전원을 끈다
    def test_guest_ignores_shutdown_then_power_off(self):
        self.set_vm("esx01ev01", shutdown="ignore")
        t0 = time.time()
        res, lines = self.apply(["prepare", "t1"], "opt shutdown_wait 2\nvm esx01ev01\nvm esx01ev02\nset esx01ev02 x.y=1\n")
        self.assertEqual(res, "changed 1 value(s)")
        self.assertGreaterEqual(time.time() - t0, 2.0)
        self.assertEqual(self.calls("power.off"), ["vim-cmd vmsvc/power.off 10"], "끝까지 켜져 있던 VM 만 전원 끄기")
        self.assertIn("  esx01ev01: still on 2 s after the guest shutdown request: power off", lines)
        self.assertEqual([v["state"] for v in self.e.esx_vms("esx01").values()], ["off", "off"])

    def test_tools_not_running_power_off_at_once(self):
        self.set_vm("esx01ev01", shutdown="fault")
        self.set_vm("esx01ev02", shutdown="fault0")
        res, lines = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev02 x.y=1\n")
        self.assertEqual(res, "changed 1 value(s)")
        self.assertEqual(self.calls("power.off"), ["vim-cmd vmsvc/power.off 10", "vim-cmd vmsvc/power.off 11"])
        self.assertTrue(any("guest shutdown is not possible" in x and "VMware Tools is not running" in x for x in lines))

    def test_power_off_failure_is_an_error_before_any_change(self):
        self.set_vm("esx01ev01", shutdown="fault", poweroff="fail")
        before = self.read_vmx("esx01ev02")
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "host /Cpu/HTWholeCoreThreshold=0\nset esx01ev02 x.y=1\n")
        self.assertTrue(res.startswith("error VM 'esx01ev01' did not power off"), res)
        self.assertEqual(self.calls(" set "), [])
        self.assertEqual(self.read_vmx("esx01ev02"), before)

    # ---- (i) 없는 VM, 없는 호스트 옵션은 변경 전 오류
    def test_unknown_vm_changes_nothing(self):
        res, _ = self.apply(["prepare", "t1"], "vm esx01ev01\nvm esx01ev09\nhost /Cpu/HTWholeCoreThreshold=0\nset esx01ev01 x.y=1\n")
        self.assertTrue(res.startswith("error VM 'esx01ev09' is not registered"), res)
        self.assertEqual(self.changing_calls(), [])
        self.assertEqual(self.backups("esx01ev01"), [])

    def test_unknown_host_option_changes_nothing(self):
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "host /Numa/NoSuchOption=1\nset esx01ev01 x.y=1\n")
        self.assertTrue(res.startswith("error host option '/Numa/NoSuchOption' cannot be read"), res)
        self.assertIn("Unable to find option NoSuchOption", res)
        self.assertEqual(self.changing_calls(), [])

    def test_integer_option_with_text_value_changes_nothing(self):
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "host /Cpu/HTWholeCoreThreshold=many\n")
        self.assertIn("is an integer option", res)
        self.assertEqual(self.changing_calls(), [])

    def test_vm_names_must_match_exactly(self):
        import json
        p = os.path.join(self.host, "vms.json")
        with open(p) as f:
            d = json.load(f)
        d["vms"].append(dict(d["vms"][0], id="20", name="esx01ev01 clone", file="clone/clone.vmx",
                             note="first line", note_more=["30 esx01ev09 [datastore1] fake/annotation.txt line"]))
        d["vms"].append(dict(d["vms"][1], id="21", file="dup/esx01ev02.vmx"))
        with open(p, "w") as f:
            json.dump(d, f)
        res, lines = self.apply(["check"], "vm esx01ev01\nset esx01ev01 x.y=1\n")
        self.assertEqual(res, "wouldchange 1 value(s) differ")
        self.assertTrue(any(x.startswith("TCGEN-VM esx01ev01 vmid=10 ") for x in lines), "이름에 공백이 있는 다른 VM 과 섞이지 않음")
        self.assertTrue(self.apply(["prepare", "t1"], "vm esx01ev02\nset esx01ev02 x.y=1\n")[0].startswith(
            "error the VM name 'esx01ev02' is on 2 lines"))
        self.assertTrue(self.apply(["prepare", "t1"], "vm esx01ev09\nset esx01ev09 x.y=1\n")[0].startswith(
            "error VM 'esx01ev09' is not registered"), "설명 칸의 글을 VM 줄로 읽지 않음")
        self.assertEqual(self.changing_calls(), [])

    def test_suspended_vm_changes_nothing(self):
        self.set_vm("esx01ev02", state="suspended")
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 x.y=1\n")
        self.assertTrue(res.startswith("error suspended VM: esx01ev02"), res)
        self.assertEqual(self.changing_calls(), [])

    def test_missing_vmx_and_summary_fallback(self):
        self.set_vm("esx01ev01", no_summary=True)
        self.assertEqual(self.apply(["check"], "vm esx01ev01\nset esx01ev01 numa.nodeAffinity=0\n")[0], "nochange",
                         "get.summary 가 안 되면 getallvms 의 File 칸을 쓴다")
        os.remove(self.e.vmx_path("esx01", "esx01ev02"))
        self.assertIn(".vmx file of VM 'esx01ev02' not found", self.apply(["check"], "vm esx01ev02\n")[0])

    def test_bad_spec_lines(self):
        for spec in ("hello world\n", "set esx01ev01 x.y=1\n", "vm esx01ev01\nset esx01ev01 nokey\n",
                     'vm esx01ev01\nset esx01ev01 k=a"b\n', "host /A/B=\n", "vm esx01ev01\nvm esx01ev01\n", "", "opt shutdown_wait soon\nvm a\n"):
            res, _ = self.apply(["prepare", "t1"], spec)
            self.assertTrue(res.startswith("error spec:"), (spec, res))
        self.assertEqual(self.apply(["prepare", "bad/tag"], "vm esx01ev01\n")[0][:13], "error bad tag")
        self.assertEqual(self.apply(["frobnicate"])[0], "error bad arguments")
        self.assertEqual(self.changing_calls(), [])

    # ---- rollback
    def test_host_set_failure_then_rollback(self):
        orig1, orig2 = self.read_vmx("esx01ev01"), self.read_vmx("esx01ev02")
        spec = self.SPEC + ("host /Cpu/HTWholeCoreThreshold=0\nhost /Numa/LocalityWeightActionAffinity=999\n"
                            "set esx01ev01 x.y=1\n")
        res, _ = self.apply(["prepare", "t1"], spec)
        self.assertTrue(res.startswith("error host option '/Numa/LocalityWeightActionAffinity' cannot be set to '999'"), res)
        self.assertEqual(self.e.esx_adv("esx01", "/Cpu/HTWholeCoreThreshold"), "0", "앞 옵션은 이미 바뀐 상태")
        res, lines = self.apply(["rollback", "t1"])
        self.assertEqual(res, "rolledback 0 VM(s), 2 host option(s)")
        self.assertEqual([x for x in lines if x.startswith("TCGEN-WASON")], ["TCGEN-WASON esx01ev01", "TCGEN-WASON esx01ev02"])
        self.assertEqual(self.e.esx_adv("esx01", "/Cpu/HTWholeCoreThreshold"), "800")
        self.assertEqual(self.e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130")
        self.assertEqual((self.read_vmx("esx01ev01"), self.read_vmx("esx01ev02")), (orig1, orig2))
        self.assertEqual(self.apply(["rollback", "t1"])[0], "nothing no change is recorded for tag t1", "두 번째 rollback 은 할 일 없음")

    def test_rollback_restores_vmx_and_powers_off_first(self):
        self.write_vmx("esx01ev01", ODD_VMX)
        spec = self.SPEC + "host /Cpu/HTWholeCoreThreshold=5\nset esx01ev01 sched.mem.pin=TRUE\nset esx01ev02 a.b=c\n"
        orig2 = self.read_vmx("esx01ev02")
        self.assertEqual(self.apply(["prepare", "t1"], spec)[0], "changed 3 value(s)")
        self.assertEqual(self.apply(["poweron", "esx01ev01"])[0], "poweredon")
        n = len(self.calls())
        res, lines = self.apply(["rollback", "t1"])
        self.assertEqual(res, "rolledback 2 VM(s), 1 host option(s)")
        after = self.calls()[n:]
        self.assertIn("vim-cmd vmsvc/power.shutdown 10", after, "켜져 있는 VM 은 끈 뒤에 vmx 를 되돌림")
        self.assertLess(after.index("vim-cmd vmsvc/power.shutdown 10"), after.index("vim-cmd vmsvc/reload 10"))
        self.assertNotIn("vim-cmd vmsvc/power.shutdown 11", after)
        self.assertEqual((self.read_vmx("esx01ev01"), self.read_vmx("esx01ev02")), (ODD_VMX, orig2))
        self.assertEqual(self.e.esx_adv("esx01", "/Cpu/HTWholeCoreThreshold"), "800")
        self.assertEqual([v["state"] for v in self.e.esx_vms("esx01").values()], ["off", "off"], "rollback 은 VM 을 켜지 않음(tcrun 이 순서대로 켠다)")
        self.assertEqual(self.apply(["rollback", "never-used"])[0], "nothing no change is recorded for tag never-used")

    def test_option_that_does_not_take_the_value(self):
        import json
        p = os.path.join(self.host, "adv.json")
        with open(p) as f:
            adv = json.load(f)
        adv["/Cpu/HTWholeCoreThreshold"]["set"] = "ignore"
        with open(p, "w") as f:
            json.dump(adv, f)
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "host /Cpu/HTWholeCoreThreshold=0\n")
        self.assertEqual(res, "error host option '/Cpu/HTWholeCoreThreshold' is '800' after setting '0'")

    def test_reload_failure(self):
        self.set_vm("esx01ev01", reload="fail")
        res, _ = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 x.y=1\n")
        self.assertTrue(res.startswith("error vim-cmd vmsvc/reload failed for VM 'esx01ev01'"), res)

    # ---- poweron
    def test_poweron(self):
        self.assertEqual(self.apply(["poweron", "esx01ev01"])[0], "poweredon")
        self.assertEqual(self.calls("power.on"), [], "이미 켜져 있으면 켜지 않음")
        self.set_vm("esx01ev02", state="off")
        self.assertEqual(self.apply(["poweron", "esx01ev02", "60"])[0], "poweredon")
        self.assertEqual(self.calls("power.on"), ["vim-cmd vmsvc/power.on 11"])
        self.set_vm("esx01ev02", state="off", poweron="fail")
        res, _ = self.apply(["poweron", "esx01ev02"])
        self.assertTrue(res.startswith("error VM 'esx01ev02' did not power on"), res)
        self.assertIn("Failed to power on VM", res)
        self.assertTrue(self.apply(["poweron", "nobody"])[0].startswith("error VM 'nobody' is not registered"))

    def test_poweron_time_limit(self):
        """켜기 대기 한도: 한도 안에 끝나면 걸린 초를 남기고, 한도를 넘기면 전원 상태를 다시 읽어 판정한다."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("tcgen_apply_under_test", APPLY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.POWERON_WAIT, 5400, "한도를 주지 않으면 90분")
        # 한도 안에 끝나는 느린 켜기
        self.set_vm("esx01ev02", state="off", poweron="slow", poweron_sec=2)
        res, lines = self.apply(["poweron", "esx01ev02", "30"])
        self.assertEqual(res, "poweredon")
        self.assertTrue(any(x.startswith("esx01ev02: powered on (vim-cmd vmsvc/power.on took ") for x in lines), lines)
        # 한도를 넘김, VM 은 아직 꺼져 있음: 오류. 켜기 작업이 호스트에서 계속되고 있을 수 있다고 알린다
        self.set_vm("esx01ev02", state="off", poweron="slow", poweron_sec=20)
        t0 = time.time()
        res, _ = self.apply(["poweron", "esx01ev02", "1"])
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(res.startswith("error VM 'esx01ev02' is not 'Powered on' 1 s after vim-cmd vmsvc/power.on was started"), res)
        self.assertIn("may still be running", res)
        # 한도를 넘김, vim-cmd 는 돌아오지 않았지만 VM 은 켜져 있음: 켜진 것으로 판정
        self.set_vm("esx01ev02", state="off", poweron="hang")
        res, lines = self.apply(["poweron", "esx01ev02", "2"])
        self.assertEqual(res, "poweredon")
        self.assertTrue(any("did not return within 2 s, the state is 'Powered on'" in x for x in lines), lines)
        self.assertEqual(self.e.esx_vms("esx01")["esx01ev02"]["state"], "on")

    # ---- 동시에 하나만
    def test_lock(self):
        lock = os.path.join(self.host, "tmp", "lock")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with open(lock, "w") as f:
                f.write(str(other.pid))
            res, _ = self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 x.y=1\n")
            self.assertTrue(res.startswith("error another tcgen_apply.py (pid %d) is running" % other.pid), res)
            self.assertEqual(self.changing_calls(), [])
            self.assertEqual(self.apply(["check"], self.SPEC)[0], "nochange", "읽기만 하는 check 는 기다리지 않음")
        finally:
            other.kill()
            other.wait()
        self.assertEqual(self.apply(["prepare", "t1"], self.SPEC + "set esx01ev01 x.y=1\n")[0], "changed 1 value(s)",
                         "끝난 프로세스가 남긴 잠금 파일은 넘겨받음")
        self.assertFalse(os.path.exists(lock))

    def test_log_file_on_the_host(self):
        self.apply(["check"], self.SPEC + "set esx01ev01 x.y=1\n")
        with open(os.path.join(self.host, "tmp", "apply.log")) as f:
            text = f.read()
        self.assertIn(" check\n", text)
        self.assertIn("TCGEN-RESULT wouldchange", text)


if __name__ == "__main__":
    unittest.main()
