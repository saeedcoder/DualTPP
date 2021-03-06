from tensorflow.contrib.keras import preprocessing
from collections import defaultdict
import itertools
import os
import tensorflow as tf
import numpy as np
import pandas as pd
import pickle


pad_sequences = preprocessing.sequence.pad_sequences


def create_dir(dirname):
    """Creates a directory if it does not already exist."""
    if not os.path.exists(dirname):
        os.makedirs(dirname)

def pad_features(feats, max_seq_len):
    padded_feats = list()
    num_feats = 1 # TODO: Support multiple features
    for i, feat_seq in enumerate(feats):
        seq_len = len(feat_seq)
        padded_feat = np.concatenate([np.array(feat_seq), np.zeros((max_seq_len-seq_len))], axis=0)
        padded_feats.append(padded_feat)
    padded_feats = np.stack(padded_feats)
    padded_feats = np.expand_dims(padded_feats, axis=2) #TODO: Remove this when supports multiple features.
    return padded_feats

def read_data(event_train_file, event_test_file, time_train_file, time_test_file,
              feats_train_file=None, feats_test_file=None,
              pad=True, normalize=False):
    """Read data from given files and return it as a dictionary."""

    with open(event_train_file, 'r') as in_file:
        eventTrain = [[int(y) for y in x.strip().split()] for x in in_file]

    with open(event_test_file, 'r') as in_file:
        eventTest = [[int(y) for y in x.strip().split()] for x in in_file]

    with open(time_train_file, 'r') as in_file:
        timeTrain = [[float(y) for y in x.strip().split()] for x in in_file]

    with open(time_test_file, 'r') as in_file:
        timeTest = [[float(y) for y in x.strip().split()] for x in in_file]

    if feats_train_file is not None:
        with open(feats_train_file, 'rb') as in_file:
            featsTrain = pickle.load(in_file)
    else:
        featsTrain = []

    if feats_test_file is not None:
        with open(feats_test_file, 'rb') as in_file:
            featsTest = pickle.load(in_file)
    else:
        featsTest = []

    assert len(timeTrain) == len(eventTrain)
    assert len(eventTest) == len(timeTest)

    train_seq_lens = [len(seq) for seq in timeTrain]
    test_seq_lens = [len(seq) for seq in timeTest]

    # nb_samples = len(eventTrain)
    # max_seqlen = max(len(x) for x in eventTrain)
    unique_samples = set()

    for x in eventTrain + eventTest:
        unique_samples = unique_samples.union(x)

    if normalize:
        maxTime = max(itertools.chain((max(x) for x in timeTrain), (max(x) for x in timeTest)))
        minTime = min(itertools.chain((min(x) for x in timeTrain), (min(x) for x in timeTest)))
    else:
        minTime, maxTime = 0, 1

    eventTrainIn = [x[:-1] for x in eventTrain]
    eventTrainOut = [x[1:] for x in eventTrain]
    timeTrainIn = [[(y - minTime) / (maxTime - minTime) for y in x[:-1]] for x in timeTrain]
    timeTrainOut = [[(y - minTime) / (maxTime - minTime) for y in x[1:]] for x in timeTrain]

    max_seq_len = max(itertools.chain((len(x) for x in timeTrain), (len(x) for x in timeTest)))
    if pad:
        train_event_in_seq = pad_sequences(eventTrainIn, maxlen=max_seq_len, padding='post')
        train_event_out_seq = pad_sequences(eventTrainOut, maxlen=max_seq_len, padding='post')
        train_time_in_seq = pad_sequences(timeTrainIn, maxlen=max_seq_len, dtype=float, padding='post')
        train_time_out_seq = pad_sequences(timeTrainOut, maxlen=max_seq_len, dtype=float, padding='post')
        if feats_train_file is not None:
            train_feat_in_seq = pad_features(featsTrain, max_seq_len)
        else:
            train_feat_in_seq = []
    else:
        train_event_in_seq = eventTrainIn
        train_event_out_seq = eventTrainOut
        train_time_in_seq = timeTrainIn
        train_time_out_seq = timeTrainOut
        train_feat_in_seq = featsTrain


    eventTestIn = [x[:-1] for x in eventTest]
    eventTestOut = [x[1:] for x in eventTest]
    timeTestIn = [[(y - minTime) / (maxTime - minTime) for y in x[:-1]] for x in timeTest]
    timeTestOut = [[(y - minTime) / (maxTime - minTime) for y in x[1:]] for x in timeTest]

    if pad:
        test_event_in_seq = pad_sequences(eventTestIn, maxlen=max_seq_len, padding='post')
        test_event_out_seq = pad_sequences(eventTestOut, maxlen=max_seq_len, padding='post')
        test_time_in_seq = pad_sequences(timeTestIn, maxlen=max_seq_len, dtype=float, padding='post')
        test_time_out_seq = pad_sequences(timeTestOut, maxlen=max_seq_len, dtype=float, padding='post')
        if feats_test_file is not None:
            test_feat_in_seq = pad_features(featsTest, max_seq_len)
        else:
            test_feat_in_seq = []
    else:
        test_event_in_seq = eventTestIn
        test_event_out_seq = eventTestOut
        test_time_in_seq = timeTestIn
        test_time_out_seq = timeTestOut
        test_feat_in_seq = featsTest

    return {
        'train_event_in_seq': train_event_in_seq,
        'train_event_out_seq': train_event_out_seq,

        'train_time_in_seq': train_time_in_seq,
        'train_time_out_seq': train_time_out_seq,

        'train_feat_in_seq': train_feat_in_seq,

        'test_event_in_seq': test_event_in_seq,
        'test_event_out_seq': test_event_out_seq,

        'test_time_in_seq': test_time_in_seq,
        'test_time_out_seq': test_time_out_seq,

        'test_feat_in_seq': test_feat_in_seq,

        'num_categories': len(unique_samples),

        'maxTime': maxTime,
        'minTime': minTime,

        'train_seq_lens': train_seq_lens,
        'test_seq_lens': test_seq_lens,
    }


