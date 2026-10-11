from typing import Optional, List, Dict
from core.logging_manager import get_logger
from core.provider import LLMRequest, LLMModelClient
from core.persona import PersonaManager

logger = get_logger("persona_progression.trigger", "cyan")

#: 发给判断引擎的文本上限（token 最优）：
#: 单条消息截断（多模态/长文保护）、总长截断（JEV 64k ctx 内，辅助 LLM 也更省）
MAX_MESSAGE_CHARS = 500
MAX_TOTAL_CHARS = 12000


def _content_to_text(content) -> str:
    """消息 content 可能是 str，也可能是多模态 list（[{'type':'text','text':...}, ...]）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("type"):
                    parts.append(f"[{item['type']}]")
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(p for p in parts if p)
    return str(content or "")


class TriggerEngine:
    def __init__(self, persona_mgr: PersonaManager, llm_client: Optional[LLMModelClient],
                 llm_clients: Optional[List[LLMModelClient]] = None):
        self.persona_mgr = persona_mgr
        self.llm_client = llm_client
        # 有序回退链：不传则单档 = llm_client（行为与旧版逐字等价）
        self.llm_clients = (list(llm_clients) if llm_clients
                            else ([llm_client] if llm_client else []))

    async def judge(self, prompt_template: str, stage: int, max_stage: int, conversation_text: str) -> str:
        """辅助 LLM 判断路径：返回 'up', 'down', 或 'stay'。

        单档时与 v1.1.5 逐字等价；多档时仅**异常**才换下一档
        （能解析出结论的回复是有效答案，不触发回退）。
        """
        if not self.llm_clients:
            logger.warning("辅助 LLM 未配置，无法进行 AI 判断，默认 stay")
            return "stay"

        prompt = prompt_template.format(
            stage=stage,
            max_stage=max_stage,
            conversation_text=conversation_text
        )
        last_exc = None
        for idx, client in enumerate(self.llm_clients, 1):
            try:
                req = LLMRequest(messages=[{"role": "user", "content": prompt}])
                resp = await client.chat(req)
                result = resp.text_response.strip().lower()
                if "up" in result:
                    return "up"
                elif "down" in result:
                    return "down"
                else:
                    return "stay"
            except Exception as e:
                last_exc = e
                if len(self.llm_clients) > 1:
                    logger.info("辅助模型第 %d 档失败（%s）⇒ 换下一档", idx, type(e).__name__)
        logger.error("AI 判断失败（全部 %d 档）: %s", len(self.llm_clients), last_exc)
        return "stay"

    def render_jev_instructions(self, prompt_template: str, stage: int, max_stage: int) -> str:
        """渲染给 JEV 的 instructions：对话走 state（只计一次费），
        模板里的 {conversation_text} 替换为固定指引，避免双份计费。"""
        try:
            return prompt_template.format(
                stage=stage,
                max_stage=max_stage,
                conversation_text="（最近对话已在随附状态中给出）",
            )
        except Exception:
            # 模板含未知占位符时不得炸掉判断：退回原样（缺失占位符保持原状）
            return prompt_template

    async def apply_stage_global(self, stage: int, stage_map: Dict[int, str]) -> bool:
        """全局模式：切换激活人格"""
        persona_id = stage_map.get(stage)
        if not persona_id:
            logger.warning("阶段 %d 未配置对应人格 ID", stage)
            return False
        try:
            await self.persona_mgr.set_active_persona(persona_id)
            logger.info("全局人格切换至 %s (阶段 %d)", persona_id, stage)
            return True
        except Exception as e:
            logger.exception("切换全局人格失败: %s", e)
            return False

    def build_conversation_text(self, messages: List[dict], ignore_tool: bool, ignore_assistant: bool, include_reasoning: bool = False) -> str:
        """根据裁剪配置构建发给判断引擎的对话文本。

        与 v1.1.5 的差异（只增不破）：多模态 content 提取文本段、
        单条/总长截断（token 上限保护）。纯文本短消息输出与旧版一致。
        """
        lines = []
        total = 0
        for msg in messages:
            role = msg.get("role", "")
            content = _content_to_text(msg.get("content", ""))
            reasoning = _content_to_text(msg.get("reasoning_content", "")) if include_reasoning else ""
            if ignore_tool and role in ("tool", "function"):
                continue
            if ignore_assistant and role == "assistant":
                continue

            if len(content) > MAX_MESSAGE_CHARS:
                content = content[:MAX_MESSAGE_CHARS] + "…"

            parts = []
            if role == "tool":
                parts.append(f"[工具结果] {content}")
            elif role == "assistant":
                parts.append(f"助手：{content}")
            elif role == "user":
                parts.append(f"用户：{content}")
            else:
                parts.append(f"{role}: {content}")

            if reasoning:
                if len(reasoning) > MAX_MESSAGE_CHARS:
                    reasoning = reasoning[:MAX_MESSAGE_CHARS] + "…"
                parts.append(f"[思考过程] {reasoning}")

            lines.append("\n".join(parts))
            total += len(lines[-1])
        # 总长超限：从**最旧**的一侧丢弃（新消息对判断最重要）
        while lines and total > MAX_TOTAL_CHARS:
            total -= len(lines.pop(0)) + 1
        return "\n".join(lines)
