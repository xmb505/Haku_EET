"""
test_cron.py —— Cron 模块单元测试
"""
import asyncio
import time

import pytest

from core.cron import Cron, CronJob, EventRule


def _make_action(results: list, name: str):
    """创建记录触发的回调"""
    async def action():
        results.append(name)
    return action


@pytest.mark.asyncio
async def test_schedule_and_fire():
    """调度 job → 按时触发 → 自动移除"""
    cron = Cron()
    results: list[str] = []
    await cron.schedule(CronJob(
        name='test',
        trigger_time=time.monotonic() + 0.02,
        action=_make_action(results, 'fired'),
    ))
    await cron.start()
    await asyncio.sleep(0.05)
    await cron.stop()

    assert results == ['fired']


@pytest.mark.asyncio
async def test_cancel_before_fire():
    """取消 job 后不触发"""
    cron = Cron()
    results: list[str] = []
    await cron.schedule(CronJob(
        name='test',
        trigger_time=time.monotonic() + 0.05,  # 50ms
        action=_make_action(results, 'fired'),
    ))
    await cron.start()
    await cron.cancel('test')
    await asyncio.sleep(0.08)
    await cron.stop()

    assert results == []


@pytest.mark.asyncio
async def test_multiple_jobs_fifo():
    """多个 job 按时间先后触发"""
    cron = Cron()
    results: list[str] = []
    await cron.schedule(CronJob(
        name='later',
        trigger_time=time.monotonic() + 0.04,
        action=_make_action(results, 'later'),
    ))
    await cron.schedule(CronJob(
        name='earlier',
        trigger_time=time.monotonic() + 0.02,
        action=_make_action(results, 'earlier'),
    ))
    await cron.start()
    await asyncio.sleep(0.08)
    await cron.stop()

    assert results == ['earlier', 'later']


@pytest.mark.asyncio
async def test_reschedule_by_io_event():
    """IO 事件触发 reschedule → 推迟触发时间"""
    cron = Cron()
    results: list[str] = []
    trigger_time = time.monotonic() + 0.03
    delay = 0.04  # reschedule delay
    job = CronJob(
        name='reschedulable',
        trigger_time=trigger_time,
        action=_make_action(results, 'fired'),
        delay=delay,
        event_rules=[EventRule('test_signal', 1, 'reschedule', delay)],
    )
    await cron.schedule(job)
    await cron.start()

    # 在触发前 reschedule
    await cron._on_io_event(type('Event', (), {'bit': 1, 'i_addr': None})())
    # mock lookup_signal_by_i:
    cron._mapper = type('MockMapper', (), {
        'lookup_signal_by_i': lambda self, addr: (1, 'test_signal'),
    })()
    await cron._on_io_event(type('Event', (), {'bit': 1, 'i_addr': 'irrelevant'})())

    # 原来的 0.03 已过，但 reschedule 推到 now+0.04
    await asyncio.sleep(0.03)
    assert results == []  # 还没触发
    await asyncio.sleep(0.05)
    assert results == ['fired']

    await cron.stop()


@pytest.mark.asyncio
async def test_cancel_by_io_event():
    """IO 事件触发 cancel → 销毁 job"""
    cron = Cron()
    results: list[str] = []
    job = CronJob(
        name='cancellable',
        trigger_time=time.monotonic() + 0.05,
        action=_make_action(results, 'fired'),
        delay=10,
        event_rules=[EventRule('cancel_sig', 1, 'cancel')],
    )
    await cron.schedule(job)
    cron._mapper = type('MockMapper', (), {
        'lookup_signal_by_i': lambda self, addr: (1, 'cancel_sig'),
    })()
    await cron.start()

    # 触发 cancel
    await cron._on_io_event(type('Event', (), {'bit': 1, 'i_addr': 'x'})())

    await asyncio.sleep(0.08)
    assert results == []
    await cron.stop()


@pytest.mark.asyncio
async def test_reschedule_multiple_times():
    """多次重调度：每次推到 now + delay"""
    cron = Cron()
    results: list[str] = []
    delay = 0.03
    job = CronJob(
        name='multi_resched',
        trigger_time=time.monotonic() + 0.02,
        action=_make_action(results, 'fired'),
        delay=delay,
        event_rules=[EventRule('trigger', 1, 'reschedule', delay)],
    )
    await cron.schedule(job)
    cron._mapper = type('MockMapper', (), {
        'lookup_signal_by_i': lambda self, addr: (1, 'trigger'),
    })()
    await cron.start()

    # 两次 reschedule（模拟光幕反复触发）
    await cron._on_io_event(type('Event', (), {'bit': 1, 'i_addr': 'x'})())
    await asyncio.sleep(0.025)
    await cron._on_io_event(type('Event', (), {'bit': 1, 'i_addr': 'x'})())

    # 第一次调度 (0.02) 已过；第二次 (now+0.03) 还没
    await asyncio.sleep(0.02)
    assert results == []
    # 等第二次到期
    await asyncio.sleep(0.04)
    assert results == ['fired']

    await cron.stop()


@pytest.mark.asyncio
async def test_same_name_overwrites():
    """同名 job 重新 schedule → 旧 job 被取消"""
    cron = Cron()
    results: list[str] = []
    await cron.schedule(CronJob(
        name='dup',
        trigger_time=time.monotonic() + 0.04,
        action=_make_action(results, 'old'),
    ))
    await cron.schedule(CronJob(
        name='dup',
        trigger_time=time.monotonic() + 0.02,
        action=_make_action(results, 'new'),
    ))
    await cron.start()
    await asyncio.sleep(0.06)
    await cron.stop()

    # 只有新 job 触发
    assert results == ['new']
