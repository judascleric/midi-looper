"""
config - data structures and methods for reading and writing connect configs
"""

from dataclasses import dataclass, field
import os
from typing import List

from dataclasses_json import dataclass_json


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/auto-connect-midi.json")


@dataclass_json
@dataclass
class Device:
    name: str = ""
    client_id: int = 0


@dataclass_json
@dataclass
class Connection:
    input: Device
    output: Device


@dataclass_json
@dataclass
class Config:
    connections: List[Connection] = field(default_factory=list)


def read_config(config_path):
    if not os.path.exists(config_path):
        return Config()
    with open(config_path) as f:
        config_json = f.read()
    # pylint: disable=no-member
    config = Config.from_json(config_json)
    return config
