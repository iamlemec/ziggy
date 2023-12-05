# general utilities

from math import ceil, log2
from itertools import chain, islice, accumulate, groupby
from operator import itemgetter
from threading import Thread, Event
from queue import Queue, Empty
import toml
import time

##
## pure play python
##

# printer for streaming
def sprint(s):
    print(s, end='', flush=True)

# print iterator while streaming
def tee(iterable):
    for item in iterable:
        sprint(item)
        yield item

# mostly for reallocations
def next_power_of_2(x):
    return pow(2, round(ceil(log2(x))))

# allow list or single item
def allow_list(func):
    def wrapper(self, keys):
        many = type(keys) is list
        keys = keys if many else [keys]
        rets = func(self, keys)
        if rets is not None:
            return rets if many else rets[0]
    return wrapper

# group tuples by `idx` element, preserving other orders
def groupby_dict(vals, grps):
    getter = itemgetter(1)
    tups = sorted(zip(vals, grps), key=getter)
    return {
        k: [i for i, _ in v] for k, v in groupby(tups, key=getter)
    }

# cumulative sum
def cumsum(lengths):
    return list(chain([0], accumulate(lengths)))

# cumsum generator
def cumul_bounds(seq):
    total = 0
    for item in seq:
        yield total, total+item
        total += item

# generate (resolved) batches from generator
def batch_generator(gen, batch_size):
    while (batch := list(islice(gen, batch_size))) != []:
        yield batch

# get batch indices
def batch_indices(length, batch_size):
    return [(i, min(i+batch_size, length)) for i in range(0, length, batch_size)]

# get cumulative indices
def cumul_indices(lengths):
    sums = cumsum(lengths)
    return [(i, j) for i, j in zip(sums[:-1], sums[1:])]

def string_splitter(text, maxlen):
    for i, j in batch_indices(len(text), maxlen):
        yield text[i:j]

# convert sentencepiece tokens to text
def convert_sentencepice(toks):
    return ''.join([
        tok.replace('▁', ' ').replace('<0x0A>', '\n') for tok in toks
    ])

##
## importing
##

class MissingModule:
    def __init__(self, msg):
        self.msg = msg

    def __getattr__(self, key):
        raise Exception(self.msg)

##
## collections
##

class IndexDict(dict):
    def __init__(self, data=None):
        data = [] if data is None else data
        super().__init__(data)

    @classmethod
    def load(cls, data):
        return cls(data)

    def save(self):
        return dict(self)

    @allow_list
    def add(self, keys):
        skeys = set(keys)
        new = skeys - (self.keys() & skeys)
        n0, n1 = len(self), len(new)
        ids = range(n0, n0 + n1)
        self.update(zip(new, ids))

    @allow_list
    def idx(self, keys):
        return [self[k] for k in keys]

class OrderedSet(list):
    def __init__(self, data=None):
        data = [] if data is None else data
        super().__init__(data)
        self._set = set(data)

    @classmethod
    def load(cls, data):
        return cls(data)

    def save(self):
        return list(self)

    def isdisjoint(self, keys):
        return self._set.isdisjoint(keys)

    def intersection(self, keys):
        return self._set.intersection(keys)

    def extend(self, keys):
        if not self.isdisjoint(keys):
            raise ValueError('Trying to add existing keys')
        self._set.update(keys)
        super().extend(keys)

class Bundle(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()
        for d in args + (kwargs,):
            self.update(d)

    @classmethod
    def from_tree(cls, tree):
        if isinstance(tree, dict):
            return cls([(k, cls.from_tree(v)) for k, v in tree.items()])
        else:
            return tree

    @classmethod
    def from_toml(cls, path):
        return cls.from_tree(toml.load(path))

    def __repr__(self):
        return '\n'.join([f'{k} = {v}' for k, v in self.items()])

    def keys(self):
        return sorted(super().keys())

    def items(self):
        return sorted(super().items(), key=itemgetter(0))

    def values(self):
        return [k for k, _ in self.items()]

    def __getattr__(self, key):
        return self[key]
    
    def __setattr__(self, key, value):
        self[key] = value

##
## request tracking
##

class RequestTracker:
    def __init__(self, limits, period):
        self.reqs = []
        self.lims = limits
        self.span = period

    def add(self, *req):
        current = time.time()
        self.reqs.append((current, req))

    def ensure(self):
        # trim request queue
        current = time.time()
        cutoff = current - self.span
        self.reqs = [(t, n) for t, n in self.reqs if t >= cutoff]

        # if empty we are good
        if len(self.reqs) == 0:
            return

        # get the current in period totals
        usage = tuple(map(list, zip(*[n for _, n in self.reqs])))
        total = tuple(map(sum, usage))
        print(f'usage = {total}')

        # determine how long to wait for compliance
        if any([t > l for t, l in zip(total, self.lims)]):
            # get compliance cutoff for each series
            cumuse = [cumsum(reversed(u)) for u in usage]
            usecut = [
                next((i for i, c in enumerate(cu) if c > l), 0)
                for cu, l in zip(cumuse, self.lims)
            ]

            # compute delay to full compliance
            cut, _ = self.reqs[len(self.reqs)-max(usecut)]
            delay = self.span - (current-cut)

            # implement delay and notify
            print(f'waiting {delay:.2f} seconds for rate limit (usage = {total})')
            time.sleep(delay)

##
## torch utils
##

def resize_alloc(a, size):
    a.resize_(size, *a.shape[1:])

##
## thread rig
##

# process queue items (None terminates)
def worker_thread(func, queue_prev, queue_next, kill, poll):
    while True:
        try:
            data = queue_prev.get(timeout=poll)
            queue_next.put(func(data))
            queue_prev.task_done()
        except Empty:
            pass
        if kill.is_set():
            break

def pipeline_threads(load, *funcs, maxsize=0, poll=0.01):
    funcs = [(f, 1) if type(f) is not tuple else f for f in funcs]

    # create queues (last should be unbounded)
    queue_load = Queue(maxsize=maxsize)
    queue_work = [Queue(maxsize=maxsize) for _ in funcs[:-1]] + [Queue()]
    queues = [queue_load] + queue_work
    kill = Event()

    # create num threads for each function
    threads = [
        [
            Thread(target=worker_thread, args=(f, q0, q1, kill, poll)) for _ in range(n)
        ]
        for (f, n), q0, q1 in zip(funcs, queues[:-1], queues[1:])
    ]

    # start all threads
    for t in chain(*threads):
        t.start()

    # handle keyboard interrupt gracefully
    try:
        # put data in load queue
        for i in load:
            queue_load.put(i)

        # wait for all data to be processed
        for q in queues[:-1]:
            q.join()
    except KeyboardInterrupt:
        print('Terminating threads...')
    finally:
        # stop all threads
        kill.set()

        # wait for all threads
        for t in chain(*threads):
            t.join()

        # print number processed
        return queue_work[-1].qsize()