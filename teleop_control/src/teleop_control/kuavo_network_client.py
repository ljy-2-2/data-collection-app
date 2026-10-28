"""App-local HTTPS service profiles; no SSH or alternate-address fallback."""
import http.client
import json
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit


class StreamBusy(RuntimeError):
    """The server refused before opening a capture; bounded retry is safe."""


def profile(root,host):
    base=Path(root)/'teleop_control/runtime/kuavo/network'
    path=base/host/'client.json'
    if not path.exists():raise RuntimeError(host+' 尚未配置网络采集服务；请先安装 App 随附服务包')
    value=json.loads(path.read_text());value['ca_file']=str(path.parent/value['ca_file']);value['token_file']=str(path.parent/value['token_file'])
    return value


def request(config,endpoint,body=None,timeout=20):
    url=urlsplit(config['url'])
    if url.scheme!='https' or url.path not in ('','/') or not url.hostname or url.username:raise ValueError('无效采集服务地址')
    ctx=ssl.create_default_context(cafile=config['ca_file']);ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    conn=http.client.HTTPSConnection(url.hostname,url.port or 7444,context=ctx,timeout=timeout)
    token=Path(config['token_file']).read_text().strip()
    conn.request('POST',endpoint,body=json.dumps(body or {}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    response=conn.getresponse()
    if response.status!=200:
        detail=response.read(4096).decode(errors='replace');status=response.status;response.close();conn.close()
        try:busy=json.loads(detail).get('error')=='此数据流已有连接，请等待旧连接退出'
        except (ValueError,AttributeError):busy=False
        if status==409 and endpoint in ('/camera','/joints') and busy:raise StreamBusy('此数据流已有连接，请等待旧连接退出')
        raise RuntimeError('网络采集服务拒绝请求：'+detail)
    return conn,response


def open_stream(config,endpoint,body,wait_s=8):
    deadline=time.monotonic()+wait_s
    while True:
        try:return request(config,endpoint,body)
        except StreamBusy:
            if time.monotonic()>=deadline:raise
            time.sleep(min(.25,max(0,deadline-time.monotonic())))


def discover(root,host):
    conn,response=request(profile(root,host),'/discover')
    try:return json.loads(response.read(1024*1024))
    finally:conn.close()


def stream(config,endpoint,body):
    import sys,threading,os
    def lease():
        while os.read(0,1):pass
        os._exit(0)  # Only this owned HTTP reader; socket EOF releases the agent capture.
    threading.Thread(target=lease,daemon=True).start()
    conn,response=open_stream(config,endpoint,body)
    try:
        while True:
            block=response.read1(65536)
            if not block:raise RuntimeError('网络数据流已断开')
            sys.stdout.buffer.write(block);sys.stdout.buffer.flush()
    finally:response.close();conn.close()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('config');parser.add_argument('endpoint');parser.add_argument('body');a=parser.parse_args()
    stream(json.loads(a.config),a.endpoint,json.loads(a.body))
