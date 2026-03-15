"""
并行智算云（Paratera AI）模型调用测试脚本。
使用 OpenAI 兼容 API，从 .env 读取配置。
"""

import os
from openai import OpenAI
from dotenv import load_dotenv

# 从 .env 加载环境变量
load_dotenv()

# 从 .env 读取模型列表（逗号分隔）与当前选用模型
def _get_model_list() -> list[str]:
    raw = os.getenv("PARATERA_MODEL_LIST", "").strip()
    if not raw:
        return [
            "DeepSeek-V3.2-Thinking",
            "Kimi-K2.5",
            "GLM-5",
            "Qwen3-235B-A22B-Thinking-2507",
        ]
    return [m.strip() for m in raw.split(",") if m.strip()]


def _get_default_model() -> str:
    model = os.getenv("PARATERA_MODEL", "").strip()
    if model:
        return model
    lst = _get_model_list()
    return lst[0] if lst else "DeepSeek-V3.2-Thinking"


MODEL_LIST = _get_model_list()
DEFAULT_MODEL = _get_default_model()


def chat_with_model(prompt: str, model_name: str = DEFAULT_MODEL) -> str | None:
    """
    调用 Paratera AI 聊天接口，返回模型回复文本；失败时返回 None 并打印错误。
    """
    api_key = os.getenv("PARATERA_API_KEY")
    base_url = os.getenv("PARATERA_BASE_URL")

    if not api_key or api_key.strip() == "" or api_key == "你的API_KEY":
        print("[错误] 未配置有效的 PARATERA_API_KEY，请在 .env 中填写你的 API Key。")
        return None
    if not base_url or base_url.strip() == "":
        print("[错误] 未配置 PARATERA_BASE_URL，请在 .env 中填写接口地址。")
        return None

    try:
        client = OpenAI(api_key=api_key.strip(), base_url=base_url.strip())
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content
        return content
    except Exception as e:
        print(f"[异常] 调用失败: {type(e).__name__}: {e}")
        raise


def main() -> None:
    test_prompt = "Hello, are you ready?"
    print(f"发送: {test_prompt}")
    print("-" * 40)
    try:
        reply = chat_with_model(test_prompt)
        if reply is not None:
            print(f"回复: {reply}")
        else:
            print("未得到有效回复，请检查配置与网络。")
    except Exception:
        print("测试结束：请根据上方报错检查 API Key、Base URL 或网络。")


if __name__ == "__main__":
    main()
