# Imports
import argparse
import re

import pysam
from pysam import VariantFile

from collections import Counter
from intervaltree import IntervalTree
from collections import defaultdict
import numpy as np
import gzip
import bz2file
import os, errno
import pickle
from time import time
import pandas as pd
from plotnine import *
import pprint

import logging
import csv
import statistics

# Default BAM file for testing
# On the HPC
# wd = '/hpc/cog_bioinf/ridder/users/lsantuari/Datasets/DeepSV/artificial_data/run_test_INDEL/samples/T0/BAM/T0/mapping'
# inputBAM = wd + "T0_dedup.bam"
# Locally
wd = '/Users/lsantuari/Documents/Data/HPC/DeepSV/Artificial_data/run_test_INDEL/BAM/'
inputBAM = wd + "T1_dedup.bam"

# Chromosome lengths for reference genome hg19/GRCh37
chrom_lengths = {'1': 249250621, '2': 243199373, '3': 198022430, '4': 191154276, '5': 180915260, '6': 171115067, \
                 '7': 159138663, '8': 146364022, '9': 141213431, '10': 135534747, '11': 135006516, '12': 133851895, \
                 '13': 115169878, '14': 107349540, '15': 102531392, '16': 90354753, '17': 81195210, '18': 78077248, \
                 '19': 59128983, '20': 63025520, '21': 48129895, '22': 51304566, 'X': 155270560, \
                 'Y': 59373566, 'MT': 16569}

# Flag used to set either paths on the local machine or on the HPC
HPC_MODE = False

# Only clipped read positions supported by at least min_cr_support clipped reads are considered
min_cr_support = 3
# Window half length
win_hlen = 100
# Window size
win_len = win_hlen * 2

__bpRE__ = None
__symbolicRE__ = None


# Classes

class SVRecord_SUR:

    def __init__(self, record):
        if type(record) != pysam.VariantRecord:
            raise TypeError('VCF record is not of type pysam.VariantRecord')

        self.chrom = record.chrom
        self.chrom2 = record.info['CHR2']
        self.start = record.pos
        self.end = record.stop
        self.supp_vec = record.info['SUPP_VEC']
        self.svtype = record.info['SVTYPE']
        self.samples = record.samples


class SVRecord_nanosv:

    def __init__(self, record):

        if type(record) != pysam.VariantRecord:
            raise TypeError('VCF record is not of type pysam.VariantRecord')
        # print(record)

        ct, chr2, pos2, indellen = self.get_bnd_info(record)

        # print(record.info.keys())

        self.id = record.id
        self.chrom = record.chrom
        self.start = record.pos
        self.chrom2 = chr2
        self.end = pos2
        self.alt = record.alts[0]
        self.cipos = record.info['CIPOS']
        self.ciend = record.info['CIEND']
        self.filter = record.filter

        # Deletions are defined by 3to5 connection, same chromosome for start and end, start before end
        if ct == '3to5' and self.chrom == self.chrom2 and self.start <= self.end:
            self.svtype = 'DEL'
        else:
            self.svtype = record.info['SVTYPE']

    @staticmethod
    def stdchrom(chrom):

        if chrom[0] == 'c':
            return chrom[3:]
        else:
            return chrom

        # Modified from the function ctAndLocFromBkpt in mergevcf

    def locFromBkpt(self, ref, pre, delim1, pair, delim2, post):
        '''
        Function of the mergevcf tool by Jonathan Dursi (Simpson Lab)
        URL: https://github.com/ljdursi/mergevcf
        :param record: pysam.VariantRecord
        :return: tuple with connection (3' to 5', 3' to 3', 5' to 5' or 5' to 3'), chromosome and position of the
        second SV endpoint, length of the indel
        '''

        chpos = pair.split(':')
        # print(chpos[0])
        chr2 = self.stdchrom(chpos[0])
        pos2 = int(chpos[1])
        assert delim1 == delim2  # '['..'[' or ']'...']'
        joinedAfter = True
        extendRight = True
        connectSeq = ""

        if len(pre) > 0:
            connectSeq = pre
            joinedAfter = True
            assert len(post) == 0
        elif len(post) > 0:
            connectSeq = post
            joinedAfter = False

        if delim1 == "]":
            extendRight = False
        else:
            extendRight = True

        indellen = len(connectSeq) - len(ref)

        if joinedAfter:
            if extendRight:
                ct = '3to5'
            else:
                ct = '3to3'
        else:
            if extendRight:
                ct = '5to5'
            else:
                ct = '5to3'

        return ct, chr2, pos2, indellen

    def get_bnd_info(self, record):
        '''
        Function of the mergevcf tool by Jonathan Dursi (Simpson Lab)
        URL: https://github.com/ljdursi/mergevcf
        :param record: pysam.VariantRecord
        :return: tuple with connection (3' to 5', 3' to 3', 5' to 5' or 5' to 3'), chromosome and position of the
        second SV endpoint, length of the indel
        '''
        setupREs()

        altstr = str(record.alts[0])
        resultBP = re.match(__bpRE__, altstr)

        if resultBP:
            ct, chr2, pos2, indellen = self.locFromBkpt(str(record.ref), resultBP.group(1),
                                                        resultBP.group(2), resultBP.group(3), resultBP.group(4),
                                                        resultBP.group(5))
        return (ct, chr2, pos2, indellen)


