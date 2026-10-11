# -*- coding: utf-8 -*-
"""KPP — JEV 端点实联测试（真 HTTP 调用，默认离线跳过）。

通过插件自带的 jev.py（零依赖纯 stdlib，可独立导入）打真实 JEV 端点，验证：
  1. 连通性与协议（/v1/models + /v1/systemone）；
  2. 中文判断样本：明显升温 / 明显降温 / 平稳 三类对话的判定合理性；
  3. 置信门槛、$$ENV、熔断语义；
  4. 回退链实联：第 1 档坏端点 + 第 2 档真端点 ⇒ 自动切换成功；
  5. 真实提供商格式借道：按 KiraAI 3.0 现代格式注册提供商 → 真 ProviderManager
     解析 → 插件解析器出链 → 真实判定（需要 --core30 或 KIRA_CORE_30）。

用法（任意机器，**密钥走环境变量，绝不入库**）：
  export KPP_JEV_BASE="https://api.typesafe.ai"      # 或你的中转站地址
  export KPP_JEV_KEY="sk-..."                         # 也可用 TYPESAFE_API_KEY
  python3 live_jev_test.py [--core30 /path/to/KiraAI-3.0]
未设置 KPP_JEV_KEY 时：只跑离线段（端点规整/熔断/配置解析），实联段自动跳过。
"""
import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

_ap = argparse.ArgumentParser()
_ap.add_argument("--core30", default=os.environ.get("KIRA_CORE_30", ""))
_ap.add_argument("--plugin-dir", default=os.environ.get("KPP_PLUGIN_DIR", ""))
_ARGS, _ = _ap.parse_known_args()
CORE30 = (_ARGS.core30 or "").strip()
# 插件目录：默认=仓库根（tests/ 上一级）；可用 --plugin-dir 覆盖
PLUGIN_DIR = Path((_ARGS.plugin_dir or "").strip() or Path(__file__).resolve().parent.parent)
JEV_PY = PLUGIN_DIR / "jev.py"

BASE = os.environ.get("KPP_JEV_BASE", "https://api.typesafe.ai").strip()
KEY = (os.environ.get("KPP_JEV_KEY") or os.environ.get("TYPESAFE_API_KEY") or "").strip()
LIVE = bool(KEY)


def mask(s: str) -> str:
    """密钥脱敏：只留前 3 位。"""
    return (s[:3] + "***") if s else s


spec = importlib.util.spec_from_file_location("kpp_jev", str(JEV_PY))
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)

PASS = []
def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""), flush=True)


class Log:
    def warning(self, *a): print("   [warn]", *a)
    def info(self, *a): pass
    def error(self, *a): print("   [err]", *a)


SAMPLES = [
    ("明显升温（暧昧推进）",
     "用户：宝贝，我今天特别想你，想抱抱你\n助手：我也想你呀，过来让我抱抱\n用户：以后每天晚上都想这样和你聊天",
     "up"),
    ("明显降温（冲突冷淡）",
     "用户：你别烦我了行不行\n助手：……好吧\n用户：我现在不想说话，你走吧",
     "down"),
    ("平稳日常（无信号）",
     "用户：今天中午吃什么呢\n助手：要不要试试楼下那家面馆？\n用户：好主意，那就吃面吧",
     "stay"),
]

INSTR = "你是一个人格阶段判断器。当前阶段为 1，最大阶段为 2。请根据最近对话判断两人的关系阶段应升级、降级还是保持。"


