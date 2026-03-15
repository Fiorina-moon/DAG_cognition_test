"""
谁是变色龙（Who Is Chameleon）实验模块。
用于测试 LLM 的一级错误信念（First-order False Belief），对应论文《The Social Cognition Ability Evaluation of LLMs》中的 Sally-Anne 范式。
角色：4 平民（有关键词）、1 变色龙（无关键词）、1 线人/鼹鼠（知悉变色龙，无关键词；受试 AI）。
"""

import json
import os
import re
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

_DEFAULT_CHAMELEON_SYSTEM = os.path.join(_SCRIPT_DIR, "prompts", "chameleon_system.txt")
_DEFAULT_CHAMELEON_TOM = os.path.join(_SCRIPT_DIR, "prompts", "chameleon_tom.txt")
CHAMELEON_SYSTEM_PROMPT_PATH = (
    os.getenv("CHAMELEON_SYSTEM_PROMPT_PATH", "").strip() or _DEFAULT_CHAMELEON_SYSTEM
)
CHAMELEON_TOM_PROMPT_PATH = (
    os.getenv("CHAMELEON_TOM_PROMPT_PATH", "").strip() or _DEFAULT_CHAMELEON_TOM
)


def _load_chameleon_config() -> dict[str, Any]:
    path = os.path.join(_SCRIPT_DIR, "config", "chameleon_config.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"变色龙配置不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_prompt_path(env_path: str, default_abs: str) -> str:
    if os.path.isabs(env_path):
        return env_path
    return os.path.normpath(os.path.join(_SCRIPT_DIR, env_path))


# ---------- 平民/变色龙 简易系统提示（内联，仅用于描述与投票） ----------
def _civilian_system_prompt(keyword: str, player_name: str, player_id: str) -> str:
    return f"""你是「谁是变色龙」游戏中的平民。你是 {player_name}（ID：{player_id}）。
本局关键词是：{keyword}。你不能直接说出关键词，只能用与关键词相关的说法描述，同时，你的描述不能过于明显，以防止变色龙猜出关键词，但也不能过于模糊，防止自己被误认为变色龙。
你的目标：在描述环节用一句话描述与关键词相关的内容；在投票环节投票选出你认为的变色龙（输出一位玩家 ID，如 P1）。"""


def _chameleon_system_prompt(player_name: str, player_id: str) -> str:
    return f"""你是「谁是变色龙」游戏中的变色龙特工。你是 {player_name}（ID：{player_id}）。
你没有本局的关键词，需要隐藏身份。描述时用一句模糊、可泛用的说法；投票时投给你认为平民中最像变色龙的某人（输出一位玩家 ID）。"""


def _build_mole_system_prompt(
    mole_id: str,
    mole_name: str,
    chameleon_id: str,
    chameleon_name: str,
    keyword: str,
) -> str:
    path = _resolve_prompt_path(CHAMELEON_SYSTEM_PROMPT_PATH, _DEFAULT_CHAMELEON_SYSTEM)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"线人 System Prompt 不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        t = f.read()
    return (
        t.replace("{{mole_id}}", mole_id)
        .replace("{{mole_name}}", mole_name)
        .replace("{{chameleon_id}}", chameleon_id)
        .replace("{{chameleon_name}}", chameleon_name)
        .replace("{{keyword}}", keyword)
    )


# ---------- 指控解析：从描述或专门指控回合中提取「谁指控了谁」 ----------
_ACCUSATION_PATTERN = re.compile(
    r"(?:指控|指认|认为.*是.*变色龙|投票.*给)\s*[：:]\s*(P[1-6])|"
    r"(P[1-6])\s*(?:是变色龙|为变色龙)|"
    r"我选\s*(P[1-6])|(?:投|选)\s*(P[1-6])",
    re.IGNORECASE,
)


def _extract_accusation_from_text(text: str) -> str | None:
    """从一段文本中提取被指控的玩家 ID（P1-P6），若有多个取第一个。"""
    if not text:
        return None
    for m in _ACCUSATION_PATTERN.finditer(text):
        for g in m.groups():
            if g:
                return g.upper()
    # 兜底：任意出现 P1..P6 且像是对象
    for pid in ["P6", "P5", "P4", "P3", "P2", "P1"]:
        if re.search(rf"\b{pid}\b", text, re.IGNORECASE):
            return pid
    return None


def _extract_single_id_from_text(text: str) -> str | None:
    """从回复中提取单个玩家 ID（P1-P6），用于投票或事实题。"""
    if not text:
        return None
    text = (text or "").strip()
    # 优先代码块或明确格式
    m = re.search(r"\b(P[1-6])\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _parse_vote_and_reason(raw: str) -> tuple[str | None, str]:
    """从投票自由发言中解析「投票对象」与「理由」。支持 投票：P? / 理由：… 或首行 ID + 其余为理由。"""
    if not raw or not raw.strip():
        return None, ""
    text = raw.strip()
    vote_id = None
    reason = ""
    # 显式格式：投票：P3  理由：…
    m = re.search(r"投票[：:]\s*(P[1-6])\b", text, re.IGNORECASE)
    if m:
        vote_id = m.group(1).upper()
    mr = re.search(r"理由[：:]\s*([\s\S]*)", text)
    if mr:
        reason = mr.group(1).strip()
    if not vote_id:
        vote_id = _extract_single_id_from_text(text)
    if not reason and vote_id:
        # 去掉首行/首个 Pn，其余当理由
        rest = re.sub(r"^(.*?)(P[1-6])\b", "", text, count=1, flags=re.IGNORECASE).strip()
        rest = rest.lstrip("：: \n")
        if rest:
            reason = rest
    return vote_id, reason


# ---------- ChameleonPlayer：单名玩家（含平民/变色龙/线人） ----------
class ChameleonPlayer:
    def __init__(
        self,
        player_id: str,
        name: str,
        role: str,
        keyword: str,
        chameleon_id: str | None,
        chameleon_name: str | None,
        model: str,
        client: OpenAI,
    ):
        self.player_id = player_id
        self.name = name
        self.role = role
        self.keyword = keyword
        self.chameleon_id = chameleon_id
        self.chameleon_name = chameleon_name
        self.model = model
        self.client = client
        self.history: list[dict[str, str]] = []

        if role == "mole" and chameleon_id and chameleon_name:
            self._system_prompt = _build_mole_system_prompt(
                player_id, name, chameleon_id, chameleon_name, keyword
            )
        elif role == "civilian":
            self._system_prompt = _civilian_system_prompt(keyword, name, player_id)
        else:
            self._system_prompt = _chameleon_system_prompt(name, player_id)

    def _messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            *self.history,
        ]

    def speak_description(self, round_context: str) -> str:
        """描述环节：返回一句话描述（或含指控）。"""
        self.history.append({"role": "user", "content": round_context})
        try:
            r = self.client.chat.completions.create(
                model=self.model,
                messages=self._messages(),
            )
            content = (r.choices[0].message.content or "").strip()
            self.history.append({"role": "assistant", "content": content})
            return content
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[异常] {e}"})
            raise

    def speak_accusation(self, prompt: str) -> str:
        """强制指控：最后发言者必须指控一人，返回其回复（含 ID）。"""
        self.history.append({"role": "user", "content": prompt})
        try:
            r = self.client.chat.completions.create(
                model=self.model,
                messages=self._messages(),
            )
            content = (r.choices[0].message.content or "").strip()
            self.history.append({"role": "assistant", "content": content})
            return content
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[异常] {e}"})
            raise

    def vote(self, vote_prompt: str) -> str:
        """投票：返回投给的玩家 ID。"""
        self.history.append({"role": "user", "content": vote_prompt})
        try:
            r = self.client.chat.completions.create(
                model=self.model,
                messages=self._messages(),
            )
            content = (r.choices[0].message.content or "").strip()
            self.history.append({"role": "assistant", "content": content})
            return content
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[异常] {e}"})
            raise

    def ask_freeform(self, question: str) -> str:
        """ToM 自由回答（事实题/错误信念题）。"""
        self.history.append({"role": "user", "content": question})
        try:
            r = self.client.chat.completions.create(
                model=self.model,
                messages=self._messages(),
            )
            content = (r.choices[0].message.content or "").strip()
            self.history.append({"role": "assistant", "content": content})
            return content
        except Exception as e:
            self.history.append({"role": "assistant", "content": f"[异常] {e}"})
            raise


# ---------- ChameleonGameManager ----------
class ChameleonGameManager:
    def __init__(self):
        self.config = _load_chameleon_config()
        self.model = get_model_for_game()
        self.client = get_client()
        self.keyword = self.config.get("keyword", "西瓜")
        players_cfg = self.config.get("players", [])
        if len(players_cfg) != 6:
            raise ValueError("config 需包含 6 名玩家")

        chameleon_id = None
        chameleon_name = None
        civilian_ids: list[str] = []
        mole_id = None

        for p in players_cfg:
            rid = (p.get("id") or "").strip().upper()
            role = (p.get("role") or "").strip().lower()
            if role == "chameleon":
                chameleon_id = rid
                chameleon_name = (p.get("name") or rid)
            elif role == "civilian":
                civilian_ids.append(rid)
            elif role == "mole":
                mole_id = rid

        if not chameleon_id or not mole_id or len(civilian_ids) != 4:
            raise ValueError("config 需包含 4 civilian、1 chameleon、1 mole")

        self.chameleon_id = chameleon_id
        self.chameleon_name = chameleon_name or chameleon_id
        self.civilian_ids = civilian_ids
        self.mole_id = mole_id

        self.players: list[ChameleonPlayer] = []
        for p in players_cfg:
            pid = (p.get("id") or "").strip().upper()
            name = (p.get("name") or pid)
            role = (p.get("role") or "").strip().lower()
            self.players.append(
                ChameleonPlayer(
                    player_id=pid,
                    name=name,
                    role=role,
                    keyword=self.keyword,
                    chameleon_id=chameleon_id if role == "mole" else None,
                    chameleon_name=chameleon_name if role == "mole" else None,
                    model=self.model,
                    client=self.client,
                )
            )

        self.player_order: list[ChameleonPlayer] = self.players  # 描述与投票顺序
        self.remaining_player_ids: set[str] = {p.player_id for p in self.players}
        self.rounds: list[dict[str, Any]] = []  # 每轮：descriptions, votes, vote_reasons, eliminated, remaining_after
        self.game_winner: str | None = None  # "civilians" | "chameleon" | None
        self.descriptions: list[dict[str, str]] = []  # 当前轮描述
        self.accused_id: str | None = None
        self.accuser_id: str | None = None
        self.forced_accusation: bool = False
        self.votes: dict[str, str] = {}
        self.vote_reasons: list[dict[str, Any]] = []
        self.tom_fact_answer: str = ""
        self.tom_false_belief_answer: str = ""
        self.mole_knowledge_of_chameleon: bool = False
        self.actual_vote: str | None = None
        self._log_path: str | None = None

    def _get_player_by_id(self, pid: str) -> ChameleonPlayer | None:
        for p in self.players:
            if p.player_id.upper() == (pid or "").upper():
                return p
        return None

    def _pick_innocent_accused(self, remaining: set[str] | None = None) -> str:
        """在无人指控或指控对象为变色龙时，从剩余玩家中选一名无辜平民作为被指控者。"""
        r = remaining if remaining is not None else self.remaining_player_ids
        for p in self.player_order:
            if p.player_id in r and p.role == "civilian" and p.player_id != self.chameleon_id:
                return p.player_id
        return next(iter(r), "")

    def _format_vote_reasons_for_tom(self) -> str:
        """将 vote_reasons 格式化为供 ToM 提问参考的「投票自由发言」文本。"""
        if not self.vote_reasons:
            return "（暂无投票自由发言）"
        lines = []
        for r in self.vote_reasons:
            pid = r.get("player_id", "")
            name = r.get("player_name", "")
            vote = r.get("vote", "")
            reason = (r.get("reason") or "").strip()
            lines.append(f"{pid}（{name}）投给 {vote}，理由：{reason}")
        return "\n".join(lines)

    def _order_remaining(self, remaining: set[str]) -> list[ChameleonPlayer]:
        """按 player_order 顺序返回仍在场上的玩家。"""
        return [p for p in self.player_order if p.player_id in remaining]

    def _flush_logs(self) -> None:
        if not self._log_path:
            return
        payload = {
            "model": self.model,
            "config_ref": "config/chameleon_config.json",
            "keyword": self.keyword,
            "chameleon_id": self.chameleon_id,
            "mole_id": self.mole_id,
            "remaining_player_ids": list(self.remaining_player_ids),
            "rounds": list(self.rounds),
            "game_winner": self.game_winner,
            "descriptions": list(self.descriptions),
            "accused_id": self.accused_id,
            "accuser_id": self.accuser_id,
            "forced_accusation": self.forced_accusation,
            "votes": dict(self.votes),
            "vote_reasons": list(self.vote_reasons),
            "tom_fact_answer": self.tom_fact_answer,
            "tom_false_belief_answer": self.tom_false_belief_answer,
            "mole_knowledge_of_chameleon": self.mole_knowledge_of_chameleon,
            "actual_vote": self.actual_vote,
            "agents": [
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "role": p.role,
                    "messages": list(p.history),
                }
                for p in self.players
            ],
        }
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def run_description_phase(self, remaining: set[str]) -> None:
        """描述环节：仅剩余玩家依次发言，可对先前已发言的玩家发起指控；若无人指控则强制最后发言者指控一名无辜平民。"""
        print("\n--- 描述环节（可对先前发言者指控，无人指控则强制指控） ---")
        self.descriptions = []
        order = self._order_remaining(remaining)
        accused_from_descriptions: str | None = None
        accuser_from_descriptions: str | None = None

        for i, player in enumerate(order):
            if i == 0:
                ctx = (
                    "当前是描述环节，你是本轮第一位发言者。请用一句话描述与本局主题相关的内容（不要直接说出关键词）。"
                )
            else:
                prev_list = "\n".join(
                    f"  {d['id']}（{d['name']}）：{d['text'][:120]}{'…' if len(d['text']) > 120 else ''}"
                    for d in self.descriptions
                )
                prev_ids = "、".join(d["id"] for d in self.descriptions)
                ctx = (
                    f"当前是描述环节。以下是本轮先前已发言的玩家及其内容：\n{prev_list}\n\n"
                    "你可以：（1）用一句话描述与本局主题相关的内容（不要直接说出关键词）；"
                    "（2）若你对某位先前发言者产生怀疑，可以对其发起指控（在描述中写出「指控 P?」或「我指控 P?」，P? 为上述之一："
                    f"{prev_ids}）。可以只描述、只指控、或同时描述与指控。指控后说出你怀疑ta的理由。"
                )
            print(f"  {player.name} ({player.player_id}) 描述中…", flush=True)
            try:
                text = player.speak_description(ctx)
            except Exception as e:
                text = f"[描述异常] {e}"
            self.descriptions.append({
                "id": player.player_id,
                "name": player.name,
                "text": text,
            })
            extracted = _extract_accusation_from_text(text)
            if extracted and extracted in remaining:
                accused_from_descriptions = extracted
                accuser_from_descriptions = player.player_id
        self._flush_logs()

        # Accuser 机制：若无人指控，强制最后发言者指控一名无辜平民
        if accused_from_descriptions is None:
            self.forced_accusation = True
            last_speaker = order[-1]
            self.accuser_id = last_speaker.player_id
            self.accused_id = self._pick_innocent_accused(remaining)
            print(f"  [强制指控] 最后发言者 {last_speaker.name} 指控 → {self.accused_id}（无辜平民）", flush=True)
        else:
            self.accused_id = accused_from_descriptions
            self.accuser_id = accuser_from_descriptions
            if self.accused_id == self.chameleon_id or self.accused_id not in remaining:
                self.accused_id = self._pick_innocent_accused(remaining)
        print(f"  被指控者（众人怀疑对象）: {self.accused_id}")
        self._flush_logs()

    def run_voting_phase(self, remaining: set[str]) -> None:
        """
        投票环节：所有在场玩家由 AI 扮演投票；每人投票时不展示他人投票结果（并行决策/屏蔽）。
        """
        print("\n--- 投票环节（AI 扮演，屏蔽他人投票结果） ---")
        summary = "  ".join(
            f"{d['id']}: {d['text'][:50]}..." for d in self.descriptions
        )
        accused_note = f"\n当前被指控者（众人怀疑对象）：{self.accused_id}。" if self.accused_id else ""
        rem_ids = "、".join(sorted(remaining))
        vote_prompt = (
            f"描述环节摘要：{summary}{accused_note}\n\n"
            f"当前在场玩家：{rem_ids}。请从其中选一人投票淘汰（视为你认为的变色龙），并说明理由。"
            "本环节不会向你展示其他玩家的投票结果，以避免相互影响。\n\n"
            "请严格按以下两行格式输出：\n投票：P?\n理由：\n"
        )

        self.votes = {}
        self.vote_reasons = []
        order_rem = self._order_remaining(remaining)
        for player in order_rem:
            print(f"  {player.name}（{player.player_id}）投票中…", flush=True)
            raw = ""
            try:
                raw = player.vote(vote_prompt)
                vid, reason = _parse_vote_and_reason(raw)
                if vid not in remaining:
                    vid = _extract_single_id_from_text(raw) or next(iter(remaining))
                self.votes[player.player_id] = vid or ""
                self.vote_reasons.append({
                    "player_id": player.player_id,
                    "player_name": player.name,
                    "vote": vid or "",
                    "reason": reason or raw[:200] if raw else "",
                })
                if player.player_id == self.mole_id:
                    self.actual_vote = vid
            except Exception as e:
                self.votes[player.player_id] = ""
                self.vote_reasons.append({
                    "player_id": player.player_id,
                    "player_name": player.name,
                    "vote": "",
                    "reason": str(e),
                })
                if player.player_id == self.mole_id:
                    self.actual_vote = None
        self._flush_logs()

    def _eliminate_by_vote(self, remaining: set[str]) -> str | None:
        """根据本轮投票结果淘汰得票最多者（仅统计对剩余玩家的票），平票时按 player_order 取第一个。返回被淘汰者 ID。"""
        vote_count: dict[str, int] = {pid: 0 for pid in remaining}
        for voter_id, target_id in self.votes.items():
            if voter_id not in remaining or target_id not in remaining:
                continue
            vote_count[target_id] = vote_count.get(target_id, 0) + 1
        order_rem = self._order_remaining(remaining)
        max_votes = max(vote_count.values()) if vote_count else 0
        if max_votes == 0:
            return None
        for p in order_rem:
            if vote_count.get(p.player_id) == max_votes:
                return p.player_id
        return None

    def run_tom_phase(self) -> None:
        """ToM 评测：事实题 + 错误信念题，记录并解析线人回答。"""
        print("\n--- ToM 评测（线人） ---")
        path_tom = _resolve_prompt_path(CHAMELEON_TOM_PROMPT_PATH, _DEFAULT_CHAMELEON_TOM)
        if not os.path.isfile(path_tom):
            print("  [跳过] chameleon_tom.txt 不存在")
            return
        with open(path_tom, "r", encoding="utf-8") as f:
            parts = f.read().split("【ToM 认知测试问题")
        fact_part = parts[0].replace("【ToM 控制性问题 - Fact Check】", "").strip()
        false_belief_part = ""
        if len(parts) > 1:
            false_belief_part = "【ToM 认知测试问题" + parts[1].strip()

        mole = self._get_player_by_id(self.mole_id)
        if not mole:
            return

        fact_q = fact_part if fact_part else "谁是真正的变色龙特工？请只输出该玩家的 ID（P1-P6）。"
        self.tom_fact_answer = mole.ask_freeform(fact_q)
        self.mole_knowledge_of_chameleon = (
            _extract_single_id_from_text(self.tom_fact_answer) or ""
        ).upper() == self.chameleon_id
        print(f"  事实题回答: {self.tom_fact_answer[:100]}... | 正确: {self.mole_knowledge_of_chameleon}")

        vote_speeches_block = self._format_vote_reasons_for_tom()
        false_q = (
            "在本次游戏中，描述环节结束后出现了指控或集体怀疑，随后进行了投票。\n\n"
            "【各位在投票时的自由发言（投票对象与理由）】\n"
            f"{vote_speeches_block}\n\n"
            "请参考上述自由发言，回答：其他玩家（平民等）认为谁是变色龙？他们为什么会这样认为？\n\n"
            "请严格按以下格式回答（便于实验者解析）：\n\n"
            "他人认为的变色龙：P?\n"
            "推断逻辑：\n"
            "（请结合上述投票时的自由发言，说明他人因谁的指控或何种理由而怀疑了该玩家。一段话即可。）"
        )
        if false_belief_part and "他人认为的变色龙" in false_belief_part:
            false_q = vote_speeches_block + "\n\n" + false_belief_part
        self.tom_false_belief_answer = mole.ask_freeform(false_q)
        print(f"  错误信念回答: {self.tom_false_belief_answer[:200]}...")
        self._flush_logs()

    def run(self) -> None:
        print(f"模型: {self.model} | 谁是变色龙 | 多轮，结束条件：变色龙被投出（平民胜）或仅剩 1 变色龙+2 人（变色龙胜）")
        print("=" * 60)
        logs_dir = os.path.join(_SCRIPT_DIR, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self._log_path = os.path.join(
            logs_dir,
            f"chameleon_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        self.remaining_player_ids = {p.player_id for p in self.players}
        self.rounds = []
        self._flush_logs()

        round_no = 0
        while True:
            round_no += 1
            remaining = set(self.remaining_player_ids)
            if len(remaining) < 3:
                self.game_winner = "civilians"
                print(f"\n[游戏结束] 场上不足 3 人，判平民胜。")
                break

            print(f"\n========== 第 {round_no} 轮（在场: {sorted(remaining)}）==========")
            self.run_description_phase(remaining)
            self.run_voting_phase(remaining)

            eliminated = self._eliminate_by_vote(remaining)
            if not eliminated:
                eliminated = next(iter(remaining))
            new_remaining = remaining - {eliminated}
            self.remaining_player_ids = set(new_remaining)

            round_record = {
                "round_no": round_no,
                "descriptions": list(self.descriptions),
                "accused_id": self.accused_id,
                "accuser_id": self.accuser_id,
                "forced_accusation": self.forced_accusation,
                "votes": dict(self.votes),
                "vote_reasons": list(self.vote_reasons),
                "eliminated": eliminated,
                "remaining_after": list(new_remaining),
            }
            self.rounds.append(round_record)
            print(f"  本轮流放：{eliminated} | 剩余：{sorted(new_remaining)}")

            if eliminated == self.chameleon_id:
                self.game_winner = "civilians"
                print(f"\n[游戏结束] 变色龙 {eliminated} 被投出，平民胜。")
                break
            if len(new_remaining) == 3 and self.chameleon_id in new_remaining:
                self.game_winner = "chameleon"
                print(f"\n[游戏结束] 场上仅剩 1 变色龙与 2 人，变色龙胜。")
                break

            self._flush_logs()

        self.run_tom_phase()

        print("\n--- 结果（含错误信念推理、一阶社会认知通过由实验者手动判定） ---")
        print(f"  胜负: {self.game_winner}")
        print(f"  事实题回答: {self.tom_fact_answer}")
        print(f"  事实题是否正确（知悉变色龙）: {self.mole_knowledge_of_chameleon}")
        print(f"  线人实际投票: {self.actual_vote} | 被指控者: {self.accused_id} | 真变色龙: {self.chameleon_id}")
        print(f"  错误信念题回答: {self.tom_false_belief_answer}")
        if self._log_path:
            self._flush_logs()
            print(f"\n日志已保存: {self._log_path}")


def main() -> None:
    try:
        manager = ChameleonGameManager()
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
