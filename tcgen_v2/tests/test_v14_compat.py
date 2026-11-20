# -*- coding: utf-8 -*-
"""
시각 지정 방식이 v1.4 와 같은 결과를 내는지 확인한다 (블록 A, B, C, D 를 바이트 단위로 비교).

- 기준 자료: tests/fixtures/v14_blocks.json (v1.4 의 tcgen_core.generate 결과. make_v14_fixture.py 로 만든 것)
- 이 폴더 옆에 v1.4 소스(../tcgen/tcgen_core.py)가 있으면 그 코드도 직접 실행해 한 번 더 비교한다.
  (저장소 main 에는 그 폴더가 없다. 태그 tcgen-v1.4 로 받은 tcgen 폴더를 옆에 두면 된다.)
"""
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import tcgen_core as core  # noqa: E402
import make_v14_fixture as fx  # noqa: E402

V14_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tcgen")
KEYS = ("errors", "warnings", "setup", "vm", "esx", "vm_wipe", "wipe_hosts", "wipe_conflicts", "lines", "skipped")


class V14CompatTests(unittest.TestCase):
    def setUp(self):
        with io.open(fx.FIXTURE, encoding="utf-8") as f:
            self.data = json.load(f)

    def compare(self, name, got, want):
        for key in KEYS:
            if key in ("setup", "vm", "vm_wipe"):
                self.assertEqual(got[key].encode("utf-8"), want[key].encode("utf-8"), "%s: 블록 %s 가 v1.4 와 다름" % (name, key))
            elif key == "esx":
                self.assertEqual(sorted(got["esx"]), sorted(want["esx"]), "%s: ESX 호스트 목록" % name)
                for host in want["esx"]:
                    self.assertEqual(got["esx"][host].encode("utf-8"), want["esx"][host].encode("utf-8"),
                                     "%s: ESX 블록 %s 가 v1.4 와 다름" % (name, host))
            else:
                self.assertEqual(got[key], want[key], "%s: %s" % (name, key))

    def test_fixture_inputs_are_current(self):
        """기준 자료의 입력이 make_v14_fixture.py 의 입력과 같은지(입력만 고치고 기준 자료를 다시 만들지 않은 경우를 찾는다)."""
        self.assertEqual(self.data["run"], fx.RUN)
        self.assertEqual(self.data["mon"], fx.MON)
        self.assertEqual([dict((k, v) for k, v in sc.items() if k != "expect") for sc in self.data["scenarios"]],
                         json.loads(json.dumps(fx.scenarios())))
        self.assertGreaterEqual(len(self.data["scenarios"]), 6)

    def test_time_mode_equals_v14_fixture(self):
        blocks = 0
        for sc in self.data["scenarios"]:
            got = fx.run_one(core, sc, mode="time")
            self.compare(sc["name"], got, sc["expect"])
            blocks += bool(got["vm"]) + len(got["esx"]) + bool(got["vm_wipe"])
        self.assertGreaterEqual(blocks, 12, "비교한 블록 수가 너무 적음")

    @unittest.skipUnless(os.path.isfile(os.path.join(V14_DIR, "tcgen_core.py")), "옆 폴더에 v1.4 소스가 없음")
    def test_time_mode_equals_v14_source(self):
        v14 = fx.load_core(V14_DIR)
        if v14.VERSION != "1.4":
            self.skipTest("옆 폴더의 tcgen 이 v1.4 가 아님")
        for sc in fx.scenarios():
            self.compare(sc["name"], fx.run_one(core, sc, mode="time"), fx.run_one(v14, sc))

    def test_jump_files_same_as_v14(self):
        """점프 VM 의 vmssh, askpass, setpw 는 v1.4 와 글자 하나까지 같아야 한다(v1.4 로 돌아가도 그대로 쓰게)."""
        files = dict(core.jump_files())
        v14_setup = self.data["scenarios"][0]["expect"]["setup"]
        self.assertEqual(core.setup_block(), v14_setup)
        self.assertEqual(files["askpass"], 'cat "$HOME/.tcgen/vmpw"\n')
        # v1.4 A 블록의 printf 줄을 풀어 파일 내용을 다시 만든다
        made = {}
        for line in core.hd_unescape(v14_setup).split("\n"):
            if not line.startswith("printf ") or " >> $D/" not in line:
                continue
            body, target = line.rsplit(" >> $D/", 1)
            fmt, _, quoted = body[len("printf "):].partition(" ")
            text = quoted[1:-1].replace("'\\''", "'")
            made[target] = made.get(target, "") + text + ("\n" if fmt == "'%s\\n'" else "")
        self.assertEqual(files["vmssh"], made["vmssh"])
        self.assertEqual(files["setpw"], made["setpw"])


if __name__ == "__main__":
    unittest.main()
