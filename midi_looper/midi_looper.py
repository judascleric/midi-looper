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
from typing import AsyncGenerator, Awaitable, List

from alsa_midi import (
    Event,
    SequencerClient,
    ControlChangeEvent,
    Port,
    READ_PORT,
    WRITE_PORT,
    NOTEON,
    NOTEOFF,
)
from gpiozero import Button

from config import read_config, DEFAULT_CONFIG_PATH

playrec_btn = Button(16)
stop_btn = Button(18)


class State(Enum):
    Stop = 0
    Record = 1
    Play = 2


@dataclass
class ButtonState:
    down: bool = False
    pressed: bool = False
    unpressed: bool = False
    since: float = field(default_factory=time.time)


@dataclass
class AllInputs:
    playrec: ButtonState
    stop: ButtonState


@dataclass
class LooperState:
    state: State = State.Stop
    prev: float = field(default_factory=time.time)
    btn: AllInputs
    rec: List[Event] = field(default_factory=list)
    client: SequencerClient
    rec_port: Port
    play_port: Port
    rec_gen: AsyncGenerator[Event]


EVENT_LOOP_SEC = 0.03  # 30ms / 33Hz

MAX_RECORD_TIME_SEC = 60 * 5
MAX_RECORD_IDLE_TIMEOUT_SEC = 10
REC_EVENT_TYPE_FILTER = [
    NOTEON,
    NOTEOFF,
]
CC_ALL_NOTES_OFF = 123


async def main():
    config_path = os.environ.get("ACM_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    config = read_config(config_path)

    client = SequencerClient("midi-looper")
    src_port, dest_port = get_ports(client, config)
    if src_port is None or dest_port is None:
        print(
            f"Failed to find an active src + dest port from {config_path}",
            file=sys.stderr,
        )
        sys.exit(1)

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
    done, _ = await asyncio.sleep(0)
    while True:
        now = time.time()
        next = now + EVENT_LOOP_SEC
        state.btn = get_button_states(state.btn)
        if state.state == State.Stop:
            if state.btn.stop.pressed:
                print("Stop -> Reset")
                await panic(state.client, state.play_port)
                del state.rec[:]
                state.rec = list()
                state.state == State.Stop
            elif state.btn.play.pressed:
                if len(state.rec) == 0:
                    print("Stop -> Record")
                    state.state = State.Record
                    state.rec_gen = record(state, now + MAX_RECORD_TIME_SEC)
                else:
                    print("Stop -> Play")
                    state.play_coro = play(state)
                    state.state = State.Play
        elif state.state == State.Record:
            if state.btn.stop.pressed:
                print("Record -> Stop")
                await state.rec_gen.aclose()
                await panic(state.client, state.play_port)
                state.state == State.Stop
            elif state.btn.play.pressed:
                print("Record -> Play")
                state.rec = [
                    event for event in await state.rec_gen
                ] + create_panic_events()
                state.play_coro = play(state)
                state.state = State.Play
        elif state.state == State.Play:
            if state.btn.play.pressed or state.btn.stop.pressed:
                print("Play -> Stop")
                await state.play_coro.cancel()
                await panic(state.client, state.play_port)
                state.state == State.Stop
            elif state.play_coro in done:
                print("Play Loop")
                # loop if done
                state.play_coro = play(state)

        done, _ = await asyncio.sleep(next - time.time())


async def panic(client: SequencerClient, dest_port: Port):
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


async def record(state: LooperState, rec_timeout: float) -> AsyncGenerator[Event, None]:
    while time.time() < rec_timeout:
        event = await state.client.event_input(timeout=MAX_RECORD_IDLE_TIMEOUT_SEC)
        if event is not None and event.event in REC_EVENT_TYPE_FILTER:
            print(f"record event - {repr(event)}")
            yield event


async def play(state: LooperState) -> Awaitable[None]:
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
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