class Label:

    def __init__(self, chr, position, label_dict, distance):
        self.chr = chr
        self.position = position
        self.label_dict = label_dict
        self.distance_from_bpj = distance


def setupREs():
    '''
    Function of the mergevcf tool by Jonathan Dursi (Simpson Lab)
    URL: https://github.com/ljdursi/mergevcf
    '''
    global __symbolicRE__
    global __bpRE__
    if __symbolicRE__ is None or __bpRE__ is None:
        __symbolicRE__ = re.compile(r'.*<([A-Z:]+)>.*')
        __bpRE__ = re.compile(r'([ACGTNactgn\.]*)([\[\]])([a-zA-Z0-9\.]+:\d+)([\[\]])([ACGTNacgtn\.]*)')


def get_chr_len_by_chr(ibam, chrName):
    # check if the BAM file exists
    assert os.path.isfile(ibam)
    # open the BAM file
    bamfile = pysam.AlignmentFile(ibam, "rb")

    # Extract chromosome length from the BAM header
    header_dict = bamfile.header
    chrLen = [i['LN'] for i in header_dict['SQ'] if i['SN'] == chrName][0]

    return chrLen


def get_chr_len_dict(ibam):
    # check if the BAM file exists
    assert os.path.isfile(ibam)
    # open the BAM file
    bamfile = pysam.AlignmentFile(ibam, "rb")

    # Extract chromosome length from the BAM header
    header_dict = bamfile.header
    chr_dict = {i['SN']: i['LN'] for i in header_dict['SQ']}

    return chr_dict


def create_dir(directory):
    '''
    Create a directory if it does not exist. Raises an exception if the directory exists.
    :param directory: directory to create
    :return: None
    '''
    try:
        os.makedirs(directory)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def load_clipped_read_positions(sampleName, chrName):

    channel_dir = '/Users/lsantuari/Documents/Data/HPC/DeepSV/GroundTruth'

    vec_type = 'clipped_read_pos'
    print('Loading CR positions for Chr%s' % chrName)
    # Load files
    if HPC_MODE:
        fn = '/'.join((sampleName, vec_type, chrName + '_' + vec_type + '.pbz2'))
    else:
        fn = '/'.join((channel_dir, sampleName, vec_type, chrName + '_' + vec_type + '.pbz2'))

    with bz2file.BZ2File(fn, 'rb') as f:
        cpos = pickle.load(f)

    # Filter by minimum support
    cr_pos = [elem for elem, cnt in cpos.items() if cnt >= min_cr_support]

    # Remove positions with windows falling off chromosome boundaries
    cr_pos = [pos for pos in cr_pos if win_hlen <= pos <= (chrom_lengths[chrName] - win_hlen)]

    return cr_pos


def load_all_clipped_read_positions(sampleName):

    cr_pos = {}
    for chrName in chrom_lengths.keys():
        cr_pos[chrName] = load_clipped_read_positions(sampleName, chrName)
    return cr_pos


def initialize_nanosv_vcf_paths(sampleName):
    vcf_files = dict()

    if HPC_MODE:

        if sampleName == 'NA12878':

            vcf_dir = '/hpc/cog_bioinf/kloosterman/shared/nanosv_comparison/'+sampleName

            for mapper in ['bwa', 'minimap2', 'ngmlr', 'last']:

                vcf_files[mapper] = dict()

                vcf_files[mapper]['nanosv'] = vcf_dir + '/' + mapper + '/' + mapper + '_nanosv.sorted.vcf'
                assert os.path.isfile(vcf_files[mapper]['nanosv'])

                vcf_files[mapper]['nanosv_sniffles_settings'] = vcf_dir + '/' + mapper + '/' + \
                                                                mapper + '_nanosv_with_sniffles_settings.sorted.vcf'
                assert os.path.isfile(vcf_files[mapper]['nanosv_sniffles_settings'])

                if mapper in ['bwa', 'ngmlr']:
                    vcf_files[mapper]['sniffles'] = vcf_dir + '/' + mapper + '/' + mapper + '_sniffles.sorted.vcf'
                    assert os.path.isfile(vcf_files[mapper]['sniffles'])

                    vcf_files[mapper]['sniffles_nanosv_settings'] = vcf_dir + '/' + mapper + '/' + \
                                                                    mapper + '_sniffles_with_nanosv_settings.sorted.vcf'
                    assert os.path.isfile(vcf_files[mapper]['sniffles_nanosv_settings'])

    else:

        if sampleName == 'NA12878':

            vcf_dir = '/Users/lsantuari/Documents/Data/HPC/DeepSV/GroundTruth/' + sampleName + '/SV'
            vcf_files = dict()

            for mapper in ['bwa', 'last']:

                vcf_files[mapper] = dict()

                vcf_files[mapper]['nanosv'] = vcf_dir + '/' + mapper + '/' + mapper + '_nanosv_pysam.sorted.vcf'
                assert os.path.isfile(vcf_files[mapper]['nanosv'])

                if mapper in ['bwa']:
                    vcf_files[mapper]['sniffles'] = vcf_dir + '/' + mapper + '/' + mapper + '_sniffles.sorted.vcf'
                    assert os.path.isfile(vcf_files[mapper]['sniffles'])

        elif sampleName == 'Patient1' or sampleName == 'Patient2':

            vcf_dir = '/Users/lsantuari/Documents/Data/HPC/DeepSV/GroundTruth/' + sampleName + '/SV'
            vcf_files = dict()

            for mapper in ['last']:
                vcf_files[mapper] = dict()

                vcf_files[mapper]['nanosv'] = vcf_dir + '/' + mapper + '/' + mapper + '_nanosv.sorted.vcf'
                assert os.path.isfile(vcf_files[mapper]['nanosv'])

    return vcf_files


