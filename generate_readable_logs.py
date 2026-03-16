"""
从现有游戏日志生成可读记录（不修改游戏逻辑）。
支持：猜数字、拍卖、谁是卧底。
用法：python generate_readable_logs.py <日志路径.json> [--output 输出路径]
"""

import argparse
import json
import os
import re
from typing import Any


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _detect_game_type(log_path: str) -> str:
    basename = os.path.basename(log_path).lower()
    if "guess_number" in basename:
        return "guess_number"
    if "auction" in basename:
        return "auction"
    if "chameleon" in basename:
        return "chameleon"
    return ""


def _default_output_path(log_path: str, ext: str = ".md") -> str:
    """可读报告输出到项目下的 readable/ 目录，不放在 logs/ 下。"""
    readable_dir = os.path.join(_script_dir(), "readable")
    basename = os.path.basename(log_path)
    name_without_ext, _ = os.path.splitext(basename)
    return os.path.join(readable_dir, name_without_ext + "_readable" + ext)


# ---------- 猜数字：模型、每轮每个 agent 的数字与理由对比 ----------
def _render_guess_number(data: dict[str, Any]) -> str:
    lines = [
        "# 猜数字 - 可读记录",
        "",
        f"**模型**：{data.get('model', '')}",
        f"**玩家数**：{data.get('num_players', 0)}",
        f"**轮数**：{data.get('num_rounds', 0)}",
        "",
        "---",
        "",
    ]
    round_results = data.get("round_results") or []
    for r in round_results:
        round_no = r.get("round", 0)
        choices = r.get("choices") or {}
        thoughts = r.get("thoughts") or {}
        average = r.get("average")
        target = r.get("target")
        winner = r.get("winner", "")
        lines.append(f"## 第 {round_no} 轮")
        lines.append("")
        if average is not None and target is not None:
            lines.append(f"- 全场平均：{average:.2f}，Target：{target:.2f}，获胜者：{winner}")
            lines.append("")
        lines.append("| 玩家 | 出数 | 理由（推理/thought） |")
        lines.append("|------|------|----------------------|")
        for name in sorted(choices.keys()):
            num = choices[name]
            reason = (thoughts.get(name) or "").replace("\n", " ").strip()[:500]
            if len((thoughts.get(name) or "")) > 500:
                reason += "…"
            reason_esc = reason.replace("|", "\\|")
            lines.append(f"| {name} | {num} | {reason_esc} |")
        lines.append("")
    return "\n".join(lines)


# ---------- 拍卖：每 agent 每轮行为、想法、对问题的回答、意图/欲望猜测正确率 ----------
def _extract_bid_reason(content: str) -> str:
    """从 assistant 的 JSON 中提取 reason。"""
    try:
        m = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
        if m:
            return m.group(1).replace("\\\"", '"').strip()
    except Exception:
        pass
    return content.strip()[:300]


def _parse_auction_messages_by_item_round(agents_data: dict) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """
    按 agent_name -> item_id -> [(bid_reason, observe_answer), ...] 组织，顺序与日志一致。
    """
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for agent_name, agent in agents_data.items():
        messages = agent.get("messages") or []
        # 列表 (item_id, round_index) 对应 (bid_reason, observe_answer)
        by_key: dict[tuple[str, int], tuple[str, str]] = {}
        i = 0
        while i < len(messages):
            if messages[i].get("role") != "user":
                i += 1
                continue
            content = messages[i].get("content") or ""
            m = re.search(r"当前拍卖物品[：:]\s*[^（(]*[（(]([a-z0-9_]+)[）)]", content)
            if not m:
                i += 1
                continue
            item_id = m.group(1)
            if "请输出 JSON" in content and "action" in content:
                bid_reason = ""
                if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                    bid_reason = _extract_bid_reason(messages[i + 1].get("content") or "")
                i += 2
                observe_answer = ""
                if i < len(messages) and messages[i].get("role") == "user":
                    obs_content = messages[i].get("content") or ""
                    if "观察者" in obs_content or "推断其本轮行为的意图" in obs_content:
                        if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                            observe_answer = (messages[i + 1].get("content") or "").strip()[:800]
                        i += 2
                round_idx = len([k for k in by_key if k[0] == item_id])
                by_key[(item_id, round_idx)] = (bid_reason, observe_answer)
                continue
            i += 1
        # 转成 agent -> item_id -> list of (bid, observe) 按 round 序
        result[agent_name] = {}
        for (it, r), pair in sorted(by_key.items(), key=lambda x: (x[0][0], x[0][1])):
            if it not in result[agent_name]:
                result[agent_name][it] = []
            while len(result[agent_name][it]) <= r:
                result[agent_name][it].append(("", ""))
            result[agent_name][it][r] = pair
    return result


