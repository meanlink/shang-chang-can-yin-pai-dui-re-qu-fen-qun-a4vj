"""接续派单编排逻辑的本地单元测试（无第三方依赖，stub 掉 fastapi/pymysql/redis）。

运行：PYTHONPATH=/workspace/backend python3 tests/test_continuation.py
"""
import sys
import types
from datetime import datetime

# ---- 依赖 stub：仅为在无 pip 环境导入 src 模块 ----
fastapi = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


fastapi.HTTPException = HTTPException
sys.modules['fastapi'] = fastapi


class IntegrityError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.args = (code, 'stub integrity error')


pymysql = types.ModuleType('pymysql')
pymysql.IntegrityError = IntegrityError
pymysql.cursors = types.ModuleType('pymysql.cursors')
pymysql.cursors.DictCursor = object
sys.modules['pymysql'] = pymysql
sys.modules['pymysql.cursors'] = pymysql.cursors
redis_mod = types.ModuleType('redis')
redis_mod.Redis = lambda *a, **k: types.SimpleNamespace()
sys.modules['redis'] = redis_mod

from src import continuation  # noqa: E402


class FakeCursor:
    """按 SQL 特征分发到内存表，复刻 continuation.py 用到的查询语义。"""

    def __init__(self, db):
        self.db = db
        self.lastrowid = None
        self._result = None

    def execute(self, sql, params=None):
        params = params or ()
        self._result = self._dispatch(sql, params)

    def executemany(self, sql, seq):  # pragma: no cover - 接续模块未使用
        for params in seq:
            self.execute(sql, params)

    def fetchone(self):
        row = self._result.pop(0) if self._result else None
        return row

    def fetchall(self):
        rows, self._result = self._result, []
        return rows

    def _hour(self, hour_id):
        return next(h for h in self.db['floor_hours'] if h['id'] == hour_id)

    def _hotspot(self, hotspot_id):
        return next(h for h in self.db['hotspots'] if h['id'] == hotspot_id)

    def _dispatch(self, sql, p):
        d = self.db
        if sql.startswith('SELECT f.id AS hour_id'):
            hotspot = self._hotspot(p[0])
            hour = self._hour(hotspot['hour_id'])
            return [dict(hour_id=hour['id'], floor=hour['floor'],
                         hour_start=hour['hour_start'], status=hour['status'])]
        if sql.startswith('SELECT id,status,hour_start FROM floor_hours'):
            return [dict(h) for h in d['floor_hours']
                    if h['floor'] == p[0] and h['hour_start'] == p[1]]
        if 'COUNT(*) AS total FROM hotspots' in sql:
            return [{'total': sum(1 for h in d['hotspots']
                                  if h['hour_id'] == p[0] and h['status'] == 'proposed')}]
        if sql.startswith('SELECT store_id FROM analysis_points'):
            return [{'store_id': ap['store_id']} for ap in d['analysis_points']
                    if ap['hotspot_id'] == p[0]]
        if sql.startswith('SELECT DISTINCT t.id AS task_id'):
            return self._candidates(p)
        if sql.startswith('SELECT h.id,h.status,h.zone_number,s.staging_point'):
            hotspot = self._hotspot(p[0])
            store = next(s for s in d['stores'] if s['id'] == hotspot['anchor_store_id'])
            return [{'id': hotspot['id'], 'status': hotspot['status'],
                     'zone_number': hotspot['zone_number'], 'staging_point': store['staging_point']}]
        if sql.startswith('SELECT task_id FROM hotspot_task_links'):
            return [{'task_id': lk['task_id']} for lk in d['hotspot_task_links']
                    if lk['hotspot_id'] == p[0]]
        if sql.startswith('INSERT INTO dispatch_tasks'):
            new_id = max((t['id'] for t in d['dispatch_tasks']), default=0) + 1
            d['dispatch_tasks'].append({'id': new_id, 'hotspot_id': p[0], 'staging_point': p[1],
                                        'instruction': p[2], 'status': 'assigned', 'handled_by': None})
            self.lastrowid = new_id
            return []
        if sql.startswith('INSERT INTO hotspot_task_links'):
            hour_id, hotspot_id, task_id, origin, linked_by = p
            if any(lk['hotspot_id'] == hotspot_id for lk in d['hotspot_task_links']):
                raise IntegrityError(1062)
            # 复刻 uq_link_task_per_hour：同一张单同小时只接一片热区。
            if any(lk['task_id'] == task_id and lk['hour_id'] == hour_id
                   for lk in d['hotspot_task_links']):
                raise IntegrityError(1062)
            d['hotspot_task_links'].append({'id': len(d['hotspot_task_links']) + 1,
                                            'hour_id': hour_id, 'hotspot_id': hotspot_id,
                                            'task_id': task_id, 'origin': origin, 'linked_by': linked_by})
            self.lastrowid = len(d['hotspot_task_links'])
            return []
        if sql.startswith('UPDATE hotspots SET status'):
            hotspot = self._hotspot(p[1])
            hotspot['status'] = 'confirmed'
            hotspot['confirmed_by'] = p[0]
            return []
        if sql.startswith('INSERT INTO audit_events'):
            d['audit_events'].append({'actor_id': p[0], 'action': p[1], 'target': str(p[2])})
            return []
        raise AssertionError(f'未覆盖的 SQL: {sql}')  # pragma: no cover

    def _candidates(self, p):
        floor, prev_start, _, _, member_tuple, cur_floor, cur_start = p
        prev_hour = next((h for h in self.db['floor_hours']
                          if h['floor'] == floor and h['hour_start'] == prev_start), None)
        cur_hour = next((h for h in self.db['floor_hours']
                         if h['floor'] == cur_floor and h['hour_start'] == cur_start), None)
        if not prev_hour:
            return []
        current_task_ids = {
            lk['task_id'] for lk in self.db['hotspot_task_links']
            if self._hotspot(lk['hotspot_id'])['hour_id'] == cur_hour['id']
        }
        rows = []
        for task in self.db['dispatch_tasks']:
            if task['status'] not in ('assigned', 'active') or task['id'] in current_task_ids:
                continue
            for lk in self.db['hotspot_task_links']:
                if lk['task_id'] != task['id']:
                    continue
                hotspot = self._hotspot(lk['hotspot_id'])
                if hotspot['hour_id'] != prev_hour['id']:
                    continue
                shared = {ap['store_id'] for ap in self.db['analysis_points']
                          if ap['hotspot_id'] == hotspot['id']} & set(member_tuple)
                if not shared:
                    continue
                anchor = next(s for s in self.db['stores'] if s['id'] == hotspot['anchor_store_id'])
                rows.append({'task_id': task['id'], 'status': task['status'],
                             'handled_by': task['handled_by'], 'zone_number': hotspot['zone_number'],
                             'anchor': anchor['name'], 'staging_point': anchor['staging_point'],
                             'hour_start': prev_hour['hour_start']})
        return sorted(rows, key=lambda r: (r['zone_number'], r['task_id']))


