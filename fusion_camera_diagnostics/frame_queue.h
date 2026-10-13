#ifndef FUSION_FRAME_QUEUE_H
#define FUSION_FRAME_QUEUE_H
#include <pthread.h>
#include <stdint.h>
#include <stddef.h>

#define CAMERA_COUNT 3
#define QUEUE_DEPTH 8
#define FRAME_BYTES (640 * 480 * 2)
#define MAX_FRAME_BYTES (2 * 1024 * 1024)

typedef struct {
  unsigned char header[40];
  unsigned char data[MAX_FRAME_BYTES];
  size_t data_bytes;
  uint64_t captured_ns;
  unsigned camera_id;
} queued_frame;

typedef struct {
  uint64_t captured[3], sent[3], overflow_dropped[3], shutdown_dropped[3];
  uint64_t failed_frames[3], max_queue_age_ns[3];
  unsigned pending[3], in_flight[3], high_water[3];
  int producer_done, send_failed;
  uint64_t drain_deadline_ns;
} queue_stats;

typedef struct {
  pthread_mutex_t mutex;
  queued_frame slots[CAMERA_COUNT][QUEUE_DEPTH];
  unsigned head[3], next_camera;
  queue_stats stats;
} frame_queue;

int fq_init(frame_queue *q);
/* Drop the oldest queued frame of this camera when full. Never wait for I/O. */
int fq_push(frame_queue *q, unsigned camera_id, const unsigned char header[40],
             const void *data, size_t data_bytes, uint64_t captured_ns);
int fq_pop(frame_queue *q, queued_frame *out);
void fq_sent(frame_queue *q, unsigned camera_id, uint64_t queue_age_ns);
void fq_fail(frame_queue *q, unsigned camera_id);
void fq_finish(frame_queue *q, uint64_t drain_deadline_ns);
void fq_discard_pending(frame_queue *q);
queue_stats fq_stats(frame_queue *q);
void fq_destroy(frame_queue *q);
#endif
