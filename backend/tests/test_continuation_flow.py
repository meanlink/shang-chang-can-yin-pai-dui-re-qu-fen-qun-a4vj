"""端到端演练：两小时共享原单，认领/复测记录沿用，两小时均可查。

运行：PYTHONPATH=/workspace/backend python3 tests/test_continuation_flow.py
"""
import sys
import types
from datetime import datetime

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
        self.args = (code, 'stub')


pymysql = types.ModuleType('pymysql')
pymysql.IntegrityError = IntegrityError
pymysql.cursors = types.ModuleType('pymysql.cursors')
pymysql.cursors.DictCursor = object
sys.modules['pymysql'] = pymysql
sys.modules['pymysql.cursors'] = pymysql.cursors
redis_mod = types.ModuleType('redis')
redis_mod.Redis = lambda *a, **k: types.SimpleNamespace()
sys.modules['redis'] = redis_mod

# 复用单测里的假光标与建库
from tests.test_continuation import FakeCursor, build_db, OPERATOR  # noqa: E402
from src import continuation  # noqa: E402

db = build_db()
cur = FakeCursor(db)
h13 = datetime(2026, 11, 21, 13)

# 1) 13:00 热区 20 接续 12:00 的 #100（active，有共同门店）
res = continuation.confirm_zone(cur, 20, OPERATOR, continue_task_id=100)
assert res['task_id'] == 100 and res['continued'] is True

# 2) 两个小时都能查到原单 #100
def hours_of(task_id):
    return sorted({db['floor_hours'][[h['id'] for h in db['hotspots']].index(lk['hotspot_id'])]['hour_start']
                   for lk in db['hotspot_task_links'] if lk['task_id'] == task_id})

spans = hours_of(100)
assert spans == [datetime(2026, 11, 21, 12), h13], spans
print('ok 原单 #100 在 12:00 与 13:00 两个小时均可查到')

# 3) 沿用认领：#100 的 handled_by 仍是原认领人，未新建单、未重置状态
ticket = next(t for t in db['dispatch_tasks'] if t['id'] == 100)
assert ticket['handled_by'] == 7 and ticket['status'] == 'active'
assert len(db['dispatch_tasks']) == 1
print('ok 接续沿用原认领人与处理中状态，未新建单')

# 4) 模拟结案（直接改状态，等价于 move_task close 通过校验后的写入）
ticket['status'] = 'closed'
ticket['cleared_queue'] = 4

# 5) 14:00 新小时。先按规则把 13:00 剩余的待确认热区 21 收尾，否则 14:00 派单会被封存等待拦下
cur = FakeCursor(db)
left = continuation.confirm_zone(cur, 21, OPERATOR)  # 13:00 已无未结案单 -> 新派
assert left['continued'] is False and left['task_id'] == 101
db['floor_hours'].append({'id': 3, 'floor': 3, 'hour_start': datetime(2026, 11, 21, 14), 'status': 'ready'})
db['hotspots'].append({'id': 30, 'hour_id': 3, 'zone_number': 1, 'anchor_store_id': 302,
                       'status': 'proposed', 'confirmed_by': None})
db['analysis_points'].append({'hour_id': 3, 'store_id': 302, 'hotspot_id': 30})
cur = FakeCursor(db)
members = continuation.hotspot_members(cur, 30)
# 上小时（13:00）的单都已结案 -> 无候选
assert continuation.continuation_candidates(
    cur, 3, datetime(2026, 11, 21, 14), members) == []
fresh = continuation.confirm_zone(cur, 30, OPERATOR)
assert fresh['task_id'] == 102 and fresh['continued'] is False
print('ok 上小时单全部结案后，下一小时无单可接 -> 新派 #102')

print('\n端到端接续流程演练通过')