def _render_auction(data: dict[str, Any]) -> str:
    lines = [
        "# 拍卖 - 可读记录",
        "",
        f"**模型**：{data.get('model', '')}",
        "",
    ]
    summary = data.get("summary") or {}
    lines.append("## 总体正确率")
    lines.append("")
    lines.append(f"- 意图猜测正确数：{summary.get('intent_correct', 0)}")
    lines.append(f"- 欲望猜测正确数：{summary.get('desire_correct', 0)}")
    lines.append(f"- 意图正确率：{summary.get('intent_accuracy_pct', 0):.1f}%")
    lines.append(f"- 欲望正确率：{summary.get('desire_accuracy_pct', 0):.1f}%")
    lines.append("")
    lines.append("---")
    lines.append("")

    eval_raw = data.get("eval_results_raw") or []
    agents_data = {a.get("name") or a.get("agent_id"): a for a in (data.get("agents") or [])}
    msg_by_agent_item = _parse_auction_messages_by_item_round(agents_data)

    for er in eval_raw:
        item_id = er.get("item_id", "")
        item_name = er.get("item_name", item_id)
        round_no = er.get("round_no", 0)
        lines.append(f"## 物品 {item_name}（{item_id}）第 {round_no} 轮")
        lines.append("")

        # 本轮行为
        round_actions = er.get("round_actions") or []
        lines.append("### 本轮行为")
        lines.append("")
        for pair in round_actions:
            if len(pair) >= 2:
                lines.append(f"- **{pair[0]}**：{pair[1]}")
        lines.append("")

        # 真实意图（self_intents），None/空 显示为 -
        self_intents = er.get("self_intents") or {}
        if self_intents:
            lines.append("### 真实意图（A=真心想要，B=抬价/不想要）")
            lines.append("")
            for agent_name, intent in self_intents.items():
                disp = "-" if (intent is None or intent == "" or str(intent).lower() == "none") else intent
                lines.append(f"- {agent_name}：{disp}")
            lines.append("")

        # 各观察者的推断与正确性（从 intent_guesses, most_urgent_guesses 与 intent_correct/desire_correct 结合）
        intent_guesses = er.get("intent_guesses") or {}
        most_urgent_guesses = er.get("most_urgent_guesses") or {}
        intent_correct_list = er.get("intent_correct") or []
        desire_correct_list = er.get("desire_correct") or []
        most_urgent_gt = er.get("most_urgent_ground_truth", "")

        # 意图正确：observer 对 actor 的猜测与 self_intents[actor] 一致
        intent_correct_set = {(c.get("observer"), c.get("actor")) for c in intent_correct_list}
        desire_correct_observers = {c.get("observer") for c in desire_correct_list if c.get("observer")}

        lines.append("### 各 Agent 的推断与正确性")
        lines.append("")
        def _intent_disp(g: Any) -> str:
            if g is None or g == "" or str(g).lower() == "none":
                return "-"
            return str(g)

        for obs_name, guesses in intent_guesses.items():
            intent_ok = sum(1 for actor, g in (guesses or {}).items() if (obs_name, actor) in intent_correct_set)
            desire_ok = 1 if obs_name in desire_correct_observers else 0
            most_urgent_guess = (most_urgent_guesses or {}).get(obs_name, "") or "-"
            if str(most_urgent_guess).lower() == "none":
                most_urgent_guess = "-"
            guess_str = "；".join(f"{a}: {_intent_disp(g)}" for a, g in (guesses or {}).items())
            lines.append(f"- **{obs_name}**")
            lines.append(f"  - 意图推断：{guess_str}")
            lines.append(f"  - 意图正确数：{intent_ok}")
            most_urgent_gt_disp = "-" if (not most_urgent_gt or str(most_urgent_gt).lower() == "none") else most_urgent_gt
            lines.append(f"  - 需求最迫切推断：{most_urgent_guess}（标准答案：{most_urgent_gt_disp}）")
            lines.append(f"  - 欲望正确：{'是' if desire_ok else '否'}")
            lines.append("")

        # 从 messages 中摘取本轮对应出价理由与观察回答
        lines.append("### 各 Agent 本轮想法与对问题的回答（来自 message）")
        lines.append("")
        round_idx = round_no - 1
        for agent_name in agents_data:
            bid_reason = ""
            observe_answer = ""
            if agent_name in msg_by_agent_item and item_id in msg_by_agent_item[agent_name]:
                rounds_list = msg_by_agent_item[agent_name][item_id]
                if round_idx < len(rounds_list):
                    bid_reason, observe_answer = rounds_list[round_idx]
            if bid_reason or observe_answer:
                lines.append(f"- **{agent_name}**")
                if bid_reason:
                    lines.append(f"  - 出价理由：{bid_reason[:400]}{'…' if len(bid_reason) > 400 else ''}")
                if observe_answer:
                    lines.append(f"  - 观察回答：{observe_answer[:400]}{'…' if len(observe_answer) > 400 else ''}")
                lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------- 谁是卧底：游戏背景流程、每人发言、对 mole 的提问、mole 的回答和内心想法 ----------
