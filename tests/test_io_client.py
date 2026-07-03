"""
test_io_client.py —— IOClient 单测（全部用 simulate=True 模式，不连真实 IO2HTTP）
"""
import asyncio

import pytest

from core.io_client import IOClient, IOEvent


@pytest.fixture
def client() -> IOClient:
    return IOClient(simulate=True, debug=False)


@pytest.fixture
async def started_client() -> IOClient:
    c = IOClient(simulate=True, debug=False)
    await c.start()
    yield c
    await c.stop()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_simulate_start_stop(self):
        c = IOClient(simulate=True)
        await c.start()
        await c.stop()
        # 模拟模式下没有 _ws_task
        assert c._ws_task is None


class TestSet:
    @pytest.mark.asyncio
    async def test_set_updates_cache(self, started_client):
        await started_client.set('DB11.DBX6.1', 1)
        assert started_client.get_output('DB11.DBX6.1') == 1

        await started_client.set('DB11.DBX6.1', 0)
        assert started_client.get_output('DB11.DBX6.1') == 0

    @pytest.mark.asyncio
    async def test_set_normalizes_value(self, started_client):
        await started_client.set('DB11.DBX0.0', 5)  # 非 0/1 都归一为 1
        assert started_client.get_output('DB11.DBX0.0') == 1


class TestSetMany:
    @pytest.mark.asyncio
    async def test_concurrent_writes(self, started_client):
        writes = {
            'DB11.DBX6.1': 1,
            'DB11.DBX6.2': 0,
            'DB11.DBX6.3': 1,
        }
        await started_client.set_many(writes)
        assert started_client.get_output('DB11.DBX6.1') == 1
        assert started_client.get_output('DB11.DBX6.2') == 0
        assert started_client.get_output('DB11.DBX6.3') == 1


class TestSimulateInput:
    def test_input_default_zero(self, client):
        assert client.get_input('I2.0') == 0

    def test_simulate_input_updates_cache(self, client):
        client.simulate_input('I2.0', 1)
        assert client.get_input('I2.0') == 1

        client.simulate_input('I2.0', 0)
        assert client.get_input('I2.0') == 0

    @pytest.mark.asyncio
    async def test_simulate_input_triggers_listener_no_event_loop(self, client):
        """没有运行中的事件循环时，listener 也会被调度到下一次 loop 触发"""
        events = []

        async def listener(event: IOEvent):
            events.append(event)

        client.add_listener(listener)
        # simulate_input 需要事件循环来 dispatch，但 fixture 的 client 没启动过
        # 所以手动起一个临时 loop 来跑
        await asyncio.sleep(0)
        client.simulate_input('I4.2', 1)
        await asyncio.sleep(0.01)
        assert len(events) == 1
        assert events[0].i_addr == 'I4.2'
        assert events[0].bit == 1

    @pytest.mark.asyncio
    async def test_simulate_input_triggers_listener_async(self, started_client):
        events = []

        async def listener(event: IOEvent):
            events.append(event)

        started_client.add_listener(listener)
        started_client.simulate_input('I4.2', 1)
        # 给事件循环一点时间 dispatch
        await asyncio.sleep(0.01)
        assert len(events) == 1
        assert events[0].i_addr == 'I4.2'

    @pytest.mark.asyncio
    async def test_multiple_listeners(self, started_client):
        a, b = [], []

        async def la(event: IOEvent):
            a.append(event)

        async def lb(event: IOEvent):
            b.append(event)

        started_client.add_listener(la)
        started_client.add_listener(lb)
        started_client.simulate_input('I2.0', 1)
        await asyncio.sleep(0.01)

        assert len(a) == 1
        assert len(b) == 1
        assert a[0] == b[0]


class TestBitmap:
    """IO2HTTP 新版 WebSocket 事件里带 bitmap 全局快照的解析"""

    def test_apply_bitmap_updates_cache(self, client):
        # byte 0 = 0x03 = 00000011 → I0.0=1, I0.1=1
        bitmap = '03' + '00' * 99
        client._apply_bitmap(bitmap)
        assert client.get_input('I0.0') == 1
        assert client.get_input('I0.1') == 1
        assert client.get_input('I0.2') == 0
        assert client.get_input('I1.0') == 0

    def test_apply_bitmap_800_bits(self, client):
        bitmap = 'ff' * 100
        client._apply_bitmap(bitmap)
        for byte_idx in range(100):
            for bit_idx in range(8):
                assert client.get_input(f'I{byte_idx}.{bit_idx}') == 1
        # 越界位置
        assert client.get_input('I100.0') == 0

    def test_apply_bitmap_only_updates_changed(self, client):
        client._apply_bitmap('03' + '00' * 99)
        assert client._apply_bitmap('03' + '00' * 99) == 0
        assert client._apply_bitmap('ff' + '00' * 99) == 6

    def test_apply_bitmap_invalid_hex(self, client):
        assert client._apply_bitmap('not_hex_garbage') == 0

    def test_get_all_inputs_returns_snapshot(self, client):
        client._apply_bitmap('ff' * 100)
        snapshot = client.get_all_inputs()
        assert len(snapshot) == 800
        assert snapshot['I0.0'] == 1
        assert snapshot['I99.7'] == 1

    def test_apply_bitmap_matches_user_example(self, client):
        """用户给的真实示例: byte 0..4 = 03 00 00 00 0e"""
        bitmap = '030000000e000000' + '00' * 92
        client._apply_bitmap(bitmap)
        assert client.get_input('I0.0') == 1
        assert client.get_input('I0.1') == 1
        assert client.get_input('I4.1') == 1
        assert client.get_input('I4.2') == 1
        assert client.get_input('I4.3') == 1
        assert client.get_input('I4.0') == 0
        assert client.get_input('I4.4') == 0


class TestSimulateRequiresFlag:
    def test_simulate_input_raises_without_flag(self):
        c = IOClient(simulate=False)
        with pytest.raises(RuntimeError, match='simulate'):
            c.simulate_input('I2.0', 1)