/* Bounded JPEG encoding on the sender thread, never the USB callback.
 * The camera declares BT.601; limited YUV range is an explicit assumption.
 * Load the installed JPEG-8 ABI without replacing any system library. */
#include <dlfcn.h>
#include <setjmp.h>
#include <jpeglib.h>

typedef struct {
    void *library;
    __typeof__(&jpeg_std_error) std_error;
    __typeof__(&jpeg_CreateCompress) create;
    __typeof__(&jpeg_destroy_compress) destroy;
    __typeof__(&jpeg_mem_dest) destination;
    __typeof__(&jpeg_set_defaults) defaults;
    __typeof__(&jpeg_set_quality) quality;
    __typeof__(&jpeg_start_compress) start;
    __typeof__(&jpeg_write_raw_data) write;
    __typeof__(&jpeg_finish_compress) finish;
} ZedJpegApi;
static ZedJpegApi zed_jpeg_api;
static int zed_jpeg_available(void) {
    if (zed_jpeg_api.library) return 1;
    void *lib = dlopen("libjpeg.so.8", RTLD_NOW | RTLD_LOCAL);
    if (!lib) { fprintf(stderr, "ZED requires installed libjpeg.so.8: %s\n", dlerror()); return 0; }
#define ZED_JPEG_SYMBOL(field, symbol) do { \
    *(void **)(&zed_jpeg_api.field) = dlsym(lib, symbol); \
    if (!zed_jpeg_api.field) { dlclose(lib); return 0; } \
} while (0)
    ZED_JPEG_SYMBOL(std_error, "jpeg_std_error");
    ZED_JPEG_SYMBOL(create, "jpeg_CreateCompress");
    ZED_JPEG_SYMBOL(destroy, "jpeg_destroy_compress");
    ZED_JPEG_SYMBOL(destination, "jpeg_mem_dest");
    ZED_JPEG_SYMBOL(defaults, "jpeg_set_defaults");
    ZED_JPEG_SYMBOL(quality, "jpeg_set_quality");
    ZED_JPEG_SYMBOL(start, "jpeg_start_compress");
    ZED_JPEG_SYMBOL(write, "jpeg_write_raw_data");
    ZED_JPEG_SYMBOL(finish, "jpeg_finish_compress");
#undef ZED_JPEG_SYMBOL
    zed_jpeg_api.library = lib;
    return 1;
}
typedef struct {
    struct jpeg_compress_struct compressor;
    struct jpeg_error_mgr error;
    jmp_buf escape;
    unsigned char *jpeg, *row;
    unsigned long size;
} ZedJpegJob;
static void zed_jpeg_error(j_common_ptr info) {
    ZedJpegJob *job = (ZedJpegJob *)info->client_data;
    (*info->err->output_message)(info);
    longjmp(job->escape, 1);
}
static unsigned char zed_full_range(int value, int chroma) {
    int v = chroma ? ((value-128)*255 + (value>=128 ? 112 : -112))/224+128 : ((value-16)*255+109)/219;
    return v < 0 ? 0 : v > 255 ? 255 : (unsigned char)v;
}
static int zed_encode_left(const unsigned char *yuyv, unsigned width, unsigned height,
                           unsigned char **jpeg, unsigned long *size) {
    if (!zed_jpeg_available()) return 0;
    ZedJpegJob *job = calloc(1, sizeof(*job));
    if (!job) return 0;
    job->compressor.err = zed_jpeg_api.std_error(&job->error);
    job->error.error_exit = zed_jpeg_error;
    job->compressor.client_data = job;
    if (setjmp(job->escape)) {
        zed_jpeg_api.destroy(&job->compressor);
        free(job->jpeg); free(job->row); free(job); return 0;
    }
    zed_jpeg_api.create(&job->compressor, JPEG_LIB_VERSION, sizeof(job->compressor));
    job->row = malloc(width * 2U * 8U);
    if (!job->row) { zed_jpeg_api.destroy(&job->compressor); free(job); return 0; }
    zed_jpeg_api.destination(&job->compressor, &job->jpeg, &job->size);
    job->compressor.image_width = width; job->compressor.image_height = height;
    job->compressor.input_components = 3; job->compressor.in_color_space = JCS_YCbCr;
    zed_jpeg_api.defaults(&job->compressor);
    zed_jpeg_api.quality(&job->compressor, 90, TRUE);
    job->compressor.comp_info[0].h_samp_factor = 2;
    job->compressor.comp_info[0].v_samp_factor = 1; /* preserve source 4:2:2 */
    job->compressor.dct_method = JDCT_IFAST;
    job->compressor.raw_data_in = TRUE;
    zed_jpeg_api.start(&job->compressor, TRUE);
    unsigned char luma[256], chroma[256];
    for (unsigned i = 0; i < 256; ++i) { luma[i] = zed_full_range(i, 0); chroma[i] = zed_full_range(i, 1); }
    JSAMPROW y_rows[8], u_rows[8], v_rows[8];
    JSAMPARRAY planes[3] = {y_rows, u_rows, v_rows};
    for (unsigned row = 0; row < 8; ++row) {
        y_rows[row] = job->row + row*width;
        u_rows[row] = job->row + 8U*width + row*width/2;
        v_rows[row] = job->row + 12U*width + row*width/2;
    }
    while (job->compressor.next_scanline < height) {
        for (unsigned row = 0; row < 8; ++row) {
            unsigned source_row = job->compressor.next_scanline + row;
            if (source_row >= height) source_row = height-1;
            const unsigned char *src = yuyv + source_row * width * 2U;
            for (unsigned x = 0; x < width; x += 2) {
                y_rows[row][x] = luma[src[x*2]];
                y_rows[row][x+1] = luma[src[x*2+2]];
                u_rows[row][x/2] = chroma[src[x*2+1]];
                v_rows[row][x/2] = chroma[src[x*2+3]];
            }
        }
        if (zed_jpeg_api.write(&job->compressor, planes, 8) != 8) longjmp(job->escape, 1);
    }
    zed_jpeg_api.finish(&job->compressor);
    *jpeg = job->jpeg; *size = job->size;
    zed_jpeg_api.destroy(&job->compressor);
    free(job->row); free(job);
    return *size >= 128 && *size <= 6U*1024U*1024U;
}
