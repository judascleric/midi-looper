#! /usr/bin/env python3
"""
midi_looper
Record and playback midi note commands

2-button operation
Record/Play/Pause
  No recording - record
  Recording - Stop and playback
  Playing - Stop + Rewind
Reset/Panic
  Playing - Stop Playback + Stop all notes
  Stopped - Erase Recording
"""
import asyncio
from dataclasses import dataclass, field
from enum import Enum
import os
import sys
import time
from typing import Awaitable, List, Optional

from alsa_midi import (
    Event,
    AsyncSequencerClient,
    ControlChangeEvent,
    Port,
    READ_PORT,
    WRITE_PORT,
    EventType,
)
from gpiozero import Button, LED

from midi_looper.config import read_config, DEFAULT_CONFIG_PATH

playrec_btn = Button(2)
stop_btn = Button(3)
indicator_led = LED(17)


class State(Enum):
    STOP = 0
    RECORD = 1
    PLAY = 2


@dataclass
class ButtonState:
    down: bool = False
    pressed: bool = False
    unpressed: bool = False
    since: float = field(default_factory=time.time)


@dataclass
class AllInputs:
    playrec: ButtonState = field(default_factory=ButtonState)
    stop: ButtonState = field(default_factory=ButtonState)


@dataclass
# pylint: disable=too-many-instance-attributes
class LooperState:
    client: AsyncSequencerClient
    rec_port: Port
    play_port: Port
    state: State = State.STOP
    prev: float = field(default_factory=time.time)
    rec_queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    rec: List[Event] = field(default_factory=list)
    btn: AllInputs = field(default_factory=AllInputs)
    rec_task: Optional[Awaitable[None]] = None
    play_task: Optional[Awaitable[None]] = None


EVENT_LOOP_SEC = 0.03  # 30ms / 33Hz

RECORD_BLINK_TIME = 0.1
PLAY_BLINK_TIME = 0.75

MAX_RECORD_TIME_SEC = 60 * 5
MAX_RECORD_IDLE_TIMEOUT_SEC = 10
REC_EVENT_TYPE_FILTER = [
    EventType.NOTEON,
    EventType.NOTEOFF,
]
CC_ALL_NOTES_OFF = 123

def main_sync():
    asyncio.run(main())

