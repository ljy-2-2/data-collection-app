/* Isolated diagnostic, not the persistent snapshot service. YUYV 640x480/30.
 * stdout: 40-byte little-endian header + image; stderr: JSON status.
 * Caller must ensure robot is stationary and no other camera user is active.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>
#include <sys/resource.h>
#include <sched.h>
#include <libusb.h>
#include <libuvc/libuvc.h>
#include "frame_queue.h"
#include "mjpeg_mode.h"

int LIBUSB_CALL libusb_detach_kernel_driver(libusb_device_handle *d, int i) {
  (void)d; (void)i; return LIBUSB_ERROR_NOT_SUPPORTED;
}
int LIBUSB_CALL libusb_attach_kernel_driver(libusb_device_handle *d, int i) {
  (void)d; (void)i; return LIBUSB_ERROR_NOT_SUPPORTED;
}
int LIBUSB_CALL libusb_reset_device(libusb_device_handle *d) {
  (void)d; return LIBUSB_ERROR_NOT_SUPPORTED;
}
int LIBUSB_CALL libusb_set_configuration(libusb_device_handle *d, int c) {
  (void)d; (void)c; return LIBUSB_ERROR_NOT_SUPPORTED;
}

static volatile sig_atomic_t stopped;
static void stop(int sig) { (void)sig; stopped = 1; }
static uint64_t now_ns(void) {
  struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
  return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}
static int write_all(int fd_out, const void *buf, size_t n, uint64_t deadline) {
  const char *p = buf;
  while (n && !stopped && now_ns() < deadline) {
    ssize_t r = write(fd_out, p, n);
    if (r > 0) { p += r; n -= r; continue; }
    if (r < 0 && errno != EAGAIN && errno != EINTR) return -1;
    struct pollfd fd = {fd_out, POLLOUT, 0}; poll(&fd, 1, 20);
  }
  return n ? -1 : 0;
}
static void le32(unsigned char *p, uint32_t v) {
  for (int j = 0; j < 4; j++) p[j] = (v >> (8*j)) & 255;
}
static void le64(unsigned char *p, uint64_t v) {
  for (int j = 0; j < 8; j++) p[j] = (v >> (8*j)) & 255;
}
typedef struct {
  frame_queue *queue;
  queued_frame *scratch;
  int fd_out;
} sender_args;

static void *send_frames(void *opaque) {
  sender_args *a = opaque;
  for (;;) {
    queue_stats state = fq_stats(a->queue);
    if (stopped || state.send_failed ||
        (state.producer_done && now_ns() >= state.drain_deadline_ns)) break;
    if (!fq_pop(a->queue, a->scratch)) {
      if (state.producer_done) break;
      usleep(1000);
      continue;
    }
    queued_frame *f = a->scratch;
    uint64_t age = now_ns() - f->captured_ns;
    uint64_t deadline = now_ns() + 1000000000ULL;
    /* No queue mutex held here. Both writes share one frame deadline. */
    if (write_all(a->fd_out, f->header, sizeof(f->header), deadline) ||
        write_all(a->fd_out, f->data, f->data_bytes, deadline)) {
      fq_fail(a->queue, f->camera_id);
      break;
    }
    fq_sent(a->queue, f->camera_id, age);
  }
  fq_discard_pending(a->queue);
  return NULL;
}

static void log_queue(frame_queue *q, const char *event) {
  queue_stats s = fq_stats(q);
  fprintf(stderr, "{\"queue_event\":\"%s\"", event);
#define COUNTERS(name) fprintf(stderr, ",\"" #name "\":[%llu,%llu,%llu]", \
    (unsigned long long)s.name[0], (unsigned long long)s.name[1], (unsigned long long)s.name[2])
  COUNTERS(captured); COUNTERS(sent); COUNTERS(overflow_dropped);
  COUNTERS(shutdown_dropped); COUNTERS(failed_frames); COUNTERS(max_queue_age_ns);
  COUNTERS(pending); COUNTERS(in_flight); COUNTERS(high_water);
#undef COUNTERS
  fprintf(stderr, ",\"send_failed\":%d}\n", s.send_failed);
}

