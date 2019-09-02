from __future__ import absolute_import, division, print_function, unicode_literals

import sys
import dask.array as da
import h5py
import os, errno
import numpy as np
import matplotlib.pyplot as plt
import json
import gzip
import pandas as pd
import logging
from time import time
import argparse
import bcolz

from sklearn.utils import class_weight
from sklearn.model_selection import StratifiedKFold

from keras.models import Sequential
from keras.layers import Dense, Activation, Convolution1D, Lambda, \
    Convolution2D, Flatten, \
    Reshape, LSTM, Dropout, TimeDistributed, BatchNormalization, MaxPooling1D
from keras.regularizers import l2
from keras.optimizers import Adam
from keras.utils import to_categorical

from keras.callbacks import TensorBoard, EarlyStopping
from keras.models import load_model

from sklearn.model_selection import train_test_split
from collections import Counter

from model_functions import create_model_with_mcfly, train_model_with_mcfly, evaluate_model, create_dir

import tensorflow as tf

gpu_options = tf.compat.v1.GPUOptions(allow_growth=True)
sess = tf.compat.v1.Session(config=tf.compat.v1.ConfigProto(gpu_options=gpu_options,
                                                            intra_op_parallelism_threads=0,
                                                            inter_op_parallelism_threads=0,
                                                            allow_soft_placement=True
                                                            ))
tf.compat.v1.keras.backend.set_session(sess)

mapclasses_all = {'DEL': 0, 'noDEL': 1, 'UK_other': 2, 'UK_single_partial': 3, 'UK_multiple_on_either_windows': 4}
mapclasses = {'DEL': 0, 'noDEL': 1}


# mapclasses = {'DEL_start': 0, 'DEL_end': 1, 'noDEL': 2}


def get_channel_labels():
    # Fill labels for legend

    labels = list()
    labels.append("coverage")
    labels.append("discordant_reads_F")
    labels.append("discordant_reads_R")
    labels.append("mean_read_quality")

    labels.append("median_base_quality")
    labels.append("SNV_frequency")

    labels.append("#left_clipped_reads")
    labels.append("#right_clipped_reads")

    labels.append("#left split reads")
    labels.append("#right split reads")

    labels.append("#CIGAR_D_left_reads")
    labels.append("#CIGAR_D_right_reads")
    labels.append("#CIGAR_I_right_reads")

    labels.append("INV_before")
    labels.append("INV_after")
    labels.append("DUP_before")
    labels.append("DUP_after")
    labels.append("TRA_opposite")
    labels.append("TRA_same")

    for direction in ['Forward', 'Reverse']:
        for clipped in ['Left', 'Right', 'All']:
            for value in ['median']:
                # for value in ['median']:
                labels.append(direction + '_' + clipped + '_ClippedRead_distance_' + value)

    for clipped in ['L', 'R']:
        for value in ['median']:
            labels.append(clipped + '_SplitRead_distance_' + value)

    labels.append("Mappability")

    for nuc in ['A', 'T', 'C', 'G', 'N']:
        labels.append("One_hot_encoding_" + nuc)

    # for k, l in enumerate(labels):
    #     print(str(k) + ': ' + l)

    return labels


def plot_channels(outDir, X, z, l):
    mapclasses_rev = {v: k for k, v in mapclasses.items()}

    title_plot = mapclasses_rev[z] + '_' + str(l)
    print('Plotting %s' % title_plot)

    number_channels = X.shape[1]
    # print(number_channels)
    label = get_channel_labels()
    # print(len(label))

    fig = plt.figure(figsize=(6, 4))
    fig.suptitle(mapclasses_rev[z] + ' ' + l, fontsize=20)

    for j in range(number_channels):  # - 1, -1, -1):

        if sum(X[:, j]) != 0:
            X_win = (X[:, j] - min(X[:, j])) / max(X[:, j])
        else:
            X_win = X[:, j]

        Z = [x + j + 1 for x in X_win]
        plt.plot(Z, label=label[j], linewidth=0.9)
        plt.fill_between(Z, 0, interpolate=True)
        plt.legend(bbox_to_anchor=(1.05, 1), loc=2, borderaxespad=0., prop={'size': 5})
        plt.yticks(range(0, len(label) + 1, 1))
        plt.tick_params(axis='both', which='major', labelsize=5)
        plt.axvline(x=200, color='r', linewidth=0.05, alpha=0.5)
        plt.axvline(x=210, color='r', linewidth=0.05, alpha=0.5)

    plt.savefig(os.path.join(outDir, title_plot + '.png'),
                format='png', dpi=300, bbox_inches='tight')
    plt.show()
    # plt.close()