# pylint: disable=too-many-branches,too-many-statements
async def main():
    config_path = os.environ.get("ACM_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    config = read_config(config_path)

    client = AsyncSequencerClient("midi-looper")
    src_port, dest_port = get_ports(client, config)
    if src_port is None or dest_port is None:
        print(
            f"Failed to find an active src + dest port from {config_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Using src_port = {src_port}")
    print(f"Using dest_port = {dest_port}")

    rec_queue = client.create_queue("recqueue")
    rec_port = client.create_port(
        "input",
        WRITE_PORT,
        timestamping=True,
        timestamp_real=True,
        timestamp_queue=rec_queue,
    )
    rec_port.connect_to(src_port)
    play_port = client.create_port(
        "output",
        READ_PORT,
    )
    play_port.connect_to(dest_port)

    state = LooperState(client=client, rec_port=rec_port, play_port=play_port)
    done = []
    while True:
        now = time.time()
        next_time = now + EVENT_LOOP_SEC
        state.btn = get_button_states(state.btn)
        if state.state == State.STOP:
            if state.btn.stop.pressed:
                print("Stop -> Reset")
                await panic(state.client, state.play_port)
                del state.rec[:]
                state.rec = []
                state.state = State.STOP
                indicator_led.off()
            elif state.btn.playrec.pressed:
                if len(state.rec) == 0:
                    print("Stop -> Record")
                    state.state = State.RECORD
                    state.rec_task = asyncio.create_task(record(state, now + MAX_RECORD_TIME_SEC))
                    indicator_led.blink(on_time=RECORD_BLINK_TIME, off_time=RECORD_BLINK_TIME)
                else:
                    print("Stop -> Play")
                    state.play_task = asyncio.create_task(play(state))
                    state.state = State.PLAY
                    indicator_led.blink(on_time=PLAY_BLINK_TIME, off_time=PLAY_BLINK_TIME)
        elif state.state == State.RECORD:
            # flush recording queue
            for _ in range(state.rec_queue.qsize()):
                state.rec.append(state.rec_queue.get_nowait())
            if state.btn.stop.pressed:
                print("Record -> Stop")
                state.rec_task.cancel()
                state.rec_task = None
                await panic(state.client, state.play_port)
                state.state = State.STOP
                del state.rec[:]
                state.rec = []
                indicator_led.off()
            elif state.btn.playrec.pressed:
                print("Record -> Play")
                state.rec_task.cancel()
                state.rec_task = None
                state.rec += create_panic_events()
                print(len(state.rec))
                state.play_task = asyncio.create_task(play(state))
                state.state = State.PLAY
                indicator_led.blink(on_time=PLAY_BLINK_TIME, off_time=PLAY_BLINK_TIME)
        elif state.state == State.PLAY:
            if state.btn.playrec.pressed or state.btn.stop.pressed:
                print("Play -> Stop")
                state.play_task.cancel()
                state.play_task = None
                await panic(state.client, state.play_port)
                state.state = State.STOP
                indicator_led.on()
            elif state.play_task and state.play_task in done:
                print("Play Loop")
                state.play_task.cancel()
                state.play_task = None
                await panic(state.client, state.play_port)
                state.state = State.STOP
                indicator_led.on()
                # # loop if done
                # print(len(state.rec))
                # # state.play_task = asyncio.create_task(play(state))
                # indicator_led.blink(on_time=PLAY_BLINK_TIME, off_time=PLAY_BLINK_TIME)

        sleep_timeout=next_time - time.time()
        active_tasks = [task for task in [state.rec_task, state.play_task] if task]
        done = []
        if len(active_tasks) > 0:
            done, _ = await asyncio.wait(active_tasks, timeout=sleep_timeout)
        else:
            await asyncio.sleep(sleep_timeout)



async def panic(client: AsyncSequencerClient, dest_port: Port):
    """stop notes on dest port on all channels"""
    for event in create_panic_events():
        await client.event_output(event, port=dest_port)
    await client.drain_output()


def get_button_state(old: ButtonState, down: bool):
    new = ButtonState(down)
    if new.down == old.down:
        new.since = old.since
    else:
        new.pressed = down
        new.unpressed = not down
    return new


def get_button_states(old: AllInputs) -> AllInputs:
    playrec_new = get_button_state(old.playrec, playrec_btn.is_pressed)
    stop_new = get_button_state(old.stop, stop_btn.is_pressed)
    return AllInputs(playrec=playrec_new, stop=stop_new)


async def record(state: LooperState, rec_timeout: float):
    while time.time() < rec_timeout:
        event = await state.client.event_input(timeout=MAX_RECORD_IDLE_TIMEOUT_SEC)
        if event is not None and event.event in REC_EVENT_TYPE_FILTER:
            print(f"record event - {repr(event)}")
            state.rec_queue.put(event)


async def play(state: LooperState):
    for event in state.rec:
        await state.client.event_output(event, port=state.play_port)
    await state.client.drain_output()


def create_panic_events() -> List[Event]:
    events = []
    for channel in range(1, 16 + 1):
        off_event = ControlChangeEvent(channel=channel, param=CC_ALL_NOTES_OFF, value=0)
        events.append(off_event)
    return events


def get_ports(client, config):
    in_ports = client.list_ports(input=True)
    out_ports = client.list_ports(output=True)
    for conn in config.connections:
        for in_port in in_ports:
            if conn.input.client_id == in_port.client_id:
                for out_port in out_ports:
                    if conn.output.client_id == out_port.client_id:
                        return in_port, out_port
    return None, None


if __name__ == "__main__":
    main_sync()
