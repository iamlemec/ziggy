# vector index

import torch

from .quant import QuantizedEmbedding, Float, Half
from .utils import IndexDict, OrderedSet, resize_alloc, next_power_of_2

##
## Pure Torch
##

class TorchVectorIndex:
    def __init__(
        self, dims=None, size=1024, device='cuda', qspec=Float, allocate=True
    ):
        # set runtime options
        self.device = device

        # init state
        if allocate:
            # default datatype
            if qspec is None:
                qspec = Half if device == 'cuda' else Float

            # set up storage
            self.labels = OrderedSet()
            self.grpids = IndexDict()
            self.values = QuantizedEmbedding(size, dims, qspec=qspec, device=device)
            self.groups = torch.empty(size, device=self.device, dtype=torch.int32)

    @classmethod
    def load(cls, data, device='cuda', **kwargs):
        self = cls(allocate=False, device=device, **kwargs)
        self.labels = OrderedSet.load(data['labels'])
        self.grpids = IndexDict.load(data['grpids'])
        self.values = QuantizedEmbedding.load(data['values'], device=device)
        self.groups = data['groups']
        return self

    def save(self):
        return {
            'labels': self.labels.save(),
            'grpids': self.grpids.save(),
            'values': self.values.save(),
            'groups': self.groups,
        }

    def size(self):
        return len(self.labels)

    def expand(self, size, power=False):
        if power:
            size = next_power_of_2(size)
        if size > self.values.size():
            self.values.resize(size)
            resize_alloc(self.groups, size)

    def add(self, labs, vecs, groups=None, strict=False):
        # allow for single vec
        if type(labs) is not list and vecs.ndim == 1:
            labs = [labs]
            vecs = vecs.unsqueeze(0)

        # ensure on device
        vecs = vecs.to(self.device)

        # validate input size
        nlabs = len(labs)
        nv, dv = vecs.shape
        d0 = self.values.dims
        assert(nv == nlabs)
        assert(dv == d0)

        # get breakdown of new vs old
        slabs = set(labs)
        exist = self.labels.intersection(slabs)
        novel = slabs - exist

        # raise if trying invalid strict add
        if strict and len(exist) > 0:
            raise Exception(f'Trying to add existing labels in strict mode.')

        # expand groups if needed
        self.grpids.add(groups)
        gidx = self.grpids.idx(groups)
        if type(groups) is list:
            gids = torch.tensor(gidx, device=self.device, dtype=torch.int32)
        else:
            gids = torch.full((nlabs,), gidx, device=self.device, dtype=torch.int32)

        if len(exist) > 0:
            # update existing
            elocs, idxs = map(list, zip(*[
                (i, self.labels.index(x)) for i, x in enumerate(labs) if x in exist
            ]))
            self.values[idxs] = vecs[elocs,:]
            self.groups[idxs] = gids[elocs]

        if len(novel) > 0:
            # get new labels in input order
            xlocs, xlabs = map(list, zip(*[
                (i, x) for i, x in enumerate(labs) if x in novel
            ]))

            # expand size if needed
            nlabels0 = self.size()
            nlabels1 = nlabels0 + len(novel)
            self.expand(nlabels1, power=True)

            # add in new labels and vectors
            self.labels.extend(xlabs)
            self.values[nlabels0:nlabels1] = vecs[xlocs,:]
            self.groups[nlabels0:nlabels1] = gids[xlocs]

    def remove(self, labs=None, func=None):
        size = self.size()
        labs = [l for l in self.labels if func(l)] if func is not None else labs
        for lab in self.labels.intersection(labs):
            idx = self.labels.index(lab)
            self.labels.pop(idx)
            self.values.raw[idx] = self.values.raw[size]
            self.groups[idx] = self.groups[size]

    def clear(self, zero=False):
        self.labels = OrderedSet()
        self.grpids = IndexDict()
        if zero:
            self.values.zero_()
            self.groups.zero_()

    def get(self, labels):
        # convert to indices
        labels = [labels] if type(labels) is not list else labels
        indices = torch.tensor([self.labels.index(l) for l in labels])

        # validate indices
        if (indices == -1).any():
            raise Exception(f'Some labels not found.')

        # return values
        return self.values[indices]

    def idx(self, indices):
        # convert to tensor if needed
        indices = [indices] if type(indices) is int else indices
        indices = torch.tensor(indices, device=self.device, dtype=torch.int32)

        # handle negative indices
        size = self.size()
        indices = torch.where(indices < 0, indices + size, indices)

        # validate indices
        if (indices < 0).any() or (indices >= size).any():
            raise Exception(f'Some indices out of bounds.')

        # return values
        return self.values[indices]

    def search(self, vecs, k, groups=None, return_simil=True):
        # allow for single vec
        squeeze = vecs.ndim == 1
        if squeeze:
            vecs = vecs.unsqueeze(0)

        # clamp k to max size
        num = self.size()
        k1 = min(k, num)

        # get compare values
        if groups is None:
            idx = num
            labs = self.labels
        else:
            ids = torch.tensor(self.grpids.idx(groups), device=self.device, dtype=torch.int32)
            sel = torch.isin(self.groups[:num], ids)
            idx = torch.nonzero(sel).squeeze()
            labs = [self.labels[i] for i in idx]

        # compute similarity matrix
        sims = self.values.similarity(vecs, mask=idx)

        # get top results
        tops = sims.topk(k1)
        klab = [[labs[i] for i in row] for row in tops.indices]
        kval = tops.values

        # return single vec if needed
        if squeeze:
            klab, kval = klab[0], kval[0]

        # return labels/simils
        return (klab, kval) if return_simil else klab

    def simil(self, vecs, mask=None):
        mask = self.size() if mask is None else mask
        vecs = torch.atleast_2d(vecs)
        sims = self.values.similarity(vecs, mask=mask)
        return sims.squeeze()

##
## FAISS
##

# this won't handle deletion
class FaissIndex:
    def __init__(self, dims, spec='Flat', device='cuda'):
        import faiss

        # initialize index
        self.dims = dims
        self.labels = []
        self.values = faiss.index_factory(dims, spec)

        # move to gpu if needed
        if device == 'cuda':
            res = faiss.StandardGpuResources()
            self.values = faiss.index_cpu_to_gpu(res, 0, self.values)

    def size(self):
        return len(self.labels)

    def load(self, path):
        data = torch.load(path) if type(path) is str else path
        self.labels = data['labels']
        self.values = self.values.add(data['values'])

    def save(self, path):
        data = {
            'labels': self.labels,
            'values': self.values.reconstruct_n(0, self.size())
        }
        if path is not None:
            torch.save(data, path)
        else:
            return data

    def add(self, labs, vecs):
        # validate input size
        nlabs = len(labs)
        nv, dv = vecs.shape
        assert(nv == nlabs)
        assert(dv == self.dims)

        # reject adding existing labels
        exist = set(labs).intersection(self.labels)
        if len(exist) > 0:
            raise Exception(f'Adding existing labels not supported.')

        # construct label ids
        size0 = self.size()
        size1 = size0 + nlabs
        ids = torch.arange(size0, size1)

        # add to index
        self.labels.extend(labs)
        self.values.add(vecs)

    def search(self, vecs, k, return_simil=True):
        # allow for single vec
        squeeze = vecs.ndim == 1
        if squeeze:
            vecs = vecs.unsqueeze(0)

        # exectute search
        vals, ids = self.values.search(vecs, k)
        labs = [[self.labels[i] for i in row] for row in ids]

        # return single vec if needed
        if squeeze:
            labs, vals = labs[0], vals[0]

        # return labels/simils
        return labs, vals if return_simil else labs