/* Counts are weak health indicators, NOT proof of real-time latency isolation. */
static int rt_counts(unsigned long long values[4]) {
  FILE *f = fopen("/proc/xenomai/stat", "r"); if (!f) return -1;
  char line[512], name[200]; int found = 0;
  while (fgets(line, sizeof(line), f)) {
    int cpu, pid; unsigned long long msw, csw, pf; unsigned stat; float use;
    if (sscanf(line, "%d %d %llu %llu %llu %x %f %199[^\n]",
               &cpu, &pid, &msw, &csw, &pf, &stat, &use, name) != 8) continue;
    int i = !strcmp(name, "RTPSI") ? 0 : !strcmp(name, "FX_RtTask") ? 1 : -1;
    if (i >= 0) { values[2*i] = msw; values[2*i+1] = pf; found |= 1 << i; }
  }
  fclose(f); return found == 3 ? 0 : -1;
}
typedef struct {
  uvc_device_handle_t *dev;
  uvc_stream_handle_t *stream;
  uint32_t last_seq;
  uint64_t count, gaps, last_ns, observed, invalid;
  unsigned consecutive_invalid;
} camera;

int main(int argc, char **argv) {
  if (argc != 5 && argc != 6) { fprintf(stderr, "usage: capture-three seconds devnum1 devnum2 devnum4 [yuyv|mjpeg]\n"); return 2; }
  int jpeg=argc==6 && !strcmp(argv[5],"mjpeg");
  unsigned width=jpeg ? 1024 : 640, height=jpeg ? 768 : 480;
  if(argc==6 && !jpeg && strcmp(argv[5],"yuyv")) return 2;
  enum uvc_frame_format format=jpeg ? UVC_FRAME_FORMAT_MJPEG : UVC_FRAME_FORMAT_YUYV;
  char *end; long duration = strtol(argv[1], &end, 10);
  if (*end || duration < 1 || duration > 1800) return 2;
  int addresses[3];
  for (int i = 0; i < 3; i++) {
    addresses[i] = strtol(argv[i+2], &end, 10);
    if (*end || addresses[i] < 1 || addresses[i] > 127) return 2;
    for (int j=0;j<i;j++) if(addresses[i]==addresses[j]) return 2;
  }
  struct rlimit mem = {256*1024*1024, 256*1024*1024}, core = {0,0};
  if (setrlimit(RLIMIT_AS,&mem) || setrlimit(RLIMIT_CORE,&core) || setpriority(PRIO_PROCESS,0,19)) return 2;
  cpu_set_t cpus; CPU_ZERO(&cpus); CPU_SET(3,&cpus);
  if (sched_setaffinity(0,sizeof(cpus),&cpus)) return 2;
  signal(SIGTERM,stop); signal(SIGINT,stop); signal(SIGALRM,stop); signal(SIGPIPE,SIG_IGN);
  alarm(duration+15);
  int flags=fcntl(1,F_GETFL); if(flags<0 || fcntl(1,F_SETFL,flags|O_NONBLOCK)<0) return 2;
  camera cams[3] = {0};
  unsigned long long baseline[4]={0}, health[4]={0};
  if (rt_counts(baseline)) { fprintf(stderr,"RT baseline unavailable\n"); return 2; }
  uvc_context_t *ctx=NULL; uvc_device_t **list=NULL;
  int error=0, result=1, queue_ready=0, sender_started=0;
  frame_queue *queue=calloc(1,sizeof(*queue));
  queued_frame *scratch=malloc(sizeof(*scratch));
  pthread_t sender;
  sender_args sending = {queue, scratch, STDOUT_FILENO};
  if (!queue || !scratch || fq_init(queue)) goto done;
  queue_ready=1;
  fprintf(stderr,"{\"pipeline\":\"capture_queue_sender\",\"queue_depth_per_camera\":%d,\"queue_and_sender_bytes\":%zu}\n",
          QUEUE_DEPTH,sizeof(*queue)+sizeof(*scratch));
  if ((error=uvc_init(&ctx,NULL))<0 || (error=uvc_get_device_list(ctx,&list))<0) goto done;
  for(int i=0;i<3 && !stopped;i++) {
    uvc_device_t *chosen=NULL;
    for(int j=0;list[j];j++) {
      if(uvc_get_bus_number(list[j])!=1 || uvc_get_device_address(list[j])!=addresses[i]) continue;
      uvc_device_descriptor_t *d=NULL;
      if((error=uvc_get_device_descriptor(list[j],&d))<0) goto done;
      int valid=i==0 ? (d->idVendor==0x0bda && d->idProduct==0x5883) :
                       (d->idVendor==0x32e4 && d->idProduct==0x9230);
      uvc_free_device_descriptor(d); if(valid) chosen=list[j];
    }
    if(!chosen) { fprintf(stderr,"Camera identity mismatch %d\n",i); goto done; }
    if((error=uvc_open(chosen,&cams[i].dev))<0) goto done;
    uvc_stream_ctrl_t ctrl; memset(&ctrl,0,sizeof(ctrl));
    error=jpeg ? negotiate_mjpeg30(cams[i].dev,&ctrl,i,width,height) :
                 uvc_get_stream_ctrl_format_size(cams[i].dev,&ctrl,format,640,480,30);
    if(error<0) goto done;
    fprintf(stderr,"{\"negotiated_camera\":%d,\"interval_100ns\":%u,\"max_payload\":%u,\"max_frame\":%u}\n",
            i,ctrl.dwFrameInterval,ctrl.dwMaxPayloadTransferSize,ctrl.dwMaxVideoFrameSize);
    if(ctrl.dwFrameInterval<333333 || ctrl.dwFrameInterval>333334 ||
       !ctrl.dwMaxVideoFrameSize || ctrl.dwMaxVideoFrameSize>MAX_FRAME_BYTES ||
       (!jpeg && ctrl.dwMaxVideoFrameSize!=FRAME_BYTES) || !ctrl.dwMaxPayloadTransferSize || ctrl.dwMaxPayloadTransferSize>3072) {
      fprintf(stderr,"Negotiation did not produce bounded requested 30 Hz mode\n"); goto done;
    }
    if((error=uvc_stream_open_ctrl(cams[i].dev,&cams[i].stream,&ctrl))<0) goto done;
  }
  if(stopped) goto done;
  if (pthread_create(&sender,NULL,send_frames,&sending)) goto done;
  sender_started=1;
  for(int i=0;i<3;i++) if((error=uvc_stream_start(cams[i].stream,NULL,NULL,0))<0) goto done;
  uint64_t start=now_ns(), last_health=start;
  while(!stopped && now_ns()-start<(uint64_t)duration*1000000000ULL) {
    if (fq_stats(queue).send_failed) { fprintf(stderr,"Output failed or stalled\n"); goto done; }
    int received=0;
    for(int i=0;i<3;i++) {
      uvc_frame_t *f=NULL;
      if((error=uvc_stream_get_frame(cams[i].stream,&f,-1))<0) goto done;
      if(!f) {
        if(now_ns()-(cams[i].last_ns ? cams[i].last_ns : start)>2000000000ULL) {
          fprintf(stderr,"Camera stalled %d\n",i); goto done;
        }
        continue;
      }
      received=1;
      if(f->data_bytes>MAX_FRAME_BYTES || (!jpeg && f->data_bytes!=FRAME_BYTES) ||
         f->width!=width || f->height!=height || f->frame_format!=format) {
        fprintf(stderr,"Invalid frame geometry/size camera %d bytes %zu\n",i,f->data_bytes); goto done;
      }
      if(cams[i].observed && f->diag_monotonic_ns<=cams[i].last_ns) { fprintf(stderr,"Timestamp regression\n"); goto done; }
      if(cams[i].observed) cams[i].gaps += (uint32_t)(f->sequence-cams[i].last_seq-1);
      cams[i].last_seq=f->sequence; cams[i].last_ns=f->diag_monotonic_ns;
      cams[i].observed++;
      if(jpeg && (f->data_bytes<128 || ((unsigned char*)f->data)[0]!=0xff || ((unsigned char*)f->data)[1]!=0xd8)) {
        cams[i].invalid++; cams[i].consecutive_invalid++;
        fprintf(stderr,"{\"invalid_jpeg_camera\":%d,\"sequence\":%u,\"bytes\":%zu,\"invalid_total\":%llu,\"prefix_hex\":\"",
                i,f->sequence,f->data_bytes,(unsigned long long)cams[i].invalid);
        for(size_t j=0;j<f->data_bytes && j<16;j++) fprintf(stderr,"%02x",((unsigned char*)f->data)[j]);
        fprintf(stderr,"\"}\n");
        if(cams[i].consecutive_invalid>=3 || cams[i].invalid>=10) {
          fprintf(stderr,"MJPEG invalid-frame budget exceeded\n"); goto done;
        }
        continue;
      }
      cams[i].consecutive_invalid=0;
      unsigned char header[40]={0}; memcpy(header,jpeg ? "FJM1" : "FUV1",4);
      le32(header+4,i); le32(header+8,f->sequence); le64(header+12,f->diag_monotonic_ns);
      le32(header+20,f->data_bytes); le32(header+24,width); le32(header+28,height);
      le32(header+32,f->diag_pts); le32(header+36,f->diag_scr);
      if(fq_push(queue,i,header,f->data,f->data_bytes,f->diag_monotonic_ns)) { fprintf(stderr,"Frame queue rejected payload\n"); goto done; }
      cams[i].count++;
    }
    double elapsed=(now_ns()-start)/1e9;
    if(jpeg && elapsed>=1.0) {
      for(int i=0;i<3;i++) if(cams[i].observed > elapsed*35.0+3) {
        fprintf(stderr,"MJPEG camera %d exceeds requested frame rate: %llu in %.3fs\n",i,(unsigned long long)cams[i].count,elapsed);
        goto done;
      }
    }
    if(now_ns()-last_health>2000000000ULL) {
      if(rt_counts(health) || memcmp(health,baseline,sizeof(health))) { fprintf(stderr,"RT counters changed/unavailable; stop\n"); goto done; }
      fprintf(stderr,"{\"elapsed\":%.3f,\"frames\":[%llu,%llu,%llu],\"sequence_gaps\":[%llu,%llu,%llu]}\n",
        (now_ns()-start)/1e9,(unsigned long long)cams[0].count,(unsigned long long)cams[1].count,(unsigned long long)cams[2].count,
        (unsigned long long)cams[0].gaps,(unsigned long long)cams[1].gaps,(unsigned long long)cams[2].gaps);
      log_queue(queue,"progress");
      last_health=now_ns();
    }
    if(!received) usleep(1000);
  }
  if(!stopped) result=0;
done:
  /* Stop acquiring at the deadline; allow at most 1 s to start draining frames.
   * A frame already in flight has its own 1 s deadline. Pending frames are counted. */
  if (queue_ready) fq_finish(queue, result==0 ? now_ns()+1000000000ULL : 0);
  for(int i=2;i>=0;i--) { if(cams[i].stream) uvc_stream_close(cams[i].stream); if(cams[i].dev) uvc_close(cams[i].dev); }
  if(list) uvc_free_device_list(list,1);
  if(ctx) uvc_exit(ctx);
  if (sender_started) pthread_join(sender,NULL);
  if (queue_ready) {
    /* A producer may have enqueued just as the sender reported failure. */
    fq_discard_pending(queue);
    if (fq_stats(queue).send_failed) result=1;
    log_queue(queue,"final");
    fq_destroy(queue);
  }
  free(scratch); free(queue);
  for(int i=0;i<3;i++) fprintf(stderr,"{\"capture_final_camera\":%d,\"observed\":%llu,\"valid\":%llu,\"invalid\":%llu,\"sequence_gaps\":%llu}\n",
     i,(unsigned long long)cams[i].observed,(unsigned long long)cams[i].count,
     (unsigned long long)cams[i].invalid,(unsigned long long)cams[i].gaps);
  if(error<0) fprintf(stderr,"UVC error %d: %s\n",error,uvc_strerror(error));
  fprintf(stderr,"{\"exit\":%d}\n",result); return result;
}
