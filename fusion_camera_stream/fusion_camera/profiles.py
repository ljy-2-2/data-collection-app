"""Validate delivered image profiles independently of camera model labels."""
ZED_LEFT_MODES = {(2208, 1242): (15,), (1920, 1080): (15, 30),
                  (1280, 720): (15, 30, 60), (672, 376): (15, 30, 60, 100)}
LEGACY_SIZES = {'mjpeg': ((1024, 768), (1280, 1024), (1920, 1080)),
                'yuyv': ((320, 240), (640, 480))}
SOURCE_FIELDS = ('camera_model', 'eye', 'source_format', 'source_width', 'source_height',
                 'encoding', 'jpeg_quality', 'source_yuv_matrix', 'source_yuv_range',
                 'limited_range_assumption', 'fps', 'mode_id')


def valid_profile(profile):
    if not isinstance(profile, dict) or any(type(profile.get(k)) is not int for k in ('width', 'height', 'fps')):
        return False
    size = profile['width'], profile['height']
    if profile.get('camera_model') == 'ZED Mini' or profile.get('eye') is not None:
        return (profile.get('camera_model') == 'ZED Mini' and profile.get('eye') == 'left'
                and profile.get('format') == 'mjpeg' and profile.get('source_format') == 'yuyv'
                and profile.get('source_width') == size[0] * 2 and profile.get('source_height') == size[1]
                and profile['fps'] in ZED_LEFT_MODES.get(size, ())
                and profile.get('encoding') == 'controller_jpeg')
    return profile['fps'] == 30 and size in LEGACY_SIZES.get(profile.get('format'), ())


def source_metadata(profile):
    return {key: profile[key] for key in SOURCE_FIELDS if key in profile}