async def main():
    # ---------- 离线段（无需密钥，任何机器可跑） ----------
    print("== 0) 离线：端点规整 / 配置解析 / 熔断 ==", flush=True)
    check("裸域名规整", jev.endpoint_of("https://api.typesafe.ai") == "https://api.typesafe.ai/v1/systemone")
    check("/v1 规整", jev.endpoint_of("https://x.com/v1") == "https://x.com/v1/systemone")
    check("完整端点不重复拼", jev.endpoint_of("https://x.com/v1/systemone/") == "https://x.com/v1/systemone")
    check("chat/completions 尾巴剥掉", jev.endpoint_of("https://x.com/v1/chat/completions") == "https://x.com/v1/systemone")
    cfg = jev.JevConfig(enabled=True, links=[("https://x.com", "k", "m")], min_confidence=0.99)
    check("门槛解析", abs(cfg.min_confidence - 0.99) < 1e-9)
    check("$$ENV 未设置解析为空", jev._env("$$KPP_LIVE_NOPE_404") == "")
    check("缺密钥不就绪", not jev.JevConfig(enabled=True, links=[("https://x.com", "", "m")]).ready)
    bad = jev.JevClient([("http://127.0.0.1:9/none", "sk-x", "jev-latest")], 1.0, Log())
    for _ in range(3):
        await bad.decide("s", "i")
    check("3 连败熔断", not bad.ready and bad.cooldown_left > 0, f"fails={bad.fails}")
    check("熔断中返回 None", await bad.decide("s", "i") is None)

    if not LIVE:
        print("\n（未设 KPP_JEV_KEY / TYPESAFE_API_KEY ⇒ 实联段跳过；离线段已全过）", flush=True)
    else:
        print(f"== 1) /v1/models 连通性（{BASE}，key={mask(KEY)}） ==", flush=True)
        req = urllib.request.Request(BASE + "/v1/models", headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        ids = [m.get("id") for m in data.get("data", [])]
        check("模型列表非空", bool(ids), str(ids)[:120])

        print("== 2) 中文样本判定（链式客户端单档） ==", flush=True)
        client = jev.JevClient([(BASE, KEY, "jev-latest")], 8.0, Log())
        check("端点规整正确", client.endpoint == BASE + "/v1/systemone", client.endpoint)
        check("客户端就绪", client.ready)
        t0 = time.monotonic()
        for name, conv, expect in SAMPLES:
            r = await client.decide(conv, INSTR)
            check(f"{name} → 返回成功", r is not None)
            if not r:
                continue
            match = "✓" if r["choice"] == expect else f"≠期望{expect}"
            print(f"      判定={r['choice']}({match}) 置信={r['confidence']:.2f} 概率={r['probabilities']} 模型={r['model']}", flush=True)
            check(f"{name} → 判定合理", r["choice"] == expect, f"got={r['choice']}")
        dt = time.monotonic() - t0
        check("三次判定总耗时 < 15s", dt < 15, f"{dt:.1f}s")
        print(f"      累计输入 token={client.tokens}（≈${client.tokens/1e6*0.042:.6f}）", flush=True)

        print("== 3) 回退链实联：坏档 + 真档 ==", flush=True)
        chain = jev.JevClient([("http://127.0.0.1:9/none", "sk-bad", "jev-latest"),
                               (BASE, KEY, "jev-latest")], 6.0, Log())
        r = await chain.decide("用户：想你了\n助手：我也想", INSTR)
        check("坏档自动换真档成功", r is not None and r["choice"] in ("up", "down", "stay"),
              str(r and r["choice"]))
        check("换档后模型来自第 2 档", r is not None and r.get("model", "").startswith("jev"),
              str(r and r.get("model")))

        if CORE30 and os.path.isdir(os.path.join(CORE30, "core", "plugin")):
            print("== 4) 真实提供商格式借道（3.0 现代格式 → 实联） ==", flush=True)
            sys.path.insert(0, CORE30)
            os.chdir(CORE30)
            os.makedirs("data", exist_ok=True)
            from core.provider.provider_manager import ProviderManager
            providers = {"provLive": {
                "format": "openai", "status": "active", "model_config_version": 2,
                "name": "JEV实联",
                "provider_config": {"base_url": BASE, "api_key": KEY},
                "model_config": {"llm": {"1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d":
                                          {"model_name": "jev-latest", "config": {}}}},
            }}
            pm = ProviderManager(None, {"providers": providers})
            info = pm.get_model_info("provLive", "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d")
            check("真 ProviderManager 解析（3.0 格式）", info is not None
                  and info.model_name == "jev-latest"
                  and info.provider_config.get("api_key") == KEY)
            # 插件解析器出链（模拟 initialize 内的调用路径）
            links = []
            conf = getattr(info, "provider_config", None) or {}
            base = jev._pick(conf, jev._BASE_KEYS)
            key = jev._env(jev._pick(conf, jev._KEY_KEYS))
            model = getattr(info, "model_name", None) or getattr(info, "model_id", "")
            if base and key:
                links.append((base, key, model))
            check("插件解析器出链", links == [(BASE, KEY, "jev-latest")],
                  str([(b, mask(k), m) for b, k, m in links]))
            live = jev.JevClient(links, 8.0, Log())
            r = await live.decide(SAMPLES[0][1], INSTR)
            check("提供商借道 → 真实判定成功", r is not None and r["choice"] in ("up", "down", "stay"),
                  str(r and (r["choice"], round(r["confidence"], 2))))
        else:
            print("== 4) 提供商借道段：未提供 --core30 ⇒ 跳过 ==", flush=True)

    fails = [n for n, ok in PASS if not ok]
    print(f"\n===== 汇总：{len(PASS)-len(fails)}/{len(PASS)} 通过 =====")
    if fails:
        sys.exit(1)
    print("LIVE JEV TEST PASSED" if LIVE else "OFFLINE PART PASSED（实联跳过）")


if __name__ == "__main__":
    asyncio.run(main())