def get_hpc_flag():
    fileDir = os.path.dirname(os.path.abspath(__file__))
    parentDir = os.path.dirname(fileDir)
    newPath = os.path.join(parentDir, 'genome_wide')
    # print('appending path: ' + newPath)
    sys.path.append(newPath)
    # print(os.listdir(newPath))

    from functions import get_config_file

    config = get_config_file()
    HPC_MODE = config["DEFAULT"]["HPC_MODE"]

    return HPC_MODE


def get_data_dir(sampleName):
    HPC_MODE = get_hpc_flag()

    channel_dir = \
        os.path.join('/Users/lsantuari/Documents/Processed/channel_maker_output',
                     sampleName) if not HPC_MODE else \
            os.path.join('/hpc/cog_bioinf/ridder/users/lsantuari/Processed/DeepSV/channel_data',
                         sampleName)

    return channel_dir


def data(sampleName):
    def filter_labels(X, y, win_ids):
        # print(y)
        keep = [i for i, v in enumerate(y) if v in ['DEL', 'noDEL']]
        # print(keep)
        X = X[np.array(keep)]
        # print(y)
        y = [y[i] for i in keep]
        win_ids = [win_ids[i] for i in keep]

        print(Counter(y))
        return X, y, win_ids

    logging.info('Loading data for {}...'.format(sampleName))

    channel_dir = get_data_dir(sampleName)

    y = []
    # numpy_array = []
    win_ids = []

    # class_dict = {'positive': 'DEL', 'negative': 'noDEL'}

    # for label_type in ['positive', 'negative']:

    # fn = os.path.join(channel_dir, 'windows', label_type + '.hdf5')
    # d = h5py.File(fn)
    #
    # fn = os.path.join(channel_dir, 'windows', label_type + '_labels.json.gz')
    #
    # with gzip.GzipFile(fn, 'r') as fin:
    #     labels = json.loads(fin.read().decode('utf-8'))

    for label_type in ['test']:
        carray_file = os.path.join(channel_dir,
                                   'windows', label_type + '_win200_carray')
        logging.info('Loading file: {}'.format(carray_file))
        assert os.path.exists(carray_file), carray_file + ' not found'
        X = bcolz.open(rootdir=carray_file)

        labels = X.attrs['labels']
        y.extend(labels.values())
        win_ids.extend(labels.keys())

        # if label_type == 'positive':
        #
        #     numpy_array.append(d['data'])
        #     y.extend(labels.values())
        #     win_ids.extend(labels.keys())
        #
        # elif label_type == 'negative':
        #
        #     rnd_idx = np.random.choice(d['data'].shape[0], size=numpy_array[0].shape[0], replace=True)
        #
        #     labs = list(d['labels'].item().values())
        #     labs_keys = list(d['labels'].item().keys())
        #
        #     # print(len(labs))
        #     numpy_array.append(d['data'][rnd_idx])
        #     y.extend(list(map(lambda i: labs[i], rnd_idx)))
        #     win_ids.extend(list(map(lambda i: labs_keys[i], rnd_idx)))

    # X = np.concatenate(numpy_array, axis=0)

    # Select only coverage, CR and SR channels
    # X = X[:, :, np.array([0,6,7,8,9,25,26])]

    # X = X[:, :, np.array([0, 6, 7])]

    # if sampleName == 'NA12878':
    #     X = np.delete(X, 33, 2)

    logging.info(X.shape)
    logging.info(Counter(y))

    # mapclasses = {'DEL': 0, 'noDEL': 1, 'UK_other': 2, 'UK_single_partial': 3, 'UK_multiple_on_either_windows': 4}
    # mapclasses = {'DEL': 0, 'noDEL': 1}

    X, y, win_ids = filter_labels(X, y, win_ids)

    # X = np.stack([X[:, :200, :], X[:, 210:, :]], axis=0)
    # y = list(map(lambda x: x + '_start' if x == 'DEL' else x, y))
    # y.extend(
    #     list(map(lambda x: x + '_end' if x == 'DEL' else x, y))
    # )
    # print(Counter(y))
    # win_ids = win_ids + win_ids

    y = np.array([mapclasses[i] for i in y])
    win_ids = np.array(win_ids)

    # Shuffle arrays
    # new_indices = np.arange(X.shape[0])
    # np.random.shuffle(new_indices)
    # # print(new_indices)
    # X = X[new_indices]
    # y = y[new_indices]
    # win_ids = win_ids[new_indices]

    logging.info('Data for {} loaded'.format(sampleName))

    print(X.shape)

    return X, y, win_ids