def build_db():
    h12 = datetime(2026, 11, 21, 12)
    h13 = datetime(2026, 11, 21, 13)
    return {
        'floor_hours': [
            {'id': 1, 'floor': 3, 'hour_start': h12, 'status': 'ready'},
            {'id': 2, 'floor': 3, 'hour_start': h13, 'status': 'ready'},
        ],
        'stores': [
            {'id': 302, 'name': '禾野煲仔', 'staging_point': '东廊 E2 黄线'},
            {'id': 301, 'name': '山岚汤面', 'staging_point': '东廊 E1 黄线'},
        ],
        'hotspots': [
            {'id': 10, 'hour_id': 1, 'zone_number': 1, 'anchor_store_id': 302,
             'status': 'confirmed', 'confirmed_by': 9},
            {'id': 20, 'hour_id': 2, 'zone_number': 1, 'anchor_store_id': 302,
             'status': 'proposed', 'confirmed_by': None},
            {'id': 21, 'hour_id': 2, 'zone_number': 2, 'anchor_store_id': 301,
             'status': 'proposed', 'confirmed_by': None},
        ],
        'analysis_points': [
            {'hour_id': 1, 'store_id': 301, 'hotspot_id': 10},
            {'hour_id': 1, 'store_id': 302, 'hotspot_id': 10},
            {'hour_id': 2, 'store_id': 301, 'hotspot_id': 20},
            {'hour_id': 2, 'store_id': 302, 'hotspot_id': 20},
            {'hour_id': 2, 'store_id': 301, 'hotspot_id': 21},
        ],
        'dispatch_tasks': [
            {'id': 100, 'hotspot_id': 10, 'status': 'active', 'handled_by': 7,
             'staging_point': '东廊 E2 黄线', 'instruction': continuation.INSTRUCTION},
        ],
        'hotspot_task_links': [
            {'id': 1, 'hour_id': 1, 'hotspot_id': 10, 'task_id': 100, 'origin': 1, 'linked_by': 9},
        ],
        'audit_events': [],
    }


