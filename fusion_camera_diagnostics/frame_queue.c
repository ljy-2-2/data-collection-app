#include "frame_queue.h"
#include <string.h>

int fq_init(frame_queue *q) {
  memset(q, 0, sizeof(*q));
  return pthread_mutex_init(&q->mutex, NULL);
}

int fq_push(frame_queue *q, unsigned id, const unsigned char header[40],
             const void *data, size_t data_bytes, uint64_t captured_ns) {
  if (id >= CAMERA_COUNT || !data_bytes || data_bytes > MAX_FRAME_BYTES) return -1;
  pthread_mutex_lock(&q->mutex);
  q->stats.captured[id]++;
  if (q->stats.pending[id] == QUEUE_DEPTH) {
    q->head[id] = (q->head[id] + 1) % QUEUE_DEPTH;
    q->stats.pending[id]--;
    q->stats.overflow_dropped[id]++;
  }
  unsigned tail = (q->head[id] + q->stats.pending[id]) % QUEUE_DEPTH;
  queued_frame *f = &q->slots[id][tail];
  memcpy(f->header, header, sizeof(f->header));
  memcpy(f->data, data, data_bytes);
  f->data_bytes = data_bytes;
  f->captured_ns = captured_ns;
  f->camera_id = id;
  q->stats.pending[id]++;
  if (q->stats.pending[id] > q->stats.high_water[id])
    q->stats.high_water[id] = q->stats.pending[id];
  pthread_mutex_unlock(&q->mutex);
  return 0;
}

int fq_pop(frame_queue *q, queued_frame *out) {
  pthread_mutex_lock(&q->mutex);
  for (unsigned n = 0; n < CAMERA_COUNT; n++) {
    unsigned id = (q->next_camera + n) % CAMERA_COUNT;
    if (!q->stats.pending[id]) continue;
    /* Copy under the lock so producers cannot overwrite an in-flight image.
     * The lock is released before ALL network/pipe operations. */
    queued_frame *f = &q->slots[id][q->head[id]];
    memcpy(out->header, f->header, sizeof(out->header));
    memcpy(out->data, f->data, f->data_bytes);
    out->data_bytes = f->data_bytes;
    out->captured_ns = f->captured_ns;
    out->camera_id = f->camera_id;
    q->head[id] = (q->head[id] + 1) % QUEUE_DEPTH;
    q->stats.pending[id]--;
    q->stats.in_flight[id]++;
    q->next_camera = (id + 1) % CAMERA_COUNT;
    pthread_mutex_unlock(&q->mutex);
    return 1;
  }
  pthread_mutex_unlock(&q->mutex);
  return 0;
}

void fq_sent(frame_queue *q, unsigned id, uint64_t queue_age_ns) {
  pthread_mutex_lock(&q->mutex);
  q->stats.in_flight[id]--;
  q->stats.sent[id]++;
  if (queue_age_ns > q->stats.max_queue_age_ns[id])
    q->stats.max_queue_age_ns[id] = queue_age_ns;
  pthread_mutex_unlock(&q->mutex);
}

void fq_fail(frame_queue *q, unsigned id) {
  pthread_mutex_lock(&q->mutex);
  q->stats.in_flight[id]--;
  q->stats.failed_frames[id]++;
  q->stats.send_failed = 1;
  pthread_mutex_unlock(&q->mutex);
}

void fq_finish(frame_queue *q, uint64_t drain_deadline_ns) {
  pthread_mutex_lock(&q->mutex);
  q->stats.producer_done = 1;
  q->stats.drain_deadline_ns = drain_deadline_ns;
  pthread_mutex_unlock(&q->mutex);
}

void fq_discard_pending(frame_queue *q) {
  pthread_mutex_lock(&q->mutex);
  for (unsigned id=0; id<CAMERA_COUNT; id++) {
    q->stats.shutdown_dropped[id] += q->stats.pending[id];
    q->stats.pending[id] = 0;
  }
  pthread_mutex_unlock(&q->mutex);
}

queue_stats fq_stats(frame_queue *q) {
  pthread_mutex_lock(&q->mutex);
  queue_stats copy = q->stats;
  pthread_mutex_unlock(&q->mutex);
  return copy;
}

void fq_destroy(frame_queue *q) { pthread_mutex_destroy(&q->mutex); }