def train_and_test_data(sampleName):
    # Datasets
    X, y, win_ids = data(sampleName)

    X = np.array(X)
    y = np.array(y)

    # split into train/validation sets
    X_train, X_test, y_train, y_test, win_ids_train, win_ids_test = train_test_split(X, y, win_ids,
                                                                                     test_size=0.3, random_state=2,
                                                                                     stratify=y,
                                                                                     shuffle=True)

    return X_train, X_test, y_train, y_test, win_ids_train, win_ids_test


def create_model(dim_length, dim_channels, class_number):
    # layers = 2
    # filters = [4] * layers
    # fc_hidden_nodes = 6
    # learning_rate = 4
    # regularization_rate = 1
    # kernel_size = 7
    # drp_out1 = 0
    # drp_out2 = 0

    layers = 2
    filters = [4] * layers
    fc_hidden_nodes = 6
    learning_rate = 10 ** (-4)
    regularization_rate = 10 ** (-1)
    kernel_size = 7
    drp_out1 = 0
    drp_out2 = 0

    outputdim = class_number  # number of classes

    weightinit = 'lecun_uniform'  # weight initialization

    model = Sequential()
    model.add(
        BatchNormalization(
            input_shape=(
                dim_length,
                dim_channels)))

    for filter_number in filters:
        # model.add(MaxPooling1D(pool_size=5, strides=None, padding='same'))

        model.add(Convolution1D(filter_number, kernel_size=kernel_size, padding='same',
                                kernel_regularizer=l2(regularization_rate),
                                kernel_initializer=weightinit))
        model.add(BatchNormalization())
        model.add(Activation('relu'))

    model.add(Flatten())
    model.add(Dropout(drp_out1))
    model.add(Dense(units=fc_hidden_nodes,
                    kernel_regularizer=l2(regularization_rate),
                    kernel_initializer=weightinit))  # Fully connected layer
    model.add(Activation('relu'))  # Relu activation
    model.add(Dropout(drp_out2))
    model.add(Dense(units=outputdim, kernel_initializer=weightinit))
    model.add(BatchNormalization())
    model.add(Activation("softmax"))  # Final classification layer

    model.compile(loss='categorical_crossentropy',
                  optimizer=Adam(lr=learning_rate),
                  metrics=['accuracy'])

    # i = 0
    # for model, params, model_types in [model]:
    #     logging.info('model ' + str(i))
    #     i = i + 1
    #     logging.info(params)
    #     logging.info(model.summary())

    return model


