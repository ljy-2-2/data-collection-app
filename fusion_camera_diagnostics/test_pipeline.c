/* Exercise the production sender with synthetic frames. Never call UVC APIs. */
#define main hardware_main_not_called
#include "capture_three.c"
#undef main
#include <assert.h>

static void put(frame_queue *q, unsigned id, unsigned seq, uint64_t ts) {
  unsigned char header[40]={0};
  unsigned char *data=malloc(FRAME_BYTES); assert(data);
  memset(data,(unsigned char)seq,FRAME_BYTES);
  memcpy(header,"FUV1",4); le32(header+4,id); le32(header+8,seq);
  le64(header+12,ts); le32(header+20,FRAME_BYTES); le32(header+24,640); le32(header+28,480);
  assert(!fq_push(q,id,header,data,FRAME_BYTES,ts)); free(data);
}

static void balanced(frame_queue *q) {
  queue_stats s=fq_stats(q);
  for(unsigned i=0;i<3;i++) {
    assert(s.captured[i]==s.sent[i]+s.overflow_dropped[i]+s.shutdown_dropped[i]+s.failed_frames[i]);
    assert(!s.pending[i] && !s.in_flight[i]);
    assert(s.high_water[i]<=QUEUE_DEPTH);
  }
}

int main(void) {
  signal(SIGPIPE,SIG_IGN);
  frame_queue *q=malloc(sizeof(*q)); queued_frame *scratch=malloc(sizeof(*scratch));
  assert(q && scratch); pthread_t thread;
  /* FIFO, fair camera selection, immutable original timestamps and full payload. */
  assert(!fq_init(q)); FILE *file=tmpfile(); assert(file);
  uint64_t base=now_ns()-1000000;
  for(unsigned i=0;i<3;i++) for(unsigned n=1;n<=4;n++) put(q,i,n,base+n);
  fq_finish(q,now_ns()+1000000000ULL);
  sender_args args={q,scratch,fileno(file)};
  assert(!pthread_create(&thread,NULL,send_frames,&args)); assert(!pthread_join(thread,NULL));
  rewind(file);
  unsigned char *data=malloc(FRAME_BYTES); assert(data);
  for(unsigned n=1;n<=4;n++) for(unsigned i=0;i<3;i++) {
    unsigned char h[40],ts[8]; le64(ts,base+n);
    assert(fread(h,1,40,file)==40 && h[4]==i && h[8]==n && !memcmp(h+12,ts,8));
    assert(fread(data,1,FRAME_BYTES,file)==FRAME_BYTES);
    for(unsigned k=0;k<FRAME_BYTES;k++) assert(data[k]==n);
  }
  assert(fgetc(file)==EOF); balanced(q); fclose(file); fq_destroy(q);
  puts("PASS: sender preserves frame payload, timestamps, FIFO and fairness");

  /* A stalled output must not hold the queue lock or halt producers. */
  assert(!fq_init(q)); int fds[2]; assert(!pipe(fds));
  assert(fcntl(fds[1],F_SETFL,O_NONBLOCK)==0);
  args.fd_out=fds[1]; put(q,0,1,now_ns());
  assert(!pthread_create(&thread,NULL,send_frames,&args));
  uint64_t started=now_ns();
  while(!fq_stats(q).in_flight[0]) { assert(now_ns()-started<500000000ULL); usleep(1000); }
  for(unsigned n=2;n<=25;n++) for(unsigned i=0;i<3;i++) put(q,i,n,now_ns());
  assert(now_ns()-started<700000000ULL); /* Not waiting for the 1 s write deadline. */
  assert(!pthread_join(thread,NULL));
  assert(now_ns()-started<1800000000ULL);
  fq_discard_pending(q); balanced(q);
  queue_stats s=fq_stats(q); assert(s.send_failed && s.failed_frames[0]==1);
  for(unsigned i=0;i<3;i++) assert(s.overflow_dropped[i]>0);
  close(fds[0]); close(fds[1]); fq_destroy(q);
  puts("PASS: stalled output does not block capture; bounded overflow and timeout accounted");

  /* Full queues retain the newest frames without changing their metadata. */
  assert(!fq_init(q));
  for(unsigned n=1;n<=12;n++) put(q,0,n,base+n);
  for(unsigned n=5;n<=12;n++) {
    assert(fq_pop(q,scratch) && scratch->header[8]==n && scratch->captured_ns==base+n);
    fq_sent(q,0,0);
  }
  assert(!fq_pop(q,scratch)); balanced(q); assert(fq_stats(q).overflow_dropped[0]==4);
  fq_destroy(q);
  puts("PASS: overflow drops oldest queued frames, with exact counts");

  /* Shutdown deadline discards leftovers instead of draining indefinitely. */
  assert(!fq_init(q)); file=tmpfile(); assert(file); args.fd_out=fileno(file);
  for(unsigned i=0;i<3;i++) put(q,i,1,now_ns());
  fq_finish(q,0); assert(!pthread_create(&thread,NULL,send_frames,&args));
  assert(!pthread_join(thread,NULL)); balanced(q);
  s=fq_stats(q); for(unsigned i=0;i<3;i++) assert(s.shutdown_dropped[i]==1 && !s.sent[i]);
  fclose(file); fq_destroy(q);
  puts("PASS: bounded shutdown records unsent frames");

  /* Variable compressed payloads must not leak stale bytes or be truncated. */
  assert(!fq_init(q)); file=tmpfile(); assert(file); args.fd_out=fileno(file);
  size_t lengths[3]={127,32769,614989};
  unsigned char *large=malloc(MAX_FRAME_BYTES); assert(large);
  for(unsigned i=0;i<3;i++) {
    unsigned char h[40]={0}; memcpy(h,"FJM1",4); le32(h+4,i); le32(h+20,lengths[i]);
    memset(large,17+i,lengths[i]);
    assert(!fq_push(q,i,h,large,lengths[i],now_ns()));
  }
  unsigned char h[40]={0};
  assert(fq_push(q,0,h,large,MAX_FRAME_BYTES+1,now_ns())==-1);
  assert(fq_push(q,0,h,large,0,now_ns())==-1);
  fq_finish(q,now_ns()+1000000000ULL);
  assert(!pthread_create(&thread,NULL,send_frames,&args)); assert(!pthread_join(thread,NULL));
  rewind(file);
  for(unsigned i=0;i<3;i++) {
    assert(fread(h,1,40,file)==40 && !memcmp(h,"FJM1",4) && h[4]==i);
    uint32_t n=(uint32_t)h[20] | (uint32_t)h[21]<<8 | (uint32_t)h[22]<<16 | (uint32_t)h[23]<<24;
    assert(n==lengths[i] && fread(large,1,n,file)==n);
    for(unsigned j=0;j<n;j++) assert(large[j]==17+i);
  }
  assert(fgetc(file)==EOF); balanced(q); fclose(file); fq_destroy(q);
  free(large); free(data); free(scratch); free(q);
  puts("PASS: variable MJPEG-sized payloads preserve lengths and reject oversize input");
  return 0;
}
