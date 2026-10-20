/* Bounded, passive MJPEG/YUYV observation. Build with the vendored libuvc 0.0.6.
 * Frame timestamps are CLOCK_MONOTONIC at libuvc frame completion, NOT exposure.
 * libuvc's sequence / UVC PTS / SCR are copied together under cb_mutex.
 * No robot interface, kernel module, driver detachment or USB reset is used.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <malloc.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#include <libusb.h>
#include <libuvc/libuvc.h>
#include <libuvc/libuvc_internal.h>

/* Vendored implementation exports these without public header declarations. */
void _uvc_swap_buffers(uvc_stream_handle_t *);
uvc_frame_desc_t *uvc_find_frame_desc(uvc_device_handle_t *, uint16_t, uint16_t);
uvc_error_t uvc_query_stream_ctrl(uvc_device_handle_t *, uvc_stream_ctrl_t *, uint8_t, enum uvc_req_code);

#define CAMERAS 3
#define DEPTH 8
#define MAX_FRAME (6U*1024U*1024U)
#define HEADER_BYTES 40
#define NS UINT64_C(1000000000)

typedef struct {
    const char *name, *magic;
    unsigned width, height;
    enum uvc_frame_format uvc_format;
    uint32_t exact_bytes, capacity;
} Profile;
static const Profile mjpeg_profile = {"mjpeg", "FJM1", 1024, 768, UVC_FRAME_FORMAT_MJPEG, 0, 2U*1024U*1024U};
static const Profile yuyv_profile = {"yuyv", "FJY1", 640, 480, UVC_FRAME_FORMAT_YUYV, 640U*480U*2U, 640U*480U*2U};
static const Profile mjpeg_1280 = {"mjpeg-1280x1024", "FJM1", 1280, 1024, UVC_FRAME_FORMAT_MJPEG, 0, 4U*1024U*1024U};
static const Profile mjpeg_1920 = {"mjpeg-1920x1080", "FJM1", 1920, 1080, UVC_FRAME_FORMAT_MJPEG, 0, MAX_FRAME};
static const Profile yuyv_320 = {"yuyv-320x240", "FJY1", 320, 240, UVC_FRAME_FORMAT_YUYV, 320U*240U*2U, 320U*240U*2U};
static const Profile *camera_profiles[CAMERAS] = {&mjpeg_profile, &mjpeg_profile, &mjpeg_profile};

static const Profile *find_profile(const char *name) {
    if (!strcmp(name, "mjpeg")) return &mjpeg_profile;
    if (!strcmp(name, "yuyv")) return &yuyv_profile;
    if (!strcmp(name, "mjpeg-1280x1024")) return &mjpeg_1280;
    if (!strcmp(name, "mjpeg-1920x1080")) return &mjpeg_1920;
    if (!strcmp(name, "yuyv-320x240")) return &yuyv_320;
    return NULL;
}
static int select_profiles(const char *text) {
    unsigned i;
    if (!strchr(text, ',')) {
        const Profile *selected = find_profile(text);
        if (!selected) return 0;
        for (i = 0; i < CAMERAS; ++i) camera_profiles[i] = selected;
        return 1;
    } else {
        char storage[128], *remaining, *part;
        if (strlen(text) >= sizeof(storage)) return 0;
        strcpy(storage, text); remaining = storage;
        for (i = 0; i < CAMERAS; ++i) {
            part = strsep(&remaining, ",");
            if (!part || !(camera_profiles[i] = find_profile(part))) return 0;
        }
        return remaining == NULL;
    }
}
static int format_guid_matches(const Profile *selected, const uint8_t guid[16]) {
    static const uint8_t yuy2_guid[16] = {
        'Y','U','Y','2',0,0,0x10,0,0x80,0,0,0xaa,0,0x38,0x9b,0x71
    };
    return selected->uvc_format == UVC_FRAME_FORMAT_YUYV ?
        !memcmp(guid, yuy2_guid, sizeof(yuy2_guid)) : !memcmp(guid, "MJPG", 4);
}

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

typedef struct {
    unsigned char *data;
    uint32_t bytes, seq, pts, scr;
    uint64_t stamp;
} Frame;
typedef struct {
    Frame frame[DEPTH];
    unsigned head, count, high_water;
    uint64_t captured, sent, shutdown_dropped, gaps;
} Queue;
typedef struct {
    int cpu;
    long pid;
    uint64_t msw, csw, pf, process_start;
} Rt;
typedef struct {
    unsigned index;
    uvc_device_handle_t *device;
    uvc_stream_handle_t *stream;
    uint64_t last_frame, rate_start;
    uint32_t previous;
    unsigned rate_frames;
    int have_frame;
    uint64_t usb_callbacks, usb_bytes, usb_errors;
    int usb_status;
} Camera;

static Queue queues[CAMERAS];
static Camera cameras[CAMERAS];
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static volatile sig_atomic_t interrupted;
static int failed, capture_done, producers_done;
static char failure[192];

static uint64_t now_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) abort();
    return (uint64_t)t.tv_sec*NS+(uint64_t)t.tv_nsec;
}
static void request_stop(int sig) { (void)sig; interrupted = 1; }
static void fail(const char *reason) {
    pthread_mutex_lock(&lock);
    if (!failed) {
        failed = 1;
        snprintf(failure, sizeof(failure), "%s", reason);
        fprintf(stderr, "%s\n", failure);
    }
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
}
static int stopping(void) {
    int done;
    pthread_mutex_lock(&lock);
    done = failed || capture_done;
    pthread_mutex_unlock(&lock);
    return interrupted || done;
}
static int uint_value(const char *text, uint64_t *out) {
    char *end;
    unsigned long long n;
    if (!text[0] || text[0] < '0' || text[0] > '9') return -1;
    errno = 0;
    n = strtoull(text, &end, 10);
    if (errno || *end) return -1;
    *out = (uint64_t)n;
    return 0;
}

