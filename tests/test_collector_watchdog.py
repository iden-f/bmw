from datetime import datetime, timedelta, timezone

from collector import cycle, watchdog


def test_problem_needs_enough_passes():
    assert watchdog.problem({"empty_passes": 2}) == ""
    assert "no cars" in watchdog.problem({"empty_passes": 6})
    assert "failed" in watchdog.problem({"failed_passes": 3, "last_error": {"error": "x"}})


def test_due_respects_cooldown_and_daily_cap():
    now = datetime.now(timezone.utc)
    assert watchdog.due({}, now)
    assert not watchdog.due({"runs": [(now - timedelta(hours=1)).isoformat()]}, now)
    assert watchdog.due({"runs": [(now - timedelta(hours=4)).isoformat()]}, now)
    full = [(now - timedelta(hours=h)).isoformat() for h in (4, 8, 12)]
    assert not watchdog.due({"runs": full}, now)


def test_empty_count_and_backoff():
    memory: dict = {}
    cycle._count_empty(memory, [{"listings": []}], "ok")
    cycle._count_empty(memory, [{"listings": []}], "ok")
    assert memory["empty_passes"] == 2
    assert cycle.backoff(300, memory) == 1200
    cycle._count_empty(memory, [{"listings": [{}]}], "ok")
    assert "empty_passes" not in memory and cycle.backoff(300, memory) == 300


def test_lighter_reads_once_per_query_and_scrolls_less():
    from collector.settings import Settings
    plan = [{"queries": [{"query": "a", "order": 1}, {"query": "a", "order": 2},
                         {"query": "b", "order": 1}, {"query": "b", "order": 2}]}]
    s = Settings(scrolls=4)
    assert cycle._lighter(s, plan, {}) == (s, plan)
    s2, p2 = cycle._lighter(s, plan, {"empty_passes": 1})
    assert s2.scrolls == 1 and [q["query"] for q in p2[0]["queries"]] == ["a", "b"]


def test_one_order_setting_without_an_empty_pass():
    from collector.settings import Settings
    plan = [{"queries": [{"query": "a", "order": 1}, {"query": "a", "order": 2}]}]
    s = Settings(scrolls=2, both_orders=False)
    s2, p2 = cycle._lighter(s, plan, {})
    assert s2.scrolls == 2 and len(p2[0]["queries"]) == 1