def _render_chameleon(data: dict[str, Any]) -> str:
    lines = [
        "# 谁是卧底（变色龙） - 可读记录",
        "",
        f"**模型**：{data.get('model', '')}",
        f"**关键词**：{data.get('keyword', '')}（不向玩家展示）",
        f"**变色龙 ID**：{data.get('chameleon_id', '')}",
        f"**线人（mole）ID**：{data.get('mole_id', '')}",
        f"**游戏结果**：{data.get('game_winner', '')}",
        "",
        "---",
        "",
    ]

    # 每轮结束后对 mole 的 ToM 询问（若有 tom_rounds 则按轮展示）
    tom_rounds = data.get("tom_rounds") or []
    tom_by_round = {tr.get("round_no"): tr for tr in tom_rounds if tr.get("round_no") is not None}

    # 游戏流程：按轮（含本轮流放后对 mole 的提问与回答）
    rounds = data.get("rounds") or []
    lines.append("## 游戏流程与每轮摘要")
    lines.append("")
    for r in rounds:
        round_no = r.get("round_no", 0)
        accused = r.get("accused_id", "")
        accuser = r.get("accuser_id", "")
        eliminated = r.get("eliminated", "")
        remaining = r.get("remaining_after") or []
        forced = r.get("forced_accusation", False)
        lines.append(f"### 第 {round_no} 轮")
        lines.append(f"- 被指控者：{accused}，指控者：{accuser}" + ("（强制指控）" if forced else ""))
        lines.append(f"- 本轮流放：{eliminated}，剩余：{remaining}")
        lines.append("")
        # 该轮描述
        descs = r.get("descriptions") or []
        lines.append("**描述环节发言：**")
        for d in descs:
            lines.append(f"- {d.get('name', d.get('id', ''))}（{d.get('id', '')}）：{d.get('text', '')}")
        lines.append("")
        # 投票与理由
        vote_reasons = r.get("vote_reasons") or []
        lines.append("**投票与理由：**")
        for vr in vote_reasons:
            lines.append(f"- {vr.get('player_name', vr.get('player_id', ''))} 投票给 {vr.get('vote', '')}：{vr.get('reason', '')[:200]}{'…' if len(vr.get('reason', '')) > 200 else ''}")
        lines.append("")
        # 本轮流放后对 mole 的提问与回答（每轮结束后问一次 mole）
        if round_no in tom_by_round:
            tr = tom_by_round[round_no]
            lines.append("**本轮流放后对 mole 的提问与回答：**")
            lines.append("")
            lines.append(f"- **事实题（谁是真正的变色龙？）**：{tr.get('tom_fact_answer', '')}")
            lines.append(f"- **是否答对（知悉变色龙）**：{tr.get('mole_knowledge_of_chameleon', False)}")
            lines.append("- **错误信念题（他人认为谁是变色龙及理由）**：")
            fb = (tr.get("tom_false_belief_answer") or "").strip()
            for ln in fb.splitlines():
                lines.append(f"  {ln[:500]}{'…' if len(ln) > 500 else ''}")
            if not fb:
                lines.append("  （无）")
            lines.append("")
    lines.append("---")
    lines.append("")

    # 每个人的发言（从 agents 的 messages 中提取描述与投票）
    lines.append("## 每位玩家的发言与投票（来自 message）")
    lines.append("")
    for agent in data.get("agents") or []:
        pid = agent.get("player_id", "")
        name = agent.get("name", "")
        role = agent.get("role", "")
        lines.append(f"### {name}（{pid}）— {role}")
        lines.append("")
        for msg in agent.get("messages") or []:
            if msg.get("role") != "assistant":
                continue
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            # 描述类：不含「投票：」的通常为描述
            if "投票：" in content or "理由：" in content:
                lines.append(f"- **投票/理由**：{content[:400]}{'…' if len(content) > 400 else ''}")
            else:
                lines.append(f"- **描述**：{content[:400]}{'…' if len(content) > 400 else ''}")
        lines.append("")

    # 线人实际投票、最终 ToM 结果（无 tom_rounds 时用顶层字段）、mole 的发言与内心想法
    mole_id = data.get("mole_id", "")
    actual_vote = data.get("actual_vote", "")
    lines.append("## 线人（mole）相关汇总")
    lines.append("")
    lines.append("### 线人实际投票")
    lines.append("")
    lines.append(f"- 线人（{mole_id}）实际投票对象：{actual_vote}")
    lines.append("")
    # 若有 tom_rounds，每轮问答已在上面各轮中展示；否则用顶层字段展示一次（兼容旧日志）
    if not tom_rounds:
        tom_fact = (data.get("tom_fact_answer") or "").strip()
        tom_false = (data.get("tom_false_belief_answer") or "").strip()
        mole_knowledge = data.get("mole_knowledge_of_chameleon", False)
        lines.append("### 对 mole 的提问与回答（整局一次，旧格式日志）")
        lines.append("")
        lines.append(f"- **事实题**：{tom_fact}")
        lines.append(f"- **是否答对**：{mole_knowledge}")
        lines.append(f"- **错误信念题**：{tom_false[:400]}{'…' if len(tom_false) > 400 else ''}")
        lines.append("")
    else:
        lines.append("（每轮结束后对 mole 的提问与回答已在上方「游戏流程与每轮摘要」中各轮下展示。）")
        lines.append("")

    # mole 的内心想法：其 messages 中的描述与投票理由
    mole_agent = next((a for a in (data.get("agents") or []) if a.get("player_id") == mole_id), None)
    if mole_agent:
        lines.append("### 线人（mole）的发言与内心想法（其描述与投票理由）")
        lines.append("")
        for msg in mole_agent.get("messages") or []:
            if msg.get("role") == "assistant":
                content = (msg.get("content") or "").strip()
                if content:
                    lines.append(f"- {content[:500]}{'…' if len(content) > 500 else ''}")
                    lines.append("")

    return "\n".join(lines)


