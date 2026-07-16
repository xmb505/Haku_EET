import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.app import App
from core.player import DoorState

CONFIG_PATH = Path(__file__).resolve().parent / 'config' / 'config.yaml'
IO_CONFIG_PATH = Path(__file__).resolve().parent / 'config' / 'io_config.yaml'
DISPLAY_PATH = Path(__file__).resolve().parent / 'config' / 'display_config.yaml'


async def main():
    app = App(
        config_path=CONFIG_PATH,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    await app.start()

    # init car1 at L1
    await app.reset(direction='down', target_floor=1, car_id=1)
    await asyncio.sleep(0.5)
    print(f"After init: pos={app.cars[1].position}, state={app.cars[1].state.value}, door={app.cars[1].door_state.value}")

    # enable usermode
    await app.set_usermode(True, cars=[1])
    print(f"After usermode: door={app.cars[1].door_state.value}")

    # simulate hall call L1 up
    print("=== Hall call L1 up ===")
    db = app.mapper.addr_input('hall_call_up_1', 0)
    i_addr = app.mapper.db_to_i(db)
    app.io.simulate_input(i_addr, 1)
    await asyncio.sleep(0.2)
    print(f"After hall call bit=1: pos={app.cars[1].position}, door={app.cars[1].door_state.value}, target={app.cars[1].target_floor}")
    app.io.simulate_input(i_addr, 0)
    await asyncio.sleep(0.2)

    # simulate cabin button L10
    print("=== Cabin button L10 ===")
    db = app.mapper.addr_input('cabin_button_10', 1)
    i_addr = app.mapper.db_to_i(db)
    app.io.simulate_input(i_addr, 1)
    await asyncio.sleep(0.2)
    print(f"After cabin button bit=1: pos={app.cars[1].position}, door={app.cars[1].door_state.value}, target={app.cars[1].target_floor}, pending={app.pending_calls[1]}")
    app.io.simulate_input(i_addr, 0)
    await asyncio.sleep(0.2)

    # wait for close cron to fire and door to close
    print("=== Wait for close cron ===")
    await asyncio.sleep(2)
    print(f"After wait: pos={app.cars[1].position}, door={app.cars[1].door_state.value}, target={app.cars[1].target_floor}, pending={app.pending_calls[1]}")

    # wait more
    await asyncio.sleep(2)
    print(f"After 2 more seconds: pos={app.cars[1].position}, door={app.cars[1].door_state.value}, target={app.cars[1].target_floor}, pending={app.pending_calls[1]}")

    await app.stop()


if __name__ == '__main__':
    asyncio.run(main())
