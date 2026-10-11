# -*- coding: utf-8 -*-
"""KPP — 框架真实加载器测试（2.x / 3.0）。

不用桩上下文，而是让**框架自己的 PluginManager** 走真实加载链路：
  load_plugin_from_dir → spec 导入（包式）→ schema 默认配置 → 实例化 → initialize()
→ 钩子/API 注册进框架 registry。这是与生产环境完全一致的加载路径。

用法（任意机器）：
  python3 real_loader_test.py --core2x /path/to/KiraAI-2.x --core30 /path/to/KiraAI-3.0
  也可用环境变量 KIRA_CORE_2X / KIRA_CORE_30。插件目录 = 本仓库根（自动定位）。
"""
import argparse
import asyncio
import dataclasses
import os
import sys
import types
import traceback
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def resolve_cores():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core2x", default=os.environ.get("KIRA_CORE_2X", ""))
    ap.add_argument("--core30", default=os.environ.get("KIRA_CORE_30", ""))
    ap.add_argument("--plugin-dir", default=os.environ.get("KPP_PLUGIN_DIR", ""))
    args, _ = ap.parse_known_args()
    global PLUGIN_DIR
    PLUGIN_DIR = (args.plugin_dir or "").strip() or str(_DEFAULT_ROOT)
    cores = {}
    for gen, p in (("2.x", args.core2x), ("3.0", args.core30)):
        p = (p or "").strip()
        if p and os.path.isdir(os.path.join(p, "core", "plugin")):
            cores[gen] = p
        elif p:
            print(f"⚠️  {gen} 核心路径无效：{p} ⇒ 跳过", flush=True)
    return cores
RESULTS = []


def check(gen, name, cond, detail=""):
    RESULTS.append((gen, name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] [{gen}] {name}" + (f" :: {detail}" if detail and not cond else ""), flush=True)


def swap_core(root):
    if root in sys.path:
        sys.path.remove(root)
    for m in list(sys.modules):
        if m == "core" or m.startswith("core.") or m == "plugins" or m.startswith("plugins."):
            del sys.modules[m]
    sys.path.insert(0, root)
    os.chdir(root)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)


class StubLLM:
    async def chat(self, req, **kw):
        return types.SimpleNamespace(text_response="stay")


class StubProviderMgr:
    def get_default_llm(self): return None
    def get_default_fast_llm(self): return StubLLM()
    def get_model_client(self, pid, mid, *a): return StubLLM()
    def get_model_info(self, pid, mid, model_type=None):
        return types.SimpleNamespace(model_id=mid, model_name="可读名模型",
                                     provider_id=pid, provider_name="可读提供商",
                                     provider_config={}, model_config={})


def build_ctx(gen, PluginContext, persona_mgr):
    """按世代用桩填充 PluginContext 必填字段（插件用不到的给 None/对象即可）。"""
    fields = {f.name for f in dataclasses.fields(PluginContext)}
    kwargs = {name: None for name in fields}
    for name in fields:
        if name == "persona_mgr":
            kwargs[name] = persona_mgr
        elif name == "provider_mgr":
            kwargs[name] = StubProviderMgr()
        elif name == "config":
            kwargs[name] = {}          # 3.0 的 _ensure_plugin_config 会 .get("providers", {})
        elif name == "session_mgr":
            kwargs[name] = types.SimpleNamespace(
                fetch_memory=lambda sid: [],
                delete_session=lambda sid: None,
                get_session_info=lambda sid: types.SimpleNamespace(sid=sid),
            )
    return PluginContext(**kwargs)


class StubPersonaMgr:
    def __init__(self, PI):
        self._p = [PI(id="default", name="默认酱", content="你是默认酱。"),
                   PI(id="warm", name="暖暖", content="你是暖暖。")]

    async def list_personas(self):
        return list(self._p)

    async def get_persona(self, pid):
        return next((p for p in self._p if p.id == pid), None)

    async def set_active_persona(self, pid):
        return True


async def run_gen(gen, core):
    swap_core(core)
    from core.plugin import PluginManager, PluginContext
    from core.persona.model import PersonaInfo

    persona_mgr = StubPersonaMgr(PersonaInfo)
    ctx = build_ctx(gen, PluginContext, persona_mgr)
    mgr = PluginManager(ctx)
    ctx.plugin_mgr = mgr

    from pathlib import Path
    pid = await mgr.load_plugin_from_dir(Path(PLUGIN_DIR), auto_install=False)
    check(gen, "真实加载器返回插件 ID", pid == "kira_persona_progression", str(pid))

    inst = mgr.plugin_instances.get("kira_persona_progression") or mgr.get_plugin_inst("kira_persona_progression")
    check(gen, "插件实例已注册", inst is not None)
    if inst is None:
        errs = getattr(mgr, "get_plugin_load_errors", lambda: {})()
        print("   load errors:", errs)
        return
    check(gen, "initialize 完成（状态管理器就绪）", getattr(inst, "state_mgr", None) is not None)
    check(gen, "数据目录由框架分配", inst.data_dir is not None and "kira_persona_progression" in str(inst.data_dir))
    check(gen, "JEV 默认关闭", inst.jev_config.enabled is False and inst.jev_client is None)
    check(gen, "人格映射默认为空（警告不炸）", isinstance(inst.stage_map, dict))
    # 图标：manifest 声明 + 框架解析 + 文件存在
    from core.utils.path_utils import resolve_manifest_icon_path
    icon = resolve_manifest_icon_path(Path(PLUGIN_DIR), "icon.png")
    check(gen, "图标经框架解析且存在", icon is not None and icon.exists(), str(icon))

    # 钩子是否注册进框架 registry
    try:
        if gen == "3.0":
            from core.plugin import registry as reg3
            comp = reg3._plugin_components.get("kira_persona_progression")
            hooks = getattr(comp, "hooks", {}) if comp else {}
            check(gen, "事件钩子已注册(3.0)", len(hooks) >= 3, str(list(hooks))[:80])
        else:
            from core.plugin import plugin_registry as reg2
            comp = reg2._plugin_components.get("kira_persona_progression")
            hooks = getattr(comp, "hooks", {}) if comp else {}
            check(gen, "事件钩子已注册(2.x)", len(hooks) >= 3, str(list(hooks))[:80])
    except Exception as e:
        check(gen, "钩子注册检查异常", False, repr(e))

    await inst.terminate()


def main():
    cores = resolve_cores()
    if not cores:
        print("用法：python3 real_loader_test.py --core2x /path/to/2.x --core30 /path/to/3.0\n"
              "或环境变量 KIRA_CORE_2X / KIRA_CORE_30。")
        sys.exit(2)
    for gen, core in cores.items():
        print(f"\n===== 真实加载器 [{gen}]（{core}）=====", flush=True)
        try:
            asyncio.run(run_gen(gen, core))
        except Exception:
            check(gen, "套件异常", False, traceback.format_exc()[-600:])
    fails = [r for r in RESULTS if not r[2]]
    print(f"\n===== 真实加载器汇总：{len(RESULTS)-len(fails)}/{len(RESULTS)} 通过 =====")
    if fails:
        sys.exit(1)
    print("REAL LOADER TEST PASSED")


if __name__ == "__main__":
    main()
