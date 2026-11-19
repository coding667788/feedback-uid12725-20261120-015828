"""命令行入口：import-batch / seal / review / report / verify / generate。

所有结果以 JSON 打印到 stdout（UTF-8、不转义中文），错误打印到 stderr 并以非零码退出。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import service
from .storage import Storage

# Windows 控制台默认可能是 cp936/cp1252；统一用 UTF-8 输出，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _print_json(obj) -> None:
    sys.stdout.write(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    )


def _read_payload(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"输入文件不存在: {path}")
    # 输入数据视为不可信数据，仅做 JSON 解析
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _open_store(args) -> Storage:
    return Storage(args.db)


def cmd_import_batch(args) -> int:
    payload = _read_payload(args.file)
    store = _open_store(args)
    try:
        result = service.import_batch(store, payload)
    finally:
        store.close()
    _print_json(result)
    return 0


def cmd_seal(args) -> int:
    store = _open_store(args)
    try:
        result = service.seal(store)
    finally:
        store.close()
    _print_json(result)
    return 0


def cmd_review(args) -> int:
    store = _open_store(args)
    try:
        result = service.review_sign(
            store,
            event_id=args.event_id,
            person=args.person,
            sign_minute=args.minute,
            expected_summary_hash=args.summary_hash,
        )
    finally:
        store.close()
    _print_json(result)
    return 0


def cmd_report(args) -> int:
    store = _open_store(args)
    try:
        result = service.build_report(store)
    finally:
        store.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


def cmd_verify(args) -> int:
    store = _open_store(args)
    try:
        result = service.verify(store)
    finally:
        store.close()
    _print_json(result)
    return 0 if result["ok"] else 1


def cmd_generate(args) -> int:
    # 延迟导入，生成器不属于核心裁定路径
    from generator.synthetic import build_sample_payload, write_sample_bundle

    if args.out_dir:
        paths = write_sample_bundle(args.out_dir)
        _print_json({"generated": paths})
    else:
        _print_json(build_sample_payload())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        description="本地值守裁定器：合成告警关联、封存与双人复核期限（纯虚构数据）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("import-batch", help="导入批次/排班 JSON 文件（幂等、可重放）")
    p_imp.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    p_imp.add_argument("--file", required=True, help="输入 JSON 文件路径")
    p_imp.set_defaults(func=cmd_import_batch)

    p_seal = sub.add_parser("seal", help="封存所有越过安全水位的连通分量")
    p_seal.add_argument("--db", required=True)
    p_seal.set_defaults(func=cmd_seal)

    p_rev = sub.add_parser("review", help="对封存事件提交一次签收")
    p_rev.add_argument("--db", required=True)
    p_rev.add_argument("--event-id", required=True)
    p_rev.add_argument("--person", required=True)
    p_rev.add_argument("--minute", required=True, type=int, help="签发整数分钟")
    p_rev.add_argument(
        "--summary-hash",
        default=None,
        help="可选：调用方持有的事件摘要哈希，用于绑定核对",
    )
    p_rev.set_defaults(func=cmd_review)

    p_rep = sub.add_parser("report", help="输出全量状态 JSON 报告")
    p_rep.add_argument("--db", required=True)
    p_rep.add_argument("--out", default=None, help="可选：同时写入文件")
    p_rep.set_defaults(func=cmd_report)

    p_ver = sub.add_parser("verify", help="独立复核数据库状态，发现问题退出码为 1")
    p_ver.add_argument("--db", required=True)
    p_ver.set_defaults(func=cmd_verify)

    p_gen = sub.add_parser("generate", help="生成虚构演示样本（批次+排班+操作指引）")
    p_gen.add_argument(
        "--out-dir",
        default=None,
        help="可选：把样本写成目录下的多个 JSON 文件；缺省打印到 stdout",
    )
    p_gen.set_defaults(func=cmd_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
