"""
并行运行 4 个模型 × 3 个游戏，共 12 次。
- 4 个模型：从 .env 的 PARATERA_MODEL_LIST 读取（逗号分隔，取前 4 个）。
- 3 个游戏：猜数字、拍卖、谁是卧底（与 guess_number_game / auction_game / chameleon_game 一致）。
每次以子进程运行对应游戏，通过环境变量 PARATERA_MODEL 指定模型（与 paratera_common.get_model_for_game 一致，不修改游戏代码）。
用法：python run_all_models_games.py [--jobs N]
"""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# 与 paratera_common 一致：先加载 .env，再读模型列表
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))


def _get_models() -> list[str]:
    """从 .env 的 PARATERA_MODEL_LIST 取前 4 个模型；不足 4 个则全部使用。"""
    raw = os.getenv("PARATERA_MODEL_LIST", "").strip()
    if not raw:
        return [
            "DeepSeek-V3.2-Thinking",
            "Kimi-K2.5",
            "GLM-5",
            "Qwen3-235B-A22B-Thinking-2507",
        ]
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models[:4] if len(models) >= 4 else models


# 与三个游戏入口一致
GAMES = [
    ("guess_number", "guess_number_game.py", "猜数字"),
    ("auction", "auction_game.py", "拍卖"),
    ("chameleon", "chameleon_game.py", "谁是卧底"),
]


def run_one(model: str, game_key: str, script_name: str, game_label: str) -> tuple[str, str, str, bool, str]:
    """
    在子进程中运行一次（某模型 + 某游戏）。
    子进程继承当前环境并设置 PARATERA_MODEL，paratera_common.get_model_for_game() 会用到该值。
    返回 (model, game_key, game_label, success, message)。
    """
    script_path = os.path.join(_SCRIPT_DIR, script_name)
    if not os.path.isfile(script_path):
        return (model, game_key, game_label, False, f"脚本不存在: {script_path}")
    env = os.environ.copy()
    env["PARATERA_MODEL"] = model
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=_SCRIPT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
            encoding="utf-8",
            errors="replace",
        )
        ok = result.returncode == 0
        msg = (result.stdout or "")[-2000:]
        if result.stderr:
            msg += "\n[stderr]\n" + (result.stderr or "")[-1000:]
        if not ok and msg.strip():
            msg = msg.strip()[-1500:]
        return (model, game_key, game_label, ok, msg)
    except subprocess.TimeoutExpired:
        return (model, game_key, game_label, False, "运行超时（3600s）")
    except Exception as e:
        return (model, game_key, game_label, False, str(e))


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="并行运行 4 模型 × 3 游戏，共 12 次（模型来自 .env PARATERA_MODEL_LIST）")
    parser.add_argument(
        "--jobs", "-j",
        type=int,
        default=12,
        help="并行任务数，默认 12；若 API 限流可改为 4 等",
    )
    args = parser.parse_args()

    models = _get_models()
    if len(models) < 4:
        print(f"警告：PARATERA_MODEL_LIST 仅解析出 {len(models)} 个模型，将运行 {len(models)}×3 = {len(models)*3} 次")

    tasks = []
    for model in models:
        for game_key, script_name, game_label in GAMES:
            tasks.append((model, game_key, script_name, game_label))

    print(f"模型（.env PARATERA_MODEL_LIST 前 4 个）：{models}")
    print(f"游戏：猜数字、拍卖、谁是卧底")
    print(f"共 {len(tasks)} 个任务，并行数 {args.jobs}")
    print("=" * 60)

    done = 0
    failed = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(run_one, model, gk, sn, gl): (model, gk, gl)
            for model, gk, sn, gl in tasks
        }
        for fut in as_completed(futures):
            model, game_key, game_label = futures[fut]
            try:
                _, _, label, success, msg = fut.result()
                done += 1
                status = "OK" if success else "FAIL"
                print(f"[{done}/{len(tasks)}] {model} | {label} | {status}")
                if not success and msg:
                    print(f"    {msg[:300]}{'…' if len(msg) > 300 else ''}")
                if not success:
                    failed.append((model, label))
            except Exception as e:
                done += 1
                print(f"[{done}/{len(tasks)}] {model} | {game_label} | EXCEPTION: {e}")
                failed.append((model, game_label))

    print("=" * 60)
    if failed:
        print(f"完成：{len(tasks) - len(failed)} 成功，{len(failed)} 失败")
        for m, g in failed:
            print(f"  - {m} | {g}")
    else:
        print(f"全部完成：{len(tasks)} 次运行成功。")


if __name__ == "__main__":
    main()