def read_nanosv_vcf(sampleName):
    '''
    This function parses the entries of the nanosv VCF file into SVRecord_nanosv objects and
    returns them in a list
    :param sampleName: str, name of the sample to consider
    :return: list, list of SVRecord_nanosv objects with the SV entries from the nanosv VCF file
    '''

    # Initialize regular expressions
    setupREs()

    # Setup locations of VCF files
    vcf_files = initialize_nanosv_vcf_paths(sampleName)

    if sampleName == 'NA12878' or sampleName == 'Patient1' or sampleName == 'Patient2':

        # Reading the Last-mapped NanoSV VCF file
        filename = vcf_files['last']['nanosv']
        vcf_in = VariantFile(filename, 'r')

        sv = []

        # create sv list with SVRecord objects
        for rec in vcf_in.fetch():
            resultBP = re.match(__bpRE__, rec.alts[0])
            if resultBP:
                svrec = SVRecord_nanosv(rec)
                sv.append(svrec)

        # Select good quality (no LowQual, only 'PASS') deletions (DEL)
        sv = [svrec for svrec in sv if svrec.svtype == 'DEL'
              # if 'LowQual' not in list(svrec.filter)]
              if 'PASS' in list(svrec.filter)]

        # How many distinct FILTERs?
        # filter_set = set([f for svrec in sv for f in svrec.filter])
        # print(filter_set)

        # How many VCF record with a specific FILTER?
        # filter_list = sorted(filter_set)
        # s = pd.Series([sum(list(map(lambda x: int(f in x),
        #                             [list(svrec.filter) for svrec in sv])))
        #                for f in filter_list],
        #               index=filter_list)
        # s = s.append(pd.Series([len(sv)], index=['Total']))
        # s = s.sort_values()
        # Print pd.Series with stats on FILTERs
        # print(s)

        return sv


