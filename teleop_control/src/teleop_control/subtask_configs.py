"""Shared persistent configuration library for desktop and browser clients."""
import json
import os
from pathlib import Path
import threading
import uuid
from fastapi import APIRouter, HTTPException
from .subtask_annotations import SubtaskConfig


def router(path=None):
    path = Path(path) if path else Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'xiaokai-lab-data-collection'/'subtask-configs.json'
    api = APIRouter(prefix='/subtask-configs')
    lock = threading.RLock()

    def read():
        return json.loads(path.read_text()) if path.exists() else []

    def write(rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        temporary.replace(path)

    @api.get('')
    def listing():
        with lock:
            return read()

    @api.post('')
    def create(body: SubtaskConfig):
        with lock:
            rows = read()
            value = dict(id=uuid.uuid4().hex, **body.model_dump())
            rows.append(value)
            write(rows)
            return value

    @api.post('/{config_id}/delete')
    def delete(config_id: str):
        with lock:
            rows = read()
            if not any(row['id'] == config_id for row in rows):
                raise HTTPException(404, '配置不存在')
            write([row for row in rows if row['id'] != config_id])
            return {'ok': True}

    return api
