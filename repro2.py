import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.app import App


async def main():
    app = App(
        config_path=Path('config/config.yaml'),
        io_config_path=Path('config/io_config.yaml'),
        display_config_path=Path('config/display_config.yaml'),
        simulate=True,
    )
    await app.start()

    await app.reset(direction='down', target_floor=1, car_id=1)
    await asyncio.sleep(0.5)
    print(f"after init: pos={app.cars[1].position} door={app.cars[1].door_state.value}")

    await app.set_usermode(True, cars=[1])

    print('=== hall call L1 up ===')
    db = app.mapper.addr_input('hall_call_up_1', 0)
    i_addr = app.mapper.db_to_i(db)
    app.io.simulate_input(i_addr, 1)
    await asyncio.sleep(0.3)
    app.io.simulate_input(i_addr, 0)
    await asyncio.sleep(0.3)
    print(f"  door={app.cars[1].door_state.value} pending={app.pending_calls[1]} target={app.cars[1].target_floor}")

    print('=== cabin button L10 ===')
    db = app.mapper.addr_input('cabin_button_10', 1)
    i_addr = app.mapper.db_to_i(db)
    app.io.simulate_input(i_addr, 1)
    await asyncio.sleep(0.3)
    app.io.simulate_input(i_addr, 0)
    await asyncio.sleep(0.3)
    print(f"  door={app.cars[1].door_state.value} pending={app.pending_calls[1]} target={app.cars[1].target_floor}")

    print('=== wait 3s for close cron ===')
    await asyncio.sleep(3)
    print(f"  pos={app.cars[1].position} door={app.cars[1].door_state.value} pending={app.pending_calls[1]} target={app.cars[1].target_floor}")

    await app.stop()


if __name__ == '__main__':
    asyncio.run(main())
