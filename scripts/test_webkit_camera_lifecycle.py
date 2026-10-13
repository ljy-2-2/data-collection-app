"""WebKitGTK regression: preview -> monitor -> stop must leave HTTP slots free.

Run with /usr/bin/python3. The default fetch transport must pass; pass 'old'
to reproduce the native multipart image loader's monitor/stop timeouts.
Uses only generated images and a temporary local server, never hardware.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf, GLib, Gtk, WebKit2

mode = sys.argv[1] if len(sys.argv) > 1 else 'fetch'
pixel = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 32, 32)
pixel.fill(0x44aaccff)
_, jpeg = pixel.save_to_bufferv('jpeg', [], [])
preview_done = threading.Event()
received = []
final_state = {}

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        received.append(self.path)
        if self.path == '/':
            body = f'''<!doctype html><body><script>
            window.result={{mode:{json.dumps(mode)},checks:[]}};
            const delay=ms=>new Promise(r=>setTimeout(r,ms));
            function openVideo(url){{let im=new Image();document.body.append(im);if(result.mode==='fetch'){{let c=new AbortController();im.cancel=()=>c.abort();fetch(url,{{signal:c.signal}}).then(async r=>{{let reader=r.body.getReader();while(!(await reader.read()).done){{}}}}).catch(()=>{{}});}}else{{im.src=url;im.cancel=()=>{{im.src='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';}};}}return im;}}
            async function ping(label){{let controller=new AbortController(),timer=setTimeout(()=>controller.abort(),1500);try{{let r=await fetch('/ping?'+label,{{signal:controller.signal}});result.checks.push([label,r.status]);}}catch(e){{result.checks.push([label,String(e)]);}}finally{{clearTimeout(timer);}}}}
            (async()=>{{
              let old=['left','center','right'].map(k=>openVideo('/preview/'+k));
              await delay(1500);
              if(result.mode!=='old') old.forEach(im=>{{im.cancel();im.remove();}});
              await fetch('/end-preview');
              await delay(500);
              if(result.mode==='old') old.forEach(im=>im.remove());
              await ping('after-preview');
              let live=['left','center','right'].map(k=>openVideo('/monitor/'+k));
              await delay(1500);
              await ping('monitor');
              if(result.mode!=='old') live.forEach(im=>{{im.cancel();im.remove();}});
              await ping('stop');
              result.done=true;
            }})();
            </script>'''.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith(('/preview/', '/monitor/')):
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream' if mode == 'fetch' else 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                while not (self.path.startswith('/preview/') and preview_done.is_set()):
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n')
                    self.wfile.flush()
                    time.sleep(.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
        else:
            if self.path == '/end-preview':
                preview_done.set()
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')

server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
view=WebKit2.WebView.new_with_context(WebKit2.WebContext.new_ephemeral())
window=Gtk.Window(); window.add(view); window.show_all()

def finished(webview, response, *_):
    raw = webview.run_javascript_finish(response).get_js_value().to_string()
    final_state.update(json.loads(raw))
    print(raw, flush=True)
    print(json.dumps(received), flush=True)
    Gtk.main_quit()

view.load_uri(f'http://127.0.0.1:{server.server_port}/')
GLib.timeout_add_seconds(13,lambda:view.run_javascript('JSON.stringify(window.result)',None,finished,None) or False)
Gtk.main()
window.destroy()
server.shutdown()
if mode == 'fetch' and final_state.get('checks') != [['after-preview', 200], ['monitor', 200], ['stop', 200]]:
    raise SystemExit(1)

