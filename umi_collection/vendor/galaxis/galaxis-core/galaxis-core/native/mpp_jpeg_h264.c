/* SPDX-License-Identifier: MIT
 * Length-prefixed JPEG input -> length-prefixed Annex-B H.264 access units.
 * One submitted frame is drained before reusing the DMA buffer; no B frames.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <setjmp.h>
#include <unistd.h>
#include <grp.h>
#include <sys/stat.h>
#include <jpeglib.h>
#include <rk_mpi.h>
#include <mpp_frame.h>
#include <mpp_packet.h>
#include <mpp_buffer.h>
#include <rk_venc_cfg.h>
struct err {struct jpeg_error_mgr base; jmp_buf jump;};
static void fail_jpeg(j_common_ptr c){longjmp(((struct err*)c->err)->jump,1);}
static int readn(void* p,size_t n){size_t off=0;while(off<n){size_t r=fread((char*)p+off,1,n-off,stdin);if(!r)return off? -1:0;off+=r;}return 1;}
static void check(MPP_RET r,const char* what){if(r){fprintf(stderr,"%s failed %d\n",what,r);exit(2);}}
static void config(MppEncCfg c,const char*k,int v){check(mpp_enc_cfg_set_s32(c,k,v),k);}
static int recording_user(void){
 /* The board's older MPP chooses a nonexistent DMA32 heap when root can
  * write /dev/dma_heap. The operator account correctly uses DRM instead.
  * Drop privileges only in this pipe-based encoder, before MPP initializes;
  * the ROS recorder retains its device access and output-file ownership.
  */
 if(geteuid()!=0)return 0;
 const char*value=getenv("GALAXIS_DATA_UID");char*end=NULL;
 unsigned long uid=value?strtoul(value,&end,10):1000;
 if(!uid||uid>2147483647UL||(value&&(!*value||*end))){fprintf(stderr,"Invalid recording user\n");return -1;}
 struct stat drm,mpp;
 if(stat("/dev/dri/card0",&drm)||stat("/dev/mpp_service",&mpp)){perror("Encoder devices");return -1;}
 gid_t groups[2]={drm.st_gid,mpp.st_gid};
 if(setgroups(2,groups)||setgid(drm.st_gid)||setuid((uid_t)uid)){perror("Encoder user");return -1;}
 return 0;
}
int main(int argc,char**argv){
 if(recording_user())return 13;
 int qp=argc>1?atoi(argv[1]):20; if(qp<0||qp>40)return 2;
 MppCtx ctx=NULL;MppApi*api=NULL;MppEncCfg cfg=NULL;MppBuffer buf=NULL;int width=0,height=0,stride=0,vstride=0;
 unsigned long long count=0;
 for(;;){
  unsigned char length[4];int status=readn(length,4);if(!status)break;if(status<0)return 3;
  uint32_t len=length[0]|length[1]<<8|length[2]<<16|(uint32_t)length[3]<<24;
  if(!len||len>16*1024*1024)return 4;
  unsigned char*jpg=malloc(len);if(!jpg||readn(jpg,len)!=1)return 5;
  struct jpeg_decompress_struct j={0};struct err e;j.err=jpeg_std_error(&e.base);e.base.error_exit=fail_jpeg;
  if(setjmp(e.jump)){fprintf(stderr,"invalid JPEG\n");return 6;}
  jpeg_create_decompress(&j);jpeg_mem_src(&j,jpg,len);jpeg_read_header(&j,TRUE);j.out_color_space=JCS_YCbCr;j.dct_method=JDCT_FASTEST;j.do_fancy_upsampling=FALSE;jpeg_start_decompress(&j);
  if(!ctx){
   width=j.output_width;height=j.output_height;if(width%2||height%2||width<2||height<2||width>4096||height>4096)return 7;
   stride=(width+15)&~15;vstride=(height+15)&~15;
   check(mpp_create(&ctx,&api),"create");MppPollType timeout=MPP_POLL_BLOCK;check(api->control(ctx,MPP_SET_OUTPUT_TIMEOUT,&timeout),"timeout");check(mpp_init(ctx,MPP_CTX_ENC,MPP_VIDEO_CodingAVC),"init");
   check(mpp_enc_cfg_init(&cfg),"cfg");check(api->control(ctx,MPP_ENC_GET_CFG,cfg),"get cfg");
   config(cfg,"prep:width",width);config(cfg,"prep:height",height);config(cfg,"prep:hor_stride",stride);config(cfg,"prep:ver_stride",vstride);config(cfg,"prep:format",MPP_FMT_YUV420SP);
   config(cfg,"prep:range",MPP_FRAME_RANGE_JPEG);
   config(cfg,"rc:mode",MPP_ENC_RC_MODE_FIXQP);config(cfg,"rc:fps_in_flex",0);config(cfg,"rc:fps_in_num",30);config(cfg,"rc:fps_in_denorm",1);config(cfg,"rc:fps_out_flex",0);config(cfg,"rc:fps_out_num",30);config(cfg,"rc:fps_out_denorm",1);config(cfg,"rc:gop",30);
   config(cfg,"rc:qp_init",qp);config(cfg,"rc:qp_min",qp);config(cfg,"rc:qp_max",qp);config(cfg,"rc:qp_min_i",qp);config(cfg,"rc:qp_max_i",qp);config(cfg,"rc:qp_ip",0);
   config(cfg,"h264:profile",100);config(cfg,"h264:level",40);config(cfg,"h264:cabac_en",1);
   check(api->control(ctx,MPP_ENC_SET_CFG,cfg),"set cfg");MppEncHeaderMode mode=MPP_ENC_HEADER_MODE_EACH_IDR;check(api->control(ctx,MPP_ENC_SET_HEADER_MODE,&mode),"headers");
   check(mpp_buffer_get(NULL,&buf,(size_t)stride*vstride*3/2),"buffer");
  }
  if((int)j.output_width!=width||(int)j.output_height!=height||j.output_components!=3)return 8;
  unsigned char*dst=mpp_buffer_get_ptr(buf);unsigned char*rows=malloc((size_t)width*6);if(!rows)return 9;
  memset(dst,0,(size_t)stride*vstride*3/2);
  for(int y=0;y<height;y+=2){
   JSAMPROW r0=rows,r1=rows+width*3;jpeg_read_scanlines(&j,&r0,1);jpeg_read_scanlines(&j,&r1,1);
   for(int x=0;x<width;x++){dst[y*stride+x]=r0[3*x];dst[(y+1)*stride+x]=r1[3*x];}
   unsigned char*uv=dst+(size_t)stride*vstride+(y/2)*stride;
   for(int x=0;x<width;x+=2){uv[x]=(r0[x*3+1]+r0[(x+1)*3+1]+r1[x*3+1]+r1[(x+1)*3+1]+2)/4;uv[x+1]=(r0[x*3+2]+r0[(x+1)*3+2]+r1[x*3+2]+r1[(x+1)*3+2]+2)/4;}
  }
  free(rows);jpeg_finish_decompress(&j);jpeg_destroy_decompress(&j);free(jpg);
  MppFrame frame=NULL;check(mpp_frame_init(&frame),"frame");mpp_frame_set_width(frame,width);mpp_frame_set_height(frame,height);mpp_frame_set_hor_stride(frame,stride);mpp_frame_set_ver_stride(frame,vstride);mpp_frame_set_fmt(frame,MPP_FMT_YUV420SP);mpp_frame_set_color_range(frame,MPP_FRAME_RANGE_JPEG);mpp_frame_set_buffer(frame,buf);mpp_frame_set_pts(frame,count++);
  check(api->encode_put_frame(ctx,frame),"put frame");mpp_frame_deinit(&frame);
  MppPacket packet=NULL;check(api->encode_get_packet(ctx,&packet),"get packet");if(!packet)return 10;
  size_t n=mpp_packet_get_length(packet);if(!n||n>16*1024*1024)return 11;
  length[0]=n;length[1]=n>>8;length[2]=n>>16;length[3]=n>>24;
  if(fwrite(length,1,4,stdout)!=4||fwrite(mpp_packet_get_pos(packet),1,n,stdout)!=n||fflush(stdout))return 12;
  mpp_packet_deinit(&packet);
 }
 if(buf) mpp_buffer_put(buf);
 if(cfg) mpp_enc_cfg_deinit(cfg);
 if(ctx) mpp_destroy(ctx);
 return 0;
}
