import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pymysql import IntegrityError, OperationalError

from scripts.seed_demo import seed

from . import continuation
from .contracts import Clearance, FloorHourInput, ObservationInput, Role, SignIn, StoreInput, ZoneConfirm
from .extension_routes import router as extension_router
from .peak_routes import router as peak_router
from .security import authenticate, login, permit
from .shift import confirm_zone, move_task, queue_shift, read_map, record_window
from .storage import audit, cache, transaction

logger = logging.getLogger('uvicorn.error')


@asynccontextmanager
async def lifespan(_app):
    migrations = Path(__file__).resolve().parents[1] / 'migrations'
    with transaction() as cur:
        for migration in sorted(migrations.glob('0*.sql')):
            for statement in migration.read_text(encoding='utf-8').split(';'):
                if statement.strip():
                    cur.execute(statement)
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        try:
            seed()
        except Exception:
            logger.exception('Demo seed failed')
    yield


app = FastAPI(title='长河广场 · 餐饮排队热区', lifespan=lifespan)
app.include_router(extension_router)
app.include_router(peak_router)


@app.middleware('http')
async def request_trace(request, call_next):
    request_id = uuid4().hex[:12]
    started = monotonic()
    response = await call_next(request)
    response.headers['X-Request-ID'] = request_id
    if request.method != 'GET' or response.status_code >= 400:
        logger.info('%s %s %s %s %.0fms', request_id, request.method, request.url.path,
                    response.status_code, (monotonic() - started) * 1000)
    return response


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.exception_handler(OperationalError)
async def database_unavailable(_request, error):
    logger.warning('Database operation failed: %s', error.args[0])
    return JSONResponse(status_code=503, content={'detail': '数据服务暂时不可用，可稍后重试'})


@app.get('/health')
def health():
    with transaction() as cur:
        cur.execute('SELECT 1')
    cache.ping()
    return {'status': 'ok'}


@app.post('/auth/login')
def sign_in(data: SignIn):
    with transaction() as cur:
        result = login(data.username, data.password, cur)
        audit(cur, result['user']['id'], 'auth.login', data.username)
    return result


@app.get('/stores')
def get_stores(floor: int = Query(ge=1, le=4), _user=Depends(authenticate)):
    with transaction() as cur:
        cur.execute('SELECT * FROM stores WHERE floor=%s ORDER BY code', (floor,))
        return cur.fetchall()


@app.post('/stores', status_code=201)
def create_store(data: StoreInput, user=Depends(permit(Role.OPERATOR))):
    with transaction() as cur:
        try:
            cur.execute('INSERT INTO stores(code,name,floor,x,y,staging_point) VALUES (%s,%s,%s,%s,%s,%s)',
                        (data.code, data.name, data.floor, data.x, data.y, data.staging_point))
        except IntegrityError as error:
            if error.args[0] != 1062:
                raise
            raise HTTPException(409, '门店编号或楼层坐标已登记') from None
        store_id = cur.lastrowid
        audit(cur, user['id'], 'store.created', store_id)
    return {'id': store_id}


@app.get('/observations')
def get_windows(query: Annotated[FloorHourInput, Query()], _user=Depends(authenticate)):
    with transaction() as cur:
        cur.execute('SELECT * FROM floor_hours WHERE floor=%s AND hour_start=%s', (query.floor, query.hour_start))
        hour = cur.fetchone()
        cur.execute('SELECT id,code,name FROM stores WHERE floor=%s ORDER BY code', (query.floor,))
        stores = cur.fetchall()
        observations = []
        if hour:
            cur.execute('SELECT o.*,u.username FROM observations o JOIN users u ON u.id=o.recorded_by '
                        'WHERE hour_id=%s ORDER BY window_start', (hour['id'],))
            observations = cur.fetchall()
    return {'hour': hour, 'stores': stores, 'observations': observations}


@app.post('/observations')
def create_window(data: ObservationInput, user=Depends(permit(Role.OBSERVER))):
    with transaction() as cur:
        return record_window(cur, data, user)


