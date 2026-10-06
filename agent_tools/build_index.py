# 用途：构建 / 增量更新 AetherBreath 的 RAG 索引
#       （结构化切分 → bge-m3 向量化 → ChromaDB；同步落一份 BM25 语料）
# 用法：python agent_tools/build_index.py
#         --rebuild            清空后全量重建
#         --budget 100         软预算 100 秒，到点存进度退出（下次调用自动续跑）
#         --dir <目录>         指定语料目录（默认取 config.yaml 的 rag.knowledge_dir）
#         --stats              只打印统计，不索引
# 说明：首行的「用途」注释是审批卡片上显示的 AB 自述内容，请勿挪动或删除。
import argparse
import json
import sys
import time
from pathlib import Path

# agent_tools/build_index.py → 项目根在（父）上一级
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_tools.rag import RAGIndex          # noqa: E402  （需先修正 sys.path）


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 AetherBreath RAG 索引")
    ap.add_argument("--dir", default=None, help="语料目录（默认取配置 rag.knowledge_dir）")
    ap.add_argument("--rebuild", action="store_true", help="清空集合后全量重建")
    ap.add_argument("--budget", type=float, default=None,
                    help="软预算（秒）：到点保存进度并退出，下次调用从断点续跑")
    ap.add_argument("--stats", action="store_true", help="只打印索引统计")
    args = ap.parse_args()

    idx = RAGIndex()
    if args.stats:
        print(json.dumps(idx.stats(), ensure_ascii=False, indent=2))
        return 0

    t0 = time.time()
    print(f"[START] 语料目录: {args.dir or idx.cfg['knowledge_dir']}", flush=True)
    report = idx.build(directory=args.dir, budget_seconds=args.budget, rebuild=args.rebuild)
    report["elapsed_s"] = round(time.time() - t0, 1)
    print("[REPORT] " + json.dumps(report, ensure_ascii=False), flush=True)
    if report.get("partial"):
        print("[NEXT] 预算到点，仍有未完成文档 —— 再次运行本脚本即可续跑", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