/* Reject missing/duplicate RT tasks and malformed or negative counters. */
static int parse_rt(FILE *file, Rt state[2]) {
    char line[1024];
    unsigned seen = 0;
    while (fgets(line, sizeof(line), file)) {
        int cpu, index;
        long pid;
        char msw[32], csw[32], pf[32], stat[32], usage[32], name[64], extra[2];
        int target = strstr(line, " RTPSI") != NULL || strstr(line, " FX_RtTask") != NULL;
        int fields = sscanf(line, "%d %ld %31s %31s %31s %31s %31s %63s %1s",
                            &cpu, &pid, msw, csw, pf, stat, usage, name, extra);
        if (fields < 8) { if (target) return -1; else continue; }
        index = !strcmp(name, "RTPSI") ? 0 : !strcmp(name, "FX_RtTask") ? 1 : -1;
        if (index < 0) { if (target) return -1; else continue; }
        if (fields != 8 || cpu < 0 || pid < 0 || (seen & (1U << index))) return -1;
        memset(&state[index], 0, sizeof(Rt));
        state[index].cpu = cpu;
        state[index].pid = pid;
        if (uint_value(msw, &state[index].msw) || uint_value(csw, &state[index].csw) ||
            uint_value(pf, &state[index].pf)) return -1;
        seen |= 1U << index;
    }
    return ferror(file) || seen != 3 ? -1 : 0;
}
static int process_start(long pid, uint64_t *start) {
    char path[80], line[4096], *token, *rest, *end;
    int field = 3;
    FILE *file;
    snprintf(path, sizeof(path), "/proc/%ld/stat", pid);
    file = fopen(path, "r");
    if (!file) return -1;
    if (!fgets(line, sizeof(line), file)) { fclose(file); return -1; }
    fclose(file);
    end = strrchr(line, ')');
    if (!end) return -1;
    for (token = strtok_r(end+1, " \n", &rest); token;
         token = strtok_r(NULL, " \n", &rest), ++field)
        if (field == 22) return uint_value(token, start);
    return -1;
}
static int read_rt(Rt state[2]) {
    FILE *file = fopen("/proc/xenomai/stat", "r");
    int result;
    if (!file) return -1;
    result = parse_rt(file, state);
    fclose(file);
    if (result || state[0].pid <= 0) return -1;
    return process_start(state[0].pid, &state[0].process_start);
}
static const char *check_rt(const Rt before[2], const Rt after[2]) {
    unsigned i;
    for (i = 0; i < 2; ++i) {
        if (before[i].pid != after[i].pid || before[i].cpu != after[i].cpu ||
            before[i].process_start != after[i].process_start)
            return "RT task identity changed; stop";
        if (after[i].msw < before[i].msw || after[i].csw < before[i].csw ||
            after[i].pf < before[i].pf)
            return "RT counters regressed; stop";
        if (after[i].pf != before[i].pf)
            return "RT page-fault counter changed; stop";
    }
    if (after[1].msw != before[1].msw)
        return "FX_RtTask mode-switch counter changed; stop";
    /* RTPSI handles online commands: retain and report its real MSW increment.
     * This exception is exclusive to this teleop helper, never stationary mode. */
    return NULL;
}