def get_labels_from_nanosv_vcf(sampleName, ibam):
    '''
    This function writes the label files based on the nanosv VCF file information

    :param sampleName: str, name of the sample considered
    :param chrName: str, chromosome name
    :param ibam: str, path of the BAM file in input
    :return: None
    '''

    # Lines to write in the BED file
    lines = []

    # def closest_loc(pos, pos_list):
    #     '''
    #
    #     :param pos: reference position
    #     :param pos_list: list of clipped read positions
    #     :return: tuple of (position, distance) for the closest position to the reference position
    #     '''
    #     pos_array = np.asarray(pos_list)
    #     deltas = np.abs(pos_array - pos)
    #     idx = np.argmin(deltas)
    #     return (pos_list[idx], deltas[idx])

    # Load SV list
    sv_list = read_nanosv_vcf(sampleName)

    # Select deletions (DELs)
    sv_list = [sv for sv in sv_list if sv.svtype == 'DEL']

    # list of chromosomes
    chr_list = set([var.chrom for var in sv_list])

    # print('Plotting CI distribution')
    # plot_ci_dist(sv_list, sampleName)

    # print(chr_list)
    print('Total # of DELs: %d' % len(sv_list))

    cnt = Counter([sv.chrom for sv in sv_list])
    chr_series = pd.Series([v for v in cnt.values()], index=cnt.keys())
    # print('# SVs per chromosome:')
    # print(chr_series)

    assert sum(chr_series) == len(sv_list)

    confint = 100

    labels_list = defaultdict(list)

    for chrName in chr_list:

        sv_list_chr = [var for var in sv_list if var.chrom == chrName]

        # Load CR positions
        cr_pos = load_clipped_read_positions(sampleName, chrName)

        # print(sorted(cr_pos))

        # Using IntervalTree for interval search
        t = IntervalTree()

        # print('# SVs in Chr: %d' % len(sv_list_chr))

        for var in sv_list_chr:
            # cipos[0] and ciend[0] are negative in the VCF file
            id_start = var.svtype + '_start'
            id_end = var.svtype + '_end'

            # id_start = '_'.join((var.chrom, str(var.start+var.cipos[0]),  str(var.start+var.cipos[1])))
            # id_end = '_'.join((var.chrom, str(var.end + var.ciend[0]), str(var.end+var.ciend[1])))
            assert var.start < var.end

            # print('var start -> %s:%d CIPOS: (%d, %d)' % (chrName, var.start, var.cipos[0], var.cipos[1]))
            # print('var end -> %s:%d CIEND: (%d, %d)' % (chrName, var.end, var.ciend[0], var.ciend[1]))

            t[var.start + var.cipos[0]:var.start + var.cipos[1] + 1] = id_start
            t[var.end + var.ciend[0]:var.end + var.ciend[1] + 1] = id_end

            # t[var.start - confint:var.start + confint + 1] = var.svtype + '_start'
            # t[var.end - confint:var.end + confint + 1] = var.svtype + '_end'

        label_search = [sorted(t[p - win_hlen: p + win_hlen + 1]) for p in cr_pos]

        crpos_full_ci, crpos_partial_ci = get_crpos_win_with_ci_overlap(sv_list_chr, cr_pos)

        # print('Clipped read positions with complete CI overlap: %s' % crpos_full_ci)
        # print('Clipped read positions with partial CI overlap: %s' % crpos_partial_ci)
        # crpos_ci_isec = set(crpos_full_ci) & set(crpos_partial_ci)
        # print('Intersection: %s' % crpos_ci_isec)

        print('# CRPOS in CI: %d' % len([l for l in label_search if len(l) != 0]))

        count_zero_hits = 0
        count_multiple_hits = 0

        label_ci_full_overlap = []

        for elem, pos in zip(label_search, cr_pos):
            if len(elem) == 1:
                # print(elem)
                if pos in crpos_full_ci:
                    label_ci_full_overlap.append(elem[0].data)

                    lines.append(bytes(chrName + '\t' + str(elem[0].begin) + '\t' \
                                       + str(elem[0].end) + '\t' + elem[0].data + '\n', 'utf-8'))

                elif pos in crpos_partial_ci:
                    label_ci_full_overlap.append('UK')
                else:
                    label_ci_full_overlap.append('noSV')
            elif len(elem) == 0:
                count_zero_hits += 1
                label_ci_full_overlap.append('noSV')
            elif len(elem) > 1:
                count_multiple_hits += 1
                label_ci_full_overlap.append('UK')
                # if pos in crpos_full_ci:
                #     label_ci_full_overlap.append('Multiple_Full')
                #     #print('Multiple full: %s -> %s' % ( [d for s,e,d in elem], set([d for s,e,d in elem]) ) )
                #     #for s, e, d in elem:
                #     #    print('%d -> %d %d %s' % (pos, s, e, d))
                # #else:
                #     #label_ci_full_overlap.append('Multiple_Partial')

        print('CR positions: %d' % len(cr_pos))
        print('Label length: %d' % len(label_search))
        assert len(label_ci_full_overlap) == len(cr_pos)

        print('Label_CI_full_overlap: %s' % Counter(label_ci_full_overlap))
        print('Zero hits:%d' % count_zero_hits)
        print('Multiple hits:%d' % count_multiple_hits)

        # Write labels for chromosomes
        if not HPC_MODE:
            channel_dir = '/Users/lsantuari/Documents/Data/HPC/DeepSV/GroundTruth'
        else:
            channel_dir = ''

        output_dir = '/'.join((channel_dir, sampleName, 'label'))
        create_dir(output_dir)

        # print(output_dir)

        with gzip.GzipFile('/'.join((output_dir, chrName + '_label_ci_full_overlap.npy.gz')), "w") as f:
            np.save(file=f, arr=label_ci_full_overlap)
        f.close()

    # Write BED file with labelled CI positions
    outfile = sampleName + '_nanosv_vcf_ci_labelled.bed.gz'
    f = gzip.open(outfile, 'wb')
    try:
        # use set to make lines unique
        for l in set(lines):
            f.write(l)
    finally:
        f.close()


def write_sv_without_cr(sampleName, ibam):
    '''
    Writes NanoSV SVs with no clipped read support into a BED file
    :param sampleName: name of the sample to consider
    :param ibam: path of the BAM file. Used to get chromosome lengths
    :return: None
    '''

    # Load SV list
    sv_list = read_nanosv_vcf(sampleName)
    # Select deletions
    sv_list = [sv for sv in sv_list if sv.svtype == 'DEL' if sv.chrom == sv.chrom2 if sv.start < sv.end]
    # list of chromosomes
    chr_list = set([var.chrom for var in sv_list])

    var_with_cr = 0

    bedout = open(sampleName + '_nanosv_no_cr.bed', 'w')

    for chrName in sorted(chr_list):

        sv_list_chr = [var for var in sv_list if var.chrom == chrName]

        # Load CR positions
        cr_pos = load_clipped_read_positions(sampleName, chrName)

        # Using IntervalTree for interval search
        t = IntervalTree()

        for var in sv_list_chr:
            id_start = var.svtype + '_start'
            id_end = var.svtype + '_end'

            t[var.start + var.cipos[0]:var.start + var.cipos[1] + 1] = id_start
            t[var.end + var.ciend[0]:var.end + var.ciend[1] + 1] = id_end

        hits = [sorted(t[p]) for p in cr_pos]

        inter_list = [nested_elem for elem in hits for nested_elem in elem]

        start_var_list = []
        end_var_list = []

        for start, end, data in inter_list:
            se = data.split('_')[1]
            if se == 'start':
                start_var_list.append(start)
            elif se == 'end':
                end_var_list.append(start)

        for var in sv_list_chr:

            if var.start + var.cipos[0] in start_var_list and \
                    var.end + var.ciend[0] in end_var_list:
                var_with_cr += 1

            if var.start + var.cipos[0] not in start_var_list:
                bedout.write('\t'.join((var.chrom,
                                        str(var.start + var.cipos[0]),
                                        str(var.start + var.cipos[1]))) + '\n')
            if var.end + var.ciend[0] not in end_var_list:
                bedout.write('\t'.join((var.chrom,
                                        str(var.end + var.ciend[0]),
                                        str(var.end + var.ciend[1]))) + '\n')

    bedout.close()

    print('VCF entries with CR on both sides: %d/%d' % (var_with_cr, len(sv_list)))


