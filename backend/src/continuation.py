"""跨小时疏导单接续规则。

同层相邻两个小时的热区若有共同门店，本小时热区优先接续上小时尚未结案的疏导单；
没有可接的单才新派。上小时封存的分析与确认未完成前，本小时不得派单。热区拆分或
合并时由值班运营选择接续哪张单、接到哪片热区，同一张单在本小时只接一片热区。
"""

from datetime import timedelta

from fastapi import HTTPException

from .storage import audit

INSTRUCTION = '将队尾移至临时等候线，保持主通道畅通并复测队列'


def hour_context(cur, hotspot_id):
    """返回热区所在楼层小时，并加行锁，保证派单与封存顺序串行。"""
    cur.execute('SELECT f.id AS hour_id,f.floor,f.hour_start,f.status '
                'FROM hotspots h JOIN floor_hours f ON f.id=h.hour_id '
                'WHERE h.id=%s FOR UPDATE', (hotspot_id,))
    return cur.fetchone()


def previous_hour(cur, floor, hour_start):
    prev_start = hour_start - timedelta(hours=1)
    cur.execute('SELECT id,status,hour_start FROM floor_hours '
                'WHERE floor=%s AND hour_start=%s', (floor, prev_start))
    return cur.fetchone()


def continuation_blocked(cur, floor, hour_start):
    """上小时已封存但分析/确认未做完时，本小时先等待，返回等待原因。"""
    previous = previous_hour(cur, floor, hour_start)
    if not previous or previous['status'] == 'collecting':
        return None
    if previous['status'] == 'queued':
        return '上一小时已封存但热区分析尚未完成，请等分析结果出来后再派本小时的疏导单'
    if previous['status'] == 'failed':
        return '上一小时热区分析失败，请先重试分析并确认后再派本小时的疏导单'
    cur.execute("SELECT COUNT(*) AS total FROM hotspots WHERE hour_id=%s AND status='proposed'",
                (previous['id'],))
    pending = cur.fetchone()['total']
    if pending:
        return f'上一小时仍有 {pending} 个热区待确认，请完成后再派本小时的疏导单'
    return None


def continuation_candidates(cur, floor, hour_start, member_store_ids):
    """同层上小时、与本热区有共同门店且尚未结案、本小时尚未被接走的疏导单。"""
    if not member_store_ids:
        return []
    prev_start = hour_start - timedelta(hours=1)
    cur.execute('SELECT DISTINCT t.id AS task_id,t.status,t.handled_by,h.zone_number, '
                's.name AS anchor,s.staging_point,ph.hour_start '
                'FROM dispatch_tasks t '
                'JOIN hotspot_task_links l ON l.task_id=t.id '
                'JOIN hotspots h ON h.id=l.hotspot_id '
                'JOIN floor_hours ph ON ph.id=h.hour_id '
                'JOIN stores s ON s.id=h.anchor_store_id '
                'WHERE ph.floor=%s AND ph.hour_start=%s AND t.status IN (%s,%s) '
                'AND EXISTS (SELECT 1 FROM analysis_points p '
                '            WHERE p.hour_id=h.hour_id AND p.hotspot_id=h.id '
                '            AND p.store_id IN %s) '
                'AND NOT EXISTS (SELECT 1 FROM hotspot_task_links l2 '
                '                JOIN hotspots h2 ON h2.id=l2.hotspot_id '
                'JOIN floor_hours fh2 ON fh2.id=h2.hour_id '
                '                WHERE l2.task_id=t.id AND fh2.floor=%s AND fh2.hour_start=%s) '
                'ORDER BY h.zone_number,t.id',
                (floor, prev_start, 'assigned', 'active', tuple(member_store_ids), floor, hour_start))
    return cur.fetchall()


def hotspot_members(cur, hotspot_id):
    cur.execute('SELECT store_id FROM analysis_points WHERE hotspot_id=%s ORDER BY store_id', (hotspot_id,))
    return [row['store_id'] for row in cur.fetchall()]


def validate_candidate(cur, task_id, floor, hour_start, member_store_ids):
    candidates = continuation_candidates(cur, floor, hour_start, member_store_ids)
    if not any(row['task_id'] == task_id for row in candidates):
        raise HTTPException(409, '该疏导单不可接续：须为上小时同层、与本热区有共同门店且未结案的单')
    return next(row for row in candidates if row['task_id'] == task_id)


def link_hotspot(cur, hour_id, hotspot_id, task_id, actor, origin):
    try:
        cur.execute('INSERT INTO hotspot_task_links(hour_id,hotspot_id,task_id,origin,linked_by) '
                    'VALUES (%s,%s,%s,%s,%s)', (hour_id, hotspot_id, task_id, 1 if origin else 0, actor))
    except Exception as error:
        # 唯一键冲突：热区已接单，或该单本小时已接到另一片热区。
        if error.args and error.args[0] == 1062:
            raise HTTPException(409, '该热区或疏导单在本小时已建立接续，不能重复派单') from None
        raise


def confirm_zone(cur, hotspot_id, user, continue_task_id=None):
    cur.execute('SELECT h.id,h.status,h.zone_number,s.staging_point FROM hotspots h '
                'JOIN stores s ON s.id=h.anchor_store_id WHERE h.id=%s FOR UPDATE', (hotspot_id,))
    hotspot = cur.fetchone()
    if not hotspot:
        raise HTTPException(404, '热区不存在')

    cur.execute('SELECT task_id FROM hotspot_task_links WHERE hotspot_id=%s', (hotspot_id,))
    existing = cur.fetchone()
    if existing:
        audit(cur, user['id'], 'hotspot.confirm.duplicate', hotspot_id)
        return {'task_id': existing['task_id'], 'duplicate': True, 'continued': False}

    context = hour_context(cur, hotspot_id)
    reason = continuation_blocked(cur, context['floor'], context['hour_start'])
    if reason:
        raise HTTPException(409, reason)

    members = hotspot_members(cur, hotspot_id)
    continued = False
    if continue_task_id is not None:
        chosen = validate_candidate(cur, continue_task_id, context['floor'], context['hour_start'], members)
        task_id = chosen['task_id']
        continued = True
    else:
        cur.execute('INSERT INTO dispatch_tasks(hotspot_id,staging_point,instruction) VALUES (%s,%s,%s)',
                    (hotspot_id, hotspot['staging_point'], INSTRUCTION))
        task_id = cur.lastrowid
        audit(cur, user['id'], 'task.assigned', task_id)

    cur.execute("UPDATE hotspots SET status='confirmed',confirmed_by=%s WHERE id=%s", (user['id'], hotspot_id))
    link_hotspot(cur, context['hour_id'], hotspot_id, task_id, user['id'], origin=not continued)
    audit(cur, user['id'], 'hotspot.confirmed', hotspot_id)
    if continued:
        audit(cur, user['id'], 'task.continued', task_id)
    return {'task_id': task_id, 'duplicate': False, 'continued': continued}
