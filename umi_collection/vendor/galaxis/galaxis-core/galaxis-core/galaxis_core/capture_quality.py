"""Passive capture quality notes. Never alter samples or fake calibration."""
import math
import struct

CAMERA_SUFFIX = '/image_raw/compressed'

def calibration_summary(message):
    k = list(message.k)
    finite = len(k) == 9 and all(math.isfinite(float(v)) for v in k)
    valid = bool(finite and message.width > 0 and message.height > 0
                 and k[0] > 0 and k[4] > 0 and abs(k[8]-1.0) < 1e-6)
    return dict(calibrated=valid,width=int(message.width),height=int(message.height),
                distortion_model=str(message.distortion_model),
                reason='' if valid else '相机内参缺失或无效，需要真实标定')

def jpeg_structure_valid(payload):
    """Validate CDR bounds and JPEG markers, permitting device tail padding."""
    try:
        if payload[:2] not in (b'\0\1',b'\0\0'):return False
        order='<' if payload[1] else '>'
        offset=12
        for _ in range(2):
            offset=4+((offset-4+3)//4)*4
            length=struct.unpack_from(order+'I',payload,offset)[0]
            if length<1 or offset+4+length>len(payload):return False
            offset+=4+length
        offset=4+((offset-4+3)//4)*4
        length=struct.unpack_from(order+'I',payload,offset)[0]
        start=offset+4;end=start+length
        return (length>=4 and end<=len(payload) and payload[start:start+2]==b'\xff\xd8'
                and payload.rfind(b'\xff\xd9',start+2,end)>=start+2)
    except (IndexError,struct.error,TypeError):
        return False

def capture_quality(counts, calibration, malformed, transport, continuity):
    cameras=sorted(t for t in counts if t.endswith(CAMERA_SUFFIX))
    warnings=[];details={}
    for topic in cameras:
        info=topic.removesuffix(CAMERA_SUFFIX)+'/camera_info'
        row=calibration.get(info,dict(calibrated=False,reason='未收到相机内参'))
        details[topic]=row
        if not row.get('calibrated'):warnings.append(topic+': '+row.get('reason','未标定'))
        if malformed.get(topic,0):warnings.append(topic+': '+str(malformed[topic])+' 帧 JPEG 结构异常，原始消息已保留')
        timing=transport.get('topics',{}).get(topic,{})
        rate=timing.get('source_hz')
        if rate is not None and rate<27:warnings.append(topic+': 实际源帧率 '+format(rate,'.2f')+' Hz')
        if timing.get('header_nonincreasing'):warnings.append(topic+': 图像时间戳重复或倒退')
        if continuity.get(topic):warnings.append(topic+': 存在超过 100 ms 的采样间隔')
    return dict(version=1,method='passive-stream-checks; no frame replacement or timestamp rewriting',
                warnings=warnings,needs_review=bool(warnings),camera_calibration=details,
                malformed_jpeg_frames=dict(malformed),
                timing_notes=[
                    'PICO header 是主机接收时间；device_time_ns 是未同步的设备时钟',
                    '速度计算应使用设备端采样间隔；跨设备对齐需独立时钟标定',
                    '现有 PICO tracked/valid 可能仅表示位姿可解析，不等于硬件跟踪已确认'],
                latency_definitions=transport.get('latency_definitions',{}),
                topic_timing=transport.get('topics',{}))
