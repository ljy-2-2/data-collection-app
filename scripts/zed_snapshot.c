/* One ZED Mini stereo frame. Raw output is converted on the laptop. */
#define _POSIX_C_SOURCE 200809L
#include <libusb.h>
#include <libuvc/libuvc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Do not detach drivers, reset USB, or change device configuration. */
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
static double monotonic_seconds(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--check")) return 0;
    if (argc != 3) return 2;
    const size_t bytes = 3840U * 1080U * 2U;
    uvc_context_t *ctx = NULL;
    uvc_device_t **devices = NULL, *chosen = NULL;
    uvc_device_handle_t *device = NULL;
    uvc_stream_handle_t *stream = NULL;
    uvc_stream_ctrl_t ctrl;
    unsigned char *copy = NULL;
    uint32_t sequence = 0;
    int err = uvc_init(&ctx, NULL), captured = 0;
    if (err < 0) goto done;
    err = uvc_get_device_list(ctx, &devices);
    if (err < 0) goto done;
    for (int i = 0; devices[i]; ++i) {
        uvc_device_descriptor_t *desc = NULL;
        if (uvc_get_bus_number(devices[i]) != atoi(argv[1]) ||
            uvc_get_device_address(devices[i]) != atoi(argv[2])) continue;
        err = uvc_get_device_descriptor(devices[i], &desc);
        if (err < 0) goto done;
        if (desc->idVendor == 0x2b03 && desc->idProduct == 0xf682) chosen = devices[i];
        uvc_free_device_descriptor(desc);
    }
    err = chosen ? uvc_open(chosen, &device) : UVC_ERROR_NO_DEVICE;
    if (err < 0) goto done;
    memset(&ctrl, 0, sizeof(ctrl));
    err = uvc_get_stream_ctrl_format_size(device, &ctrl, UVC_FRAME_FORMAT_YUYV, 3840, 1080, 30);
    if (err < 0) goto done;
    const uvc_format_desc_t *format;
    const uvc_frame_desc_t *frame;
    int valid_mode = 0;
    for (format = uvc_get_format_descs(device); format; format = format->next)
        if (format->bFormatIndex == ctrl.bFormatIndex && !memcmp(format->guidFormat, "YUY2", 4))
            for (frame = format->frame_descs; frame; frame = frame->next)
                if (frame->bFrameIndex == ctrl.bFrameIndex && frame->wWidth == 3840 && frame->wHeight == 1080)
                    valid_mode = 1;
    if (!valid_mode || ctrl.dwFrameInterval != 333333 || ctrl.dwMaxVideoFrameSize < bytes ||
        ctrl.dwMaxVideoFrameSize > 32U * 1024U * 1024U) {
        err = UVC_ERROR_INVALID_MODE; goto done;
    }
    fprintf(stderr, "ZED Mini: negotiated 3840x1080 YUYV, 30 fps; warming up 10 frames\n");
    err = uvc_stream_open_ctrl(device, &stream, &ctrl);
    if (err < 0) goto done;
    err = uvc_stream_start(stream, NULL, NULL, 0);
    if (err < 0) goto done;
    double deadline = monotonic_seconds() + 8;
    int valid_frames = 0;
    while (monotonic_seconds() < deadline) {
        uvc_frame_t *f = NULL;
        err = uvc_stream_get_frame(stream, &f, 250000);
        if (err == UVC_ERROR_TIMEOUT) continue;
        if (err < 0) goto done;
        if (!f) continue;
        if (f->width != 3840 || f->height != 1080 || f->frame_format != UVC_FRAME_FORMAT_YUYV ||
            f->data_bytes != bytes || (f->step && f->step != 7680)) continue;
        if (++valid_frames <= 10) continue;
        copy = malloc(bytes);
        if (!copy) { err = UVC_ERROR_NO_MEM; goto done; }
        memcpy(copy, f->data, bytes);
        sequence = f->sequence;
        captured = 1;
        break;
    }
    if (!captured) err = UVC_ERROR_TIMEOUT;
done:
    if (stream) uvc_stream_close(stream);
    if (device) uvc_close(device);
    if (devices) uvc_free_device_list(devices, 1);
    if (ctx) uvc_exit(ctx);
    if (!captured) {
        fprintf(stderr, "ZED snapshot failed: %s (%d)\n", uvc_strerror(err), err);
        free(copy); return 1;
    }
    /* Transfer only after the camera has been closed. */
    unsigned char header[8] = {'Z','E','D','1',0,0,0,0};
    for (int i = 0; i < 4; ++i) header[4+i] = (sequence >> (8*i)) & 255;
    int ok = fwrite(header, 1, 8, stdout) == 8 && fwrite(copy, 1, bytes, stdout) == bytes;
    free(copy);
    return ok && fflush(stdout) == 0 ? 0 : 1;
}
