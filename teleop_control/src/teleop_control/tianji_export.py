"""Select uploaded Tianji curves without changing the local dual-arm recording."""

SIDES = ("left", "right")
CURVE_FILES = {
    "record/bundles.jsonl", "record/actions.jsonl", "record/joints.jsonl",
    "record/aligned_joints.jsonl", "record/teleop_feedback.jsonl",
}


def upload_sides(metadata):
    """Older recordings retain their original archive contract."""
    selected = metadata.get("upload_sides")
    if selected is None:
        return None
    controlled = metadata.get("controlled_sides", list(SIDES))
    if (not isinstance(selected, list) or not selected
            or any(side not in SIDES or side not in controlled for side in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("天机上传曲线选择无效")
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
        raise ValueError("天机上传曲线缺少有效关节名称")
    indices = [i for i, name in enumerate(names)
               if any(name.startswith(side + "_") for side in selected)]
    if not indices:
        raise ValueError("天机上传曲线中没有所选机械臂")
    for key in vector_keys:
        if key not in row:
            continue
        values = row[key]
        if not isinstance(values, list) or len(values) != len(names):
            raise ValueError("天机上传曲线名称与数值维度不一致：" + key)
        row[key] = [values[i] for i in indices]
    row[name_key] = [names[i] for i in indices]


def _select_arm_diagnostics(row, keys, selected):
    for key in keys:
        values = row.get(key)
        if isinstance(values, list) and len(values) == len(SIDES):
            row[key] = [values[i] for i, side in enumerate(SIDES) if side in selected]


def project_row(relative, original, selected):
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
            raise ValueError("天机上传回合缺少曲线 tensors")
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
    return row
