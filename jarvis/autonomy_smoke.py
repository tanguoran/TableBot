"""Bounded live local-planner acceptance. Requires Unity Play and local Ollama."""
import argparse
import json
import time

from . import config
from .autonomy import AutonomousController, InferenceGate, LocalPlanner
from .desktop_client import DesktopMCPClient
from .llm import create_chat_client


def run(seconds=45):
    desktop = DesktopMCPClient(config.DESKTOP_BRIDGE_URL)
    proposals = []
    planner = LocalPlanner(create_chat_client(profile='autonomy'), InferenceGate())

    def recording(context):
        proposal = planner(context)
        proposals.append(proposal)
        print('PLAN:', json.dumps(proposal, ensure_ascii=False), flush=True)
        return proposal

    controller = AutonomousController(desktop, recording)
    try:
        snapshot = desktop.call('get_desktop_state', {})
        if 'error' in snapshot:
            raise RuntimeError(snapshot['error'])
        controller.enable()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(.5)
            if controller.mode != 'on':
                raise RuntimeError(str(controller.status()))
        controller.pause('验收结束', off=True)
        controller.tick()
        status = controller.status()
        successful = [r for r in status['recent'] if r['action']['name'] != 'wait' and
                      r['result'].get('state') in ('succeeded', 'arrived')]
        if not any(p is not None for p in proposals) or not successful:
            raise RuntimeError('Expected a real model proposal and a completed Unity action: ' + str(status))
        print(json.dumps({'passed': True, 'proposals': len(proposals),
                          'completed_actions': successful, 'status': status}, ensure_ascii=False), flush=True)
        return 0
    finally:
        controller.close()
        desktop.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=45)
    raise SystemExit(run(parser.parse_args().seconds))
