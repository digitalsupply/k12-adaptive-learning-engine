#!/usr/bin/env python3
"""自适应学习引擎 CLI。

用法示例：
  python cli.py groups                          # 列出所有学段×科目
  python cli.py search 函数 --band 初中          # 搜知识点
  python cli.py kp <kp_id> [--student SID]      # 知识点详情
  python cli.py new-student 小明 --model bkt     # 建学生档案
  python cli.py students                        # 列出学生
  python cli.py quiz SID <kp_id> --correct      # 记录一次作答
  python cli.py simulate SID --subject 数学 --band 初中 --n 30  # 模拟练习
  python cli.py diagnose SID                    # 错题归因
  python cli.py plan SID <goal_kp_id>           # 学习路径
  python cli.py next SID [--goal <kp_id>]       # 推荐下一步
  python cli.py reviews SID                     # 复习队列
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ale.engine import AdaptiveEngine

eng = AdaptiveEngine()


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _pad(text: str, width: int) -> str:
    """按显示宽度补齐（中文算 2 列），避免依赖全角空格做填充字符。"""
    def width_of(s: str) -> int:
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)
    out = text
    while width_of(out) < width:
        out += " "
    return out


def cmd_groups(_a):
    for g in eng.dataset.list_groups():
        print(f"{_pad(g['band'], 6)} {_pad(g['subject'], 8)}"
              f"  知识点 {g['kp_count']:>5}  关系 {g['relation_count']:>5}")


def cmd_search(a):
    hits = eng.dataset.search(a.keyword, band=a.band, subject=a.subject, limit=a.limit)
    if not hits:
        print("无结果")
        return
    for kp in hits:
        print(f"{kp.id}  [{kp.grade_band}/{kp.subject}] {kp.name}  "
              f"(难度 {kp.difficulty:.2f}, {kp.kp_type_label})")


def cmd_kp(a):
    d = eng.kp_detail(a.kp_id, sid=a.student)
    print(f"【{d['name']}】  {d['grade_band']}/{d['subject']}  {d['grades']}")
    print(f"  ID: {d['kp_id']}   类型: {d['kp_type_label']}   难度: {d['difficulty']:.2f}   置信度: {d['confidence']}")
    if d.get("mastery"):
        m = d["mastery"]
        print(f"  掌握度: {_fmt_pct(m['p'])}  作答 {m['attempts']} 次 (对 {m['correct']})  {'✅已掌握' if m['mastered'] else '⏳未掌握'}")
    if d["learning_objectives"]:
        print("  学习目标:")
        for o in d["learning_objectives"]:
            print(f"    · {o}")
    if d["misconceptions"]:
        print("  常见误区:")
        for m_ in d["misconceptions"]:
            print(f"    ⚠ {m_}")
    print("  依赖链(→该知识点): " + " ← ".join(d["chain_to_root"]))
    if d["dependents"]:
        print("  被依赖: " + "、".join(d["dependents"][:8]) + ("…" if len(d["dependents"]) > 8 else ""))


def cmd_new_student(a):
    stu = eng.create_student(a.name, model=a.model)
    print(f"已创建学生：{stu.name}  id={stu.id}  模型={stu.model}")


def cmd_students(_a):
    for s in eng.store.list_all():
        print(f"{s['id']}  {s['name']}  模型={s['model']}  作答={s['attempt_count']}")


def cmd_quiz(a):
    r = eng.record_attempt(a.sid, a.kp_id, correct=a.correct)
    print(f"{'✅ 答对' if r.correct else '❌ 答错'}  {r.kp_name}")
    print(f"掌握度 {_fmt_pct(r.p_before)} → {_fmt_pct(r.p_after)}"
          f"{'  🎉 已掌握！' if r.mastered else ''}")
    if not r.correct:
        d = eng.diagnose(a.sid, [r.kp_id])
        if d.root_causes:
            print("归因：")
            for c in d.root_causes[:2]:
                print(f"  ⚡ {c.kp.name}（掌握 {_fmt_pct(c.p_mastery)}）{c.reason}")


def cmd_simulate(a):
    groups = [(a.band, a.subject)] if a.band and a.subject else \
        [(g["band"], g["subject"]) for g in eng.dataset.list_groups()
         if g["subject"] == a.subject] if a.subject else \
        [(g["band"], g["subject"]) for g in eng.dataset.list_groups() if g["band"] == a.band]
    if not groups:
        print("没有匹配的科目分组", file=sys.stderr)
        sys.exit(1)
    band, subject = groups[0]
    kps, _ = eng.dataset.group(band, subject)
    import random
    rng = random.Random(a.seed)
    picks = rng.sample(sorted(kps), min(a.n, len(kps)))
    results = eng.simulate_learner(a.sid, picks, ability=a.ability, noise=a.noise,
                                   seed=a.seed)
    ok = sum(1 for r in results if r.correct)
    mastered = sum(1 for r in results if r.mastered)
    print(f"模拟完成：{band}/{subject}  共 {len(results)} 题，正确 {ok}，新掌握 {mastered}")


def cmd_diagnose(a):
    d = eng.diagnose_recent_failures(a.sid) if not a.symptoms else eng.diagnose(a.sid, a.symptoms)
    if not d.symptom_ids:
        print("没有可归因的错题记录（先用 quiz / simulate 产生一些作答）")
        return
    print(f"症状 {len(d.symptom_ids)} 个 → 根因 {len(d.root_causes)} 个")
    for i, c in enumerate(d.root_causes, 1):
        print(f"\n{i}. {c.kp.name}  [{c.kp.grade_band}/{c.kp.subject}]  "
              f"掌握 {_fmt_pct(c.p_mastery)}  得分 {c.score:.2f}")
        print(f"   {c.reason}")
        for e in c.explained[:2]:
            print(f"   ↳ 解释 {e.symptom_name}：{' ← '.join(e.path_names)}")


def cmd_plan(a):
    p = eng.make_plan(a.sid, a.goal_id)
    d = p.to_dict()
    print(f"目标：{d['goal']['name']}（{d['goal']['grade_band']}/{d['goal']['subject']}）")
    print(f"路径 {d['total_nodes']} 个知识点，约 {d['total_minutes']} 分钟，"
          f"跳过已掌握 {d['skipped_mastered']} 个"
          f"{'（已截断）' if d['truncated'] else ''}")
    for st in d["stages"]:
        print(f"\n— 第 {st['stage'] + 1} 阶段 —")
        for n in st["nodes"]:
            print(f"  · {n['name']}  [{n['grades']}]  掌握 {n['p_mastery']:.0%}  "
                  f"~{n['est_minutes']}min   {n['reason']}")


def cmd_next(a):
    recs = eng.recommend(a.sid, goal_id=a.goal)
    if not recs:
        print("没有推荐（可能都已掌握或先修未满足）")
        return
    print("推荐下一步：")
    for i, r in enumerate(recs, 1):
        print(f"{i}. {r.kp.name}  [{r.kp.grade_band}/{r.kp.subject}]  "
              f"掌握 {_fmt_pct(r.p_mastery)}  分 {r.score:.2f}")
        print(f"   {r.rationale}")


def cmd_reviews(a):
    items = eng.due_reviews(a.sid)
    if not items:
        print("没有到期复习项")
        return
    print(f"到期复习 {len(items)} 项：")
    for r in items:
        kp = eng.dataset.get_kp(r.kp_id)
        print(f"  · {kp.name}  间隔 {r.interval_days:.0f} 天  第 {r.reps} 次复习")


def cmd_prebuild_index(_a):
    """预热派生索引（id→分组、名称→id），避免首次请求时全量扫描数据集。"""
    src = eng.dataset.rebuild_indexes()
    cache_dir = eng.config.cache_dir
    print(f"索引已重建（来源：{src}），缓存目录：{cache_dir}")
    for f in sorted(Path(cache_dir).glob("*.json")):
        print(f"  {f.name}  {f.stat().st_size / 1024:.0f} KB")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("groups").set_defaults(fn=cmd_groups)
    sub.add_parser("prebuild-index").set_defaults(fn=cmd_prebuild_index)

    p = sub.add_parser("search"); p.add_argument("keyword")
    p.add_argument("--band"); p.add_argument("--subject"); p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("kp"); p.add_argument("kp_id"); p.add_argument("--student")
    p.set_defaults(fn=cmd_kp)

    p = sub.add_parser("new-student"); p.add_argument("name")
    p.add_argument("--model", choices=["bkt", "elo"], default="bkt")
    p.set_defaults(fn=cmd_new_student)

    sub.add_parser("students").set_defaults(fn=cmd_students)

    p = sub.add_parser("quiz"); p.add_argument("sid"); p.add_argument("kp_id")
    p.add_argument("--correct", action=argparse.BooleanOptionalAction, default=True)
    p.set_defaults(fn=cmd_quiz)

    p = sub.add_parser("simulate"); p.add_argument("sid")
    p.add_argument("--band", default="初中"); p.add_argument("--subject", default="数学")
    p.add_argument("--n", type=int, default=30); p.add_argument("--seed", type=int, default=42)
    p.add_argument("--ability", type=float, default=0.6)
    p.add_argument("--noise", type=float, default=0.15)
    p.set_defaults(fn=cmd_simulate)

    p = sub.add_parser("diagnose"); p.add_argument("sid")
    p.add_argument("--symptoms", nargs="*")
    p.set_defaults(fn=cmd_diagnose)

    p = sub.add_parser("plan"); p.add_argument("sid"); p.add_argument("goal_id")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("next"); p.add_argument("sid"); p.add_argument("--goal")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("reviews"); p.add_argument("sid")
    p.set_defaults(fn=cmd_reviews)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