OPERATOR = {'id': 9, 'role': 'operator'}


def test_blocked_waits_for_previous_confirmation():
    db = build_db()
    # 上小时回退为仍有 proposed 热区
    db['hotspots'][0]['status'] = 'proposed'
    cur = FakeCursor(db)
    try:
        continuation.confirm_zone(cur, 20, OPERATOR)
    except HTTPException as exc:
        assert exc.status_code == 409 and '上一小时' in exc.detail
    else:  # pragma: no cover
        raise AssertionError('上小时未确认完成时应阻断派单')
    assert len(db['dispatch_tasks']) == 1, '阻断时不得新派单'
    assert not any(lk['hotspot_id'] == 20 for lk in db['hotspot_task_links'])
    print('ok 上小时封存未确认完成 -> 阻断本小时派单')


def test_continue_open_task_keeps_same_ticket():
    db = build_db()
    cur = FakeCursor(db)
    members = continuation.hotspot_members(cur, 20)
    candidates = continuation.continuation_candidates(
        cur, 3, datetime(2026, 11, 21, 13), members)
    assert [r['task_id'] for r in candidates] == [100], '共同门店未结案单应成为候选'
    result = continuation.confirm_zone(cur, 20, OPERATOR, continue_task_id=100)
    assert result == {'task_id': 100, 'duplicate': False, 'continued': True}
    assert len(db['dispatch_tasks']) == 1, '接续不得新建疏导单'
    link = next(lk for lk in db['hotspot_task_links'] if lk['hotspot_id'] == 20)
    assert link['task_id'] == 100 and link['origin'] == 0
    assert db['hotspots'][1]['status'] == 'confirmed'
    assert any(e['action'] == 'task.continued' and e['target'] == '100' for e in db['audit_events'])
    print('ok 接续未结案单 -> 同编号、origin=0、写 task.continued 审计')


def test_no_candidate_dispatches_fresh_ticket():
    db = build_db()
    # 上小时单已结案，则无候选 -> 新派
    db['dispatch_tasks'][0]['status'] = 'closed'
    cur = FakeCursor(db)
    result = continuation.confirm_zone(cur, 20, OPERATOR)
    assert result['continued'] is False and result['duplicate'] is False
    assert result['task_id'] == 101
    link = next(lk for lk in db['hotspot_task_links'] if lk['hotspot_id'] == 20)
    assert link['origin'] == 1 and link['task_id'] == 101
    assert any(e['action'] == 'task.assigned' for e in db['audit_events'])
    print('ok 无单可接 -> 新派单并建 origin 关联')