def calc_base_rate(data, training=True):
    """Calculates the base-rate for intelligent parameter initialization from the training data."""
    suffix = 'train' if training else 'test'
    in_key = suffix + '_time_in_seq'
    out_key = suffix + '_time_out_seq'
    valid_key = suffix + '_event_in_seq'

    dts = (data[out_key] - data[in_key])[data[valid_key] > 0]
    return 1.0 / np.mean(dts)


def calc_base_event_prob(data, training=True):
    """Calculates the base probability of event types for intelligent parameter initialization from the training data."""
    dict_key = 'train_event_in_seq' if training else 'test_event_in_seq'

    class_count = defaultdict(lambda: 0.0)
    for evts in data[dict_key]:
        for ev in evts:
            class_count[ev] += 1.0

    total_events = 0.0
    probs = []
    for cat in range(1, data['num_categories'] + 1):
        total_events += class_count[cat]

    for cat in range(1, data['num_categories'] + 1):
        probs.append(class_count[cat] / total_events)

    return np.array(probs)


def data_stats(data):
    """Prints basic statistics about the dataset."""
    train_valid = data['train_event_in_seq'] > 0
    test_valid = data['test_event_in_seq'] > 0

    print('Num categories = ', data['num_categories'])
    print('delta-t (training) = ')
    print(pd.Series((data['train_time_out_seq'] - data['train_time_in_seq'])[train_valid]).describe())
    train_base_rate = calc_base_rate(data, training=True)
    print('base-rate = {}, log(base_rate) = {}'.format(train_base_rate, np.log(train_base_rate)))
    print('Class probs = ', calc_base_event_prob(data, training=True))

    print('delta-t (testing) = ')
    print(pd.Series((data['test_time_out_seq'] - data['test_time_in_seq'])[test_valid]).describe())
    test_base_rate = calc_base_rate(data, training=False)
    print('base-rate = {}, log(base_rate) = {}'.format(test_base_rate, np.log(test_base_rate)))
    print('Class probs = ', calc_base_event_prob(data, training=False))

    print('Training sequence lenghts = ')
    print(pd.Series(train_valid.sum(axis=1)).describe())

    print('Testing sequence lenghts = ')
    print(pd.Series(test_valid.sum(axis=1)).describe())


def variable_summaries(var, name=None):
    """Attach a lot of summaries to a Tensor (for TensorBoard visualization)."""
    if name is None:
        name = var.name.split('/')[-1][:-2]

    with tf.name_scope('summaries-' + name):
        mean = tf.reduce_mean(var)
        tf.summary.scalar('mean', mean)
        with tf.name_scope('stddev'):
            stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
        tf.summary.scalar('stddev', stddev)
        tf.summary.scalar('max', tf.reduce_max(var))
        tf.summary.scalar('min', tf.reduce_min(var))
        tf.summary.histogram('histogram', var)


def MAE(time_preds, time_true, events_out, seq_lens):
    """Calculates the MAE between the provided and the given time, ignoring the inf,
    nans, and with masked sequence lengths. Returns both the MAE and the number of items considered."""

    # Predictions may not cover the entire time dimension.
    # This clips time_true to the correct size.
    seq_limit = time_preds.shape[1]
    clipped_time_true = time_true[:, :seq_limit]
    clipped_events_out = events_out[:, :seq_limit]

    mask = np.stack([np.concatenate([np.ones(l), np.zeros(time_true.shape[1]-l)]) for l in seq_lens])
    mask = np.array(mask, dtype=np.bool)
    is_finite = np.isfinite(time_preds) & (clipped_events_out > 0) & mask

    return np.mean(np.abs(time_preds - clipped_time_true)[is_finite]), np.sum(is_finite)


def ACC(event_preds, event_true):
    """Returns the accuracy of the event prediction, provided the output probability vector."""
    clipped_event_true = event_true[:, :event_preds.shape[1]]
    is_valid = clipped_event_true > 0

    # The indexes start from 0 whereare event_preds start from 1.
    highest_prob_ev = event_preds.argmax(axis=-1) + 1

    return np.sum((highest_prob_ev == clipped_event_true)[is_valid]) / np.sum(is_valid)
