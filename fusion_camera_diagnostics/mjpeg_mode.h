#ifndef FUSION_MJPEG_MODE_H
#define FUSION_MJPEG_MODE_H
#include <libuvc/libuvc_internal.h>

/* Request precisely 640x480/30 without first starting a high-rate stream.
 * Descriptor intervals and negotiated values are logged independently. */
static int negotiate_mjpeg30(uvc_device_handle_t *dev, uvc_stream_ctrl_t *ctrl, int camera_id, unsigned width, unsigned height) {
  const uvc_format_desc_t *fmt;
  const uvc_frame_desc_t *frame=NULL;
  for (fmt=uvc_get_format_descs(dev); fmt; fmt=fmt->next) {
    if (fmt->bDescriptorSubtype != UVC_VS_FORMAT_MJPEG) continue;
    for(frame=fmt->frame_descs; frame; frame=frame->next)
      if(frame->wWidth==width && frame->wHeight==height) break;
    if(frame) break;
  }
  if(!fmt || !frame) return UVC_ERROR_INVALID_MODE;
  fprintf(stderr,"{\"camera\":%d,\"mjpeg_advertised_intervals\":[",camera_id);
  if(frame->intervals) {
    for(unsigned j=0; frame->intervals[j]; j++)
      fprintf(stderr,"%s%u",j ? "," : "",frame->intervals[j]);
  }
  fprintf(stderr,"],\"default_interval\":%u}\n",frame->dwDefaultFrameInterval);
  int advertised30=0;
  if(frame->intervals) for(unsigned j=0;frame->intervals[j];j++)
    if(frame->intervals[j]>=333333 && frame->intervals[j]<=333334) advertised30=1;
  if(!advertised30) {
    fprintf(stderr,"MJPEG %ux%u does not advertise 30 Hz; refusing hidden high-rate mode\n",width,height);
    return UVC_ERROR_INVALID_MODE;
  }
  memset(ctrl,0,sizeof(*ctrl));
  ctrl->bInterfaceNumber=fmt->parent->bInterfaceNumber;
  int err=uvc_claim_if(dev,ctrl->bInterfaceNumber);
  if(err<0) return err;
  ctrl->bmHint=1;
  ctrl->bFormatIndex=fmt->bFormatIndex;
  ctrl->bFrameIndex=frame->bFrameIndex;
  ctrl->dwFrameInterval=333333;
  if((err=uvc_query_stream_ctrl(dev,ctrl,1,UVC_SET_CUR))<0) return err;
  if((err=uvc_query_stream_ctrl(dev,ctrl,1,UVC_GET_CUR))<0) return err;
  fprintf(stderr,"{\"camera\":%d,\"requested_interval\":333333,\"returned_interval\":%u,\"returned_format_index\":%u,\"returned_frame_index\":%u,\"max_frame_bytes\":%u,\"max_payload\":%u}\n",
          camera_id,ctrl->dwFrameInterval,ctrl->bFormatIndex,ctrl->bFrameIndex,
          ctrl->dwMaxVideoFrameSize,ctrl->dwMaxPayloadTransferSize);
  if(ctrl->bFormatIndex!=fmt->bFormatIndex || ctrl->bFrameIndex!=frame->bFrameIndex ||
      ctrl->dwFrameInterval<333333 || ctrl->dwFrameInterval>333334 ||
      !ctrl->dwMaxVideoFrameSize || ctrl->dwMaxVideoFrameSize>2097152 ||
      !ctrl->dwMaxPayloadTransferSize || ctrl->dwMaxPayloadTransferSize>3072)
    return UVC_ERROR_INVALID_MODE;
  return 0;
}
#endif
