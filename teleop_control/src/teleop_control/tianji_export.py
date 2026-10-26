"""Project Tianji uploads without changing local recordings."""

SIDES = ("left", "right")
CURVE_FILES = {
    "record/bundles.jsonl", "record/actions.jsonl", "record/joints.jsonl",
    "record/aligned_joints.jsonl", "record/teleop_feedback.jsonl",
}
CAMERA_NAMES = {"usb-1":"head_color", "usb-2":"right_color", "usb-4":"left_color"}


def select_upload_cameras(metadata, directory, selected):
    # Explicit versioning leaves existing/queued recordings on their old contract.
    if metadata.get("upload_camera_policy") != "selected_wrist_optional_head_v1":
        return None
    if not selected or len(selected)!=1:
        raise ValueError("单臂上传相机策略需要且仅需要一侧机械臂")
    def has_data(name):
        camera=directory/'camera'/name
        index=camera/(name+'.txt')
        return ((metadata.get('cameras',{}).get(name) or {}).get('frames',0)>0
                and index.is_file() and index.stat().st_size>0
                and any(p.suffix.lower() in ('.jpg','.jpeg','.png') for p in (camera/'frames').glob('*')))
    wrist=selected[0]+'_color'
    if not has_data(wrist):
        raise ValueError("所选机械臂腕部相机缺少有效记录，拒绝上传："+wrist)
    return tuple(name for name in ('head_color',wrist) if has_data(name))


def camera_file_selected(relative, selected):
    if selected is None:return True
    for name in set(CAMERA_NAMES.values())-set(selected):
        if relative.startswith('camera/'+name+'/') or relative=='record/'+name+'_frames.jsonl':
            return False
    return True


def project_cameras(value, selected):
    """Remove excluded camera maps/indices, preserving kept timestamps verbatim."""
    if selected is None:return value
    excluded=set(CAMERA_NAMES.values())-set(selected)
    excluded.update(key for key,name in CAMERA_NAMES.items() if name not in selected)
    def walk(item):
        if isinstance(item,dict):
            return {key:walk(val) for key,val in item.items() if key not in excluded}
        if isinstance(item,list):
            return [walk(val) for val in item if not isinstance(val,str) or val not in excluded]
        return item
    return walk(value)


def upload_sides(metadata):
    """Older recordings retain their original archive contract."""
    selected = metadata.get("upload_sides")
    if selected is None:
        return None
    controlled = metadata.get("controlled_sides", list(SIDES))
    if (not isinstance(selected, list) or not selected
            or any(side not in SIDES or side not in controlled for side in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("天机 Marvin M6上传曲线选择无效")
    return tuple(side for side in SIDES if side in selected)


def _side_maps(value, selected):
    if isinstance(value, dict):
        return {key: _side_maps(item, selected) for key, item in value.items()
                if key not in SIDES or key in selected}
    if isinstance(value, list):
        return [_side_maps(item, selected) for item in value]
    return value


def _select_vectors(row, name_key, vector_keys, selected):
    names = row.get(name_key)
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise ValueError("天机 Marvin M6上传曲线缺少有效关节名称")
    indices = [i for i, name in enumerate(names)
               if any(name.startswith(side + "_") for side in selected)]
    if not indices:
        raise ValueError("天机 Marvin M6上传曲线中没有所选机械臂")
    for key in vector_keys:
        if key not in row:
            continue
        values = row[key]
        if not isinstance(values, list) or len(values) != len(names):
            raise ValueError("天机 Marvin M6上传曲线名称与数值维度不一致：" + key)
        row[key] = [values[i] for i in indices]
    row[name_key] = [names[i] for i in indices]


def _select_arm_diagnostics(row, keys, selected):
    for key in keys:
        values = row.get(key)
        if isinstance(values, list) and len(values) == len(SIDES):
            row[key] = [values[i] for i, side in enumerate(SIDES) if side in selected]


def project_row(relative, original, selected, selected_cameras=None):
    """Return a projected copy; camera mappings, timestamps and indices survive."""
    row = _side_maps(original, selected)
    if relative == "meta_info.json":
        for key in ("joint_order", "aligned_state_order"):
            if key in row:
                row[key] = [name for name in row[key]
                            if any(name.startswith(side + "_") for side in selected)]
        row["upload_sides"] = list(selected)
        row["upload_projection"] = "selected_arm_curves_v1"
        row["upload_projection_note"] = (
            "Curve streams contain only upload_sides. controlled_sides describes actual "
            "robot control; quality diagnostics describe the original local capture. "
            "All camera streams and original sample timestamps are retained."
        )
    elif relative == "record/bundles.jsonl":
        tensors = row.get("tensors")
        if not isinstance(tensors, dict):
            raise ValueError("天机 Marvin M6上传回合缺少曲线 tensors")
        for prefix in ("state", "action"):
            name_key = prefix + "__joint__name"
            keys = [key for key in tensors if key.startswith(prefix + "__joint__") and key != name_key]
            _select_vectors(tensors, name_key, keys, selected)
        for key in list(tensors):
            if any(key.startswith(prefix + "__" + side + "_")
                   for prefix in ("state", "action") for side in SIDES if side not in selected):
                del tensors[key]
        _select_arm_diagnostics(row, ("joint_frame_serial",), selected)
    elif relative == "record/actions.jsonl":
        _select_vectors(row, "joint_names", ("position_rad",), selected)
    elif relative == "record/joints.jsonl":
        _select_vectors(row, "joint_names", ("position_deg", "velocity_deg_s", "torque_nm"), selected)
        _select_arm_diagnostics(row, ("frame_serial", "arm_state"), selected)
    elif relative == "record/aligned_joints.jsonl":
        _select_vectors(row, "names", ("position_rad",), selected)
        _select_arm_diagnostics(row, ("joint_frame_serial",), selected)
    row=project_cameras(row,selected_cameras)
    if relative=='meta_info.json' and selected_cameras is not None:
        row['upload_cameras']=list(selected_cameras)
        row['upload_projection']='selected_arm_and_cameras_v2'
        row['upload_projection_note']='Selected arm curves and recorded wrist/head images; timestamps unchanged. Quality diagnostics describe the original local capture.'
    return row