def generate_readable(log_path: str, output_path: str | None = None, skip_if_exists: bool = True) -> str | None:
    """
    为单个日志生成可读报告。若 skip_if_exists 且输出文件已存在则跳过并返回 None，否则返回输出路径。
    """
    out_path = output_path or _default_output_path(log_path)
    if skip_if_exists and os.path.isfile(out_path):
        return None
    with open(log_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    game_type = _detect_game_type(log_path)
    if not game_type:
        raise ValueError(f"无法从文件名识别游戏类型，请使用包含 guess_number、auction 或 chameleon 的日志路径：{log_path}")
    if game_type == "guess_number":
        out = _render_guess_number(data)
    elif game_type == "auction":
        out = _render_auction(data)
    else:
        out = _render_chameleon(data)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return out_path


def _collect_logs_dir(logs_dir: str) -> list[str]:
    """收集 logs_dir 下所有可识别的游戏日志（guess_number / auction / chameleon 的 .json）。"""
    if not os.path.isdir(logs_dir):
        return []
    paths = []
    for name in os.listdir(logs_dir):
        if not name.endswith(".json"):
            continue
        if _detect_game_type(name):
            paths.append(os.path.join(logs_dir, name))
    return sorted(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description="从游戏日志生成可读记录（.md）")
    parser.add_argument(
        "log_path",
        nargs="?",
        default=None,
        help="可选。指定则只处理该日志；不指定则处理 logs/ 下所有可识别日志。",
    )
    parser.add_argument("--output", "-o", default=None, help="仅当指定 log_path 时有效，输出路径")
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="已存在 _readable.md 时也重新生成（默认不重复生成）",
    )
    args = parser.parse_args()
    logs_dir = os.path.join(_script_dir(), "logs")

    if args.log_path:
        out = generate_readable(args.log_path, args.output, skip_if_exists=not args.force)
        if out:
            print(f"已生成可读记录：{out}")
        else:
            print(f"已存在，跳过：{_default_output_path(args.log_path)}")
        return

    paths = _collect_logs_dir(logs_dir)
    if not paths:
        print(f"未在 {logs_dir} 下找到可识别的游戏日志（guess_number / auction / chameleon 的 .json）")
        return
    generated = 0
    skipped = 0
    for log_path in paths:
        out = generate_readable(log_path, skip_if_exists=not args.force)
        if out:
            print(f"已生成：{out}")
            generated += 1
        else:
            skipped += 1
    print(f"完成：新生成 {generated} 份，跳过（已存在）{skipped} 份。")


if __name__ == "__main__":
    main()