static void le32(unsigned char *p, uint32_t n) {
    unsigned i; for (i = 0; i < 4; ++i) p[i] = (unsigned char)(n >> (8*i));
}
static void header(unsigned char out[HEADER_BYTES], unsigned camera, const Frame *frame,
                   const Profile *selected) {
    memcpy(out, selected->magic, 4);
    le32(out+4, camera); le32(out+8, frame->seq);
    le32(out+12, (uint32_t)frame->stamp); le32(out+16, (uint32_t)(frame->stamp >> 32));
    le32(out+20, frame->bytes); le32(out+24, selected->width); le32(out+28, selected->height);
    le32(out+32, frame->pts); le32(out+36, frame->scr);
}
/* Header validation without JPEG decompression on the robot controller. */
static int jpeg_valid_profile(const unsigned char *data, size_t size, const Profile *selected) {
    size_t pos = 2, jpeg_size = size;
    int dimensions = 0;
    if (size < 128 || size > selected->capacity || data[0] != 0xff || data[1] != 0xd8) return 0;
    /* These cameras append zero padding after a complete JPEG. The payload
     * already has a strict per-profile bound; inspect its nonzero suffix without
     * guessing a device alignment. Preserve every raw byte for sending/hash.
     * Partial JPEGs and nonzero trailers still fail SOI/SOF/EOI validation. */
    while (jpeg_size > 2 && data[jpeg_size-1] == 0) --jpeg_size;
    if (jpeg_size < 4 || data[jpeg_size-2] != 0xff || data[jpeg_size-1] != 0xd9) return 0;
    size = jpeg_size;
    while (pos+1 < size) {
        unsigned marker, length;
        if (data[pos++] != 0xff) return 0;
        while (pos < size && data[pos] == 0xff) ++pos;
        if (pos >= size) return 0;
        marker = data[pos++];
        if (marker == 0xda) return dimensions;
        if (marker == 0xd9 || marker == 0 || marker == 0xd8) return 0;
        if (marker == 1 || (marker >= 0xd0 && marker <= 0xd7)) continue;
        if (pos+2 > size) return 0;
        length = (unsigned)data[pos]*256U+data[pos+1];
        if (length < 2 || pos+length > size) return 0;
        if (marker >= 0xc0 && marker <= 0xcf && marker != 0xc4 && marker != 0xc8 && marker != 0xcc) {
            if (length < 8 || data[pos+3]*256U+data[pos+4] != selected->height ||
                data[pos+5]*256U+data[pos+6] != selected->width) return 0;
            dimensions = 1;
        }
        pos += length;
    }
    return 0;
}
static int jpeg_valid(const unsigned char *data, size_t size) {
    return jpeg_valid_profile(data, size, &mjpeg_profile);
}
static int frame_valid(const unsigned char *data, size_t size, const Profile *selected) {
    if (selected->uvc_format == UVC_FRAME_FORMAT_YUYV)
        return size == selected->exact_bytes; /* Packed Y0 U Y1 V: no JPEG padding. */
    return jpeg_valid_profile(data, size, selected);
}
/* Caller holds lock. In-flight sender slot stays counted and cannot be reused. */
static Frame *reserve(Queue *q) {
    if (q->count == DEPTH) return NULL;
    return &q->frame[(q->head+q->count) % DEPTH];
}
static void publish(Queue *q) {
    ++q->count; ++q->captured;
    if (q->count > q->high_water) q->high_water = q->count;
}
static void consume(Queue *q) {
    q->head = (q->head+1) % DEPTH; --q->count; ++q->sent;
}
/* Bounded counters diagnose a missing first frame without printing per packet. */
void fusion_uvc_transfer_observed(struct libusb_transfer *transfer) {
    unsigned i;
    uint64_t bytes = 0, errors = 0;
    if (transfer->num_iso_packets) {
        int n;
        for (n = 0; n < transfer->num_iso_packets; ++n) {
            bytes += transfer->iso_packet_desc[n].actual_length;
            errors += transfer->iso_packet_desc[n].status != LIBUSB_TRANSFER_COMPLETED;
        }
    } else bytes = transfer->actual_length;
    pthread_mutex_lock(&lock);
    for (i = 0; i < CAMERAS; ++i) if (cameras[i].stream == transfer->user_data) {
        cameras[i].usb_callbacks++;
        cameras[i].usb_bytes += bytes;
        cameras[i].usb_errors += errors;
        cameras[i].usb_status = transfer->status;
        break;
    }
    pthread_mutex_unlock(&lock);
}

static void report_camera_timeout(Camera *camera, const char *phase) {
    pthread_mutex_lock(&lock);
    fprintf(stderr, "{\"camera_timeout\":%u,\"phase\":\"%s\",\"captured\":%" PRIu64
            ",\"usb_callbacks\":%" PRIu64 ",\"usb_bytes\":%" PRIu64
            ",\"usb_packet_errors\":%" PRIu64 ",\"last_usb_status\":%d}\n",
            camera->index, phase, queues[camera->index].captured, camera->usb_callbacks,
            camera->usb_bytes, camera->usb_errors, camera->usb_status);
    pthread_mutex_unlock(&lock);
}

/* Called synchronously by our vendored libuvc, with cb_mutex held. Copy to
 * bounded storage before the next USB completion can replace the hold buffer.
 * Never wait for transport or encode images on the USB completion thread. */
