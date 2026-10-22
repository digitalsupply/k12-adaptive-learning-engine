"""持久化与并发安全的回归测试。

覆盖此前会真正丢数据/报 500 的三个问题：
  1. 学生档案写到一半崩溃 → 留下半截 JSON → get() 抛 JSONDecodeError → API 500；
  2. 直接 write_text 覆盖写，中途崩溃会截断原档案；
  3. 并发「读-改-写」互相覆盖（多 worker 下丢掉后写的那次更新）。
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from ale.state import Attempt, Student, StudentStore


# ---------------- 原子写

def test_save_leaves_no_temp_file(tmp_path):
    """保存后不留临时文件，且目标文件一次成型。"""
    store = StudentStore(tmp_path)
    stu = store.create("原子写")
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / f"{stu.id}.json").exists()


def test_save_overwrite_keeps_file_valid(tmp_path):
    """覆盖写不会留下损坏文件（旧实现直接 write_text，崩溃即截断）。"""
    store = StudentStore(tmp_path)
    stu = store.create("覆盖写")
    for i in range(5):
        stu.attempts.append(Attempt(kp_id=f"k{i}", correct=True, ts=0.0))
        store.save(stu)
        # 每次保存后立刻能读回来，说明文件始终是完整 JSON
        assert store.get(stu.id).id == stu.id
    assert len(store.get(stu.id).attempts) == 5


def test_failed_save_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    """写临时文件失败时，目标文件保持原样，且不留半截临时文件。"""
    store = StudentStore(tmp_path)
    stu = store.create("失败保护")
    stu.name = "新名字"
    original = (tmp_path / f"{stu.id}.json").read_text(encoding="utf-8")

    def boom(*_a, **_kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(stu)

    # 原档案完好无损
    assert (tmp_path / f"{stu.id}.json").read_text(encoding="utf-8") == original
    assert json.loads(original)["name"] == "失败保护"
    # 没有残留临时文件
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------- 损坏档案

def test_get_corrupt_file_raises_keyerror(tmp_path):
    """半截 JSON 必须抛 KeyError（API 能回 404/提示），不能抛 JSONDecodeError。"""
    store = StudentStore(tmp_path)
    stu = store.create("损坏")
    (tmp_path / f"{stu.id}.json").write_text('{"id": "abc", "name": ', encoding="utf-8")
    with pytest.raises(KeyError) as ei:
        store.get(stu.id)
    assert "损坏" in str(ei.value)


def test_get_missing_file_raises_keyerror(tmp_path):
    store = StudentStore(tmp_path)
    with pytest.raises(KeyError):
        store.get("nonexistent")


def test_list_all_skips_corrupt_files(tmp_path):
    """一个坏档案不能把整个学生列表接口拖垮。"""
    store = StudentStore(tmp_path)
    good = store.create("好的")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    ids = [s["id"] for s in store.list_all()]
    assert good.id in ids
    assert "broken" not in ids


# ---------------- 并发：读-改-写

def test_mutate_preserves_concurrent_updates(tmp_path):
    """并发 mutate 不能丢更新：N 个线程各 +1，最终计数必须是 N。"""
    store = StudentStore(tmp_path)
    stu = store.create("并发")

    def worker():
        def bump(student: Student) -> None:
            student.attempts.append(Attempt(kp_id="x", correct=True, ts=0.0))
        store.mutate(stu.id, bump)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.get(stu.id).attempts) == 20


def test_mutate_persists_change(tmp_path):
    store = StudentStore(tmp_path)
    stu = store.create("变更")
    store.mutate(stu.id, lambda s: setattr(s, "name", "改名了"))
    assert store.get(stu.id).name == "改名了"


def test_mutate_returns_result_and_student(tmp_path):
    store = StudentStore(tmp_path)
    stu = store.create("返回值")

    def bump(student: Student) -> int:
        student.attempts.append(Attempt(kp_id="x", correct=True, ts=0.0))
        return len(student.attempts)

    result, student = store.mutate(stu.id, bump)
    assert result == 1
    assert student.id == stu.id
