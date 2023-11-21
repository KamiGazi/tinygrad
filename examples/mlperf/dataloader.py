import random, time
from PIL import Image
from tqdm import tqdm
from tinygrad.helpers import dtypes, partition, getenv
from tinygrad.tensor import Tensor, Device
from multiprocessing import Queue, Process
from collections import defaultdict

def shuffled_indices(n):
  indices = {}
  for i in range(n-1, -1, -1):
    j = random.randint(0, i)
    if i not in indices: indices[i] = i
    if j not in indices: indices[j] = j
    indices[i], indices[j] = indices[j], indices[i]
    yield indices[i]
    del indices[i]

def loader_process(q_in, q_out, X):
  while (_recv := q_in.get()) is not None:
    idx, fn = _recv
    img = Image.open(fn)
    img = img.convert('RGB') if img.mode != "RGB" else img

    # eval: 76.08%, load in 0m7.366s (0m5.301s with simd)
    # CC="cc -mavx2" pip install -U --force-reinstall pillow-simd
    rescale = min(img.size) / 256
    crop_left = (img.width - 224*rescale) / 2.0
    crop_top = (img.height - 224*rescale) / 2.0
    img = img.resize((224, 224), Image.BILINEAR, box=(crop_left, crop_top, crop_left+224*rescale, crop_top+224*rescale))

    X[idx].assign(img.tobytes())   # NOTE: this is slow!
    q_out.put(idx)

def batch_load_resnet(batch_size=64, val=False, shuffle=True):
  from extra.datasets.imagenet import get_train_files, get_val_files
  files = get_val_files() if val else get_train_files()
  from extra.datasets.imagenet import get_imagenet_categories
  cir = get_imagenet_categories()

  BATCH_COUNT = 32
  q_in, q_out = Queue(), Queue()
  X = Tensor.empty(batch_size*BATCH_COUNT, 224, 224, 3, dtype=dtypes.uint8, device=f"disk:/dev/shm/resnet_X")
  Y = [None] * (batch_size*BATCH_COUNT)

  procs = []
  for _ in range(64):
    p = Process(target=loader_process, args=(q_in, q_out, X))
    p.daemon = True
    p.start()
    procs.append(p)

  gen = shuffled_indices(len(files)) if shuffle else iter(range(len(files)))
  def enqueue_batch(num):
    for idx in range(num*batch_size, (num+1)*batch_size):
      fn = files[next(gen)]
      q_in.put((idx, fn))
      Y[idx] = cir[fn.split("/")[-2]]
  for bn in range(BATCH_COUNT): enqueue_batch(bn)

  class Cookie:
    def __init__(self, num): self.num = num
    def __del__(self):
      try: enqueue_batch(self.num)
      except StopIteration: pass

  gotten = [0]*BATCH_COUNT
  def receive_batch():
    while 1:
      num = q_out.get()//batch_size
      gotten[num] += 1
      if gotten[num] == batch_size: break
    gotten[num] = 0
    return X[num*batch_size:(num+1)*batch_size], Y[num*batch_size:(num+1)*batch_size], Cookie(num)


  # NOTE: this is batch aligned, last ones are ignored
  for _ in range(0, len(files)//batch_size):
    yield receive_batch()

  for _ in procs: q_in.put(None)
  for p in procs: p.join()

if __name__ == "__main__":
  from extra.datasets.imagenet import get_train_files, get_val_files
  VAL = getenv("VAL", 1)
  files = get_val_files() if VAL else get_train_files()
  with tqdm(total=len(files)) as pbar:
    for x,y,_ in batch_load_resnet(val=VAL):
      pbar.update(x.shape[0])