def train(sampleName, params, X_train, y_train, y_train_binary):
    channel_data_dir = get_data_dir(sampleName)

    # win_len = 200
    # padding_len = 10
    # dim = win_len * 2 + padding_len

    # print(Counter(y_train))
    # class_weights = class_weight.compute_class_weight('balanced',
    #                                                   np.unique(y_train),
    #                                                   y_train)
    # class_weights = dict(zip(np.unique(y_train), class_weights))
    # print(class_weights)

    # Balancing dataset
    sampling = 'oversample'

    cnt_lab = Counter(y_train)

    # maximum training samples per class
    max_train = 10**5

    min_v = min([v for k, v in cnt_lab.items()])
    max_v = max([v for k, v in cnt_lab.items()])

    print(cnt_lab)
    print('Minimum number of labels = ' + str(min_v))
    print('Maximum number of labels = ' + str(max_v))

    data_balanced = []
    labels_balanced = []

    for l in cnt_lab.keys():
        # print(l)
        iw = np.where(y_train == l)

        if sampling == 'oversample':
            ii = np.random.choice(a=iw[0], size=min(max_v, max_train), replace=True)
        elif sampling == 'undersample':
            ii = np.random.choice(a=iw[0], size=min_v, replace=False)

        data_balanced.extend(X_train[ii])
        labels_balanced.extend(y_train[ii])

    print(Counter(labels_balanced))

    X_train = np.array(data_balanced)
    y_train = np.array(labels_balanced)
    y_train_binary = to_categorical(y_train, num_classes=params['n_classes'])

    # End balancing

    # X_train, X_val, y_train, y_val = train_test_split(X_train, y_train,
    #                                                   test_size=0.3,
    #                                                   random_state=2,
    #                                                   stratify=y_train,
    #                                                   shuffle=True)
    #
    # y_train_binary = to_categorical(y_train, num_classes=params['n_classes'])
    # y_val_binary = to_categorical(y_val, num_classes=params['n_classes'])
    #
    # model = create_model_with_mcfly(X_train, y_train_binary)
    #
    # history, model = train_model_with_mcfly(model, X_train, y_train_binary,
    #                                         X_val, y_val_binary)

    # Design model
    logging.info('Creating model...')
    model = create_model(params['dim'], params['n_channels'], params['n_classes'])

    esCallback = EarlyStopping(monitor='val_loss', patience=0, verbose=1, mode='auto')
    tbCallBack = TensorBoard(log_dir=os.path.join(channel_data_dir, 'Graph'),
                             histogram_freq=0,
                             write_graph=True,
                             write_images=True)

    logging.info('Fitting model...')
    # Train model on dataset
    history = model.fit(X_train, y_train_binary,
                        validation_split=params['val_split'],
                        batch_size=params['batch_size'],
                        epochs=params['epochs'],
                        shuffle=True,
                        # class_weight=class_weights,
                        verbose=0,
                        callbacks=[esCallback]
                        )

    return model, history, X_train.shape[0], int(X_train.shape[0] * params['val_split'])


def cross_validation(sampleName, outDir):

    X, y, win_ids = data(sampleName)
    y_binary = to_categorical(y, num_classes=len(mapclasses.keys()))

    kfold_splits = 10

    # Instantiate the cross validator
    skf = StratifiedKFold(n_splits=kfold_splits, shuffle=True)

    # Loop through the indices the split() method returns
    for index, (train_indices, test_indices) in enumerate(skf.split(X, y)):
        print("Training on fold " + str(index + 1) + "/10...")

        # Generate batches from indices
        X_train, X_test = X[train_indices], X[test_indices]
        y_train, y_test = y[train_indices], y[test_indices]
        y_train_binary, y_test_binary = y_binary[train_indices], y_binary[test_indices]
        win_ids_train, win_ids_test = win_ids[train_indices], win_ids[test_indices]

        batch_size = 256
        epochs = 20

        # Parameters
        params = {'dim': X_train.shape[1],
                  'batch_size': batch_size,
                  'epochs': epochs,
                  'val_split': 0.2,
                  'n_classes': len(mapclasses.keys()),
                  'n_channels': X_train.shape[2],
                  'shuffle': True}

        model_fn = os.path.join(outDir, 'model_' + sampleName + '_cv' + str(index + 1) + '.hdf5')

        # if os.path.exists(model_fn):
        #
        #     print('Model {} found. Loading model...'.format(model_fn))
        #     model = load_model(model_fn)
        #
        # else:

        print('Training model on {}...'.format(sampleName))
        model, history, train_set_size, validation_set_size = train(sampleName,
                                                                    params, X_train, y_train, y_train_binary)

        model.save(model_fn)

        results = pd.DataFrame()

        # X_test, y_test, win_ids_test = data(sampleName_test)
        # ytest_binary = to_categorical(y_test, num_classes=len(mapclasses.keys()))
        # print(win_ids_test[0])

        # mapclasses = {'DEL': 0, 'noDEL': 1}

        outDit_eval = os.path.join(outDir,
                                   'train_' + sampleName + '_test_' + sampleName + '_cv' + str(index + 1))

        intermediate_results, metrics = evaluate_model(model, X_test, y_test_binary, win_ids_test,
                                                       results, 1, 'results', mapclasses, outDit_eval)

        results = results.append(intermediate_results)

        results.to_csv(os.path.join(outDir,
                                    'train_' + sampleName + '_test_' + \
                                    sampleName + '_cv' + str(index + 1) + '_results.csv'),
                       sep='\t')


