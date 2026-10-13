#!/usr/bin/env python3
"""Self-contained LeRobot Dataset v2.1 -> v3.0 converter for Rebake output."""
import argparse, json, shutil, subprocess, tempfile
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

V21, V30 = "v2.1", "v3.0"

def read_json(p):
    with p.open() as f: return json.load(f)
def write_json(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
def jsonl(p):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
def flatten(value, prefix=""):
    out = {}
    for k, v in value.items():
        key = f"{prefix}/{k}" if prefix else k
        if isinstance(v, dict): out.update(flatten(v, key))
        else: out[key] = v
    return out
def ffprobe(path):
    return float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)], text=True).strip())
def concat_video(paths, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in paths: f.write("file '" + str(p).replace("'", "'\\''") + "'\n")
        manifest = f.name
    # Encode H.264 for broad OpenCV/FFmpeg/LeRobot viewer compatibility;
    # Rebake's AV1 files are not decodable by all downstream readers.
    try: subprocess.run(["ffmpeg","-y","-v","error","-f","concat","-safe","0","-i",manifest,"-c:v","libx264","-pix_fmt","yuv420p","-crf","18","-preset","fast",str(dest)], check=True)
    finally: Path(manifest).unlink(missing_ok=True)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root", required=True); ap.add_argument("--keep-v21", action="store_true"); args=ap.parse_args(); root=Path(args.root)
    info=read_json(root/"meta/info.json")
    if info.get("codebase_version") != V21: raise SystemExit("expected LeRobot v2.1 staging dataset")
    old=root.with_name(root.name+"_old"); new=root.with_name(root.name+"_v30")
    if old.exists() or new.exists(): raise SystemExit("refusing to overwrite existing _old or _v30 directory")
    episodes=jsonl(root/"meta/episodes.jsonl"); stats=jsonl(root/"meta/episodes_stats.jsonl"); tasks=jsonl(root/"meta/tasks.jsonl")
    data_paths=sorted((root/"data").glob("chunk-*/*.parquet")); tables=[pq.read_table(p) for p in data_paths]
    new_data=new/"data/chunk-000/file-000.parquet"; new_data.parent.mkdir(parents=True,exist_ok=True); pq.write_table(pa.concat_tables(tables),new_data)
    (new/"meta").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(tasks).set_index("task").to_parquet(new/"meta/tasks.parquet")
    video_keys=[k for k,v in info["features"].items() if v.get("dtype")=="video"]
    video_meta={i:{} for i in range(len(episodes))}
    for key in video_keys:
        paths=sorted((root/"videos").glob(f"chunk-*/{key}/episode_*.mp4")); durations=[ffprobe(p) for p in paths]
        if len(paths)!=len(episodes): raise SystemExit(f"video episode count mismatch for {key}")
        concat_video(paths,new/f"videos/{key}/chunk-000/file-000.mp4"); t=0.0
        for i,d in enumerate(durations):
            video_meta[i].update({f"videos/{key}/chunk_index":0,f"videos/{key}/file_index":0,f"videos/{key}/from_timestamp":t,f"videos/{key}/to_timestamp":t+d}); t+=d
    offset=0; rows=[]
    for i,(ep,st) in enumerate(zip(episodes,stats)):
        length=int(ep["length"]); row={"episode_index":i,"data/chunk_index":0,"data/file_index":0,"dataset_from_index":offset,"dataset_to_index":offset+length,"meta/episodes/chunk_index":0,"meta/episodes/file_index":0,"tasks":ep["tasks"]}; row.update(video_meta[i]); row.update({"stats/"+k:v for k,v in flatten(st["stats"]).items()}); rows.append(row); offset+=length
    episodes_out = new/"meta/episodes/chunk-000/file-000.parquet"
    episodes_out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(episodes_out,index=False)
    info.update({"codebase_version":V30,"total_episodes":len(rows),"total_frames":offset,"total_tasks":len(tasks),"data_path":"data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet","video_path":"videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4","data_files_size_in_mb":100,"video_files_size_in_mb":200})
    info.pop("total_chunks",None); info.pop("total_videos",None)
    for v in info["features"].values():
        if v.get("dtype")!="video": v["fps"]=int(info["fps"])
        elif "info" in v: v["info"]["video.codec"]="h264"
    write_json(new/"meta/info.json",info)
    if (root/"meta/stats.json").exists(): shutil.copy2(root/"meta/stats.json",new/"meta/stats.json")
    shutil.move(root,old); shutil.move(new,root)
    if not args.keep_v21: shutil.rmtree(old)
    print(root)
if __name__=="__main__": main()
