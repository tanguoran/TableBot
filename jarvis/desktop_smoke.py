"""Live desktop acceptance: real MCP stdio -> broker -> Unity -> animation/navigation.

Requires WhaleDesk running in Play mode. Optional --model also asks the real Ollama Agent to move.
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from .desktop_client import DesktopMCPClient


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(model=False):
    client = DesktopMCPClient(config.DESKTOP_BRIDGE_URL)
    checks = []
    try:
        require(len(client.tool_schemas()) == 8, 'MCP must discover eight tools')
        deadline = time.monotonic() + 12
        while True:
            state = client.call('get_desktop_state', {})
            if 'error' not in state:
                break
            require(time.monotonic() < deadline, state.get('error'))
            time.sleep(.3)
        character = state['character']
        require(character['character_id'] == 'whale_girl' and 'animation_state' in character, 'Dedicated character state missing')
        require(all(o['object_id'] != character['character_id'] for o in state['objects']), 'Character must not be an object')
        require(len({o['object_id'] for o in state['objects']}) == len(state['objects']), 'Duplicate object IDs')
        files = any(o.get('object_type') == 'file' for o in state['objects'])
        test_type, test_tag = ('file', 'desktop_file') if files else ('computer', 'work')
        books = client.call('get_desktop_state', {'types': [test_type], 'tags': [test_tag]})
        require(books['objects'] and books['character']['character_id'] == 'whale_girl', 'Object filtering removed character')
        require(all(o['object_type'] == test_type for o in books['objects']), 'Object type filter failed')
        empty = client.call('get_desktop_state', {'types': ['missing-type']})
        require(empty['objects'] == [] and empty['character']['character_id'] == 'whale_girl', 'Empty filter lost character')
        checks.append('Live scene, stable object IDs, object filters, separate character state')
        print('PASS:', checks[-1], flush=True)

        initial = character['position']
        result = client.call('move_to', {'x': 1.33, 'z': -.47, 'heading_degrees': 90})
        require(result.get('state') == 'arrived', 'Coordinate movement failed: ' + str(result))
        require(abs((result['heading_degrees'] - 90 + 180) % 360 - 180) < 1, 'Arrival heading was not reached')
        checks.append('Coordinate movement arrived through actual Unity navigation')
        print('PASS:', checks[-1], flush=True)

        result = client.call('approach_object', {'object_id': 'laptop_01', 'side': 'nearest'})
        require(result.get('state') == 'arrived', 'Object approach failed: ' + str(result))
        checks.append('Approached laptop at a reachable standing position')
        print('PASS:', checks[-1], flush=True)

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(client.call, 'move_to', {'x': 1.33, 'z': -.47})
            deadline = time.monotonic() + 4
            while True:
                current = client.call('get_desktop_state', {})['character']
                if current['movement_state'] == 'moving' and current['action_id']:
                    break
                require(time.monotonic() < deadline and not future.done(), 'Movement never started for cancellation test')
                time.sleep(.1)
            stopped = client.call('cancel_move', {})
            require(stopped.get('state') == 'succeeded', 'Cancel not acknowledged')
            require(future.result(5).get('state') == 'cancelled', 'Interrupted move did not report cancellation')
        checks.append('Concurrent MCP cancellation interrupted an active movement')
        print('PASS:', checks[-1], flush=True)

        projected = client.call('move_to', {'x': 100, 'z': 100})
        require(projected.get('state') == 'arrived' and projected.get('destination_adjusted'),
                'Off-desk target did not arrive at a reachable fallback: ' + str(projected))
        result = client.call('move_to', {'x': initial['x'], 'z': initial['z']})
        require(result.get('state') == 'arrived', 'Return to initial position failed')
        checks.append('Projected off-desk movement to a reachable desktop position and returned to start')
        print('PASS:', checks[-1], flush=True)

        for heading in (0, 180, -90):
            here = client.call('get_desktop_state', {})['character']['position']
            result = client.call('move_to', {'x': here['x'], 'z': here['z'], 'heading_degrees': heading})
            require(result.get('state') == 'arrived' and abs((result['heading_degrees'] - heading + 180) % 360 - 180) < 1,
                    'Final heading failed: ' + str(result))
        result = client.call('sit_down', {'heading_degrees': 180})
        require(result.get('state') == 'succeeded' and result.get('posture') == 'sitting', 'Sitting failed: ' + str(result))
        time.sleep(.5)
        seated = client.call('get_desktop_state', {})['character']
        require(seated['posture'] == 'sitting' and seated['action_phase'] == 'seated', 'Sitting did not persist')
        result = client.call('observe', {'object_id': 'laptop_01', 'duration_seconds': 1})
        require(result.get('state') == 'succeeded' and result.get('posture') == 'sitting', 'Seated observation failed')
        result = client.call('stand_up', {})
        require(result.get('state') == 'succeeded' and result.get('posture') == 'standing', 'Stand-up failed')
        checks.append('Final headings 0/90/180/-90, persistent sitting, seated observation and stand-up')
        print('PASS:', checks[-1], flush=True)

        with ThreadPoolExecutor(1) as pool:
            for cancel in (False, True):
                before = client.call('get_desktop_state', {})['character']
                future = pool.submit(client.call, 'jump', {'heading_degrees': 180, 'height': .12})
                maximum = 0
                deadline = time.monotonic() + 8
                while not future.done():
                    current = client.call('get_desktop_state', {})['character']
                    maximum = max(maximum, current['vertical_offset'])
                    require(sum((current['position'][a] - before['position'][a])**2 for a in ('x','y','z')) < .00001,
                            'Jump navigation root drifted')
                    if cancel and current['posture'] == 'airborne':
                        require(client.call('cancel_move', {}).get('state') == 'succeeded', 'Jump cancel failed')
                        break
                    require(time.monotonic() < deadline, 'Jump timed out')
                    time.sleep(.06)
                result = future.result(5)
                require(result.get('state') == ('cancelled' if cancel else 'succeeded'), 'Jump result: ' + str(result))
                if not cancel: require(maximum > .06, 'Jump visual did not leave desktop')
                time.sleep(.3)
                landed = client.call('get_desktop_state', {})['character']
                require(landed['posture'] == 'standing' and landed['vertical_offset'] == 0, 'Jump did not restore grounded pose')
        checks.append('Animated jump lifts visual, keeps navigation root fixed, lands and cancels cleanly')
        print('PASS:', checks[-1], flush=True)

        if model:
            from .agent import Agent
            from .llm import create_chat_client
            from .storage import Store
            from .tools import Tools
            store = Store(':memory:')
            calls = []

            class RecordingDesktop:
                def tool_schemas(self):
                    return client.tool_schemas()
                def call(self, name, args):
                    result = client.call(name, args)
                    if name != 'get_desktop_state':
                        calls.append({'tool': name, 'result': result})
                    return result

            try:
                agent = Agent(create_chat_client(), Tools(store, desktop=RecordingDesktop(),
                              search_fn=lambda *_args, **_kwargs: {'error': 'Search disabled in desktop smoke test'}))
                print('MODEL: asking the real local Agent to walk to the laptop...', flush=True)
                answer = agent.reply('大肥鱼，请现在走到桌上的笔记本电脑旁边，实际到达后告诉我。')
                print('MODEL ANSWER:', answer, flush=True)
                require(any(c['tool'] in ('move_to', 'approach_object') and c['result'].get('state') == 'arrived' for c in calls),
                        'Local model did not complete a real movement: ' + json.dumps(calls, ensure_ascii=False))
                checks.append('Real Ollama Agent selected a desktop MCP action and Unity confirmed arrival')
                print('PASS:', checks[-1], flush=True)
            finally:
                store.conn.close()
        print(json.dumps({'passed': True, 'checks': checks}, ensure_ascii=False), flush=True)
        return 0
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', action='store_true')
    return run(parser.parse_args().model)


if __name__ == '__main__':
    raise SystemExit(main())