def plot_ci_dist(sv_list, sampleName):
    '''
    Saves the plots of the distributions of the confidence intervals reported by NanoSV
    :param sv_list: list, a list of SVs
    :param sampleName: str, name of the sample to consider
    :return: None
    '''

    # Plot distribution of CIPOS and CIEND
    df = pd.DataFrame({
        "cipos": np.array([var.cipos[1] + abs(var.cipos[0]) for var in sv_list]),
        "ciend": np.array([var.ciend[1] + abs(var.ciend[0]) for var in sv_list])
    })

    print('Max CIPOS:%d, max CIEND:%d' % (max(df['cipos']), max(df['ciend'])))

    output_dir = '/Users/lsantuari/Documents/Data/germline/plots'
    # the histogram of the data
    p = ggplot(aes(x='cipos'), data=df) + \
        geom_histogram(binwidth=1) + ggtitle(' '.join((sampleName, 'CIPOS', 'distribution')))
    p.save(filename='_'.join((sampleName, 'CIPOS', 'distribution')), path=output_dir)

    p = ggplot(aes(x='ciend'), data=df) + \
        geom_histogram(binwidth=1) + ggtitle(' '.join((sampleName, 'CIEND', 'distribution')))
    p.save(filename='_'.join((sampleName, 'CIEND', 'distribution')), path=output_dir)


def get_nanosv_manta_sv_from_SURVIVOR_merge_VCF(sampleName):
    '''

    :param sampleName: sample ID
    :return: list of SVRecord_nanosv that overlap Manta SVs as reported in SURVIVOR merge VCF file
    '''
    sv_sur = read_SURVIVOR_merge_VCF(sampleName)
    sv_nanosv = read_nanosv_vcf(sampleName)

    common_sv = defaultdict(list)
    for sv in sv_sur:
        # print(sv.supp_vec)
        # 0:Delly, 1:GRIDSS, 2:last_nanosv, 3:Lumpy, 4:Manta
        if sv.supp_vec[2] == '1' and sv.supp_vec[4] == '1':
            # print(sv.samples.keys())
            svtype = sv.samples.get('NanoSV').get('TY')
            coord = sv.samples.get('NanoSV').get('CO')
            if svtype == 'DEL' and coord != 'NaN':
                coord_list = re.split('-|_', coord)
                assert len(coord_list) == 4
                common_sv[coord_list[0]].append(int(coord_list[1]))
    # print(common_sv.keys())
    sv_nanosv_manta = [sv for sv in sv_nanosv
                       if sv.chrom in common_sv.keys() if sv.start in common_sv[sv.chrom]]
    # print(common_sv['1'])
    # print(len(sv_nanosv_manta))
    return sv_nanosv_manta


# END: NanoSV specific functions

# START: BED specific functions

def read_bed_sv(inbed):
    # Check file existence
    assert os.path.isfile(inbed)
    # Dictionary with chromosome keys to store SVs
    sv_dict = defaultdict(list)

    with(open(inbed, 'r')) as bed:
        for line in bed:
            columns = line.rstrip().split("\t")
            chrom = str(columns[0])
            if columns[3][:3] == "DEL":
                sv_dict[chrom].append((int(columns[1]), int(columns[2]), columns[3]))

    # print(sv_dict)
    return sv_dict


def get_labels_from_bed(sampleName, ibam, inbed):
    '''

    :param sampleName: name of sample considered
    :param ibam: path to the BAM file. Needed to get chromosome length
    :param inbed: path to the BED file with SVs
    :return: dictionary with list of labels per chromosome
    '''

    print('sample = %s' % sampleName)
    print('window = %d' % win_len)

    sv_list = read_bed_sv(inbed)

    # chr_list = sv_list.keys()
    # Use Chr1 for testing
    chr_list = ['1']

    labels_list = defaultdict(list)

    for chrName in chr_list:

        sv_list_chr = sv_list[chrName]

        # Load CR positions
        cr_pos = load_clipped_read_positions(sampleName, chrName)

        # print(sorted(cr_pos))

        # Using IntervalTree for interval search
        t = IntervalTree()

        # print('# Breakpoints in Chr: %d' % len(sv_list_chr))

        for start, end, lab in sv_list_chr:
            t[start:end + 1] = lab

        label = [sorted(t[p - win_hlen: p + win_hlen + 1]) for p in cr_pos]

        crpos_full_ci, crpos_partial_ci = get_crpos_win_with_bed_overlap(sv_list_chr, cr_pos)

        # print('Clipped read positions with complete CI overlap: %s' % crpos_full_ci)
        # print('Clipped read positions with partial CI overlap: %s' % crpos_partial_ci)

        crpos_ci_isec = set(crpos_full_ci) & set(crpos_partial_ci)
        # print('Intersection should be empty: %s' % crpos_ci_isec)
        assert len(crpos_ci_isec) == 0

        # print('# CRPOS in BED: %d' % len([l for l in label if len(l) != 0]))

        count_zero_hits = 0
        count_multiple_hits = 0

        label_ci_full_overlap = []

        for elem, pos in zip(label, cr_pos):
            # Single match
            if len(elem) == 1:
                # print(elem)
                if pos in crpos_full_ci:
                    label_ci_full_overlap.append(elem[0].data)
                elif pos in crpos_partial_ci:
                    label_ci_full_overlap.append('UK')
                else:
                    label_ci_full_overlap.append('noSV')
            # No match
            elif len(elem) == 0:
                count_zero_hits += 1
                label_ci_full_overlap.append('noSV')
            # Multiple match
            elif len(elem) > 1:
                count_multiple_hits += 1
                label_ci_full_overlap.append('UK')

        # print('CR positions: %d' % len(cr_pos))
        # print('Label length: %d' % len(label))
        assert len(label_ci_full_overlap) == len(cr_pos)

        # print('Label_CI_full_overlap: %s' % Counter(label_ci_full_overlap))
        # print('Zero hits:%d' % count_zero_hits)
        # print('Multiple hits:%d' % count_multiple_hits)

        # if not HPC_MODE:
        #     channel_dir = '/Users/lsantuari/Documents/Data/HPC/DeepSV/GroundTruth'
        # else:
        #     channel_dir = ''
        #
        # output_dir = '/'.join((channel_dir, sampleName, 'label'))
        # create_dir(output_dir)

        # print(output_dir)

        # with gzip.GzipFile('/'.join((output_dir, chrName + '_label_ci_full_overlap.npy.gz')), "w") as f:
        #     np.save(file=f, arr=label_ci_full_overlap)
        # f.close()

        labels_list[chrName] = label_ci_full_overlap

    return labels_list


