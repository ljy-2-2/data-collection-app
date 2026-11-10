"""Exercise the real vendor negotiation wrapper against synthetic USB failures."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.skipif(shutil.which('cc') is None, reason='C compiler unavailable')
def test_zed_probe_period_rounding_preserves_requested_rate(tmp_path):
    source = (ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/capture_teleop.c').read_text()
    start = source.index('static unsigned profile_fps(')
    end = source.index('\n}', source.index('static int profile_interval_matches(', start)) + 2
    code = '''#include <stdint.h>
#include <assert.h>
typedef struct { unsigned source_width, fps; } Profile;
''' + source[start:end] + '''
int main(void) {
    unsigned rates[] = {15,30,60,100};
    for (unsigned i=0; i<4; ++i) {
        Profile p = {1344, rates[i]};
        unsigned floor = 10000000U/p.fps, ceil = (10000000U+p.fps-1)/p.fps;
        assert(profile_interval_matches(&p, floor));
        assert(profile_interval_matches(&p, ceil));
        assert(!profile_interval_matches(&p, floor-1));
        assert(!profile_interval_matches(&p, ceil+1));
        assert(!profile_interval_matches(&p, floor*2));
    }
    Profile legacy = {0,0};
    assert(profile_interval_matches(&legacy, 333333));
    assert(!profile_interval_matches(&legacy, 333334));
    return 0;
}
'''
    path = tmp_path/'interval.c'; path.write_text(code)
    binary = tmp_path/'interval'
    subprocess.run(['cc','-Wall','-Werror',str(path),'-o',str(binary)],check=True,capture_output=True)
    subprocess.run([str(binary)],check=True,timeout=3)

@pytest.mark.skipif(shutil.which('cc') is None,reason='C compiler unavailable')
def test_probe_stops_at_first_usb_error_and_returns_real_readback_result(tmp_path):
    source=(ROOT/'fusion_data_collection/FusionCameraSDK/vendor/libuvc-0.0.6/src/stream.c').read_text()
    start=source.index('uvc_error_t uvc_probe_stream_ctrl(')
    opening=source.index('{',start);depth=1;end=opening+1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    body=source[start:end]
    test=r'''
#include <assert.h>
typedef int uvc_error_t;
typedef int uvc_device_handle_t;
typedef int uvc_stream_ctrl_t;
#define UVC_SUCCESS 0
#define UVC_SET_CUR 1
#define UVC_GET_CUR 2
static int results[2],calls;
static int uvc_query_stream_ctrl(uvc_device_handle_t *dev,uvc_stream_ctrl_t *ctrl,int probe,int request) {
    (void)dev;(void)ctrl;assert(probe==1);assert(request==calls+1);return results[calls++];
}
''' + body + r'''
int main(void) {
    calls=0;results[0]=-7;results[1]=0;assert(uvc_probe_stream_ctrl(0,0)==-7 && calls==1);
    calls=0;results[0]=0;results[1]=-9;assert(uvc_probe_stream_ctrl(0,0)==-9 && calls==2);
    calls=0;results[0]=results[1]=0;assert(uvc_probe_stream_ctrl(0,0)==0 && calls==2);
    return 0;
}
'''
    path=tmp_path/'probe.c';path.write_text(test);binary=tmp_path/'probe'
    subprocess.run(['cc','-Wall','-Werror',str(path),'-o',str(binary)],check=True,capture_output=True)
    subprocess.run([str(binary)],check=True,timeout=3)
