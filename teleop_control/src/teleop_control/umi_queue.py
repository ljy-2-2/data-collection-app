"""Read-only view of durable episodes, scoped to their original destination."""
import json
from pathlib import Path


def read_json(path):
    try:
        value = path.read_text(encoding='utf-8')
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def queue_summary(data_root, destination=None):
    root = Path(data_root)
    default = read_json(root / '.background-upload/config.json').get('default_destination')
    result = dict(pending=0, uploaded=0, processing=0, failed_processing=0,
                  other_pending=0, waiting_auth=0, uploading=0, failed=0, message='')
    reasons = []
    bags = (root / 'rosbags').resolve()
    recovery = {}
    for path in (bags / '.capture-journals').glob('*.json'):
        intent = read_json(path)
        if intent.get('state') != 'interrupted':
            continue
        folder = (bags / intent.get('folder', '')).resolve()
        if folder != bags and folder.is_relative_to(bags) and folder.is_dir():
            recovery[folder] = intent.get('metadata') or {}
    folders = {p.parent for name in ('meta.json', '.mp4-pending.json') for p in bags.glob('*/*/' + name)} | recovery.keys()
    for folder in sorted(folders):
        if (folder / '.discarded').exists():
            continue
        meta = read_json(folder / 'meta.json') or read_json(folder / '.mp4-pending.json') or recovery.get(folder, {})
        target = meta.get('upload_destination') or default
        receipt = (folder / '.data-platform-uploaded.json').is_file()
        processing = ((folder / '.mp4-pending.json').is_file() or (folder / '.recovery-pending').exists() or folder in recovery)
        failure = (folder / 'recording-failure.json').is_file()
        complete = ((folder / 'media/video-manifest.json').is_file() or
                    ((folder / 'metadata.yaml').is_file() and any(folder.glob('*.mcap'))))
        if destination is not None and target != destination:
            if not receipt and (processing or complete):
                result['other_pending'] += 1
            continue
        if processing:
            result['failed_processing' if failure else 'processing'] += 1
        elif complete and not failure:
            if receipt:
                result['uploaded'] += 1
                continue
            result['pending'] += 1
            state = read_json(folder / '.upload-status.json')
            stage = state.get('state')
            if stage in ('waiting_auth', 'uploading', 'failed'):
                result[stage] += 1
            if state.get('message') and stage != 'uploaded':
                reasons.append(state['message'])
    if result['other_pending']:
        reasons.append(f"其他平台尚有 {result['other_pending']} 段待上传，保留原目标；可切换到原平台查看")
    result['message'] = '；'.join(dict.fromkeys(reasons))
    return result