def get_crpos_win_with_ci_overlap(sv_list, cr_pos):
    '''

    :param sv_list: list, list of SVs
    :param cr_pos: list, list of clipped read positions
    :return: list, list of clipped read positions whose window completely overlap either the CIPOS interval
    or the CIEND interval
    '''

    def get_tree(cr_pos):
        # Tree with windows for CR positions
        tree = IntervalTree()
        # Populate tree
        for pos in cr_pos:
            tree[pos - win_hlen:pos + win_hlen + 1] = pos
        return tree

    def search_tree_with_sv(sv_list, tree, citype):

        if citype == 'CIPOS':
            return [sorted(tree[var.start + var.cipos[0]: var.start + var.cipos[1] + 1])
                    for var in sv_list]
        elif citype == 'CIEND':
            return [sorted(tree[var.end + var.ciend[0]: var.end + var.ciend[1] + 1])
                    for var in sv_list]

    def get_overlap(tree, sv_list, citype):

        rg_overlap = search_tree_with_sv(sv_list, tree, citype)

        if citype == 'CIPOS':
            start_ci = [var.start + var.cipos[0] for var in sv_list]
            end_ci = [var.start + var.cipos[1] for var in sv_list]
        elif citype == 'CIEND':
            start_ci = [var.end + var.ciend[0] for var in sv_list]
            end_ci = [var.end + var.ciend[1] for var in sv_list]

        full = []
        partial = []
        for rg, start, end in zip(rg_overlap, start_ci, end_ci):
            for elem in rg:
                elem_start, elem_end, elem_data = elem
                if start >= elem_start and end <= elem_end:
                    # print('CIPOS->Full: %s\t%d\t%d' % (elem, start, end))
                    full.append(elem_data)
                else:
                    # print('CIPOS->Partial: %s\t%d\t%d' % (elem, start, end))
                    partial.append(elem_data)

        return partial, full

    t = get_tree(cr_pos)
    partial_cipos, full_cipos = get_overlap(t, sv_list, 'CIPOS')
    partial_ciend, full_ciend = get_overlap(t, sv_list, 'CIEND')

    cr_full_overlap = sorted(full_cipos + full_ciend)
    cr_partial_overlap = sorted(partial_cipos + partial_ciend)

    return sorted(list(set(cr_full_overlap))), sorted(list(set(cr_partial_overlap) - set(cr_full_overlap)))


def get_crpos_win_with_bed_overlap(sv_list, cr_pos):
    '''
    :param sv_list: list, list of SV bed intervals
    :param cr_pos: list, list of clipped read positions
    :return: list, list of clipped read positions whose window completely overlap either the CIPOS interval
    or the CIEND interval
    '''
    # Tree with windows for CR positions
    t_cr = IntervalTree()

    for pos in cr_pos:
        t_cr[pos - win_hlen:pos + win_hlen + 1] = pos

    cr_full_overlap = []
    cr_partial_overlap = []

    rg_overlap = [sorted(t_cr[start: end + 1]) for start, end, lab in sv_list]
    # print('Range overlap: %s' % rg_overlap)

    for rg, start, end in zip(rg_overlap,
                              [start for start, end, lab in sv_list],
                              [end for start, end, lab in sv_list]):
        for elem in rg:
            elem_start, elem_end, elem_data = elem
            if start >= elem_start and end <= elem_end:
                cr_full_overlap.append(elem_data)
            else:
                cr_partial_overlap.append(elem_data)

    cr_full_overlap = sorted(cr_full_overlap)
    cr_partial_overlap = sorted(cr_partial_overlap)

    return sorted(list(set(cr_full_overlap))), sorted(list(set(cr_partial_overlap) - set(cr_full_overlap)))


# END: BED specific functions

