# -*- coding: utf-8 -*-
"""验证新增的“主题组合计硬顶”代码路径真的会咬。

做法：复制一份配置到临时目录，把 theme_cap_under_e 压到 1%，
其余（数据目录、口径）全部不变，跑 engine.run 看桶敞口是否被等比缩放。
如果不缩放，说明新代码是死路径 —— 和原来“开关只展示不咬”是同一类错误。
"""
import os, sys, shutil, json, tempfile
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import yaml, engine

BASE = "."
tmp = tempfile.mkdtemp(prefix="splus_theme_cap_")
cfg = engine.load_config(os.path.join(BASE, "splus_config.yaml"))

# 数据目录是相对 base_dir 的，这里把 base_dir 改成绝对路径，配置里的相对路径照样生效
cfg_path = os.path.join(tmp, "cfg.yaml")
cfg_small = json.loads(json.dumps(cfg))
cfg_small["risk"]["theme_cap_under_e"] = 0.01
with open(cfg_path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(cfg_small, fh, allow_unicode=True, sort_keys=False)

res = engine.run(cfg_path, os.path.abspath(BASE))
print("theme_buckets =", cfg_small["risk"]["theme_buckets"])
print("theme_cap     =", cfg_small["risk"]["theme_cap_under_e"])
print("haircut       =", res["switch_haircut"])
tot_theme = 0.0
for b, v in res["buckets"].items():
    mark = "THEME" if b in cfg_small["risk"]["theme_buckets"] else "     "
    print("%s %-8s pre=%.4f post=%.4f  cap=%s" % (
        mark, b, v["exposure_pre_switch"], v["exposure"], v.get("cap_reason")))
    if b in cfg_small["risk"]["theme_buckets"]:
        tot_theme += v["exposure"]
print("主题组合计 post = %.4f  (硬顶 %.4f)" % (tot_theme, cfg_small["risk"]["theme_cap_under_e"]))
print("判定:", "PASS 硬顶生效" if tot_theme <= cfg_small["risk"]["theme_cap_under_e"] + 1e-6 else "FAIL 硬顶未生效")
shutil.rmtree(tmp, ignore_errors=True)
