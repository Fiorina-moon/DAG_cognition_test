"""
猜数字（Guess Number）多轮动态博弈评估系统。
用于测量 LLM 的社会认知能力（ToM），基于 Paratera 平台。
可扩展：后续可接入其他游戏（见 GameManager.run 与 BaseGame 设计）。
"""

import json
import os
import re
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

# 从脚本所在目录加载 .env，避免因工作目录不同而读不到配置
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))

# ---------- 配置（与 test_connection 一致） ----------
def _get_model() -> str:
    model = os.getenv("PARATERA_MODEL", "").strip()
    if model:
        return model
    raw = os.getenv("PARATERA_MODEL_LIST", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()][0]
    return "DeepSeek-V3.2-Thinking"


def _get_client() -> OpenAI:
    api_key = os.getenv("PARATERA_API_KEY", "").strip()
    base_url = os.getenv("PARATERA_BASE_URL", "").strip()
    if not api_key or api_key == "你的API_KEY":
        raise ValueError("请在 .env 中配置有效的 PARATERA_API_KEY")
    if not base_url:
        raise ValueError("请在 .env 中配置 PARATERA_BASE_URL")
    return OpenAI(api_key=api_key, base_url=base_url)


MODEL_NAME = _get_model()

# ---------- 系统提示词（猜数字游戏）：从外部文件加载 ----------
# Prompt 文件路径：从 .env 的 GUESS_NUMBER_SYSTEM_PROMPT_PATH 读取，为空则用默认路径
_DEFAULT_PROMPT_PATH = os.path.join(_SCRIPT_DIR, "prompts", "guess_number_system.txt")
GUESS_NUMBER_SYSTEM_PROMPT_PATH = (
    os.getenv("GUESS_NUMBER_SYSTEM_PROMPT_PATH", "").strip() or _DEFAULT_PROMPT_PATH
)


def build_system_prompt(player_index: int) -> str:
    """从配置的 Prompt 文件读取模板，替换 {{player_index}} 后返回。"""
    path = os.path.normpath(
        GUESS_NUMBER_SYSTEM_PROMPT_PATH
        if os.path.isabs(GUESS_NUMBER_SYSTEM_PROMPT_PATH)
        else os.path.join(_SCRIPT_DIR, GUESS_NUMBER_SYSTEM_PROMPT_PATH)
    )
    if not os.path.isfile(path):
        raise FileNotFoundError(f"System Prompt 文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        template = f.read()
    return template.replace("{{player_index}}", str(player_index))


def parse_model_output(content: str) -> dict[str, Any]:
    """
    从模型回复中解析 JSON。若被包裹在 Markdown 代码块中则先剥离再解析。
    """
    text = (content or "").strip()
    # 剥离 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}. 原始内容: {content[:200]}...")


def extract_numbers_from_thought(thought: str) -> list[float]:
    """从 thought 文本中提取所有数字（用于连贯性分析）。"""
    if not thought:
        return []
    # 匹配整数或小数
    pattern = r"\d+\.?\d*"
    return [float(x) for x in re.findall(pattern, thought) if x]


def compute_coherence_score(thought: str, number: int | float) -> float:
    """
    连贯性得分：检查 thought 中提到的预测/推理与实际 number 的数学关联。
    若 thought 中出现「平均」「0.5」「target」等推导出的目标值，则 number 应与之接近。
    返回 0~1，越高表示推理与出数越一致。
    """
    if not thought or not thought.strip():
        return 0.0
    nums = extract_numbers_from_thought(thought)
    if not nums:
        return 0.5  # 无数字可对照，给中性分
    num = float(number)
    # 简单启发：若 thought 中有接近 number 的数，或存在 0.5*avg 类关系则加分
    # 检查是否有数字与 number 接近（允许 5 以内偏差）
    for v in nums:
        if abs(v - num) <= 5:
            return 0.9  # 推理中出现了与出数接近的值
    # 检查是否存在「目标」类：如 thought 中某数约为 number 的 2 倍（avg 与 0.5*avg）
    for v in nums:
        if 0 < v <= 100 and abs(v * 0.5 - num) <= 5:
            return 0.85
    # 否则根据「至少有一个数在合理范围」给部分分
    if any(0 <= v <= 100 for v in nums):
        return 0.5
    return 0.3


# ---------- Agent：独立消息历史 + API 调用 ----------
class Agent:
    """单个玩家 Agent，维护自己的 name 与 messages 历史，通过 Paratera 调用 LLM。"""

    def __init__(self, name: str, player_index: int, model: str, client: OpenAI):
        self.name = name
        self.player_index = player_index
        self.model = model
        self.client = client
        self.history: list[dict[str, str]] = []
        self._system_prompt = build_system_prompt(player_index)

    def _build_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            *self.history,
        ]

    def call_api(self, user_content: str) -> dict[str, Any]:
        """
        将 user_content 追加为 user message，调用 API，追加 assistant 回复并解析 JSON。
        返回 {"thought": str, "number": int}，解析失败则抛出 ValueError。
        """
        self.history.append({"role": "user", "content": user_content})
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self._build_messages(),
            )
            content = response.choices[0].message.content or ""
            self.history.append({"role": "assistant", "content": content})
            data = parse_model_output(content)
            thought = data.get("thought") or ""
            number = data.get("number")
            if number is None:
                raise ValueError("模型输出缺少 'number' 字段")
            if not isinstance(number, (int, float)):
                number = int(float(number))
            data["thought"] = thought
            data["number"] = int(round(number))
            return data
        except (ValueError, json.JSONDecodeError):
            raise
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[调用异常] {e}"})
            raise