def read_SURVIVOR_merge_VCF(sampleName):
    '''
    Reads the SURVIVOR merge VCF output and returns a list of SVs in as SVRecord_SUR objects
    :param sampleName: sample to consider
    :return: a list of SVs in as SVRecord_SUR objects
    '''

    if HPC_MODE:
        # To fill with HPC path
        survivor_vcf = ''
    else:
        if sampleName == 'NA12878':
            context = 'trio'
        elif sampleName == 'Patient1' or sampleName == 'Patient2':
            context = 'patients'

        survivor_vcf = '/Users/lsantuari/Documents/Data/germline/' + context + \
                       '/' + sampleName + '/SV/Filtered/survivor_merge.vcf'

    vcf_in = VariantFile(survivor_vcf)
    samples_list = list((vcf_in.header.samples))
    samples = samples_list
    # samples = [w.split('_')[0].split('/')[1] for w in samples_list]
    # print(samples)

    sv = []

    # create sv list with SVRecord_SUR objects
    for rec in vcf_in.fetch():
        # avoid SVs on chromosomes Y and MT
        if rec.chrom not in ['Y', 'MT'] and rec.info['CHR2'] not in ['Y', 'MT']:
            # print(rec)
            # print(dir(rec))
            sv.append(SVRecord_SUR(rec))

    return sv


def load_NoCR_positions():
    '''
    This function provides an overview of SV positions without clipped read support that are stored in the
    no_clipped_read_positions file.
    :return: None
    '''

    no_cr_File = '/Users/lsantuari/Documents/Data/HMF/ChannelMaker_results/HMF_Tumor/labels/' \
                 + 'no_clipped_read_positions.pk.gz'
    with gzip.GzipFile(no_cr_File, "r") as f:
        no_clipped_read_pos = pickle.load(f)
    f.close()
    print(list(no_clipped_read_pos))


# Methods to save to BED format

def clipped_read_positions_to_bed(sampleName, ibam):
    chrlist = list(map(str, range(1, 23)))
    chrlist.extend(['X', 'Y'])
    # print(chrlist)

    lines = []
    for chrName in chrlist:

        crpos_list = load_clipped_read_positions(sampleName, chrName)
        lines.extend(
            [bytes(chrName + '\t' + str(crpos) + '\t' + str(crpos + 1) + '\n', 'utf-8') for crpos in crpos_list])

    crout = sampleName + '_clipped_read_pos.bed.gz'
    f = gzip.open(crout, 'wb')
    try:
        for l in lines:
            f.write(l)
    finally:
        f.close()


def nanosv_vcf_to_bed(sampleName):
    # Load SV list
    # sv_list = read_nanosv_vcf(sampleName)
    # nanoSV & Manta SVs
    sv_list = get_nanosv_manta_sv_from_SURVIVOR_merge_VCF(sampleName)

    # Select deletions
    sv_list = [sv for sv in sv_list if sv.svtype == 'DEL' if sv.chrom == sv.chrom2 if sv.start < sv.end]

    lines = []
    for sv in sv_list:
        lines.append(bytes(sv.chrom + '\t' + str(sv.start + sv.cipos[0]) + '\t' \
                           + str(sv.start + sv.cipos[1] + 1) + '\t' + 'DEL_start' + '\n', 'utf-8'))
        lines.append(bytes(sv.chrom + '\t' + str(sv.end + sv.ciend[0]) + '\t' \
                           + str(sv.end + sv.ciend[1] + 1) + '\t' + 'DEL_end' + '\n', 'utf-8'))

    # outfile = sampleName + '_nanosv_vcf_ci.bed.gz'
    outfile = sampleName + '_manta_nanosv_vcf_ci.bed.gz'
    f = gzip.open(outfile, 'wb')
    try:
        for l in lines:
            f.write(l)
    finally:
        f.close()


