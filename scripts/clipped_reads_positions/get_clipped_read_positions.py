import argparse
import bz2
import cPickle as pickle
import os
from time import time
from collections import Counter
import pysam

import functions as fun


def get_clipped_read_positions(ibam, chrName, outFile):

    assert os.path.isfile(ibam)

    bamfile = pysam.AlignmentFile(ibam, "rb")

    clipped_pos = []
    clipped_read_1 = set()
    clipped_read_2 = set()

    def add_clipped_read(read):
        if read.is_read1:
            clipped_read_1.add(read.query_name)
        if read.is_read2:
            clipped_read_2.add(read.query_name)

    for read in bamfile.fetch(chrName):
        if (not read.is_unmapped) and (not read.mate_is_unmapped):
            # assert read.reference_name in clipped_pos.keys()
            # assert read.cigartuples[0][0] in [4, 5] or read.cigartuples[-1][0] in [4, 5]
            if len(read.get_reference_positions()) > 0:
                if fun.is_left_clipped(read):
                    add_clipped_read(read)
                    cpos = read.get_reference_positions()[0] + 1
                    clipped_pos.append(cpos)
                if fun.is_right_clipped(read):
                    add_clipped_read(read)
                    cpos = read.get_reference_positions()[-1] + 1
                    clipped_pos.append(cpos)
    #clipped_pos = list(set(clipped_pos))
    bamfile.close()

    clipped_pos_cnt = Counter(clipped_pos)

    # cPickle data persistence
    with bz2.BZ2File(outFile, 'wb') as f:
        pickle.dump((clipped_pos_cnt, clipped_read_1, clipped_read_2), f)


def main():
    wd = "/Users/lsantuari/Documents/Data/HPC/DeepSV/Artificial_data/SURVIVOR-master/Debug/"
    inputBAM = wd + "reads_chr17_SURV10kDEL_INS_Germline2_Somatic1_mapped/GS/mapping/" + "GS_dedup.subsampledto30x.cr.bam"

    parser = argparse.ArgumentParser(description='Get clipped reads positions')
    parser.add_argument('-b', '--bam', type=str,
                        default=inputBAM,
                        help="Specify input file (BAM)")
    parser.add_argument('-c', '--chr', type=str, default='17',
                        help="Specify chromosome")
    parser.add_argument('-o', '--out', type=str, default='clipped_read_pos.pbz2',
                        help="Specify output")

    args = parser.parse_args()

    t0 = time()
    get_clipped_read_positions(ibam=args.bam, chrName=args.chr, outFile=args.out)
    print(time() - t0)


if __name__ == '__main__':
    main()