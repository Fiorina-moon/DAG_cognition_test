"""
拍卖（Auction）多轮动态博弈评估系统。
用于测量 LLM 的社会认知能力（ToM），遵循 DGA 框架；配置与逻辑由 config/auction_config.json 驱动。
"""

import json
import os
from datetime import datetime
from typing import Any

from openai import OpenAI

from paratera_common import (
    get_client,
    get_model_for_game,
    get_script_dir,
    parse_model_output,
    chat_with_model,
)

_SCRIPT_DIR = get_script_dir()

# ---------- Prompt 路径（.env 可覆盖） ----------
_DEFAULT_SYSTEM_PROMPT = os.path.join(_SCRIPT_DIR, "prompts", "auction_system.txt")
_DEFAULT_TOM_SELF_INTENT = os.path.join(_SCRIPT_DIR, "prompts", "auction_tom_self_intent.txt")
_DEFAULT_TOM_OBSERVE = os.path.join(_SCRIPT_DIR, "prompts", "auction_tom_observe.txt")
AUCTION_SYSTEM_PROMPT_PATH = (
    os.getenv("AUCTION_SYSTEM_PROMPT_PATH", "").strip() or _DEFAULT_SYSTEM_PROMPT
)
AUCTION_TOM_SELF_INTENT_PATH = (
    os.getenv("AUCTION_TOM_SELF_INTENT_PATH", "").strip() or _DEFAULT_TOM_SELF_INTENT
)
AUCTION_TOM_OBSERVE_PATH = (
    os.getenv("AUCTION_TOM_OBSERVE_PATH", "").strip() or _DEFAULT_TOM_OBSERVE
)


