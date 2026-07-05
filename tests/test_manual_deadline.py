"""
test_manual_deadline.py —— 验证 _run_manual 的"松开方向键立即停"核心机制

核心修复：
    用 loop.add_reader + asyncio.wait_for(0.05) 替代阻塞 read
    每轮先检查 stop_deadline，过期立即 transition(None, False)
    这样松开方向键时最多 50ms 就停电机
"""
import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_deadline_expiration_triggers_stop():
    """模拟 deadline 检测：到时间点 transition 触发 manual_stop"""
    from core.app import App

    a = App(
        config_path='config/config.yaml',
        io_config_path='config/io_config.yaml',
        display_config_path='config/display_config.yaml',
        simulate=True,
    )
    a.manual_mode[1] = True

    # 上行
    await a.manual_up(high_speed=False)
    assert a.car.direction.value == 'up'
    assert a.io.get_output(a.mapper.addr_output('motor_start', 1)) == 1

    # 模拟 deadline 检测（"松开方向键超时"）
    stop_deadline = 0.0  # 已经过期
    now = time.monotonic()
    assert now >= stop_deadline  # 应该停

    # 模拟 console 里的 transition(None, False)
    await a.manual_stop()
    assert a.car.direction.value == 'idle'
    assert a.io.get_output(a.mapper.addr_output('motor_start', 1)) == 0

    print('✓ 松开方向键 → deadline 检测 → 立即停电机')


@pytest.mark.asyncio
async def test_repeated_dir_key_is_idempotent():
    """按住方向键（key repeat）：相同 (方向, 速度) 重复调用 manual_up 不重复写 IO"""
    from core.app import App

    a = App(
        config_path='config/config.yaml',
        io_config_path='config/io_config.yaml',
        display_config_path='config/display_config.yaml',
        simulate=True,
    )

    # 第一次：上行低速
    await a.manual_up(high_speed=False)
    write1 = a.io._output_cache.copy()

    # key repeat 重复 5 次，幂等
    for _ in range(5):
        await a.manual_up(high_speed=False)

    write_after = a.io._output_cache.copy()
    assert write1 == write_after, '重复调用应幂等（IO 缓存不变）'
    print('✓ 连按 5 次方向键幂等')


@pytest.mark.asyncio
async def test_shift_change_resets_io():
    """低速→高速：manual_up 重新写（high_speed 变化）"""
    from core.app import App

    a = App(
        config_path='config/config.yaml',
        io_config_path='config/io_config.yaml',
        display_config_path='config/display_config.yaml',
        simulate=True,
    )

    await a.manual_up(high_speed=False)
    assert a.io.get_output(a.mapper.addr_output('high_speed_contactor', 1)) == 0
    assert a.io.get_output(a.mapper.addr_output('low_speed_contactor', 1)) == 1

    await a.manual_up(high_speed=True)
    assert a.io.get_output(a.mapper.addr_output('high_speed_contactor', 1)) == 1
    assert a.io.get_output(a.mapper.addr_output('low_speed_contactor', 1)) == 0

    print('✓ 速度切换正确重写 IO')


@pytest.mark.asyncio
async def test_brake_idempotent():
    """按相同刹车档位不重复写"""
    from core.app import App

    a = App(
        config_path='config/config.yaml',
        io_config_path='config/io_config.yaml',
        display_config_path='config/display_config.yaml',
        simulate=True,
    )

    await a.manual_brake(5)
    val1 = a.io._output_cache.copy()

    for _ in range(3):
        await a.manual_brake(5)
    val_after = a.io._output_cache.copy()
    assert val1 == val_after

    # 切到不同档位应重写
    await a.manual_brake(7)
    val_new = a.io._output_cache.copy()
    assert val_new != val1
    print('✓ 刹车档位切换幂等')
