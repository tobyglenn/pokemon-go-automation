#!/usr/bin/env python3
# Internal Android implementation; launch ../battle.py instead.
"""
AutoBattler — Automates friendly trainer battles in Pokémon GO on Android.

Uses the same AutoTraderConfig.yaml as trade.py (with the battle keys appended).
Every device needs BATTLE_BTN, USE_PARTY_BTN and REMATCH_BTN.
The device that gives up each battle also needs RUN_BTN and SURRENDER_BTN.

Rematch cycle:
  1. Tap REMATCH_BTN on both devices
  2. Tap USE_PARTY_BTN on both devices
  3. Tap RUN_BTN on the surrendering device
  4. Tap SURRENDER_BTN on the surrendering device
  5. Pause 3 seconds, back to 1.
"""

import asyncio
import time
from pathlib import Path

try:
    from ppadb.client_async import ClientAsync
    from ppadb.device_async import DeviceAsync
    import yaml
    from yaml.parser import ParserError
except ModuleNotFoundError as e:
    print(e)
    print('Run "pip install -r docs/requirements.txt" to install required packages.')
    exit(1)

CONFIG_FILE_DIR  = '/storage/self/primary/'
CONFIG_FILE_NAME = 'AutoTraderConfig.yaml'
TMP_FILE_PATH    = Path('tmp_battle.yaml')
CONFIG = dict[str, list[int]]

BATTLE_KEYS    = {'BATTLE_BTN', 'USE_PARTY_BTN', 'REMATCH_BTN'}
SURRENDER_KEYS = {'RUN_BTN', 'SURRENDER_BTN'}

# Delays (seconds). Waits for the game server get SLEEP_MODIFIER added.
PARTY_SCREEN_DELAY = 4     # after Rematch/Battle, until party selection screen
BATTLE_LOAD_DELAY  = 14     # after Use this party, until battle is running
RUN_MENU_DELAY     = 1  # after Run, until Surrender is shown
SURRENDER_DELAY    = 13     # after Surrender, until back on the rematch screen
SLEEP_MODIFIER     = 0


class DeviceAsyncWrapper(DeviceAsync):
    config: CONFIG


class AutoBattlerError(Exception):
    pass


async def tap(device: DeviceAsyncWrapper, point: list[int]):
    """Sends a tap at point to device."""
    x, y = point
    # Uses a tiny swipe over 100 ms for increased reliability
    # and visibility on the Pointer Location tool.
    await device.shell(f'input swipe {x} {y} {x+1} {y+1} 100')


async def pointer(devices: list[DeviceAsyncWrapper], on: bool):
    """Turns on/off pointer location setting on all `devices`."""
    for device in devices:
        try:
            await device.shell(f'settings put system pointer_location {int(on)}')
        except Exception:
            print(f'Failed to turn {"on" if on else "off"} pointer location on', device.serial)


def surrendering_devices(devices: list[DeviceAsyncWrapper]) -> list[DeviceAsyncWrapper]:
    return [dev for dev in devices if SURRENDER_KEYS <= set(dev.config.keys())]


async def wait(seconds: float, use_modifier: bool = True):
    await asyncio.sleep(max(seconds + (SLEEP_MODIFIER if use_modifier else 0), 0))


async def battle_sequence(devices: list[DeviceAsyncWrapper], start_key: str):
    """One battle: start_key (REMATCH_BTN or BATTLE_BTN) on both devices,
    Use this party on both, then Run + Surrender on the surrendering device."""
    print('    Sending', start_key)
    await asyncio.gather(*[tap(dev, dev.config[start_key]) for dev in devices])
    await wait(PARTY_SCREEN_DELAY)

    print('    Sending USE_PARTY_BTN')
    await asyncio.gather(*[tap(dev, dev.config['USE_PARTY_BTN']) for dev in devices])
    await wait(BATTLE_LOAD_DELAY)

    for dev in surrendering_devices(devices):
        print(f'    Sending RUN_BTN on {dev.serial}')
        await tap(dev, dev.config['RUN_BTN'])
        await wait(RUN_MENU_DELAY, use_modifier=False)
        print(f'    Sending SURRENDER_BTN on {dev.serial}')
        await tap(dev, dev.config['SURRENDER_BTN'])

    await wait(SURRENDER_DELAY)