# ---------- GameManager：轮次控制、Target 计算、结果分发、日志保存 ----------
class GameManager:
    """负责多轮猜数字：创建 Agent、轮次循环、计算 Target/Winner、注入上下文、保存日志。"""

    def __init__(self, num_players: int = 5, num_rounds: int = 5):
        self.num_players = num_players
        self.num_rounds = num_rounds
        self.model = MODEL_NAME
        self.client = _get_client()
        self.agents: list[Agent] = [
            Agent(f"Player {i}", i, self.model, self.client)
            for i in range(1, num_players + 1)
        ]
        self.round_results: list[dict[str, Any]] = []

    def _format_round_result_user_message(
        self, round_index: int, choices: dict[str, int], winner: str
    ) -> str:
        """上一轮所有人的出价与获胜者，作为下一轮前注入的 User Message。不提供 Target/平均，由模型自行推算（考察信念归因能力）。"""
        parts = [f"上一轮（第 {round_index} 轮）结果："]
        for name, num in choices.items():
            parts.append(f"{name}: {num}")
        parts.append(f"当轮获胜者：{winner}。")
        parts.append(f"\n本轮是第 {round_index + 1} 轮。请根据以上信息给出你的选择。")
        return "\n".join(parts)

    def _flush_logs(self) -> None:
        """将当前状态实时写入已打开的日志文件（每次请求后调用）。"""
        if not getattr(self, "_log_path", None):
            return
        payload = {
            "model": self.model,
            "num_players": self.num_players,
            "num_rounds": self.num_rounds,
            "round_results": self.round_results,
            "agents": [
                {"name": a.name, "messages": a.history}
                for a in self.agents
            ],
        }
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def run(self) -> None:
        """执行多轮博弈：每轮收集出数、计算 Target/Winner、注入结果；每请求一个角色后实时写入日志。"""
        print(f"模型: {self.model} | 玩家数: {self.num_players} | 轮数: {self.num_rounds}")
        print("=" * 60)
        logs_dir = os.path.join(_SCRIPT_DIR, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self._log_path = os.path.join(
            logs_dir,
            f"guess_number_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        self._flush_logs()  # 初始空状态写入

        for r in range(self.num_rounds):
            round_num = r + 1
            if round_num == 1:
                user_content = "第 1 轮：请给出你的选择（JSON 格式：{\"thought\": \"...\", \"number\": 整数}）。"
            else:
                prev = self.round_results[-1]
                user_content = self._format_round_result_user_message(
                    round_num - 1,
                    prev["choices"],
                    prev["winner"],
                )

            choices: dict[str, int] = {}
            thoughts: dict[str, str] = {}
            coherence_scores: dict[str, float] = {}

            print(f"\n第 {round_num} 轮：正在依次请求 5 名玩家（每步需等待 API 响应，Thinking 模型可能较慢）…")
            for agent in self.agents:
                print(f"  正在请求 {agent.name}…", flush=True)
                try:
                    data = agent.call_api(user_content)
                    n = data["number"]
                    n = max(0, min(100, n))
                    choices[agent.name] = n
                    thoughts[agent.name] = data.get("thought", "")
                    coherence_scores[agent.name] = compute_coherence_score(
                        data.get("thought", ""), n
                    )
                except Exception as e:
                    print(f"  [异常] {agent.name} 本轮失败: {e}")
                    choices[agent.name] = 50  # 默认中点
                    thoughts[agent.name] = f"[解析/调用失败] {e}"
                    coherence_scores[agent.name] = 0.0
                self._flush_logs()  # 每个角色请求完成后立即写入日志

            avg = sum(choices.values()) / len(choices)
            target = 0.5 * avg
            distances = {name: abs(num - target) for name, num in choices.items()}
            winner = min(distances, key=distances.get)  # type: ignore

            self.round_results.append({
                "round": round_num,
                "choices": dict(choices),
                "average": avg,
                "target": target,
                "winner": winner,
                "thoughts": dict(thoughts),
                "coherence_scores": coherence_scores,
            })
            self._flush_logs()  # 本轮结果写入后再次刷新

            print(f"\n--- 第 {round_num} 轮 ---")
            print(f"  出数: {choices}")
            print(f"  平均 = {avg:.2f}, Target = {target:.2f}, 获胜: {winner}")
            print(f"  连贯性得分: {coherence_scores}")

        self._save_logs()

    def _save_logs(self) -> None:
        """最后一次刷新日志并打印路径（实时写入已在 run() 中完成）。"""
        if getattr(self, "_log_path", None):
            self._flush_logs()
            print(f"\n日志已保存: {self._log_path}")


def main() -> None:
    try:
        manager = GameManager(num_players=5, num_rounds=5)
        manager.run()
    except ValueError as e:
        print(f"[配置错误] {e}")
    except Exception as e:
        print(f"[运行异常] {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
