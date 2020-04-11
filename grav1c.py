#!/usr/bin/env python3
# lightweight client-only for encoding

import os, subprocess, re, contextlib, requests, time, sys
from curses import wrapper, curs_set
from tempfile import NamedTemporaryFile

def print_progress(n, total, size=20, suffix=""):
  fill = "█" * int((n / total) * size)
  remaining = " " * (size - len(fill))
  return f"{int(100 * n / total):3d}%|{fill}{remaining}| {n}/{total}"

def get_frames(input):
  cmd = f"ffmpeg -hide_banner -map 0:v:0 -c copy -f null {os.devnull} -i".split(" ")
  cmd.append(input)
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  return int(re.search(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8")).group(1))

@contextlib.contextmanager
def tmp_file(mode, content, suffix=""):
  try:
    file = NamedTemporaryFile(mode=mode, suffix=suffix, dir=".", delete=False)
    file.write(content)
    file.flush()
    tmp_name = file.name
    file.close()
    yield tmp_name
  finally:
    os.unlink(tmp_name)

def aom_encode(input, encoder_params, status_cb):
  output_filename = f"{input}.ivf"

  ffmpeg = f"ffmpeg -y -hide_banner -loglevel error -i {input} -strict -1 -pix_fmt yuv420p -f yuv4mpegpipe -".split(" ")
  aom = f"aomenc - --fpf={input}.log --threads=4 {encoder_params}".split(" ")

  aom.append("--passes=2")
  passes = [aom + cmd for cmd in [
    ["--pass=1", "-o", os.devnull],
    ["--pass=2", "-o", output_filename]
  ]]

  total_frames = get_frames(input)

  try:
    for pass_n, cmd in enumerate(passes, start=1):
      ffmpeg_pipe = subprocess.Popen(ffmpeg,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)

      pipe = subprocess.Popen(cmd,
        stdin=ffmpeg_pipe.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True)

      status_cb(f"{os.path.basename(input)} pass: {pass_n} {print_progress(0, total_frames)}")

      while True:
        line = pipe.stdout.readline().strip()

        if len(line) == 0 and pipe.poll() is not None:
          break

        match = re.search(r"frame *([^ ]+?)/", line)
        if match:
          status_cb(f"{os.path.basename(input)} pass: {pass_n} {print_progress(int(match.group(1)), total_frames)}")
      
      if pipe.returncode != 0:
        status_cb("error")
        return False

    if os.path.isfile(f"{input}.log"):
      os.remove(f"{input}.log")

    return output_filename
  except Exception as e:
    print("killing worker")
    pipe.kill()
    raise e

def client(host, vmaf_path, status_cb):
  while True:
    try:
      status_cb("downloading")
      r = requests.get(host + "/get_job")
      if r.status_code == 404:
        status_cb("finished")
        return
      job = type("", (), {})
      job.id = r.headers["id"]
      job.filename = r.headers["filename"]
      job.encoder_params = r.headers["encoder_params"]
      job.content = r.content
    except requests.exceptions.ConnectionError:
      status_cb("server not found")
      sys.exit()

    if not job:
      status_cb("finished")
      return

    if len(vmaf_path) > 0:
      job.encoder_params = f"{job.encoder_params} --vmaf-model-path={vmaf_path}"
    
    with tmp_file("wb", job.content, job.filename) as file:
      output = aom_encode(file, job.encoder_params, status_cb)
      if output:
        status_cb("uploading")
        with open(output, "rb") as file:
          files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
          requests.post(host + "/finish_job", data={"id": job.id, "filename": job.filename}, files=files)

        while os.path.isfile(output):
          try:
            os.remove(output)
          except:
            print("failed to delete")
            time.sleep(1)

def do(host, i):
  screen(i, "starting")
  client(host, args.vmaf_path, lambda msg: screen(i, msg))

worker_log = {}
def screen(i, msg):
  worker_log[i] = msg

def window(scr):
  scr.nodelay(1)
  curs_set(0)
  while True:
    alive = False
    for worker in workers:
      if worker.is_alive():
        alive = True
        break

    msg = []
    for worker in worker_log:
      msg.append(f"{worker} {worker_log[worker]}")

    scr.erase()
    scr.addstr(f"target: {args.target} workers: {args.workers}\n")
    scr.addstr("\n".join(msg))
    scr.refresh()

    c = scr.getch()
    if not alive or c == 3:
      #curses.endwin()
      break
  curs_set(1)

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("target", type=str, nargs="?", default="http://174.6.71.104:7899")
  parser.add_argument("--vmaf-model-path", dest="vmaf_path", default="vmaf_v0.6.1.pkl" if os.name == "nt" else "")
  parser.add_argument("--workers", dest="workers", default=1)

  args = parser.parse_args()

  from threading import Thread

  workers = []

  for i in range(0, int(args.workers)):
    worker = Thread(target=do, args=(args.target, i,), daemon=True)
    worker.start()
    workers.append(worker)
    time.sleep(0.1)

  wrapper(window)
