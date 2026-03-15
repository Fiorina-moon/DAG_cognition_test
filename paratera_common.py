"""
Paratera 平台 LLM 调用公共模块。
供 guess_number_game、auction_game、test_connection 等复用：env 加载、客户端、模型选择、JSON 解析、单轮对话。
"""

import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))


def get_client() -> OpenAI:
    api_key = os.getenv("PARATERA_API_KEY", "").strip()
    base_url = os.getenv("PARATERA_BASE_URL", "").strip()
    if not api_key or api_key == "你的API_KEY":
        raise ValueError("请在 .env 中配置有效的 PARATERA_API_KEY")
    if not base_url:
        raise ValueError("请在 .env 中配置 PARATERA_BASE_URL")
    return OpenAI(api_key=api_key, base_url=base_url)


def get_model() -> str:
    model = os.getenv("PARATERA_MODEL", "").strip()
    if model:
        return model
    raw = os.getenv("PARATERA_MODEL_LIST", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()][0]
    return "DeepSeek-V3.2-Thinking"


def get_model_for_game() -> str:
    """游戏用模型：优先 PARATERA_MODEL_FAST，未设置则用 PARATERA_MODEL。"""
    fast = os.getenv("PARATERA_MODEL_FAST", "").strip()
    if fast:
        return fast
    return get_model()


def parse_model_output(content: str) -> dict[str, Any]:
    """
    从模型回复中解析 JSON。若被包裹在 Markdown 代码块中则先剥离再解析。
    """
    text = (content or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}. 原始内容: {content[:200]}...")


def chat_with_model(
    messages: list[dict[str, str]],
    model_name: str | None = None,
) -> str:
    """
    单轮/多轮对话，返回最后一条 assistant 内容。
    与 test_connection 行为一致，供 ToM 单次提问等场景使用。
    """
    client = get_client()
    model = model_name or get_model_for_game()
    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    return (response.choices[0].message.content or "").strip()


def get_script_dir() -> str:
    """项目根目录（与 paratera_common 同目录），用于解析相对路径。"""
    return _SCRIPT_DIR
