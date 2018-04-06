import scannerpy
import scannerpy.stdlib.parsers as parsers
import scannerpy.stdlib.writers as writers
import scannerpy.stdlib.bboxes as bboxes
from scannerpy.stdlib.util import default
import numpy as np
import pickle


class BboxNMSKernel(scannerpy.Kernel):
    def __init__(self, config, protobufs):
        self._protobufs = protobufs
        self._threshold = default(config.args, 'threshold', 0.1)

    def execute(self, input_columns):
        bboxes_list = []
        for c in input_columns:
            bb = pickle.loads(input_columns[0])  #parsers.bboxes(c, self._protobufs)
            nmsed_bboxes = bboxes.nms(bb, self._threshold)
            bboxes_list.append(pickle.dumps(nmsed_bboxes))
        #return writers.bboxes([nmsed_bboxes], self._protobufs)
        return bboxes_list


KERNEL = BboxNMSKernel