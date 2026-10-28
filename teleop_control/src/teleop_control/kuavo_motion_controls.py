"""Display projections only; all writes use the versioned robot settings API."""
FIELDS = dict(position_scale='arm_scale', max_velocity_rad_s='arm_speed_rad_s',
              planning_acceleration_rad_s2='arm_acceleration_rad_s2')


def motion_controls(form):
    if not form or not form.get('values') or not form.get('schema'):
        return {}
    required = {side+'_'+suffix for side in ('left','right') for suffix in FIELDS.values()}
    if not required <= form['values'].keys() or not required <= form['schema'].keys():
        return {}  # v2 has shared speed only; retain its existing settings dialog.
    result = {}
    for mode in ('dual', 'dual_base', 'left', 'right'):
        sides = (mode,) if mode in ('left', 'right') else ('left', 'right')
        values, schema, mixed, targets = {}, {}, [], {}
        for field, suffix in FIELDS.items():
            keys = [side+'_'+suffix for side in sides]
            specs = [form['schema'][key] for key in keys]
            values[field] = form['values'][keys[0]]
            if any(form['values'][key] != values[field] for key in keys):
                mixed.append(field)
            schema[field] = dict(min=max(s['min'] for s in specs), max=min(s['max'] for s in specs),
                                 min_exclusive=any(s.get('min_exclusive', False) for s in specs))
            targets[field] = keys
        result[mode] = dict(values=values, schema=schema, mixed=mixed, targets=targets)
    return result
