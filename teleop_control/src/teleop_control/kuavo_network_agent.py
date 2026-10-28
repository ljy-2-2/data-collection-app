"""Authenticated HTTPS camera/observation service. No robot control APIs.

Import and startup are inert with respect to cameras/ROS. Streams are opened only
on explicit authenticated requests. Requests cannot supply shell/code/paths.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import selectors
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer


class AgentServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,config):
        self.config=config;self.token=Path(config['token_file']).read_text().strip()
        if len(self.token)<32:raise ValueError('agent token too short')
        self.owners=set();self.owner_lock=threading.Lock()
        super().__init__((config.get('bind','0.0.0.0'),config.get('port',7444)),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(config['cert_file'],config['key_file']);self.socket=ctx.wrap_socket(self.socket,server_side=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,value):
        body=json.dumps(value,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):self.reply(401,{'error':'未授权'});return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<=size<=8192:raise ValueError('request too large')
            self.connection.settimeout(10);body=json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(body,dict):raise ValueError('request must be an object')
            if self.path=='/status':
                capabilities=['usb_camera']
                if self.server.config.get('joint_feedback') is True:capabilities.append('joint_feedback_100hz')
                self.reply(200,dict(version=1,capabilities=capabilities,motion_control=False));return
            if self.path=='/discover':
                import kuavo_camera_remote
                self.reply(200,kuavo_camera_remote.inventory());return
            if self.path=='/camera':
                import kuavo_camera_remote
                source=next((s for s in kuavo_camera_remote.inventory()['sources'] if s['id']==body.get('source_id') and s['kind']=='usb'),None)
                if source is None:raise ValueError('相机身份不存在')
                profile=body.get('profile') or {};caps=source['capabilities']
                if not isinstance(profile,dict):raise ValueError('profile must be an object')
                if set(profile)-{'width','height','brightness'}:raise ValueError('无效相机参数')
                width,height=profile.get('width',640),profile.get('height',480)
                if type(width) is not int or type(height) is not int or dict(width=width,height=height) not in caps['resolutions']:raise ValueError('不支持此分辨率')
                b=profile.get('brightness');spec=caps.get('brightness')
                if b is not None and (type(b) is not int or not spec or not spec['min']<=b<=spec['max'] or (b-spec['min'])%max(1,spec['step'])):raise ValueError('不支持此亮度')
                source['profile']=profile
                command=[sys.executable,'-u','-c','import json,sys,kuavo_camera_remote as m;m.run(json.loads(sys.argv[1]))',json.dumps(dict(action='stream',source=source))]
                key=os.path.realpath(source['path']);content='application/octet-stream'
            elif self.path=='/joints':
                if self.server.config.get('joint_feedback') is not True:raise ValueError('此工控机不提供关节反馈')
                command=[sys.executable,'-u',str(Path(__file__).with_name('kuavo_joint_remote.py'))]
                key='joint_feedback';content='application/x-ndjson'
            else:self.reply(404,{'error':'未知接口'});return
        except (ValueError,KeyError,OSError) as exc:self.reply(409,{'error':str(exc)});return
        with self.server.owner_lock:
            if key in self.server.owners:self.reply(409,{'error':'此数据流已有连接，请等待旧连接退出'});return
            self.server.owners.add(key)
        process=None;selector=selectors.DefaultSelector()
        try:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=Path(__file__).parent)
            selector.register(process.stdout,selectors.EVENT_READ,'data');selector.register(process.stderr,selectors.EVENT_READ,'error')
            # Wait for real output, so failed opens return a useful HTTP error.
            started=False;last=time.monotonic();errors=''
            while True:
                if time.monotonic()-last>12:raise RuntimeError(errors or '数据流超时')
                for ready,_ in selector.select(.2):
                    data=os.read(ready.fd,65536)
                    if not data:
                        if ready.data=='data':raise RuntimeError(errors or '数据流已结束')
                        selector.unregister(ready.fileobj);continue
                    if ready.data=='error':errors=(errors+data.decode(errors='replace'))[-2000:];continue
                    if not started:
                        self.send_response(200);self.send_header('Content-Type',content);self.send_header('Connection','close');self.end_headers();started=True
                    self.wfile.write(data);self.wfile.flush();last=time.monotonic()
        except (OSError,RuntimeError) as exc:
            if not locals().get('started',False):
                try:self.reply(409,{'error':str(exc)})
                except OSError:pass
        finally:
            selector.close()
            if process:
                process.stdin.close()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:process.wait(timeout=3)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
                process.stdout.close();process.stderr.close()
            with self.server.owner_lock:self.server.owners.discard(key)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=json.loads(Path(args.config).read_text());AgentServer(config).serve_forever()

if __name__=='__main__':main()