def train_and_test_model(sampleName_training, sampleName_test, outDir):
    if sampleName_training == sampleName_test:
        X_train, X_test, y_train, y_test, win_ids_train, win_ids_test = train_and_test_data(sampleName_training)
    else:
        X_train, y_train, win_ids_train = data(sampleName_training)
        X_test, y_test, win_ids_test = data(sampleName_test)

    # channel_data_dir = get_data_dir(sampleName_training)
    # plots_dir = os.path.join(channel_data_dir, 'plots_' + sampleName_training)
    # create_dir(plots_dir)
    #
    # idx_positive = [i for i, v in enumerate(y_train) if v == mapclasses['DEL']]
    # idx_negative = [i for i, v in enumerate(y_train) if v == mapclasses['noDEL']]

    # for i in idx_positive[:10]:
    #     plot_channels(plots_dir, X_train[i], y_train[i], win_ids_train[i])
    #
    # for i in idx_negative[:10]:
    #     plot_channels(plots_dir, X_train[i], y_train[i], win_ids_train[i])

    batch_size = 256
    epochs = 20

    # Parameters
    params = {'dim': X_train.shape[1],
              'batch_size': batch_size,
              'epochs': epochs,
              'val_split': 0.2,
              'n_classes': len(mapclasses.keys()),
              'n_channels': X_train.shape[2],
              'shuffle': True}

    y_train_binary = to_categorical(y_train, num_classes=params['n_classes'])
    y_test_binary = to_categorical(y_test, num_classes=params['n_classes'])

    model_fn = os.path.join(outDir, 'model_' + sampleName_training + '.hdf5')

    if os.path.exists(model_fn):

        print('Model {} found. Loading model...'.format(model_fn))
        model = load_model(model_fn)

    else:

        print('Training model on {}...'.format(sampleName_training))
        model, history, train_set_size, validation_set_size = train(sampleName_training,
                                                                    params, X_train, y_train, y_train_binary)

        model.save(model_fn)

    results = pd.DataFrame()

    # X_test, y_test, win_ids_test = data(sampleName_test)
    # ytest_binary = to_categorical(y_test, num_classes=len(mapclasses.keys()))
    # print(win_ids_test[0])

    # mapclasses = {'DEL': 0, 'noDEL': 1}

    outDit_eval = os.path.join(outDir,
                               'train_' + sampleName_training + '_test_' + sampleName_test)

    # intermediate_results, metrics = evaluate_model(model, X_test, ytest_binary, win_ids_test,
    #                                                results, 1, 'results', mapclasses, outDit_eval)

    intermediate_results, metrics = evaluate_model(model, X_test, y_test_binary, win_ids_test,
                                                   results, 1, 'results', mapclasses, outDit_eval)

    results = results.append(intermediate_results)

    results.to_csv(os.path.join(outDir,
                                'train_' + sampleName_training + '_test_' + sampleName_test + '_results.csv'), sep='\t')

    # get_channel_labels()


def main():
    parser = argparse.ArgumentParser(description='Train and test model')
    parser.add_argument('-p', '--outputpath', type=str,
                        default='/Users/lsantuari/Documents/Processed/channel_maker_output',
                        help="Specify output path")
    parser.add_argument('-t', '--training_sample', type=str, default='NA12878',
                        help="Specify training sample")
    parser.add_argument('-x', '--test_sample', type=str, default='NA12878',
                        help="Specify training sample")
    parser.add_argument('-l', '--logfile', default='windows.log',
                        help='File in which to write logs.')
    parser.add_argument('-m', '--mode', type=str, default='training',
                        help="training/test mode")

    args = parser.parse_args()

    cmd_name = 'cnn'
    output_dir = os.path.join(args.outputpath, args.training_sample, cmd_name)
    create_dir(output_dir)
    logfilename = os.path.join(output_dir, args.logfile)
    # output_file = os.path.join(output_dir, args.out)

    FORMAT = '%(asctime)s %(message)s'
    logging.basicConfig(
        format=FORMAT,
        filename=logfilename,
        filemode='w',
        level=logging.INFO)

    print('Writing log file to {}'.format(logfilename))

    t0 = time()

    # get_channel_labels()

    # train_and_test_model(sampleName_training=args.training_sample,
    #                      sampleName_test=args.test_sample,
    #                      outDir=output_dir
    #                      )

    cross_validation(sampleName=args.training_sample,
                     outDir=output_dir
                     )

    # print('Elapsed time channel_maker_real on BAM %s and Chr %s = %f' % (args.bam, args.chr, time() - t0))
    logging.info('Elapsed time training and testing = %f seconds' % (time() - t0))


if __name__ == '__main__':
    main()