def _load_auction_config() -> dict[str, Any]:
    path = os.path.join(_SCRIPT_DIR, "config", "auction_config.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"拍卖配置不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_tom_intent(answer_raw: str) -> str | None:
    """从 ToM 回答中解析二选一意图，返回 "A" 或 "B"，无法解析则返回 None。"""
    s = (answer_raw or "").strip().upper()
    for c in s:
        if c == "A":
            return "A"
        if c == "B":
            return "B"
    return None


def _parse_observer_intents(answer_raw: str, agent_names: list[str]) -> dict[str, str | None]:
    """从观察者回答中解析对每个 Agent 的意图推断，返回 {agent_name: "A"|"B"|None}。"""
    s = (answer_raw or "").strip()
    result = {name: None for name in agent_names}
    for name in agent_names:
        for sep in (f"{name}:", f"{name}：", f"{name} :"):
            if sep in s:
                idx = s.find(sep)
                rest = s[idx + len(sep) :].strip()
                if rest.startswith("A") or rest.startswith("a"):
                    result[name] = "A"
                elif rest.startswith("B") or rest.startswith("b"):
                    result[name] = "B"
                break
    return result


def _normalize_most_urgent(answer_raw: str, agent_names: list[str]) -> str | None:
    """从 ToM 回答中解析「需求最迫切」的 Agent，返回 agent 名称，无法解析则返回 None。"""
    s = (answer_raw or "").strip()
    for sep in ("需求最迫切：", "需求最迫切:", "需求最迫切 "):
        if sep in s:
            part = s.split(sep, 1)[-1].strip().split("\n")[0].strip()
            break
    else:
        part = s
    part_upper = part.upper()
    for name in agent_names:
        if name.upper() in part_upper:
            return name
    for name in agent_names:
        short = name.replace("Agent ", "").strip()
        if short.upper() == part_upper or (len(part_upper) == 1 and part_upper == short.upper()):
            return name
    return None


def _role_to_str(role: Any) -> str:
    if isinstance(role, str):
        return role
    if isinstance(role, dict):
        parts = []
        if role.get("competes_with"):
            parts.append(f"与 {', '.join(role['competes_with'])} 竞争")
        if role.get("cooperates_with"):
            parts.append(f"与 {', '.join(role['cooperates_with'])} 合作")
        return "；".join(parts) if parts else "无特别说明"
    return str(role)


def _resolve_prompt_path(env_path: str, default_abs: str) -> str:
    if os.path.isabs(env_path):
        return env_path
    return os.path.normpath(os.path.join(_SCRIPT_DIR, env_path))


def build_auction_system_prompt(
    agent_id: str,
    agent_name: str,
    budget: int,
    target_item: str,
    role: Any,
) -> str:
    path = _resolve_prompt_path(
        AUCTION_SYSTEM_PROMPT_PATH,
        _DEFAULT_SYSTEM_PROMPT,
    )
    if not os.path.isfile(path):
        raise FileNotFoundError(f"拍卖 System Prompt 文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        template = f.read()
    return (
        template.replace("{{agent_id}}", agent_id)
        .replace("{{agent_name}}", agent_name)
        .replace("{{budget}}", str(budget))
        .replace("{{target_item}}", target_item)
        .replace("{{role}}", _role_to_str(role))
    )


# ---------- Auction Agent ----------
class AuctionAgent:
    """拍卖 Agent：budget、target_item、role、memory（历史出价摘要）、history（messages）。"""

    def __init__(
        self,
        agent_id: str,
        name: str,
        budget: int,
        target_item: str,
        role: Any,
        model: str,
        client: OpenAI,
    ):
        self.agent_id = agent_id
        self.name = name
        self.budget = budget
        self.target_item = target_item
        self.role = role
        self.model = model
        self.client = client
        self.memory: list[str] = []
        self.history: list[dict[str, str]] = []
        self._system_prompt = build_auction_system_prompt(
            agent_id, name, budget, target_item, role
        )

    def _build_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            *self.history,
        ]

    def call_api(self, user_content: str) -> dict[str, Any]:
        """
        追加 user_content，调用 API，解析 JSON。
        返回 {"action": "bid"|"fold", "amount": int, "reason": str}；解析失败或非法则抛出。
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
            action = (data.get("action") or "").strip().lower()
            if action not in ("bid", "fold"):
                raise ValueError(f"非法 action: {action}")
            amount = data.get("amount", 0)
            if not isinstance(amount, (int, float)):
                amount = int(float(amount))
            amount = max(0, int(amount))
            data["action"] = action
            data["amount"] = amount
            data["reason"] = data.get("reason") or ""
            return data
        except (ValueError, json.JSONDecodeError):
            raise
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[调用异常] {e}"})
            raise

    def ask_freeform(self, user_content: str) -> str:
        """追加 user 消息并调用 API，返回原始回复文本（不解析 JSON）。用于 ToM 观察者提问等。"""
        self.history.append({"role": "user", "content": user_content})
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self._build_messages(),
            )
            content = response.choices[0].message.content or ""
            self.history.append({"role": "assistant", "content": content})
            return content.strip()
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[调用异常] {e}"})
            raise


# ---------- AuctionGameManager ----------
class AuctionGameManager:
    """4 Agent、5 件物品；public_board、轮次控制、价格翻倍时 ToM 探测、日志与 eval_results。"""

    def __init__(self):
        self.config = _load_auction_config()
        self.model = get_model_for_game()
        self.client = get_client()
        agents_cfg = self.config.get("agents", [])
        items_cfg = self.config.get("items", [])
        if len(agents_cfg) != 4 or len(items_cfg) != 5:
            raise ValueError("config 需包含 4 个 agents 与 5 个 items")
        self.agents: list[AuctionAgent] = [
            AuctionAgent(
                agent_id=a["id"],
                name=a["name"],
                budget=int(a.get("budget", 10000)),
                target_item=a.get("target_item", ""),
                role=a.get("role", {}),
                model=self.model,
                client=self.client,
            )
            for a in agents_cfg
        ]
        self.items: list[dict[str, Any]] = items_cfg
        self.public_board: list[str] = []
        self.eval_results: list[dict[str, Any]] = []
        self.item_results: list[dict[str, Any]] = []
        self._log_path: str | None = None
        self._agent_names = [a.name for a in self.agents]

    def _format_board_for_agent(
        self,
        item_id: str,
        item_name: str,
        current_price: float,
        leading_agent: str | None,
        board_entries: list[str],
    ) -> str:
        parts = [
            f"当前拍卖物品：{item_name}（{item_id}）",
            f"当前最高价：{current_price}",
            f"当前领先者：{leading_agent or '无'}",
            "历史出价序列：",
            *(board_entries if board_entries else ["（暂无）"]),
            "",
            "请输出 JSON：{\"action\": \"bid\" 或 \"fold\", \"amount\": 加价幅度（仅 bid 时有效）, \"reason\": \"...\"}",
        ]
        return "\n".join(parts)

    def _build_log_summary(self) -> dict[str, Any]:
        """按物品聚合 ToM 结果，生成可读的 summary 与 items 结构。"""
        by_item: dict[str, list[dict[str, Any]]] = {}
        for r in self.eval_results:
            iid = r.get("item_id", "")
            if iid not in by_item:
                by_item[iid] = []
            by_item[iid].append(r)
        total_intent = sum(len(r.get("intent_correct", [])) for r in self.eval_results)
        total_desire = sum(len(r.get("desire_correct", [])) for r in self.eval_results)
        total_rounds = len(self.eval_results)
        n_obs = len(self._agent_names)
        max_intent_per_round = n_obs * (n_obs - 1) if n_obs > 1 else 0
        max_desire_per_round = n_obs
        total_max_intent = total_rounds * max_intent_per_round
        total_max_desire = total_rounds * max_desire_per_round
        summary = {
            "total_rounds": total_rounds,
            "intent_correct": total_intent,
            "desire_correct": total_desire,
            "intent_accuracy_pct": round(100 * total_intent / total_max_intent, 1) if total_max_intent else 0,
            "desire_accuracy_pct": round(100 * total_desire / total_max_desire, 1) if total_max_desire else 0,
        }
        items_detailed: list[dict[str, Any]] = []
        for ir in self.item_results:
            iid = ir["item_id"]
            rounds_for_item = by_item.get(iid, [])
            tom_rounds = [
                {
                    "round_no": r["round_no"],
                    "round_actions": r["round_actions"],
                    "self_intents": r["self_intents"],
                    "intent_correct_count": len(r.get("intent_correct", [])),
                    "desire_correct_count": len(r.get("desire_correct", [])),
                    "intent_accuracy_pct_round": r.get("intent_accuracy_pct_round"),
                    "desire_accuracy_pct_round": r.get("desire_accuracy_pct_round"),
                    "intent_correct": r.get("intent_correct", []),
                    "desire_correct": r.get("desire_correct", []),
                }
                for r in rounds_for_item
            ]
            item_intent = sum(len(r.get("intent_correct", [])) for r in rounds_for_item)
            item_desire = sum(len(r.get("desire_correct", [])) for r in rounds_for_item)
            items_detailed.append({
                "item_id": iid,
                "item_name": ir["item_name"],
                "auction": {
                    "starting_price": ir["starting_price"],
                    "final_price": ir["final_price"],
                    "winner": ir["winner"],
                    "board_entries": ir["board_entries"],
                },
                "tom_rounds": tom_rounds,
                "item_intent_correct": item_intent,
                "item_desire_correct": item_desire,
            })
        return {"summary": summary, "items": items_detailed}

    def _flush_logs(self) -> None:
        if not self._log_path:
            return
        log_summary = self._build_log_summary()
        payload = {
            "model": self.model,
            "config_ref": "config/auction_config.json",
            "summary": log_summary["summary"],
            "items": log_summary["items"],
            "public_board": list(self.public_board),
            "agents": [
                {
                    "name": a.name,
                    "agent_id": a.agent_id,
                    "budget": a.budget,
                    "target_item": a.target_item,
                    "role": a.role,
                    "memory": list(a.memory),
                    "messages": a.history,
                }
                for a in self.agents
            ],
            "eval_results_raw": self.eval_results,
            "item_results": self.item_results,
        }
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _run_tom_after_round(
        self,
        item_id: str,
        item_name: str,
        board_snapshot: str,
        round_no: int,
        round_actions: list[tuple[str, str]],
        active_at_round_start: set[str],
    ) -> None:
        """每轮出价/放弃后：询问自身意图、他人意图与欲望，并记录推测是否正确。"""
        if not round_actions:
            return
        agent_names = self._agent_names
        path_self = _resolve_prompt_path(AUCTION_TOM_SELF_INTENT_PATH, _DEFAULT_TOM_SELF_INTENT)
        path_observe = _resolve_prompt_path(AUCTION_TOM_OBSERVE_PATH, _DEFAULT_TOM_OBSERVE)
        if not os.path.isfile(path_self) or not os.path.isfile(path_observe):
            return
        with open(path_self, "r", encoding="utf-8") as f:
            template_self = f.read()
        with open(path_observe, "r", encoding="utf-8") as f:
            template_observe = f.read()

        self_intents: dict[str, str | None] = {}
        for agent_name, action_desc in round_actions:
            try:
                q = (
                    template_self.replace("{{agent_name}}", agent_name)
                    .replace("{{action_description}}", action_desc)
                )
                ans = chat_with_model(
                    messages=[{"role": "user", "content": q}],
                    model_name=self.model,
                )
                self_intents[agent_name] = _normalize_tom_intent(ans)
            except Exception as e:
                print(f"    [ToM 自身意图] {agent_name} 失败: {e}")
                self_intents[agent_name] = None

        agents_acted_this_round = [name for name, _ in round_actions]
        agents_not_this_round = [n for n in agent_names if n not in agents_acted_this_round]
        agents_acted_str = "、".join(agents_acted_this_round) if agents_acted_this_round else "无"
        agents_not_str = "、".join(agents_not_this_round) if agents_not_this_round else "无"

        intent_guesses: dict[str, dict[str, str | None]] = {}
        most_urgent_guesses: dict[str, str | None] = {}
        for agent in self.agents:
            observer_name = agent.name
            observe_content = (
                template_observe.replace("{{public_board_snapshot}}", board_snapshot)
                .replace("{{agents_acted_this_round}}", agents_acted_str)
                .replace("{{agents_not_this_round}}", agents_not_str)
                .replace("{{observer_name}}", observer_name)
            )
            try:
                ans = agent.ask_freeform(observe_content)
                intent_guesses[observer_name] = _parse_observer_intents(ans, agent_names)
                most_urgent_guesses[observer_name] = _normalize_most_urgent(ans, agent_names)
            except Exception as e:
                print(f"    [ToM 观察他人] {observer_name} 失败: {e}")
                intent_guesses[observer_name] = {n: None for n in agent_names}
                most_urgent_guesses[observer_name] = None

        most_urgent_ground_truth: str | None = None
        for a in self.agents:
            if a.target_item == item_id:
                most_urgent_ground_truth = a.name
                break

        intent_correct: list[dict[str, Any]] = []
        desire_correct: list[dict[str, Any]] = []
        for observer in agent_names:
            for actor in agent_names:
                if observer == actor:
                    continue
                self_int = self_intents.get(actor)
                guess = (intent_guesses.get(observer) or {}).get(actor)
                if self_int is not None and guess is not None and self_int == guess:
                    intent_correct.append({"observer": observer, "actor": actor, "intent": self_int})
            gt = most_urgent_ground_truth
            guess_urgent = most_urgent_guesses.get(observer)
            if gt is not None and guess_urgent is not None and gt == guess_urgent:
                desire_correct.append({"observer": observer, "most_urgent": gt})

        max_intent_round = len(agents_acted_this_round) * (len(agent_names) - 1) if agent_names else 0
        max_desire_round = len(agent_names)
        intent_accuracy_pct_round = round(100 * len(intent_correct) / max_intent_round, 1) if max_intent_round else 0.0
        desire_accuracy_pct_round = round(100 * len(desire_correct) / max_desire_round, 1) if max_desire_round else 0.0

        record = {
            "trigger": "after_round",
            "item_id": item_id,
            "item_name": item_name,
            "round_no": round_no,
            "round_actions": round_actions,
            "agents_acted_this_round": agents_acted_this_round,
            "agents_not_this_round": agents_not_this_round,
            "self_intents": self_intents,
            "intent_guesses": intent_guesses,
            "most_urgent_guesses": most_urgent_guesses,
            "most_urgent_ground_truth": most_urgent_ground_truth,
            "intent_correct": intent_correct,
            "desire_correct": desire_correct,
            "intent_accuracy_pct_round": intent_accuracy_pct_round,
            "desire_accuracy_pct_round": desire_accuracy_pct_round,
        }
        self.eval_results.append(record)
        print(f"    [ToM] 第{round_no}轮 自身意图={self_intents} | 意图正确={len(intent_correct)}/{max_intent_round} ({intent_accuracy_pct_round}%) | 欲望正确={len(desire_correct)}/{max_desire_round} ({desire_accuracy_pct_round}%)")
        self._flush_logs()

    def run(self) -> None:
        print(f"模型: {self.model} | 4 Agent | 5 件物品")
        print("=" * 60)
        logs_dir = os.path.join(_SCRIPT_DIR, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self._log_path = os.path.join(
            logs_dir,
            f"auction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        self._flush_logs()

        for item in self.items:
            item_id = item["id"]
            item_name = item.get("name", item_id)
            starting_price = float(item.get("starting_price", 100))
            current_price = starting_price
            leading_agent: str | None = None
            board_entries: list[str] = []
            active_ids = {a.agent_id for a in self.agents}
            self.public_board = [
                f"物品 {item_id}（{item_name}）开拍，起价 {starting_price}。"
            ]

            print(f"\n--- 拍卖物品: {item_name} ({item_id})，起价 {starting_price} ---")
            round_no = 0
            active_at_round_start: set[str] = set()
            while True:
                round_no += 1
                active_at_round_start = set(a.agent_id for a in self.agents if a.agent_id in active_ids)
                round_actions: list[tuple[str, str]] = []
                no_bid_this_round = True
                for agent in self.agents:
                    if agent.agent_id not in active_ids:
                        continue
                    user_content = self._format_board_for_agent(
                        item_id,
                        item_name,
                        current_price,
                        leading_agent,
                        board_entries,
                    )
                    print(f"  请求 {agent.name}…", flush=True)
                    try:
                        data = agent.call_api(user_content)
                    except Exception as e:
                        print(f"  [异常] {agent.name}: {e}")
                        data = {"action": "fold", "amount": 0, "reason": str(e)}
                    self._flush_logs()

                    if data["action"] == "fold":
                        active_ids.discard(agent.agent_id)
                        board_entries.append(f"{agent.name} 选择 fold。")
                        agent.memory.append(f"本物品选择 fold。")
                        round_actions.append((agent.name, "放弃"))
                        continue
                    inc = data["amount"]
                    if inc <= 0:
                        round_actions.append((agent.name, "出价+0（视为放弃）"))
                        continue
                    new_price = current_price + inc
                    if new_price > agent.budget:
                        active_ids.discard(agent.agent_id)
                        board_entries.append(f"{agent.name} 出价 +{inc} 但超出预算，视为 fold。")
                        round_actions.append((agent.name, f"出价+{inc}（超出预算视为放弃）"))
                        continue
                    no_bid_this_round = False
                    current_price = new_price
                    leading_agent = agent.name
                    board_entries.append(
                        f"{agent.name} 出价 +{inc}，当前价 {current_price}。"
                    )
                    agent.memory.append(
                        f"本物品出价 +{inc}，当前价 {current_price}。"
                    )
                    round_actions.append((agent.name, f"出价+{inc}"))
                    self.public_board = [
                        f"物品 {item_id}（{item_name}）起价 {starting_price}。"
                    ] + board_entries
                    self._flush_logs()

                self._run_tom_after_round(
                    item_id,
                    item_name,
                    "\n".join(self.public_board),
                    round_no,
                    round_actions,
                    active_at_round_start,
                )

                if no_bid_this_round or len(active_ids) <= 1:
                    break

            winner = leading_agent
            if winner:
                for a in self.agents:
                    if a.name == winner:
                        a.budget -= int(current_price)
                        break
            self.item_results.append({
                "item_id": item_id,
                "item_name": item_name,
                "starting_price": starting_price,
                "final_price": current_price,
                "winner": winner,
                "board_entries": list(board_entries),
            })
            print(f"  结束：最终价 {current_price}，得主 {winner}")
            self._flush_logs()

        if getattr(self, "_log_path", None):
            self._flush_logs()
            print(f"\n日志已保存: {self._log_path}")


def main() -> None:
    try:
        manager = AuctionGameManager()
        manager.run()
    except ValueError as e:
        print(f"[配置错误] {e}")
    except FileNotFoundError as e:
        print(f"[文件错误] {e}")
    except Exception as e:
        print(f"[运行异常] {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
