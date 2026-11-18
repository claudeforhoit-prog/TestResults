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
                   TCGEN_POLL_SEC="0.05", TCGEN_LPAGE_POLL_SEC="0.05", FAKE_ESX_HOME=d, FAKE_HOST="esx01", TZ="UTC")
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
        self.assertEqual(self.e.esx_adv("esx01", "/Cpu/HTWholeCoreThreshold"), "0", "앞 옵션은 이미 변경된 상태")
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
        self.assertEqual(self.apply(["poweron", "esx01ev01"])[0], "alreadyon")
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

    def test_poweroff(self):
        res, lines = self.apply(["poweroff", "esx01ev01", "30"])
        self.assertEqual(res, "poweredoff")
        self.assertIn("vim-cmd vmsvc/power.shutdown 10", self.calls())
        self.assertEqual(self.e.esx_vms("esx01")["esx01ev01"]["state"], "off")
        self.assertEqual(self.apply(["poweroff", "esx01ev01"])[0], "alreadyoff", "이미 꺼져 있으면 아무것도 하지 않음")
        self.set_vm("esx01ev02", shutdown="fault")
        self.assertEqual(self.apply(["poweroff", "esx01ev02", "5"])[0], "poweredoff", "게스트 종료를 못 하면 전원을 끈다")
        self.assertIn("vim-cmd vmsvc/power.off 11", self.calls())
        self.assertTrue(self.apply(["poweroff", "nobody"])[0].startswith("error VM 'nobody' is not registered"))

    def write_lpage_vmx(self, vm, on=True, mem=16384):
        text = self.read_vmx(vm).decode("latin-1")
        text = text.replace('memSize = "16384"', 'memSize = "%d"' % mem)
        if on:
            text += 'sched.mem.lpage.enable1GPage = "TRUE"\n'
        self.write_vmx(vm, text.encode("latin-1"))

    def test_lpage(self):
        """1GB page backing 읽기: vmx 에 1GB 설정이 없으면 off, 꺼져 있으면 poweredoff, 켜져 있으면 현재 수, 목표(GB), 실패 수."""
        self.assertEqual(self.apply(["lpage", "esx01ev01"])[0], "lpage off", "1GB 설정이 없는 VM")
        self.write_lpage_vmx("esx01ev01", mem=65536)
        self.set_vm("esx01ev01", state="off")
        self.assertEqual(self.apply(["lpage", "esx01ev01"])[0], "lpage poweredoff")
        import json
        p = os.path.join(self.host, "vms.json")
        with open(p) as f:
            d = json.load(f)
        clock = os.path.join(self.e.root, "clock")
        with open(clock) as f:
            r0, v0, sp = [float(x) for x in f.read().split()]
        now = v0 + (time.time() - r0) * sp
        for v in d["vms"]:
            if v["name"] == "esx01ev01":
                v.update(state="on", on_at=now - 5, lpage_rate=2, lpage_failed=3)
        with open(p, "w") as f:
            json.dump(d, f)
        res, lines = self.apply(["lpage", "esx01ev01"])
        cur, tgt, failed = [int(x) for x in res.split()[1:]]
        self.assertEqual((tgt, failed), (64, 3), "목표 = memSize MB / 1024")
        self.assertTrue(cur >= 8, "켠 뒤 늘어난 1GB backing 수: %s" % res)
        self.assertIn("esxcli vm process list", self.calls())
        self.assertIn("vsish -e get /memory/lpage/vmLpage/100010", self.calls())
        self.set_vm("esx01ev01", lpage_node=False)
        self.assertEqual(self.apply(["lpage", "esx01ev01"])[0], "lpage nonode", "vsish 노드를 읽지 못함")

    def test_lpagewait(self):
        """lpagewait: 앞선 시험의 VM 켜기 확인과 같은 규칙. 1GB backing 수가 memSize/1024 에 닿으면 complete,
        6번 연속으로 늘지 않으면 stalled(실패 수 0) 또는 failed(실패 수 > 0), 시간 한도를 넘으면 timeout."""
        self.assertEqual(self.apply(["lpagewait", "esx01ev01", "60"])[0], "lpage off", "1GB 설정이 없으면 기다리지 않음")
        self.write_lpage_vmx("esx01ev01", mem=65536)
        self.set_vm("esx01ev01", state="on", on_at=self.e.now(), lpage_rate=1, lpage_cap=64)
        res, lines = self.apply(["lpagewait", "esx01ev01", "600"])
        self.assertEqual(res, "lpage complete 64 64 0", lines)
        steps = [x for x in lines if x.startswith("TCGEN-LPAGE ")]
        self.assertTrue(len(steps) >= 2 and steps[-1].startswith("TCGEN-LPAGE 64 64 0"), steps)
        self.set_vm("esx01ev01", on_at=self.e.now(), lpage_cap=20)
        self.assertEqual(self.apply(["lpagewait", "esx01ev01", "600"])[0], "lpage stalled 20 64 0", "늘지 않고 실패도 없음")
        self.set_vm("esx01ev01", lpage_failed=2)
        self.assertEqual(self.apply(["lpagewait", "esx01ev01", "600"])[0], "lpage failed 20 64 2", "실패한 시도가 있으면 failed")
        self.set_vm("esx01ev01", on_at=self.e.now(), lpage_cap=None, lpage_failed=0, lpage_rate=0.1)
        res, _ = self.apply(["lpagewait", "esx01ev01", "30"])
        self.assertTrue(res.startswith("lpage timeout "), res)
        self.set_vm("esx01ev01", lpage_node=False)
        self.assertEqual(self.apply(["lpagewait", "esx01ev01", "600"])[0], "lpage nonode", "vsish 노드가 계속 없음")
        self.set_vm("esx01ev01", state="off")
        self.assertEqual(self.apply(["lpagewait", "esx01ev01", "600"])[0], "lpage poweredoff")
        self.assertEqual(self.changing_calls(), [], "lpagewait 는 읽기만 함")

    def test_snapshot_reads_the_vm_settings_and_changes_nothing(self):
        """구성 스냅샷(config snapshot)의 VM 설정: vmx 의 값을 TCGEN-CFG <vm>|<구간>|<항목>|<값> 줄로 낸다. 항목 이름은 config_snapshot 의
        VM 표와 같다. 읽기만 한다(전원, vmx, 호스트 옵션을 바꾸지 않는다)."""
        vmx2 = (fakeenv.VMX_TEMPLATE % {"name": "esx01ev02"}).replace('numvcpus = "8"', 'numvcpus = "16"\ncpuid.coresPerSocket = "8"')
        vmx2 = vmx2.replace('memSize = "16384"', 'memSize = "25088"') + (
            'sched.mem.min = "16384"\nsched.mem.pin = "TRUE"\nsched.cpu.shares = "10000"\nsched.mem.shares = "normal"\n'
            'sched.cpu.latencySensitivity = "high"\nsched.vcpu10.affinity = "20"\nsched.vcpu2.affinity = "4"\n'
            'sched.node1.affinity = "24-47"\nMonitor_Control.Halt_In_Monitor = "TRUE"\nmy.own.key = "a|b c"\n')
        self.write_vmx("esx01ev02", vmx2.encode("latin-1"))
        self.set_vm("esx01ev02", state="off")
        before = [self.read_vmx("esx01ev01"), self.read_vmx("esx01ev02")]
        res, lines = self.apply(["snapshot"], "vm esx01ev01\nvm esx01ev02\nvm nosuch\n"
                                "key my.own.key\nkey NUMVCPUS\nkey sched.mem.pin\nkey sched.vcpu2.affinity\nkey My.Own.Key\n")
        self.assertEqual(res, "snapshot 2 of 3 VM(s) read")
        cfg, order = {}, []
        for ln in lines:
            if ln.startswith("TCGEN-CFG "):
                name, group, item, value = ln[len("TCGEN-CFG "):].split("|", 3)
                cfg[(name, group, item)] = value
                order.append((name, group, item))
        self.assertEqual(len(order), len(set(order)), "항목은 VM 마다 한 번씩")

        def items(name, group):
            return [(i, cfg[(n, g, i)]) for n, g, i in order if n == name and g == group]

        self.assertEqual(items("esx01ev01", "basic"),
                         [("power state", "on"), ("cpu", "8"), ("cores per socket / sockets", "1 / 8"), ("memory (GB)", "16"),
                          ("memory reservation", ""), ("cpu shares", ""), ("mem shares", ""), ("latency sensitivity", "")])
        self.assertEqual(items("esx01ev02", "basic"),
                         [("power state", "off"), ("cpu", "16"), ("cores per socket / sockets", "8 / 2"), ("memory (GB)", "24.5"),
                          ("memory reservation", "16384 MB"), ("cpu shares", "10000"), ("mem shares", "normal"),
                          ("latency sensitivity", "high")])
        adv = items("esx01ev02", "adv")
        self.assertEqual([i for i, _ in adv][:12], list(["sched.mem.lpage.enable1GPage", "sched.mem.pin", "sched.mem.prealloc",
                         "sched.mem.prealloc.pinnedMainMem", "sched.swap.vmxSwapEnabled", "numa.vcpu.preferHT",
                         "numa.vcpu.maxPerVirtualNode", "monitor_control.halt_in_monitor", "monitor.idleLoopSpinBeforeHalt",
                         "monitor.idleLoopMinSpinUS", "monitor_control.disable_pause_loop_exiting", "vpmc.enable"]),
                         "config_snapshot 의 vm advanced config 항목과 같은 순서")
        self.assertEqual([i for i, _ in adv][12:], ["my.own.key"], "key 줄로 준 키를 덧붙인다(기본 정보, affinity 구간에 있는 키와 반복은 빼고)")
        self.assertEqual(dict(adv)["sched.mem.pin"], "TRUE")
        self.assertEqual(dict(adv)["monitor_control.halt_in_monitor"], "TRUE", "키의 대소문자는 가리지 않는다")
        self.assertEqual(dict(adv)["my.own.key"], "a|b c", "값의 | 는 그대로")
        self.assertEqual(dict(adv)["vpmc.enable"], "", "vmx 에 없는 키는 빈 값")
        self.assertEqual(items("esx01ev02", "aff"), [("sched.cpu.affinity", "all"), ("sched.vcpu2.affinity", "4"),
                                                      ("sched.vcpu10.affinity", "20"), ("sched.node1.affinity", "24-47"),
                                                      ("numa.nodeAffinity", "0")], "vcpu 는 번호 순서, 그 뒤에 node, 마지막에 numa.nodeAffinity")
        self.assertEqual(items("esx01ev01", "aff"), [("sched.cpu.affinity", "all"), ("numa.nodeAffinity", "0")])
        self.assertEqual(items("nosuch", "basic"), [("power state", "not read")])
        self.assertTrue(any(ln.startswith("vm nosuch: settings not read: VM 'nosuch' is not registered") for ln in lines))
        self.assertEqual(self.changing_calls(), [])
        self.assertEqual([self.read_vmx("esx01ev01"), self.read_vmx("esx01ev02")], before)
        res, _ = self.apply(["snapshot"], "set esx01ev01 a=b\n")
        self.assertTrue(res.startswith("error snapshot spec: unknown line"), res)
        res, _ = self.apply(["snapshot"], "key numvcpus\n")
        self.assertEqual(res, "error snapshot spec: no vm line")

    def test_snapshot_reads_the_host_options_of_the_spec(self):
        """구성 스냅샷의 host 줄: 그 호스트 옵션의 지금 값을 TCGEN-HOPT </구역/옵션>|<값> 줄로 낸다(VCF Operations 에 기록할 값).
        읽지 못한 옵션은 이유를 적고 나머지는 읽는다. 아무것도 변경하지 않는다."""
        res, lines = self.apply(["snapshot"], "vm esx01ev01\nhost /Numa/LocalityWeightActionAffinity\nhost /Numa/NoSuchOption\n"
                                "host /Numa/LocalityWeightActionAffinity\n")
        self.assertEqual(res, "snapshot 1 of 1 VM(s) read")
        self.assertEqual([ln for ln in lines if ln.startswith("TCGEN-HOPT ")], ["TCGEN-HOPT /Numa/LocalityWeightActionAffinity|130"],
                         "같은 옵션은 한 번만, 값은 호스트의 지금 값")
        self.assertTrue(any(ln.startswith("host option /Numa/NoSuchOption: not read: host option '/Numa/NoSuchOption' cannot be read")
                            for ln in lines), lines)
        self.assertEqual(self.changing_calls(), [])
        res, lines = self.apply(["snapshot"], "vm esx01ev01\n")
        self.assertEqual([ln for ln in lines if ln.startswith("TCGEN-HOPT ")], [], "host 줄이 없으면 호스트 옵션을 읽지 않는다")

    def test_check_shows_power(self):
        """check 는 VM 마다 지금 전원과 케이스가 원하는 전원을 보인다(off 줄이 있는 VM 은 끔)."""
        self.set_vm("esx01ev02", state="off")
        res, lines = self.apply(["check"], "vm esx01ev01\nvm esx01ev02\noff esx01ev01\n")
        self.assertEqual(res, "wouldchange 1 VM(s) to power on, 1 VM(s) to power off")
        self.assertIn("TCGEN-POWER esx01ev01 now=on want=off  [change]", lines)
        self.assertIn("TCGEN-POWER esx01ev02 now=off want=on  [change]", lines)
        res, lines = self.apply(["check"], "vm esx01ev01\nvm esx01ev02\noff esx01ev02\n")
        self.assertEqual(res, "nochange")
        bad, _ = self.apply(["check"], "vm esx01ev01\noff esx01ev02\n")
        self.assertTrue(bad.startswith("error spec: an off line names a VM that has no vm line"), bad)
        self.assertEqual(self.changing_calls(), [], "check 는 아무것도 바꾸지 않음")

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