async def battle_process(devices: list[DeviceAsyncWrapper], n: int, first_battle: bool = False):
    """Executes `n` battle sequences. The first one starts with BATTLE_BTN
    instead of REMATCH_BTN when `first_battle` is True."""
    if n < 1:
        return
    await pointer(devices, True)
    try:
        for i in range(1, n + 1):
            print(f'  Starting battle {i} of {n}')
            start_key = 'BATTLE_BTN' if first_battle and i == 1 else 'REMATCH_BTN'
            await battle_sequence(devices, start_key)
    finally:
        await pointer(devices, False)


async def get_config(device: DeviceAsyncWrapper) -> CONFIG:
    """Pulls config file from device and parses it. Sets the `config` attribute on success."""
    config_file_path = CONFIG_FILE_DIR + CONFIG_FILE_NAME
    await device.pull(config_file_path, TMP_FILE_PATH)
    content = TMP_FILE_PATH.read_text()
    TMP_FILE_PATH.unlink(missing_ok=True)
    if not content:
        raise AutoBattlerError(f'Found no config file at {config_file_path}')
    config: CONFIG = yaml.safe_load(content)
    assert isinstance(config, dict), 'Incorrect config file format (should be an object with keys)'
    if not BATTLE_KEYS <= set(config.keys()):
        raise AutoBattlerError(f'Missing config key(s): {BATTLE_KEYS - set(config.keys())}')
    for coords in config.values():
        assert isinstance(coords, list) and len(coords) == 2 and all(isinstance(i, int) for i in coords),\
            'Invalid coords format in config (should be list with two integers)'
    device.config = config
    return config


async def setup() -> list[DeviceAsyncWrapper]:
    """Checks for devices and loads config files from devices."""
    client = ClientAsync()
    devices: list[DeviceAsyncWrapper] = await client.devices()
    if len(devices) < 2:
        raise AutoBattlerError(f'Need at least 2 connected devices, found {len(devices)}')
    if len(devices) > 2:
        print(f'Warning: {len(devices)} devices found, using first two only.')
        devices = devices[:2]
    print('Found devices:')
    for device in devices:
        print(' ', device.serial)
    print()
    try:
        for device in devices:
            await get_config(device)
            print('Successfully loaded config from', device.serial)
    except (AutoBattlerError, AssertionError, ParserError) as e:
        raise AutoBattlerError(f'Failed to load config from {device.serial}', *e.args) from e
    surrenderers = surrendering_devices(devices)
    if not surrenderers:
        raise AutoBattlerError(
            'No device has RUN_BTN and SURRENDER_BTN in its config. '
            'One device must have them to give up each battle.')
    for dev in surrenderers:
        print(f'{dev.serial} will Run + Surrender each battle')
    return devices


def interface():
    """Runs the main loop asking for user input."""
    global SLEEP_MODIFIER
    print(
        '\n'
        ' ##                           ## \n'
        '##   AutoBattler by jonaro00   ##\n'
        ' ##                           ## \n'
    )
    devices: list[DeviceAsyncWrapper] = asyncio.run(setup())
    print("\nCommands:")
    print("  <number>     run that many rematches (start on the rematch screen)")
    print("  start        start the first battle with the Battle button (friend screen)")
    print("  delay <val>  get/set the extra delay modifier")
    print("  q            quit")
    while True:
        print()
        try:
            i = input("Number of rematches? ('q' to quit) > ").strip()
            il = i.lower()
            if il == 'q':
                break
            if il.startswith('delay'):
                args = i.split()
                if len(args) == 2:
                    SLEEP_MODIFIER = float(args[1])
                print('Current extra delay:', SLEEP_MODIFIER)
                continue
            if il == 'start':
                print('Starting first battle (Ctrl+C to cancel)...')
                try:
                    asyncio.run(battle_process(devices, 1, first_battle=True))
                except KeyboardInterrupt:
                    pass
                except Exception as e:
                    print(e)
                continue
            assert (n := int(i)) > 0
        except KeyboardInterrupt:
            print('\nDouble press interrupt to quit')
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                break
            continue
        except EOFError:
            break
        except (ValueError, AssertionError):
            print('Enter a positive integer')
            continue
        try:
            print(f'Starting {n} rematch(es) (Ctrl+C to cancel)...')
            asyncio.run(battle_process(devices, n))
        except KeyboardInterrupt:
            continue
        except Exception as e:
            print(e)


def main():
    try:
        interface()
    except AutoBattlerError as e:
        print('\n'.join(map(str, e.args)))
    except Exception as e:
        print('Unexpected error:', e.__class__.__name__, e.args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('battle', 'scripts/battle.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == '__main__':
    main()