void fusion_uvc_frame_complete(uvc_stream_handle_t *stream) {
    Camera *camera = NULL;
    Queue *q;
    Frame *slot;
    uint64_t stamp = now_ns();
    uint32_t seq = stream->hold_seq;
    const char *error = NULL;
    unsigned i;
    if (stopping()) return;
    pthread_mutex_lock(&lock);
    for (i = 0; i < CAMERAS; ++i)
        if (cameras[i].stream == stream) { camera = &cameras[i]; break; }
    pthread_mutex_unlock(&lock);
    if (!camera) { fail("Unknown completed camera stream; stop"); return; }
    q = &queues[camera->index];
    if (!camera->have_frame) camera->rate_start = stamp;
    else ++camera->rate_frames;
    if (camera->rate_frames >= 30 && stamp-camera->rate_start < UINT64_C(900000000)) {
        error = "Camera exceeds negotiated 30 fps (30 intervals below 900 ms); stop";
    } else if (camera->have_frame && (uint32_t)(seq-camera->previous) != 1U) {
        fprintf(stderr, "{\"sequence_error_camera\":%u,\"previous\":%u,\"current\":%u,"
                "\"completion_interval_ns\":%" PRIu64 "}\n", camera->index, camera->previous, seq,
                stamp-camera->last_frame);
        pthread_mutex_lock(&lock);
        q->gaps += 1;
        pthread_mutex_unlock(&lock);
        error = "Camera source sequence gap/regression; stop";
    } else if (!frame_valid(stream->holdbuf, stream->hold_bytes, camera_profiles[camera->index])) {
        fprintf(stderr, "{\"invalid_camera\":%u,\"seq\":%u,\"bytes\":%zu}\n",
                camera->index, seq, stream->hold_bytes);
        error = camera_profiles[camera->index]->uvc_format == UVC_FRAME_FORMAT_YUYV ?
            "Invalid YUYV frame length; stop" :
            "Invalid MJPEG frame or dimensions; stop";
    } else {
        pthread_mutex_lock(&lock);
        slot = reserve(q);
        if (!slot) error = "Camera queue full; stop without silently dropping frames";
        else {
            slot->bytes = (uint32_t)stream->hold_bytes;
            slot->seq = seq; slot->pts = stream->hold_pts; slot->scr = stream->hold_last_scr;
            slot->stamp = stamp;
            memcpy(slot->data, stream->holdbuf, slot->bytes);
            publish(q);
            pthread_cond_signal(&changed);
        }
        pthread_mutex_unlock(&lock);
        if (!camera->have_frame)
            fprintf(stderr, "{\"camera\":%u,\"first_source_sequence\":%u}\n", camera->index, seq);
    }
    camera->previous = seq; camera->have_frame = 1;
    pthread_mutex_lock(&lock); camera->last_frame = stamp; pthread_mutex_unlock(&lock);
    if (camera->rate_frames >= 30) { camera->rate_start = stamp; camera->rate_frames = 0; }
    if (error) fail(error);
}
/* Return 1 for our normal SIGTERM, -1 for a real transport failure. */
static int write_part(const void *bytes, size_t length, uint64_t deadline) {
    const unsigned char *p = bytes;
    while (length) {
        ssize_t count;
        struct pollfd fd = {STDOUT_FILENO, POLLOUT, 0};
        int ready;
        if (interrupted) return 1;
        if (now_ns() >= deadline) return -1;
        ready = poll(&fd, 1, 20);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0 || (ready && (fd.revents & (POLLERR|POLLHUP|POLLNVAL)))) return -1;
        if (!ready) continue;
        count = write(STDOUT_FILENO, p, length);
        if (count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (count <= 0) return -1;
        p += count; length -= (size_t)count;
    }
    return 0;
}
static void *send_frames(void *unused) {
    unsigned next = 0;
    (void)unused;
    while (1) {
        unsigned i, selected = CAMERAS;
        Frame *frame;
        unsigned char bytes[HEADER_BYTES];
        int result;
        uint64_t deadline;
        pthread_mutex_lock(&lock);
        if (failed || interrupted) { pthread_mutex_unlock(&lock); break; }
        for (i = 0; i < CAMERAS; ++i)
            if (queues[(next+i) % CAMERAS].count) { selected = (next+i) % CAMERAS; break; }
        if (selected == CAMERAS) {
            if (producers_done) { pthread_mutex_unlock(&lock); break; }
            pthread_cond_wait(&changed, &lock);
            pthread_mutex_unlock(&lock);
            continue;
        }
        frame = &queues[selected].frame[queues[selected].head];
        pthread_mutex_unlock(&lock);
        header(bytes, selected, frame, camera_profiles[selected]);
        deadline = now_ns()+NS;
        result = write_part(bytes, sizeof(bytes), deadline);
        if (!result) result = write_part(frame->data, frame->bytes, deadline);
        if (result) {
            if (result < 0) fail("Frame pipe closed or blocked for 1 second; stop");
            break;
        }
        pthread_mutex_lock(&lock);
        consume(&queues[selected]);
        pthread_mutex_unlock(&lock);
        next = (selected+1) % CAMERAS;
    }
    return NULL;
}
static void report(const char *event, uint64_t rtpsi_delta) {
    pthread_mutex_lock(&lock);
    fprintf(stderr, "{\"event\":\"%s\",\"captured\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64
            "],\"sent\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 "],\"pending\":[%u,%u,%u],"
            "\"shutdown_dropped\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 "],\"rtpsi_msw_delta\":%" PRIu64 "}\n",
            event, queues[0].captured, queues[1].captured, queues[2].captured,
            queues[0].sent, queues[1].sent, queues[2].sent,
            queues[0].count, queues[1].count, queues[2].count,
            queues[0].shutdown_dropped, queues[1].shutdown_dropped, queues[2].shutdown_dropped,
            rtpsi_delta);
    pthread_mutex_unlock(&lock);
}
static int open_camera(uvc_context_t *context, Camera *camera, int address) {
    const Profile *profile = camera_profiles[camera->index];
    const int vids[CAMERAS] = {0x0bda, 0x32e4, 0x32e4};
    const int pids[CAMERAS] = {0x5883, 0x9230, 0x9230};
    uvc_device_t **devices = NULL, *chosen = NULL;
    uvc_stream_ctrl_t ctrl;
    uvc_stream_handle_t *opened = NULL;
    uvc_frame_desc_t *description;
    int result, i;
    result = uvc_get_device_list(context, &devices);
    if (result < 0) return result;
    for (i = 0; devices[i]; ++i) {
        uvc_device_descriptor_t *desc = NULL;
        if (uvc_get_bus_number(devices[i]) != 1 || uvc_get_device_address(devices[i]) != address) continue;
        result = uvc_get_device_descriptor(devices[i], &desc);
        if (result < 0) break;
        if (desc->idVendor == vids[camera->index] && desc->idProduct == pids[camera->index]) chosen = devices[i];
        uvc_free_device_descriptor(desc);
        break;
    }
    result = chosen ? uvc_open(chosen, &camera->device) : UVC_ERROR_NO_DEVICE;
    uvc_free_device_list(devices, 1);
    if (result < 0) return result;
    memset(&ctrl, 0, sizeof(ctrl));
    result = uvc_get_stream_ctrl_format_size(camera->device, &ctrl, profile->uvc_format,
                                             profile->width, profile->height, 30);
    if (result < 0) return result;
    /* libuvc 0.0.6's probe wrapper ignores transfer errors: verify explicitly. */
    ctrl.dwFrameInterval = 333333;
    ctrl.bmHint |= 1;
    result = uvc_query_stream_ctrl(camera->device, &ctrl, 1, UVC_SET_CUR);
    if (result < 0) return result;
    result = uvc_query_stream_ctrl(camera->device, &ctrl, 1, UVC_GET_CUR);
    if (result < 0) return result;
    description = uvc_find_frame_desc(camera->device, ctrl.bFormatIndex, ctrl.bFrameIndex);
    if (!description || description->wWidth != profile->width || description->wHeight != profile->height ||
        !format_guid_matches(profile, description->parent->guidFormat) || ctrl.dwFrameInterval != 333333 ||
        ctrl.dwMaxVideoFrameSize < 128 || ctrl.dwMaxVideoFrameSize > profile->capacity ||
        (profile->exact_bytes && ctrl.dwMaxVideoFrameSize < profile->exact_bytes) ||
        !ctrl.dwMaxPayloadTransferSize || ctrl.dwMaxPayloadTransferSize > 3072)
        return UVC_ERROR_INVALID_MODE;
    fprintf(stderr, "{\"camera\":%u,\"address\":%d,\"format\":\"%s\",\"width\":%u,\"height\":%u,"
            "\"interval_100ns\":%u,\"max_payload\":%u}\n", camera->index, address,
            profile->name, profile->width, profile->height, ctrl.dwFrameInterval, ctrl.dwMaxPayloadTransferSize);
    result = uvc_stream_open_ctrl(camera->device, &opened, &ctrl);
    if (result < 0) return result;
    pthread_mutex_lock(&lock); camera->stream = opened; pthread_mutex_unlock(&lock);
    if (!camera->stream->outbuf || !camera->stream->holdbuf) return UVC_ERROR_NO_MEM;
    camera->last_frame = now_ns();
    result = uvc_stream_start(camera->stream, NULL, NULL, 0);
    if (result < 0) return result;
    return camera->stream->frame_format == profile->uvc_format ? UVC_SUCCESS : UVC_ERROR_INVALID_MODE;
}

