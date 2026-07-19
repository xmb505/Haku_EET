"""
HMI 后端 —— aiohttp Web 服务

REST API   — /api/cars, /api/car/<id>/call, /api/hall_call, ...
WebSocket  — /ws  实时推送 car_state / hall_led / io_event

所有业务逻辑委托给 core.App，本模块只做 HTTP 路由 + JSON 序列化。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / 'example_web'

# 模块级 WS 客户端集合（所有广播共享）
_ws_clients: set = set()


# ============================================================
# 内部辅助
# ============================================================

def _car_to_dict(car, executor) -> dict[str, Any]:
    """Car 状态 → JSON-safe dict"""
    return {
        'id': car.car_id,
        'state': car.state.value if car.state else 'unknown',
        'position': car.position,
        'target_floor': car.target_floor,
        'direction': car.direction.value if car.direction else 'idle',
        'door_state': car.door_state.value if car.door_state else 'closed',
        'display': car.display,
        'fault': car.fault.any_active(),
        'weight_state': getattr(car, 'weight_state', 0),
        'driver_mode': getattr(car, 'driver_mode', False),
        'pending_calls': list(car._app.pending_calls.get(car.car_id, [])) if hasattr(car, '_app') else [],
    }


# ============================================================
# REST 路由
# ============================================================

async def handle_get_state(request: web.Request) -> web.Response:
    elevator_app = request.app['elevator_app']
    app = elevator_app
    config = app.config

    try:
        cars = [app.car_state_dict(cid) for cid in app.car_ids]

        return web.json_response({
            'cars': cars,
            'connected_cars': len(cars),
            'pending_hall_calls': [list(k) for k in app.pm._pending_hall_calls] if app.pm else [],
            'usermode': app.usermode_enabled,
            'algorithm': config.get('algorithm', {}).get('name', 'simple_internal_call'),
            'simulate': getattr(app, 'simulate', False),
            'init_direction': config.get('elevator', {}).get('initialization_direction', 'down'),
        })
    except Exception as e:
        logger.exception('handle_get_state failed')
        return web.json_response({'error': str(e), 'cars': []}, status=500)


async def handle_car_call(request: web.Request) -> web.Response:
    """POST /api/car/<id>/call  {"floor": N}"""
    app = request.app['elevator_app']
    car_id = int(request.match_info['car_id'])
    body = await request.json()
    floor = int(body['floor'])
    await app.call_internal(floor, car_id=car_id)
    return web.json_response({'ok': True})


async def handle_car_door(request: web.Request) -> web.Response:
    """POST /api/car/<id>/door/<open|close>"""
    app = request.app['elevator_app']
    car_id = int(request.match_info['car_id'])
    action = request.match_info['action']
    if action == 'open':
        await app.door_open(car_id)
    elif action == 'close':
        await app.door_close(car_id)
    else:
        raise web.HTTPBadRequest(text=f'unknown door action: {action}')
    return web.json_response({'ok': True})


async def handle_hall_call(request: web.Request) -> web.Response:
    """POST /api/hall_call  {"floor": N, "direction": "up"|"down"}"""
    app = request.app['elevator_app']
    body = await request.json()
    floor = int(body['floor'])
    direction = str(body['direction'])
    if direction not in ('up', 'down'):
        raise web.HTTPBadRequest(text=f'invalid direction: {direction}')
    await app.pm._on_hall_call_button(floor, direction, bit=1)
    return web.json_response({'ok': True})


async def handle_usermode(request: web.Request) -> web.Response:
    """POST /api/usermode  {"enabled": true|false}"""
    app = request.app['elevator_app']
    body = await request.json()
    enabled = bool(body['enabled'])
    await app.set_usermode(enabled)
    return web.json_response({'ok': True})


async def handle_reset(request: web.Request) -> web.Response:
    """POST /api/reset/<car_id>  {"direction": "up"|"down", "target_floor": N}"""
    app = request.app['elevator_app']
    car_id = int(request.match_info.get('car_id', 0))
    body = await request.json()
    direction = body.get('direction', 'down')
    target = body.get('target_floor')
    await app.reset(direction=direction, target_floor=target)
    return web.json_response({'ok': True})


async def handle_control(request: web.Request) -> web.Response:
    """POST /control  前端控制面板统一入口"""
    app = request.app['elevator_app']
    body = await request.json()
    cmd = body.get('command', '')
    action = body.get('action', '')
    car_id = body.get('elevator_id', 1)

    try:
        if cmd == 'car' and action == 'call':
            await app.call_internal(int(body.get('floor', 1)), car_id=int(car_id))
        elif cmd == 'car' and action == 'door':
            state = body.get('state', 'open')
            if state == 'open':
                await app.door_open(int(car_id))
            else:
                await app.door_close(int(car_id))
        elif cmd == 'car' and action == 'stop':
            await app.executors[int(car_id)].motor.hold_stop()
        elif cmd == 'car' and action == 'driver':
            on = bool(body.get('value', False))
            app.cars[int(car_id)].driver_mode = on
        elif cmd == 'system' and action == 'escape':
            # 火警模式 — 调用 console 的处理
            app.cron.cancel_all()
            for cid in app.car_ids:
                app.cars[cid].fault._active = True
            # TODO: 完整火警流程
        elif cmd == 'system' and action == 'status':
            pass  # 只返回 ok，状态在 /stattrak 里
        elif cmd == 'module' and action == 'usermode':
            await app.set_usermode(bool(body.get('value', False)))
        elif cmd == 'module' and action == 'station_seek':
            await app.set_station_seek(bool(body.get('value', False)))
        elif cmd == 'settings' and body.get('key') == 'slow_brake':
            val = int(body.get('value', 4))
            app.config.setdefault('elevator', {})['slow_brake'] = val
        else:
            return web.json_response({'ok': False, 'error': f'unknown command: {cmd}/{action}'})
        return web.json_response({'ok': True})
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)})


# ============================================================
# 静态文件
# ============================================================

async def _handle_index(request: web.Request) -> web.Response:
    """GET / → 返回 index.html（登录页）"""
    index_path = _STATIC_DIR / 'index.html'
    if index_path.is_file():
        return web.FileResponse(index_path)
    return web.Response(text='index.html not found', status=404)


# ============================================================
# WebSocket
# ============================================================

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)
    logger.info(f'WS client connected, total={len(_ws_clients)}')

    # 接入时立即推一次全量状态
    try:
        elevator_app = request.app['elevator_app']
        cars = {str(cid): elevator_app.car_state_dict(cid) for cid in elevator_app.car_ids}
        hall_leds = _collect_hall_leds(elevator_app)
        await ws.send_str(json.dumps({
            'event': 'init_state',
            'data': {
                'cars': cars,
                'hall_leds': hall_leds,
                'algorithm': elevator_app.config.get('algorithm', {}).get('name', 'simple_internal_call'),
                'simulate': getattr(elevator_app, 'simulate', False),
                'init_direction': elevator_app.config.get('elevator', {}).get('initialization_direction', 'down'),
                'usermode': elevator_app.usermode_enabled,
            },
        }, default=str))
    except Exception as e:
        logger.warning(f'init_state push failed: {e}')

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # 客户端命令: {"cmd": "ping"} → {"event": "pong"}
                try:
                    payload = json.loads(msg.data)
                    if payload.get('cmd') == 'ping':
                        await ws.send_str(json.dumps({'event': 'pong'}))
                except Exception:
                    pass
            elif msg.type == WSMsgType.ERROR:
                logger.warning(f'WS error: {ws.exception()}')
    finally:
        _ws_clients.discard(ws)
        logger.info(f'WS client disconnected, total={len(_ws_clients)}')
    return ws


def _collect_hall_leds(elevator_app) -> dict[str, dict[str, bool]]:
    """扫描当前所有外召指示灯状态"""
    leds: dict[str, dict[str, bool]] = {}
    state = getattr(elevator_app, '_hall_indicator_state', {})
    for (floor, direction), on in state.items():
        if on:
            leds.setdefault(str(floor), {})[direction] = True
    return leds


async def ws_broadcast(event: str, data: dict[str, Any]) -> None:
    """向所有已连接 WebSocket 客户端广播状态更新（模块级 _ws_clients）"""
    if not _ws_clients:
        return
    payload = json.dumps({'event': event, 'data': data, 'ts': int(asyncio.get_event_loop().time() * 1000)}, default=str)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


# ============================================================
# 应用工厂
# ============================================================

def create_app(elevator_app, port: int = 10010) -> web.Application:
    """
    创建 aiohttp Application。

    elevator_app: core.App 实例
    port: 监听端口
    """
    app = web.Application()

    # 依赖注入
    app['elevator_app'] = elevator_app
    app['port'] = port

    # 注册 hall_led observer:外召灯变化时主动推 WS
    async def _hall_led_observer(floor: int, direction: str, on: bool) -> None:
        await ws_broadcast('hall_led', {
            'floor': floor,
            'direction': direction,
            'on': on,
        })
    elevator_app._hall_light_observers.append(_hall_led_observer)

    # REST 路由
    app.router.add_get('/api/state', handle_get_state)
    app.router.add_get('/stattrak', handle_get_state)      # 前端兼容别名
    app.router.add_post('/api/car/{car_id}/call', handle_car_call)
    app.router.add_post('/api/car/{car_id}/door/{action}', handle_car_door)
    app.router.add_post('/api/hall_call', handle_hall_call)
    app.router.add_post('/api/usermode', handle_usermode)
    app.router.add_post('/api/reset/{car_id}', handle_reset)
    app.router.add_post('/api/reset', handle_reset)
    app.router.add_post('/control', handle_control)               # 前端控制面板统一入口

    # WebSocket
    app.router.add_get('/ws', handle_ws)

    # 静态文件（example_web/）
    if _STATIC_DIR.is_dir():
        # 根路径 → index.html（登录页），其余文件由静态路由托管
        app.router.add_get('/', _handle_index)
        app.router.add_static('/', _STATIC_DIR, show_index=False)
        logger.info(f'Web static files: {_STATIC_DIR}')
    else:
        logger.warning(f'Static dir not found: {_STATIC_DIR}')

    return app


async def start_web_server(elevator_app, port: int = 10010) -> web.AppRunner:
    """启动 web 服务，返回 runner（用于 shutdown）"""
    app = create_app(elevator_app, port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f'Web server started on http://0.0.0.0:{port}')
    return runner