@app.post('/analyses', status_code=202)
def run_analysis(data: FloorHourInput, user=Depends(permit(Role.OPERATOR))):
    with transaction() as cur:
        return queue_shift(cur, data, user)


@app.get('/analyses/{hour_id}')
def get_analysis(hour_id: int, _user=Depends(authenticate)):
    with transaction() as cur:
        return read_map(cur, hour_id)


@app.post('/hotspots/{hotspot_id}/confirm')
def approve_hotspot(hotspot_id: int, data: ZoneConfirm | None = None, user=Depends(permit(Role.OPERATOR))):
    continue_task_id = data.continue_task_id if data else None
    with transaction() as cur:
        return confirm_zone(cur, hotspot_id, user, continue_task_id)


@app.get('/hotspots/{hotspot_id}/continuation')
def get_continuation(hotspot_id: int, _user=Depends(authenticate)):
    with transaction() as cur:
        context = continuation.hour_context(cur, hotspot_id)
        if not context:
            raise HTTPException(404, '热区不存在')
        reason = continuation.continuation_blocked(cur, context['floor'], context['hour_start'])
        members = continuation.hotspot_members(cur, hotspot_id)
        candidates = continuation.continuation_candidates(
            cur, context['floor'], context['hour_start'], members)
        return {'blocked_reason': reason, 'candidates': candidates}


@app.get('/tasks')
def get_tasks(floor: int = Query(ge=1, le=4), _user=Depends(authenticate)):
    with transaction() as cur:
        # 锚点固定取该单首次下发的原热区（origin=1），只保留在本楼层出现过的单。
        cur.execute('SELECT t.*, sa.name AS anchor, u.username AS handler '
                    'FROM dispatch_tasks t '
                    'JOIN hotspot_task_links lo ON lo.task_id=t.id AND lo.origin=1 '
                    'JOIN hotspots ho ON ho.id=lo.hotspot_id '
                    'JOIN stores sa ON sa.id=ho.anchor_store_id '
                    'LEFT JOIN users u ON u.id=t.handled_by '
                    'WHERE EXISTS (SELECT 1 FROM hotspot_task_links lf JOIN hotspots hf ON hf.id=lf.hotspot_id '
                    'JOIN floor_hours ff ON ff.id=hf.hotspot_id WHERE lf.task_id=t.id AND ff.floor=%s) '
                    'ORDER BY t.id DESC', (floor,))
        tasks = cur.fetchall()
        if not tasks:
            return []
        task_ids = [task['id'] for task in tasks]
        placeholders = ','.join(['%s'] * len(task_ids))
        # 真实 DATETIME 交给 FastAPI 序列化成 ISO，前端 lanes.js 的时段计算才能正常解析。
        cur.execute('SELECT lk.task_id, f.hour_start, h.zone_number '
                    'FROM hotspot_task_links lk JOIN hotspots h ON h.id=lk.hotspot_id '
                    'JOIN floor_hours f ON f.id=h.hour_id '
                    f'WHERE lk.task_id IN ({placeholders}) ORDER BY f.hour_start', task_ids)
        spans_by_task = {}
        for row in cur.fetchall():
            spans_by_task.setdefault(row['task_id'], []).append(
                {'hour_start': row['hour_start'], 'zone_number': row['zone_number']})
        for task in tasks:
            spans = spans_by_task.get(task['id'], [])
            task['spans'] = spans
            task['continued'] = len(spans) > 1
            task['hour_start'] = spans[0]['hour_start']
            task['zone_number'] = spans[-1]['zone_number']
        return tasks


@app.post('/tasks/{task_id}/claim')
def claim_task(task_id: int, user=Depends(permit(Role.STEWARD))):
    with transaction() as cur:
        return move_task(cur, task_id, user)


@app.post('/tasks/{task_id}/close')
def close_task(task_id: int, data: Clearance, user=Depends(permit(Role.STEWARD))):
    with transaction() as cur:
        return move_task(cur, task_id, user, data)