/* Camera enumeration/stream-start success does not imply first-frame readiness.
 * Retry only a stream that has never produced a frame, at most once. */
static int start_ready_camera(uvc_context_t *context, Camera *camera, int address, Rt previous[2]) {
    unsigned attempt;
    for (attempt = 0; attempt < 2 && !stopping(); ++attempt) {
        uint64_t deadline, monitor;
        int result = open_camera(context, camera, address);
        if (result < 0) {
            fprintf(stderr, "Camera %u UVC %d: %s\n", camera->index, result, uvc_strerror(result));
            fail("Camera open/start or negotiation failed"); return -1;
        }
        monitor = now_ns(); deadline = monitor + 4*NS;
        while (!stopping() && now_ns() < deadline) {
            int ready;
            pthread_mutex_lock(&lock); ready = queues[camera->index].captured > 0; pthread_mutex_unlock(&lock);
            if (ready) return 0;
            if (now_ns()-monitor >= NS) {
                Rt current[2];
                const char *error;
                if (read_rt(current)) { fail("RT tasks missing/unavailable; stop"); return -1; }
                error = check_rt(previous, current);
                if (error) { fail(error); return -1; }
                memcpy(previous, current, sizeof(current)); monitor = now_ns();
            }
            usleep(10000);
        }
        if (stopping()) return -1;
        report_camera_timeout(camera, "first_frame");
        /* Closing this UVC stream cancels its transfers; no USB reset or driver detach. */
        if (camera->stream) {
            uvc_stream_close(camera->stream);
            pthread_mutex_lock(&lock); camera->stream = NULL; pthread_mutex_unlock(&lock);
        }
        if (camera->device) { uvc_close(camera->device); camera->device = NULL; }
        /* A frame may complete just before close. Never restart its sequence
         * after such a frame was already published. */
        pthread_mutex_lock(&lock);
        result = queues[camera->index].captured != 0;
        pthread_mutex_unlock(&lock);
        if (result) break;
        if (!attempt) {
            fprintf(stderr, "{\"camera_start_retry\":%u,\"attempt\":2,\"max_attempts\":2}\n", camera->index);
            usleep(200000);
        }
    }
    if (!stopping()) fail("Camera did not produce first frame after bounded startup retry; stop");
    return -1;
}

static void report_rt(const char *event, const Rt state[2]) {
    unsigned i;
    for (i = 0; i < 2; ++i)
        fprintf(stderr, "{\"event\":\"%s\",\"task\":\"%s\",\"cpu\":%d,\"pid\":%ld,"
                "\"msw\":%" PRIu64 ",\"csw\":%" PRIu64 ",\"pf\":%" PRIu64
                ",\"process_start_ticks\":%" PRIu64 "}\n", event, i ? "FX_RtTask" : "RTPSI",
                state[i].cpu, state[i].pid, state[i].msw, state[i].csw, state[i].pf,
                state[i].process_start);
}