def test_one_ticket_only_one_zone_per_hour():
    db = build_db()
    cur = FakeCursor(db)
    continuation.confirm_zone(cur, 20, OPERATOR, continue_task_id=100)
    # 同一张单接到第二片热区：候选已被 NOT EXISTS 排除，强制接续须被拒
    members_b = continuation.hotspot_members(cur, 21)
    candidates_b = continuation.continuation_candidates(
        cur, 3, datetime(2026, 11, 21, 13), members_b)
    assert all(r['task_id'] != 100 for r in candidates_b), '本小时已接走的单不得再次成为候选'
    try:
        continuation.confirm_zone(cur, 21, OPERATOR, continue_task_id=100)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:  # pragma: no cover
        raise AssertionError('同一张单本小时只允许接一片热区')
    print('ok 同一单本小时只接一片热区')


def test_duplicate_confirm_is_idempotent():
    db = build_db()
    cur = FakeCursor(db)
    continuation.confirm_zone(cur, 20, OPERATOR, continue_task_id=100)
    again = continuation.confirm_zone(cur, 20, OPERATOR, continue_task_id=100)
    assert again['duplicate'] is True and again['task_id'] == 100
    assert len([lk for lk in db['hotspot_task_links'] if lk['hotspot_id'] == 20]) == 1
    print('ok 确认超时重试 -> 幂等不重复派单')


def test_link_conflict_does_not_duplicate_ticket():
    db = build_db()
    cur = FakeCursor(db)
    # 预置热区 20 已有 link，模拟并发下唯一键冲突
    db['hotspot_task_links'].append(
        {'id': 5, 'hour_id': 2, 'hotspot_id': 20, 'task_id': 999, 'origin': 0, 'linked_by': 9})
    result = continuation.confirm_zone(cur, 20, OPERATOR)
    assert result['duplicate'] is True and result['task_id'] == 999
    assert len(db['dispatch_tasks']) == 1, '已接单热区不得再插新单'
    print('ok 热区已存在关联 -> 走幂等分支不重复派单')


def test_database_unique_blocks_same_ticket_two_zones():
    db = build_db()
    cur = FakeCursor(db)
    # 直接模拟应用层 NOT EXISTS 被并发绕过、库表 uq_link_task_per_hour 兜底
    continuation.link_hotspot(cur, hour_id=2, hotspot_id=20, task_id=100, actor=9, origin=False)
    try:
        continuation.link_hotspot(cur, hour_id=2, hotspot_id=21, task_id=100, actor=9, origin=False)
    except HTTPException as exc:
        assert exc.status_code == 409 and '不能重复派单' in exc.detail
    else:  # pragma: no cover
        raise AssertionError('同单同小时接到第二片热区必须被唯一约束拦截')
    assert len([lk for lk in db['hotspot_task_links'] if lk['task_id'] == 100 and lk['hour_id'] == 2]) == 1
    print('ok 库表唯一约束 -> 同单同小时接两片热区被拦截')


def test_invalid_candidate_rejected():
    db = build_db()
    cur = FakeCursor(db)
    try:
        continuation.confirm_zone(cur, 20, OPERATOR, continue_task_id=999)
    except HTTPException as exc:
        assert exc.status_code == 409 and '不可接续' in exc.detail
    else:  # pragma: no cover
        raise AssertionError('非候选单必须拒绝')
    assert len(db['dispatch_tasks']) == 1
    print('ok 非候选（非同层/无共同门店/已结案）单号 -> 拒绝')


if __name__ == '__main__':
    test_blocked_waits_for_previous_confirmation()
    test_continue_open_task_keeps_same_ticket()
    test_no_candidate_dispatches_fresh_ticket()
    test_one_ticket_only_one_zone_per_hour()
    test_duplicate_confirm_is_idempotent()
    test_link_conflict_does_not_duplicate_ticket()
    test_database_unique_blocks_same_ticket_two_zones()
    test_invalid_candidate_rejected()
    print('\n全部接续规则测试通过')
