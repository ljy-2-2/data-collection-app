from pathlib import Path
import hashlib,json,shutil,time
source=Path('/home/yeap/桌面/data-collection-app/fusion_data_collection')
root=Path('/home/yeap/桌面/TJ_FX_ROBOT_CONTRL_SDK-master')
expected=json.loads((source/'installation_expected_sha256.json').read_text())
pairs=[(source/'collect_data.py',root/'collect_data.py')]
pairs += [(p,root/'FusionDataCollector'/p.relative_to(source/'FusionDataCollector'))
          for p in (source/'FusionDataCollector').rglob('*') if p.is_file() and '__pycache__' not in p.parts]
pairs += [(p,root/'Marvin-VR-Teleop/marvin_teleop'/p.name)
          for p in (source/'teleop_patch/marvin_teleop').glob('*.py')]
pairs += [(source.parent/'fusion_camera_stream/fusion_camera/stream.py',root/'FusionCameraSDK/fusion_camera/stream.py')]
for src,dst in pairs:
    rel=str(dst.relative_to(root))
    if dst.exists():
        if rel not in expected or hashlib.sha256(dst.read_bytes()).hexdigest()!=expected[rel]:
            raise RuntimeError('Target changed/unreviewed: '+str(dst))
backup=root/'FusionDataCollector/backups'/time.strftime('%Y%m%d_%H%M%S')
backup.mkdir(parents=True)
for src,dst in pairs:
    if dst.exists():
        saved=backup/dst.relative_to(root);saved.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(dst,saved)
for src,dst in pairs:
    dst.parent.mkdir(parents=True,exist_ok=True)
    staged=dst.with_name(dst.name+'.collection-new')
    shutil.copy2(src,staged)
    staged.replace(dst)
(root/'collect_data.py').chmod(0o755)
shutil.copy2(source/'teleop_integration.patch',root/'FusionDataCollector/teleop_integration.patch')
manifest={str(dst.relative_to(root)):hashlib.sha256(dst.read_bytes()).hexdigest() for src,dst in pairs}
(root/'FusionDataCollector/installed_sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'installed_files':len(pairs),'backup':str(backup)},ensure_ascii=False))
