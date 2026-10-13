/* Reuse USB safety overrides. hardware_main_not_called is never invoked. */
#define main hardware_main_not_called
#include "capture_three.c"
#undef main
#include "mjpeg_mode.h"

int main(int argc,char **argv) {
  if(argc!=4) return 2;
  uvc_context_t *ctx=NULL; uvc_device_t **list=NULL;
  int all_ok=1, err=uvc_init(&ctx,NULL);
  if(err<0) return 2;
  if(uvc_get_device_list(ctx,&list)<0) { uvc_exit(ctx); return 2; }
  for(int i=0;i<3;i++) {
    char *end; long address=strtol(argv[i+1],&end,10);
    if(*end || address<1 || address>127) { all_ok=0; break; }
    uvc_device_t *chosen=NULL;
    for(int j=0;list[j];j++) {
      if(uvc_get_bus_number(list[j])==1 && uvc_get_device_address(list[j])==address) {
        uvc_device_descriptor_t *desc=NULL;
        if(uvc_get_device_descriptor(list[j],&desc)<0) break;
        if(i==0 ? desc->idVendor==0x0bda && desc->idProduct==0x5883 :
                  desc->idVendor==0x32e4 && desc->idProduct==0x9230) chosen=list[j];
        uvc_free_device_descriptor(desc); break;
      }
    }
    uvc_device_handle_t *dev=NULL; uvc_stream_ctrl_t ctrl;
    err=chosen ? uvc_open(chosen,&dev) : UVC_ERROR_NO_DEVICE;
    if(!err) err=negotiate_mjpeg30(dev,&ctrl,i,1024,768);
    fprintf(stderr,"{\"camera\":%d,\"probe_error\":%d,\"stream_started\":false}\n",i,err);
    if(err) all_ok=0;
    if(dev) uvc_close(dev);
  }
  uvc_free_device_list(list,1); uvc_exit(ctx);
  return all_ok ? 0 : 1;
}