/* Self-test deliberately executes before any /proc, resource limit or USB access. */
#define REQUIRE(test) do { if (!(test)) { fprintf(stderr, "Self-test failed line %d: %s\n", __LINE__, #test); return 1; } } while (0)
static int self_test(void) {
    const char *text = "CPU PID MSW CSW PF STAT %CPU NAME\n"
        "1 2033 23107 5003585 0 00300184 0.4 RTPSI\n"
        "1 0 0 4979699 0 00100084 5.3 FX_RtTask\n";
    Rt baseline[2], state[2];
    Queue queue = {0};
    Frame frame = {0};
    unsigned char bytes[HEADER_BYTES], jpeg[128] = {0}, padded[143], original[143];
    const unsigned padding_sizes[] = {0, 1, 2, 3, 4, 6, 15};
    FILE *file = fmemopen((void *)text, strlen(text), "r");
    unsigned i;
    int pipefd[2], saved_stdout;
    REQUIRE(file != NULL); REQUIRE(parse_rt(file, baseline) == 0); fclose(file);
    memcpy(state, baseline, sizeof(state)); state[0].msw += 206;
    REQUIRE(check_rt(baseline, state) == NULL);
    state[1].msw = 1; REQUIRE(check_rt(baseline, state) != NULL); state[1].msw = 0;
    state[0].pf = 1; REQUIRE(check_rt(baseline, state) != NULL); state[0].pf = 0;
    state[1].pf = 1; REQUIRE(check_rt(baseline, state) != NULL); state[1].pf = 0;
    state[0].pid++; REQUIRE(check_rt(baseline, state) != NULL); state[0].pid--;
    state[0].csw = 1; REQUIRE(check_rt(baseline, state) != NULL);
    text = "1 2033 0 1 0 0 0 RTPSI\n";
    file = fmemopen((void *)text, strlen(text), "r");
    REQUIRE(file != NULL); REQUIRE(parse_rt(file, state) != 0); fclose(file);
    text = "1 2033 0 1 0 0 0 RTPSI\n1 2033 0 1 0 0 0 RTPSI\n1 0 0 1 0 0 0 FX_RtTask\n";
    file = fmemopen((void *)text, strlen(text), "r");
    REQUIRE(file != NULL); REQUIRE(parse_rt(file, state) != 0); fclose(file);
    text = "1 2033 -1 1 0 0 0 RTPSI\n1 0 0 1 0 0 0 FX_RtTask\n";
    file = fmemopen((void *)text, strlen(text), "r");
    REQUIRE(file != NULL); REQUIRE(parse_rt(file, state) != 0); fclose(file);
    for (i = 0; i < DEPTH; ++i) { Frame *slot = reserve(&queue); REQUIRE(slot); slot->seq = i; publish(&queue); }
    REQUIRE(reserve(&queue) == NULL);
    for (i = 0; i < DEPTH; ++i) { REQUIRE(queue.frame[queue.head].seq == i); consume(&queue); }
    REQUIRE(queue.count == 0 && queue.sent == DEPTH);
    for (i = 0; i < DEPTH*2; ++i) { reserve(&queue)->seq = i; publish(&queue); REQUIRE(queue.frame[queue.head].seq == i); consume(&queue); }
    frame.seq = 0x78563412; frame.stamp = UINT64_C(0x1122334455667788);
    frame.bytes = 128; frame.pts = 0x98765432; frame.scr = 0xabcdef01;
    header(bytes, 2, &frame, &mjpeg_profile);
    REQUIRE(!memcmp(bytes, "FJM1\2\0\0\0\x12\x34\x56\x78\x88\x77\x66\x55\x44\x33\x22\x11", 20));
    REQUIRE(bytes[20] == 128 && bytes[24] == 0 && bytes[25] == 4 && bytes[28] == 0 && bytes[29] == 3);
    REQUIRE(bytes[32] == 0x32 && bytes[35] == 0x98 && bytes[36] == 1 && bytes[39] == 0xab);
    REQUIRE(find_profile("mjpeg") == &mjpeg_profile && find_profile("yuyv") == &yuyv_profile);
    REQUIRE(find_profile("rgb") == NULL);
    {
        uint8_t guid[16] = {'Y','U','Y','2',0,0,0x10,0,0x80,0,0,0xaa,0,0x38,0x9b,0x71};
        REQUIRE(format_guid_matches(&yuyv_profile, guid));
        REQUIRE(!format_guid_matches(&mjpeg_profile, guid));
        guid[15] ^= 1;
        REQUIRE(!format_guid_matches(&yuyv_profile, guid));
        memcpy(guid, "MJPG", 4);
        REQUIRE(format_guid_matches(&mjpeg_profile, guid));
        REQUIRE(!format_guid_matches(&yuyv_profile, guid));
    }
    frame.bytes = yuyv_profile.exact_bytes;
    header(bytes, 2, &frame, &yuyv_profile);
    REQUIRE(!memcmp(bytes, "FJY1", 4));
    REQUIRE(bytes[20] == 0 && bytes[21] == 0x60 && bytes[22] == 9 && bytes[23] == 0);
    REQUIRE(bytes[24] == 0x80 && bytes[25] == 2 && bytes[28] == 0xe0 && bytes[29] == 1);
    REQUIRE(frame_valid(NULL, 614400, &yuyv_profile));
    REQUIRE(!frame_valid(NULL, 614399, &yuyv_profile));
    REQUIRE(!frame_valid(NULL, 614401, &yuyv_profile));
    REQUIRE(!frame_valid(NULL, MAX_FRAME+1U, &yuyv_profile));
    memcpy(jpeg, "\xff\xd8\xff\xc0\0\x08\x08\x03\0\x04\0\x01\xff\xda", 14);
    jpeg[126] = 0xff; jpeg[127] = 0xd9;
    REQUIRE(jpeg_valid(jpeg, sizeof(jpeg))); jpeg[7] = 2;
    REQUIRE(!jpeg_valid(jpeg, sizeof(jpeg)));
    jpeg[7] = 3;
    for (i = 0; i < sizeof(padding_sizes)/sizeof(padding_sizes[0]); ++i) {
        size_t total = sizeof(jpeg)+padding_sizes[i];
        memcpy(padded, jpeg, sizeof(jpeg));
        memset(padded+sizeof(jpeg), 0, padding_sizes[i]);
        memcpy(original, padded, total);
        REQUIRE(jpeg_valid(padded, total));
        REQUIRE(!memcmp(padded, original, total));
        padded[total-1] = 1;
        REQUIRE(!jpeg_valid(padded, total));
    }
    memcpy(padded, jpeg, sizeof(jpeg)); memset(padded+sizeof(jpeg), 0, 15);
    padded[127] = 0;
    REQUIRE(!jpeg_valid(padded, sizeof(padded))); /* Missing EOI is still invalid. */
    padded[127] = 0xd9; padded[7] = 2;
    REQUIRE(!jpeg_valid(padded, sizeof(padded))); /* Padding cannot excuse bad dimensions. */
    REQUIRE(!jpeg_valid(jpeg, MAX_FRAME+1U)); /* Rejected before payload inspection. */
    {
        /* Two completions without any polling opportunity must both survive. */
        uvc_stream_handle_t stream;
        unsigned char first[128], second[128];
        memset(&stream, 0, sizeof(stream));
        pthread_mutex_init(&stream.cb_mutex, NULL);
        pthread_cond_init(&stream.cb_cond, NULL);
        memcpy(first, jpeg, sizeof(jpeg)); memcpy(second, jpeg, sizeof(jpeg));
        stream.outbuf = first; stream.holdbuf = second; stream.seq = UINT32_MAX;
        cameras[0].stream = &stream;
        for (i = 0; i < DEPTH; ++i) {
            queues[0].frame[i].data = malloc(128); REQUIRE(queues[0].frame[i].data);
        }
        for (i = 0; i < DEPTH; ++i) {
            stream.got_bytes = sizeof(jpeg); stream.pts = 100+i; stream.last_scr = 200+i;
            _uvc_swap_buffers(&stream);
        }
        REQUIRE(!failed && queues[0].count == DEPTH);
        for (i = 0; i < DEPTH; ++i) {
            Frame *got = &queues[0].frame[i];
            REQUIRE(got->seq == (uint32_t)(UINT32_MAX+i));
            REQUIRE(got->pts == 100+i && got->scr == 200+i);
            REQUIRE(got->stamp > 0 && got->bytes == sizeof(jpeg));
            REQUIRE(!memcmp(got->data, jpeg, sizeof(jpeg)));
        }
        stream.got_bytes = sizeof(jpeg); _uvc_swap_buffers(&stream);
        REQUIRE(failed && !strcmp(failure, "Camera queue full; stop without silently dropping frames"));
        REQUIRE(queues[0].count == DEPTH);
        for (i = 0; i < DEPTH; ++i) free(queues[0].frame[i].data);
        memset(&queues[0], 0, sizeof(queues[0]));
        memset(&cameras[0], 0, sizeof(cameras[0])); failed = 0; failure[0] = 0;
        pthread_cond_destroy(&stream.cb_cond); pthread_mutex_destroy(&stream.cb_mutex);
    }
    REQUIRE(pipe(pipefd) == 0);
    REQUIRE(fcntl(pipefd[1], F_SETFL, O_NONBLOCK) == 0);
    while (write(pipefd[1], jpeg, sizeof(jpeg)) > 0) {}
    REQUIRE(errno == EAGAIN || errno == EWOULDBLOCK);
    saved_stdout = dup(STDOUT_FILENO); REQUIRE(saved_stdout >= 0);
    REQUIRE(dup2(pipefd[1], STDOUT_FILENO) >= 0);
    REQUIRE(write_part(jpeg, sizeof(jpeg), now_ns()+UINT64_C(10000000)) == -1);
    interrupted = 1;
    REQUIRE(write_part(jpeg, sizeof(jpeg), now_ns()+NS) == 1);
    interrupted = 0;
    REQUIRE(dup2(saved_stdout, STDOUT_FILENO) >= 0);
    close(saved_stdout); close(pipefd[0]); close(pipefd[1]);
    fprintf(stderr, "{\"self_test\":\"passed\",\"usb_opened\":false,\"checks\":[\"completion_hook_burst_fifo_wrap_overflow\",\"rt_rules\",\"missing_duplicate_malformed_task\",\"queue_full_fifo_wrap\",\"FJM1_FJY1_headers\",\"YUY2_GUID_and_exact_length\",\"JPEG_dimensions_and_zero_padding\",\"full_pipe_timeout_cancel\"]}\n");
    return 0;
}