# Get labels
def get_labels(sampleName):

    print(f'running {sampleName}')

    def make_tree_from_bed(sv_list):

        # Using IntervalTree for interval search
        t = IntervalTree()
        # print('# Breakpoints in Chr: %d' % len(sv_list_chr))
        for start, end, lab in sv_list:
            t[start:end + 1] = lab
        return t

    def make_tree_from_vcf(sv_list):

        # Using IntervalTree for interval search
        t = IntervalTree()

        for var in sv_list:
            # cipos[0] and ciend[0] are negative in the VCF file
            id_start = var.svtype + '_start'
            id_end = var.svtype + '_end'

            assert var.start < var.end

            # print('var start -> %s:%d CIPOS: (%d, %d)' % (chrName, var.start, var.cipos[0], var.cipos[1]))
            # print('var end -> %s:%d CIEND: (%d, %d)' % (chrName, var.end, var.ciend[0], var.ciend[1]))

            t[var.start + var.cipos[0]:var.start + var.cipos[1] + 1] = id_start
            t[var.end + var.ciend[0]:var.end + var.ciend[1] + 1] = id_end

            return t

    cr_pos_dict = load_all_clipped_read_positions(sampleName)

    sv_dict = dict()
    labels = dict()

    sv_dict['nanosv'] = read_nanosv_vcf(sampleName)
    sv_dict['nanosv_manta'] = get_nanosv_manta_sv_from_SURVIVOR_merge_VCF(sampleName)

    if sampleName == 'NA12878':
        inbed = '/Users/lsantuari/Documents/IGV/Screenshots/' + sampleName + \
                '/overlaps/lumpy-Mills2011_manta_nanosv.bed'
        sv_dict['nanosv_manta_Mills2011'] = read_bed_sv(inbed)

    for sv_dict_key in sv_dict.keys():
    #for sv_dict_key in ['nanosv_manta_Mills2011']:

        print(f'running {sv_dict_key}')

        labels[sv_dict_key] = {}

        sv_list = sv_dict[sv_dict_key]

        if type(sv_list) is list:
            # Select deletions (DELs)
            sv_list = [sv for sv in sv_list if sv.svtype == 'DEL']
            # list of chromosomes
            chr_list = set([var.chrom for var in sv_list])
        else:
            chr_list = sv_list.keys()

        for chrName in chr_list:

            print(f'running Chr{chrName}')

            labels[sv_dict_key][chrName] = []

            # Load CR positions, once
            cr_pos = cr_pos_dict[chrName]

            # print(type(sv_list))

            # VCF file SVs
            if type(sv_list) is list:

                sv_list_chr = [var for var in sv_list if var.chrom == chrName]
                tree = make_tree_from_vcf(sv_list_chr)

                crpos_full, crpos_partial = get_crpos_win_with_ci_overlap(sv_list_chr, cr_pos)

            # BED file SVs
            else:

                sv_list_chr = sv_list[chrName]
                tree = make_tree_from_bed(sv_list_chr)

                crpos_full, crpos_partial = get_crpos_win_with_bed_overlap(sv_list_chr, cr_pos)

            label_search = [sorted(tree[p - win_hlen: p + win_hlen + 1]) for p in cr_pos]

            count_zero_hits = 0
            count_multiple_hits = 0

            for elem, pos in zip(label_search, cr_pos):
                if len(elem) == 1:
                    # print(elem)
                    if pos in crpos_full:
                        labels[sv_dict_key][chrName].append(elem[0].data)
                    elif pos in crpos_partial:
                        labels[sv_dict_key][chrName].append('UK')
                    else:
                        labels[sv_dict_key][chrName].append('noSV')
                elif len(elem) == 0:
                    count_zero_hits += 1
                    labels[sv_dict_key][chrName].append('noSV')
                elif len(elem) > 1:
                    count_multiple_hits += 1
                    labels[sv_dict_key][chrName].append('UK')

            assert len(labels[sv_dict_key][chrName]) == len(cr_pos)

    # pp = pprint.PrettyPrinter(depth=6)
    # for key in sv_dict:
    #     pp.pprint(sv_dict[key])

    if not HPC_MODE:
        channel_dir = '/Users/lsantuari/Documents/Data/HPC/DeepSV/GroundTruth'
    else:
        channel_dir = ''

    output_dir = '/'.join((channel_dir, sampleName, 'label_npy'))
    create_dir(output_dir)

    # print(output_dir)

    with gzip.GzipFile('/'.join((output_dir, 'labels.npy.gz')), "w") as f:
        np.save(file=f, arr=labels)
    f.close()


def main():
    '''
    Main function for parsing the input arguments and calling the channel_maker function
    :return: None
    '''

    parser = argparse.ArgumentParser(description='Create channels from saved data')
    parser.add_argument('-b', '--bam', type=str,
                        default=inputBAM,
                        help="Specify input file (BAM)")
    parser.add_argument('-o', '--out', type=str, default='channel_maker.npy.gz',
                        help="Specify output")
    parser.add_argument('-s', '--sample', type=str, default='NA12878',
                        help="Specify sample")

    args = parser.parse_args()

    # bed_dict = dict()
    # for sampleName in ['NA12878', 'Patient1', 'Patient2']:
    #     bed_dict[sampleName] = dict()
    #     bed_dict[sampleName]['NanoSV_Manta'] = '/Users/lsantuari/Documents/IGV/Screenshots/' + sampleName + '/' + \
    #                                            sampleName + '_manta_nanosv_vcf_ci.bed'
    #     if sampleName == 'NA12878':
    #         bed_dict[sampleName]['NanoSV_Manta_Mills2011'] = '/Users/lsantuari/Documents/IGV/Screenshots/' + sampleName \
    #                                                          + '/overlaps/' + 'lumpy-Mills2011_manta_nanosv.bed'

    t0 = time()

    # Iterate over samples and BED files
    # for sampleName in bed_dict.keys():
    #     print('Running get_labels for %s' % sampleName)
    #     get_labels(sampleName)
    # for bed_file in bed_dict[sampleName].keys():
    #     print('Processing BED file: %s' % bed_file)
    #     labels_list = get_labels_from_bed(sampleName=sampleName, ibam=args.bam, inbed=bed_dict[sampleName][bed_file])
    #     for c in labels_list.keys():
    #         print('%s -> %s'% (c, Counter(labels_list[c])))

    # write_sv_without_cr(sampleName=args.sample, ibam=args.bam)

    # clipped_read_positions_to_bed(sampleName=args.sample, ibam=args.bam)
    # nanosv_vcf_to_bed(sampleName=args.sample)

    # get_nanosv_manta_sv_from_SURVIVOR_merge_VCF(sampleName=args.sample)

    for sampleName in ['NA12878', 'Patient1', 'Patient2']:
        get_labels(sampleName=sampleName)

    print('Elapsed time making labels = %f' % (time() - t0))


if __name__ == '__main__':
    main()
