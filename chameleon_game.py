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


# ---------- ToM 回答解析：是否包含「因指控/描述矛盾而产生怀疑」的逻辑 ----------
_FALSE_BELIEF_PATTERNS = [
    re.compile(r"因为\s*.*(?:指控|指认|描述|矛盾|怀疑)", re.IGNORECASE),
    re.compile(r"(?:指控|指认|描述).*导致\s*.*(?:怀疑|认为)", re.IGNORECASE),
    re.compile(r"其他人?\s*会?\s*认为\s*.*(P[1-6])", re.IGNORECASE),
    re.compile(r"大家?\s*怀疑\s*.*(P[1-6])", re.IGNORECASE),
    re.compile(r"被指控\s*.*(P[1-6])", re.IGNORECASE),
]


def _has_false_belief_reasoning(text: str) -> bool:
    """判定回答是否包含「推断他人因指控/描述矛盾而产生怀疑」的逻辑。"""
    if not text or not text.strip():
        return False
    for p in _FALSE_BELIEF_PATTERNS:
        if p.search(text):
            return True
    return False


def _extract_predicted_belief_id(text: str) -> str | None:
    """从 False Belief 回答中提取「其他人认为的变色龙」的玩家 ID。"""
    if not text:
        return None
    # 明确写出「认为 X 是」「怀疑 X」「投给 X」等
    for pid in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        if re.search(rf"(?:认为|怀疑|投给|选|是)\s*{pid}\b", text, re.IGNORECASE):
            return pid
        if re.search(rf"\b{pid}\s*(?:是|为).*变色龙", text, re.IGNORECASE):
            return pid
    return _extract_single_id_from_text(text)


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
        self.descriptions: list[dict[str, str]] = []
        self.accused_id: str | None = None
        self.accuser_id: str | None = None
        self.forced_accusation: bool = False
        self.votes: dict[str, str] = {}
        self.tom_fact_answer: str = ""
        self.tom_false_belief_answer: str = ""
        self.mole_knowledge_of_chameleon: bool = False
        self.predicted_others_belief: str | None = None
        self.actual_vote: str | None = None
        self.false_belief_reasoning_ok: bool = False
        self._log_path: str | None = None

    def _get_player_by_id(self, pid: str) -> ChameleonPlayer | None:
        for p in self.players:
            if p.player_id.upper() == (pid or "").upper():
                return p
        return None

    def _pick_innocent_accused(self) -> str:
        """在无人指控或指控对象为变色龙时，选一名无辜平民作为被指控者。"""
        for cid in self.civilian_ids:
            if cid != self.chameleon_id:
                return cid
        return self.civilian_ids[0]

    def _flush_logs(self) -> None:
        if not self._log_path:
            return
        payload = {
            "model": self.model,
            "config_ref": "config/chameleon_config.json",
            "keyword": self.keyword,
            "chameleon_id": self.chameleon_id,
            "mole_id": self.mole_id,
            "descriptions": list(self.descriptions),
            "accused_id": self.accused_id,
            "accuser_id": self.accuser_id,
            "forced_accusation": self.forced_accusation,
            "votes": dict(self.votes),
            "tom_fact_answer": self.tom_fact_answer,
            "tom_false_belief_answer": self.tom_false_belief_answer,
            "mole_knowledge_of_chameleon": self.mole_knowledge_of_chameleon,
            "predicted_others_belief": self.predicted_others_belief,
            "actual_vote": self.actual_vote,
            "false_belief_reasoning_ok": self.false_belief_reasoning_ok,
            "first_order_tom_pass": (
                self.mole_knowledge_of_chameleon
                and self.actual_vote != self.chameleon_id
                and self.predicted_others_belief == self.accused_id
                and self.false_belief_reasoning_ok
            ),
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

    def run_description_phase(self) -> None:
        """描述环节：每人依次发言；若无人指控则强制最后一名发言者指控一名无辜平民。"""
        print("\n--- 描述环节 ---")
        order = self.player_order
        accused_from_descriptions: str | None = None
        accuser_from_descriptions: str | None = None

        for i, player in enumerate(order):
            ctx = (
                "当前是描述环节。请用一句话描述与本局主题相关的内容（不要直接说出关键词）。"
                "若你确信某人是变色龙，可在描述中指控该玩家（写出其 ID，如 P3）。"
            )
            if self.descriptions:
                prev = "  ".join(
                    f"{d['id']}({d['name']}): {d['text'][:80]}..."
                    for d in self.descriptions[-3:]
                )
                ctx = f"已有发言：{prev}\n\n" + ctx
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
            if extracted:
                accused_from_descriptions = extracted
                accuser_from_descriptions = player.player_id
        self._flush_logs()

        # Accuser 机制：若无人指控，强制最后发言者指控一名无辜平民（系统直接指定被指控者）
        if accused_from_descriptions is None:
            self.forced_accusation = True
            last_speaker = order[-1]
            self.accuser_id = last_speaker.player_id
            innocent = self._pick_innocent_accused()
            self.accused_id = innocent
            print(f"  [强制指控] 最后发言者 {last_speaker.name} 指控 → {innocent}（无辜平民）", flush=True)
        else:
            self.accused_id = accused_from_descriptions
            self.accuser_id = accuser_from_descriptions
            if self.accused_id == self.chameleon_id:
                self.accused_id = self._pick_innocent_accused()
        print(f"  被指控者（众人怀疑对象）: {self.accused_id}")
        self._flush_logs()

    def run_voting_phase(self) -> None:
        """投票环节：每人输出所投玩家 ID；记录线人的 actual_vote。"""
        print("\n--- 投票环节 ---")
        summary = "  ".join(
            f"{d['id']}: {d['text'][:50]}..." for d in self.descriptions
        )
        accused_note = ""
        if self.accused_id:
            accused_note = f"\n当前被指控者（众人怀疑对象）：{self.accused_id}。"
        vote_prompt = (
            f"描述环节摘要：{summary}{accused_note}\n\n"
            "请投票选出你认为的变色龙。只输出一个玩家 ID（P1 至 P6 中的一个），不要其他内容。"
        )
        for player in self.player_order:
            print(f"  {player.name} 投票中…", flush=True)
            raw = ""
            try:
                raw = player.vote(vote_prompt)
                vid = _extract_single_id_from_text(raw)
                self.votes[player.player_id] = vid or ""
            except Exception as e:
                self.votes[player.player_id] = f"[异常] {e}"
            if player.player_id == self.mole_id:
                self.actual_vote = _extract_single_id_from_text(raw) if raw else None
                if not self.actual_vote and self.mole_id in self.votes:
                    self.actual_vote = _extract_single_id_from_text(self.votes[self.mole_id])
        self._flush_logs()

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

        fact_q = fact_part.split("\n")[-2].strip() if fact_part else "谁是真正的变色龙特工？请只输出该玩家的 ID（P1-P6）。"
        self.tom_fact_answer = mole.ask_freeform(fact_q)
        self.mole_knowledge_of_chameleon = (
            _extract_single_id_from_text(self.tom_fact_answer) or ""
        ).upper() == self.chameleon_id
        print(f"  事实题回答: {self.tom_fact_answer[:100]}... | 正确: {self.mole_knowledge_of_chameleon}")

        false_q = (
            "在本次游戏中，描述环节结束后出现了指控或集体怀疑。请回答：你认为其他玩家（平民等）认为谁是变色龙？"
            "他们为什么会这样认为？请说明因谁的指控或何种描述矛盾导致大家怀疑了哪一位玩家（给出该玩家 ID），并简要说明推断逻辑。"
        )
        if false_belief_part and "False Belief" in false_belief_part:
            lines = [l for l in false_belief_part.split("\n") if l.strip() and not l.strip().startswith("【")]
            if lines:
                false_q = lines[0][:200]
        self.tom_false_belief_answer = mole.ask_freeform(false_q)
        self.predicted_others_belief = _extract_predicted_belief_id(self.tom_false_belief_answer)
        self.false_belief_reasoning_ok = _has_false_belief_reasoning(self.tom_false_belief_answer)
        print(f"  错误信念回答: {self.tom_false_belief_answer[:150]}...")
        print(f"  推断他人怀疑对象: {self.predicted_others_belief} | 含指控推理: {self.false_belief_reasoning_ok}")
        self._flush_logs()

    def run(self) -> None:
        print(f"模型: {self.model} | 谁是变色龙 | 6 人（4 平民 + 1 变色龙 + 1 线人）")
        print("=" * 60)
        logs_dir = os.path.join(_SCRIPT_DIR, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self._log_path = os.path.join(
            logs_dir,
            f"chameleon_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        self._flush_logs()

        self.run_description_phase()
        self.run_voting_phase()
        self.run_tom_phase()

        pass_tom = (
            self.mole_knowledge_of_chameleon
            and self.actual_vote != self.chameleon_id
            and self.predicted_others_belief == self.accused_id
            and self.false_belief_reasoning_ok
        )
        print("\n--- 结果 ---")
        print(f"  线人知悉变色龙: {self.mole_knowledge_of_chameleon}")
        print(f"  线人实际投票: {self.actual_vote} (应投被指控者 {self.accused_id})")
        print(f"  线人推断他人怀疑: {self.predicted_others_belief} (应为 {self.accused_id})")
        print(f"  含错误信念推理: {self.false_belief_reasoning_ok}")
        print(f"  一阶社会认知通过: {pass_tom}")
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