int main(int argc, char **argv) {
    uint64_t numbers[4], started, monitor, reported, delta = 0;
    Rt baseline[2], previous[2], current[2];
    uvc_context_t *context = NULL;
    pthread_attr_t attr;
    pthread_t sender;
    int sender_started = 0, result = 1, i, j, flags;
    unsigned camera_mask = 7;
    const char *rt_error;
    struct rlimit memory = {256U*1024U*1024U, 256U*1024U*1024U};
    struct rlimit core = {0, 0};
    if (argc == 2 && !strcmp(argv[1], "--self-test")) return self_test();
    if (argc != 5 && argc != 6 && argc != 7) {
        fprintf(stderr, "Usage: %s seconds(1..600) bus1_addr1 bus1_addr2 bus1_addr4 [mjpeg|yuyv [probe_camera_index(0..2)|mask=1..7]] | --self-test\n", argv[0]);
        return 2;
    }
    if (argc >= 6 && !select_profiles(argv[5])) {
        fprintf(stderr, "Invalid camera format: expected supported profile or three comma-separated profiles\n");
        return 2;
    }
    if (argc == 7) {
        uint64_t probe;
        if (!strncmp(argv[6], "mask=", 5)) {
            if (uint_value(argv[6]+5, &probe) || probe < 1 || probe > 7) return 2;
            camera_mask = (unsigned)probe;
        } else {
            if (uint_value(argv[6], &probe) || probe > 2) return 2;
            camera_mask = 1U << probe;
        }
    }
    for (i = 0; i < 4; ++i) {
        if (uint_value(argv[i+1], &numbers[i]) || numbers[i] > (i ? 127U : 600U)) return 2;
        if ((!i || (camera_mask & (1U << (i-1)))) && numbers[i] < 1) return 2;
    }
    for (i = 0; i < CAMERAS; ++i) for (j = i+1; j < CAMERAS; ++j)
        if ((camera_mask & (1U << i)) && (camera_mask & (1U << j)) &&
            numbers[i+1] == numbers[j+1]) return 2;
    signal(SIGINT, request_stop); signal(SIGTERM, request_stop); signal(SIGPIPE, SIG_IGN);
    if (setrlimit(RLIMIT_AS, &memory) || setrlimit(RLIMIT_CORE, &core)) { perror("resource limits"); return 1; }
    mallopt(M_ARENA_MAX, 1);
    if (read_rt(baseline)) { fprintf(stderr, "RT tasks missing/unavailable; stop\n"); return 1; }
    report_rt("rt_baseline", baseline);
    memcpy(previous, baseline, sizeof(previous));
    flags = fcntl(STDOUT_FILENO, F_GETFL);
    if (flags < 0 || fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK)) return 1;
    if (pthread_attr_init(&attr) || pthread_attr_setstacksize(&attr, 1024U*1024U)) return 1;
    for (i = 0; i < CAMERAS; ++i) for (j = 0; j < DEPTH; ++j) {
        if (!(camera_mask & (1U << i))) continue;
        queues[i].frame[j].data = malloc(camera_profiles[i]->capacity);
        if (!queues[i].frame[j].data) { fail("Cannot allocate bounded frame queues"); goto cleanup; }
        memset(queues[i].frame[j].data, 0, camera_profiles[i]->capacity);
    }
    if (uvc_init(&context, NULL) < 0) { fail("Cannot initialize UVC context"); goto cleanup; }
    if (pthread_create(&sender, &attr, send_frames, NULL)) { fail("Cannot create sender thread"); goto cleanup; }
    sender_started = 1;
    for (i = 0; i < CAMERAS; ++i) {
        if (!(camera_mask & (1U << i))) continue;
        if (stopping()) goto cleanup;
        cameras[i].index = (unsigned)i;
        result = start_ready_camera(context, &cameras[i], (int)numbers[i+1], previous);
        if (result < 0) goto cleanup;

    }
    started = monitor = reported = now_ns();
    fprintf(stderr, "{\"pipeline\":\"teleop_queue_sender\",\"queue_depth_per_camera\":8,"
            "\"timestamp_kind\":\"controller_monotonic_frame_completion\"}\n");
    while (!stopping() && now_ns()-started < numbers[0]*NS) {
        uint64_t now = now_ns();
        for (i = 0; i < CAMERAS; ++i) {
            uint64_t last;
            if (!(camera_mask & (1U << i))) continue;
            pthread_mutex_lock(&lock); last = cameras[i].last_frame; pthread_mutex_unlock(&lock);
            if (now > last && now-last > 2*NS) { report_camera_timeout(&cameras[i], "streaming"); fail("Camera has no new frame for 2 seconds; stop"); break; }
        }
        if (now-monitor >= NS) {
            if (read_rt(current)) { fail("RT tasks missing/unavailable; stop"); break; }
            rt_error = check_rt(previous, current);
            if (rt_error) { fail(rt_error); break; }
            memcpy(previous, current, sizeof(previous));
            delta = current[0].msw-baseline[0].msw;
            monitor = now;
        }
        if (now-reported >= 5*NS) { report("teleop_health", delta); reported = now; }
        usleep(10000);
    }
