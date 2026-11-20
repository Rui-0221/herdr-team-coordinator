"""Shared protocol types for the coordinator, client, and workflow."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path


class DomainError(Exception):
    def __init__(self, message, next_step=''):
        super().__init__(message)
        self.next_step = next_step


def now():
    return time.time()


def new_id():
    return uuid.uuid4().hex


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(wire(value).encode('utf-8')).hexdigest()


def data_root():
    return Path(os.environ.get('HERDR_COORD_ROOT') or Path.home() / 'HerdrTeamCoordinator').resolve()