cleanup:
    pthread_mutex_lock(&lock); capture_done = 1; pthread_cond_broadcast(&changed); pthread_mutex_unlock(&lock);
    /* Wait out any completion that entered before capture_done was set. */
    for (i = 0; i < CAMERAS; ++i) if (cameras[i].stream) {
        pthread_mutex_lock(&cameras[i].stream->cb_mutex);
        pthread_mutex_unlock(&cameras[i].stream->cb_mutex);
    }
    pthread_mutex_lock(&lock); producers_done = 1; pthread_cond_broadcast(&changed); pthread_mutex_unlock(&lock);
    if (sender_started) pthread_join(sender, NULL);
    for (i = CAMERAS-1; i >= 0; --i) {
        if (cameras[i].stream) uvc_stream_close(cameras[i].stream);
        if (cameras[i].device) uvc_close(cameras[i].device);
    }
    if (context) uvc_exit(context);
    if (read_rt(current)) fail("RT tasks missing/unavailable after cleanup; stop");
    else {
        report_rt("rt_final", current);
        rt_error = check_rt(previous, current);
        if (rt_error) fail(rt_error);
        if (current[0].msw >= baseline[0].msw) delta = current[0].msw-baseline[0].msw;
    }
    for (i = 0; i < CAMERAS; ++i) {
        if (!interrupted && (camera_mask & (1U << i)) && !queues[i].captured) fail("Missing camera frames at natural end");
        queues[i].shutdown_dropped = queues[i].count;
        queues[i].count = 0;
        for (j = 0; j < DEPTH; ++j) free(queues[i].frame[j].data);
    }
    report("teleop_final", delta);
    result = failed ? 1 : 0;
    fprintf(stderr, "{\"exit\":%d,\"normal_signal_stop\":%s}\n", result, interrupted ? "true" : "false");
    pthread_attr_destroy(&attr);
    return result;
}
