import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers

import numpy as np
import pandas as pd
from itertools import chain
from bisect import bisect_right
from multiprocessing import Pool
import matplotlib.pyplot as plt
import properscoring as ps

import models
import utils
import os, sys

train_gap_metric_mae = tf.keras.metrics.MeanAbsoluteError()
train_gap_metric_mse = tf.keras.metrics.MeanSquaredError()
dev_gap_metric_mae = tf.keras.metrics.MeanAbsoluteError()
dev_gap_metric_mse = tf.keras.metrics.MeanSquaredError()
test_gap_metric_mae = tf.keras.metrics.MeanAbsoluteError()
test_gap_metric_mse = tf.keras.metrics.MeanSquaredError()

ETH = 10.0
one_by = tf.math.reciprocal_no_nan

class NegativeLogLikelihood(tf.keras.losses.Loss):
	def __init__(self, D, WT,
				 reduction=keras.losses.Reduction.AUTO,
				 name='negative_log_likelihood'):
		super(NegativeLogLikelihood, self).__init__(reduction=reduction,
													name=name)
		self.D = D
		self.WT = WT

	def call(self, gaps_true, gaps_pred):
		log_lambda_ = (self.D + (gaps_true * self.WT))
		lambda_ = tf.exp(tf.minimum(ETH, log_lambda_), name='lambda_')
		log_f_star = (log_lambda_
					  + one_by(self.WT) * tf.exp(tf.minimum(ETH, self.D))
					  - one_by(self.WT) * lambda_)
		return -log_f_star

class MeanSquareLoss(tf.keras.losses.Loss):
	def __init__(self,
				 reduction=keras.losses.Reduction.AUTO,
				 name='negative_log_likelihood'):
		super(MeanSquareLoss, self).__init__(reduction=reduction,
													name=name)
	
	def call(self, gaps_true, gaps_pred):
		error = gaps_true - gaps_pred
		return tf.reduce_mean(error * error)

def run_rmtpp(model, optimizer, data, NLL_loss, rmtpp_epochs=10):
	[train_dataset_gaps, dev_data_in_gaps, dev_data_out_gaps, train_norm_gaps] = data
	[train_norm_a_gaps, train_norm_d_gaps] = train_norm_gaps

	os.makedirs('saved_models/training_rmtpp/', exist_ok=True)
	checkpoint_path = "saved_models/training_rmtpp/cp.ckpt"
	best_dev_gap_mse = np.inf

	train_losses = list()
	for epoch in range(rmtpp_epochs):
		print('Starting epoch', epoch)
		step_train_loss = 0.0
		step_cnt = 0
		next_initial_state = None
		for sm_step, (gaps_batch_in, gaps_batch_out) in enumerate(train_dataset_gaps):
			with tf.GradientTape() as tape:
				gaps_pred, D, WT, _, next_initial_state = model(gaps_batch_in, initial_state=next_initial_state)

				# Compute the loss for this minibatch.
				if NLL_loss:
					gap_loss_fn = NegativeLogLikelihood(D, WT)
				else:
					gap_loss_fn = MeanSquareLoss()
				
				gap_loss = gap_loss_fn(gaps_batch_out, gaps_pred)
				loss = gap_loss
				step_train_loss+=loss.numpy()
				
			grads = tape.gradient(loss, model.trainable_weights)
			optimizer.apply_gradients(zip(grads, model.trainable_weights))

			train_gap_metric_mae(gaps_batch_out, gaps_pred)
			train_gap_metric_mse(gaps_batch_out, gaps_pred)
			train_gap_mae = train_gap_metric_mae.result()
			train_gap_mse = train_gap_metric_mse.result()
			train_gap_metric_mae.reset_states()
			train_gap_metric_mse.reset_states()

			# print(float(train_gap_mae), float(train_gap_mse))

			step_cnt += 1
			print('Training loss (for one batch) at step %s: %s' \
					 %(sm_step, float(loss)))
		
		# Dev calculations
		dev_gaps_pred, _, _, _, _ = model(dev_data_in_gaps)
		dev_gaps_pred_unnorm = utils.denormalize_avg(dev_gaps_pred, 
										train_norm_a_gaps, train_norm_d_gaps)
		
		dev_gap_metric_mae(dev_data_out_gaps, dev_gaps_pred_unnorm)
		dev_gap_metric_mse(dev_data_out_gaps, dev_gaps_pred_unnorm)
		dev_gap_mae = dev_gap_metric_mae.result()
		dev_gap_mse = dev_gap_metric_mse.result()
		dev_gap_metric_mae.reset_states()
		dev_gap_metric_mse.reset_states()
		if best_dev_gap_mse > dev_gap_mse:
			best_dev_gap_mse = dev_gap_mse
			print('Saving model at epoch', epoch)
			model.save_weights(checkpoint_path)

		step_train_loss /= step_cnt
		print('Training loss after epoch %s: %s' %(epoch, float(step_train_loss)))
		print('MAE and MSE of Dev data %s: %s' \
			%(float(dev_gap_mae), float(dev_gap_mse)))
		train_losses.append(step_train_loss)

	print("Loading best model")
	model.load_weights(checkpoint_path)
	dev_gaps_pred, _, _, _, _ = model(dev_data_in_gaps)
	dev_gaps_pred_unnorm = utils.denormalize_avg(dev_gaps_pred, 
									train_norm_a_gaps, train_norm_d_gaps)
	
	dev_gap_metric_mae(dev_data_out_gaps, dev_gaps_pred_unnorm)
	dev_gap_metric_mse(dev_data_out_gaps, dev_gaps_pred_unnorm)
	dev_gap_mae = dev_gap_metric_mae.result()
	dev_gap_mse = dev_gap_metric_mse.result()
	dev_gap_metric_mae.reset_states()
	dev_gap_metric_mse.reset_states()
	print('Best MAE and MSE of Dev data %s: %s' \
		%(float(dev_gap_mae), float(dev_gap_mse)))
		
	return train_losses

def simulate(model, times_in, gaps_in, t_b_plus, normalizers, prev_hidden_state = None):
	gaps_pred = list()
	data_norm_a, data_norm_d = normalizers
	
	step_gaps_pred = gaps_in[:, -1]

	times_pred = list()
	last_gaps_pred_unnorm = utils.denormalize_avg(step_gaps_pred, data_norm_a, data_norm_d)
	
	last_times_pred = times_in + last_gaps_pred_unnorm
	times_pred.append(last_times_pred)
	
	simul_step = 0

	while any(times_pred[-1]<t_b_plus):
		step_gaps_pred, _, _, prev_hidden_state, _ \
				= model(gaps_in, initial_state=prev_hidden_state)

		step_gaps_pred = step_gaps_pred[:,-1:]
		gaps_in = tf.concat([gaps_in[:,1:], step_gaps_pred], axis=1)
		step_gaps_pred = tf.squeeze(step_gaps_pred, axis=-1)
		last_gaps_pred_unnorm = utils.denormalize_avg(step_gaps_pred, data_norm_a, data_norm_d)
		last_times_pred = times_pred[-1] + last_gaps_pred_unnorm
		gaps_pred.append(last_gaps_pred_unnorm)
		times_pred.append(last_times_pred)
		
		simul_step += 1

	gaps_pred = tf.stack(gaps_pred, axis=1)
	all_gaps_pred = gaps_pred

	times_pred = tf.squeeze(tf.stack(times_pred, axis=1), axis=2)
	all_times_pred = times_pred

	return all_gaps_pred, all_times_pred, prev_hidden_state

def simulate_with_counter(model, times_in, gaps_in, out_gaps_count, normalizers, prev_hidden_state = None):
	gaps_pred = list()
	data_norm_a, data_norm_d = normalizers
	
	step_gaps_pred = gaps_in[:, -1]

	times_pred = list()
	last_gaps_pred_unnorm = utils.denormalize_avg(step_gaps_pred, data_norm_a, data_norm_d)
	
	last_times_pred = times_in + last_gaps_pred_unnorm
	times_pred.append(last_times_pred)
	
	simul_step = 0
	old_hidden_state = None

	while any(simul_step < out_gaps_count):
		step_gaps_pred, _, _, prev_hidden_state, _ \
				= model(gaps_in, initial_state=prev_hidden_state)
		
		if old_hidden_state is not None:
			prev_hidden_state = (simul_step < out_gaps_count) * prev_hidden_state + \
								(simul_step >= out_gaps_count) * old_hidden_state
			step_gaps_pred = np.expand_dims((simul_step < out_gaps_count), axis=-1) * step_gaps_pred
			
		old_hidden_state = prev_hidden_state
		step_gaps_pred = step_gaps_pred[:,-1:]
		gaps_in = tf.concat([gaps_in[:,1:], step_gaps_pred], axis=1)
		step_gaps_pred = tf.squeeze(step_gaps_pred, axis=-1)
		last_gaps_pred_unnorm = utils.denormalize_avg(step_gaps_pred, data_norm_a, data_norm_d)
		last_times_pred = times_pred[-1] + last_gaps_pred_unnorm
		last_times_pred = (simul_step < out_gaps_count) * last_times_pred
		gaps_pred.append(last_gaps_pred_unnorm)
		times_pred.append(last_times_pred)
		
		simul_step += 1

	gaps_pred = tf.stack(gaps_pred, axis=1)
	all_gaps_pred = gaps_pred

	times_pred = tf.squeeze(tf.stack(times_pred, axis=1), axis=2)
	all_times_pred = times_pred

	return all_gaps_pred, all_times_pred, prev_hidden_state


def count_events(all_times_pred, t_b_plus, t_e_plus):
	times_out_indices_tb = [bisect_right(t_out, t_b) for t_out, t_b in zip(all_times_pred, t_b_plus)]
	times_out_indices_te = [bisect_right(t_out, t_e) for t_out, t_e in zip(all_times_pred, t_e_plus)]
	event_count_preds = [times_out_indices_te[idx] - times_out_indices_tb[idx] for idx in range(len(t_b_plus))]
	return event_count_preds

def compute_event_in_bin(data, count, appender=None, size=40):
	count = tf.cast(count, tf.int32)
	event_bag = list()
	full_bag = list()
	end_event = list()
	end_gaps = list()
	for idx in range(len(count)):
		event_bag.append(data[idx,0:count[idx],0].numpy())
		end_event.append(data[idx,count[idx]-1,0])
		if appender is None:
			end_gaps.append(data[idx,count[idx],0] - data[idx,count[idx]-1,0])
		if appender is not None:
			amt = size-len(data[idx,0:count[idx],0].numpy().tolist())
			if amt<=0:
				full_bag.append(data[idx,-size:,0].numpy().tolist())
			else:
				full_bag.append(appender[idx,-amt:,0].tolist() + data[idx,0:count[idx],0].numpy().tolist())
	
	full_bag = np.array(full_bag)
	end_event = np.array(end_event)
	end_gaps = np.array(end_gaps)
	return event_bag, full_bag, end_event, end_gaps

def scaled_points(actual_bin_start, actual_bin_end, bin_start, bin_end, all_times_pred):
	all_times_pred_mask = np.ma.make_mask(all_times_pred)
	all_gaps_pred_mask = np.ma.make_mask(all_times_pred[:,1:])
	all_times_pred_scaled = (((actual_bin_end - actual_bin_start)/(bin_end - bin_start)) * \
							 (all_times_pred - bin_start)) + actual_bin_start
	all_times_pred_scaled = all_times_pred_scaled * all_times_pred_mask
	all_gaps_pred_scaled = all_times_pred_scaled[:,1:] - all_times_pred_scaled[:,:-1]
	all_gaps_pred_scaled = all_gaps_pred_scaled * all_gaps_pred_mask
	all_gaps_pred_scaled = tf.expand_dims(all_gaps_pred_scaled, axis=-1)
	return all_times_pred_scaled, all_gaps_pred_scaled

def scale_time_interval(data, t_start, t_end):
	t_start = tf.cast(t_start, tf.float32)
	t_end = tf.cast(t_end, tf.float32)
	N_bin = t_end - t_start
	scaled_time = ((data * N_bin * t_end) + t_start) / ((data * N_bin) + 1.0)
	return scaled_time

def before_time_event_count(events_in_bin_pred, timecheck):
	count=0
	for idx in range(len(events_in_bin_pred)):
		events_in_one_bin = events_in_bin_pred[idx]
		if timecheck <= events_in_one_bin[-1]:
			cnt=0
			while(timecheck >= events_in_one_bin[cnt]):
				cnt+=1
			count+=cnt
			return count
		else:
			count+=len(events_in_one_bin)
	return count

def compute_count_event_range(all_events_in_bin_pred, t_b_plus, t_e_plus):
	event_count = list()
	for batch_idx in range(len(all_events_in_bin_pred)):
		before_tb_event_count = before_time_event_count(all_events_in_bin_pred[batch_idx], t_b_plus[batch_idx])
		before_te_event_count = before_time_event_count(all_events_in_bin_pred[batch_idx], t_e_plus[batch_idx])
		event_count.append(before_te_event_count - before_tb_event_count)
	test_event_count_pred = np.array(event_count)
	return test_event_count_pred

def trim_evens_pred(all_times_pred_uncut, t_b_plus, t_e_plus):
	all_times_pred = list()
	for idx in range(len(all_times_pred_uncut)):
		lst = list()
		for each in all_times_pred_uncut[idx]:
			lst = lst+each.tolist()
		all_times_pred.append(lst)
	all_times_pred = np.array(all_times_pred)

	times_out_indices_tb = [bisect_right(t_out, t_b) for t_out, t_b in zip(all_times_pred, t_b_plus)]
	times_out_indices_te = [bisect_right(t_out, t_e) for t_out, t_e in zip(all_times_pred, t_e_plus)]
	all_times_pred = [all_times_pred[idx][times_out_indices_tb[idx]:times_out_indices_te[idx]] for idx in range(len(t_b_plus))]
	return all_times_pred

def compute_mae_cur_bound(all_event_pred, all_event_true, t_b_plus, t_e_plus):
	times_out_indices_tb = [bisect_right(t_out, t_b) for t_out, t_b in zip(all_event_pred, t_b_plus)]
	times_out_indices_te = [bisect_right(t_out, t_e) for t_out, t_e in zip(all_event_pred, t_e_plus)]
	all_event_pred_count = [times_out_indices_te[idx] - times_out_indices_tb[idx] for idx in range(len(t_b_plus))]
	all_event_pred_count = np.array(all_event_pred_count)
	all_event_pred = [all_event_pred[idx][times_out_indices_tb[idx]:times_out_indices_te[idx]] for idx in range(len(t_b_plus))]

	times_out_indices_tb = [bisect_right(t_out, t_b) for t_out, t_b in zip(all_event_true, t_b_plus)]
	times_out_indices_te = [bisect_right(t_out, t_e) for t_out, t_e in zip(all_event_true, t_e_plus)]
	all_event_true_count = [times_out_indices_te[idx] - times_out_indices_tb[idx] for idx in range(len(t_b_plus))]
	all_event_true_count = np.array(all_event_true_count)
	all_event_true = [all_event_true[idx][times_out_indices_tb[idx]:times_out_indices_te[idx]] for idx in range(len(t_b_plus))]
	mae = np.mean(np.abs(all_event_pred_count - all_event_true_count))

	return all_event_pred, all_event_true, mae 

def compute_hierarchical_mae_deep(all_event_pred, all_event_true, t_b_plus, t_e_plus, compute_depth):
	if compute_depth == 0:
		return 0
	
	all_event_pred, all_event_true, res = compute_mae_cur_bound(all_event_pred, 
											all_event_true, t_b_plus, t_e_plus)

	t_b_e_mid = (t_b_plus + t_e_plus) / 2.0
	compute_depth -= 1

	res1 = compute_hierarchical_mae_deep(all_event_pred, all_event_true, t_b_plus, t_b_e_mid, compute_depth)
	res2 = compute_hierarchical_mae_deep(all_event_pred, all_event_true, t_b_e_mid, t_e_plus, compute_depth)
	return res + res1 + res2

def compute_hierarchical_mae(all_event_pred_uncut, query_data, all_event_true, compute_depth):
	[t_b_plus, t_e_plus, true_count] = query_data
	all_event_pred = trim_evens_pred(all_event_pred_uncut, t_b_plus, t_e_plus)
	print('Event counts ', [len(x) for x in all_event_pred])
	test_event_count_pred = np.array([len(x) for x in all_event_pred])
	event_count_mse = tf.keras.losses.MSE(true_count, test_event_count_pred).numpy()
	event_count_mae = tf.keras.losses.MAE(true_count, test_event_count_pred).numpy()
	print("MSE of event count in range:", event_count_mse)
	print("MAE of event count in range:", event_count_mae)
	return compute_hierarchical_mae_deep(all_event_pred, all_event_true, t_b_plus, t_e_plus, compute_depth)

def compute_threshold_loss(all_event_pred_uncut, query_data):
	[interval_range_count_less, interval_range_count_more,
	less_threshold, more_threshold, interval_size] = query_data

	all_times_pred = list()
	for idx in range(len(all_event_pred_uncut)):
		lst = list()
		for each in all_event_pred_uncut[idx]:
			lst = lst+each.tolist()
		all_times_pred.append(lst)
	all_times_pred = np.array(all_times_pred)

	interval_range_count_more_pred = utils.get_interval_count_more_than_threshold(all_times_pred, interval_size, more_threshold)
	# threshold_mae_more = np.mean(np.abs(interval_range_count_more_pred - interval_range_count_more))
	lst = list()
	for idx in range(len(interval_range_count_more_pred)):
		if not(interval_range_count_more_pred[idx]==-1 or interval_range_count_more[idx]==-1):
			lst.append(np.abs(interval_range_count_more_pred[idx] - interval_range_count_more[idx]))
	threshold_mae_more = np.mean(np.array(lst))
	print()
	print('counting ', len(lst), 'testcase out of ', len(interval_range_count_more))
	print('MAE of computing range of more events than threshold:', threshold_mae_more)

	interval_range_count_less_pred = utils.get_interval_count_less_than_threshold(all_times_pred, interval_size, less_threshold)
	lst = list()
	for idx in range(len(interval_range_count_less_pred)):
		if not(interval_range_count_less_pred[idx]==-1 or interval_range_count_less[idx]==-1):
			lst.append(np.abs(interval_range_count_less_pred[idx] - interval_range_count_less[idx]))
	threshold_mae_less = np.mean(np.array(lst))
	print('counting ', len(lst), 'testcase out of ', len(interval_range_count_less))
	print('MAE of computing range of less events than threshold:', threshold_mae_less)
	print()

	return threshold_mae_less, threshold_mae_more

def run_rmtpp_mse(args, data, test_data):
	[test_data_in_gaps_bin, test_end_hr_bins, test_data_in_time_end_bin, 
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] =  test_data	
	rmtpp_epochs = args.epochs
	enc_len = args.enc_len
	dec_len = args.out_bin_sz
	bin_size = args.bin_size

	model_mse, optimizer_mse = models.build_rmtpp_model(args)
	model_mse.summary()
	print('\nTraining Model with Mean Square Loss')
	train_loss_mse = run_rmtpp(model_mse, optimizer_mse, data, 
								NLL_loss=False, rmtpp_epochs=rmtpp_epochs)

	next_hidden_state = None
	test_data_init_time = test_data_in_time_end_bin.astype(np.float32)
	test_data_input_gaps_bin = test_data_in_gaps_bin.astype(np.float32)
	all_event_count_preds_mse = list()
	all_times_pred_from_beg = None
	for dec_idx in range(dec_len):
		print('Simulating dec_idx', dec_idx)
		all_gaps_pred, all_times_pred, next_hidden_state = simulate(model_mse, 
												test_data_init_time, 
												test_data_input_gaps_bin,
												test_end_hr_bins[:,dec_idx], 
												(test_gap_in_bin_norm_a, 
												test_gap_in_bin_norm_d),
												prev_hidden_state=next_hidden_state)
		
		if all_times_pred_from_beg is not None:
			all_times_pred_from_beg = tf.concat([all_times_pred_from_beg, all_times_pred], axis=1)
		else:
			all_times_pred_from_beg = all_times_pred

		event_count_preds_mse = count_events(all_times_pred_from_beg, 
											 test_end_hr_bins[:,dec_idx]-bin_size, 
											 test_end_hr_bins[:,dec_idx])
		all_event_count_preds_mse.append(event_count_preds_mse)
		
		test_data_init_time = all_times_pred[:,-1:].numpy()
		
		all_gaps_pred_norm = utils.normalize_avg_given_param(all_gaps_pred,
												test_gap_in_bin_norm_a,
												test_gap_in_bin_norm_d)
		all_prev_gaps_pred = tf.concat([test_data_input_gaps_bin, all_gaps_pred_norm], axis=1)
		test_data_input_gaps_bin = all_prev_gaps_pred[:,-enc_len:].numpy()

	event_count_preds_mse = np.array(all_event_count_preds_mse).T
	return model_mse, event_count_preds_mse

def run_rmtpp_nll(args, data, test_data):
	[test_data_in_gaps_bin, test_end_hr_bins, test_data_in_time_end_bin, 
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] =  test_data	
	rmtpp_epochs = args.epochs
	enc_len = args.enc_len
	dec_len = args.out_bin_sz
	bin_size = args.bin_size

	model_nll, optimizer_nll = models.build_rmtpp_model(args)
	model_nll.summary()
	print('\nTraining Model with Log Likelihood')
	train_loss_nll = run_rmtpp(model_nll, optimizer_nll, data, 
								NLL_loss=True, rmtpp_epochs=rmtpp_epochs)

	next_hidden_state = None
	test_data_init_time = test_data_in_time_end_bin.astype(np.float32)
	test_data_input_gaps_bin = test_data_in_gaps_bin.astype(np.float32)
	all_event_count_preds_nll = list()
	all_times_pred_from_beg = None

	for dec_idx in range(dec_len):
		print('Simulating dec_idx', dec_idx)
		all_gaps_pred, all_times_pred, next_hidden_state = simulate(model_nll, 
												test_data_init_time, 
												test_data_input_gaps_bin,
												test_end_hr_bins[:,dec_idx], 
												(test_gap_in_bin_norm_a, 
												test_gap_in_bin_norm_d),
												prev_hidden_state=next_hidden_state)
		
		if all_times_pred_from_beg is not None:
			all_times_pred_from_beg = tf.concat([all_times_pred_from_beg, all_times_pred], axis=1)
		else:
			all_times_pred_from_beg = all_times_pred

		event_count_preds_nll = count_events(all_times_pred_from_beg, 
											 test_end_hr_bins[:,dec_idx]-bin_size, 
											 test_end_hr_bins[:,dec_idx])
		all_event_count_preds_nll.append(event_count_preds_nll)
		
		test_data_init_time = all_times_pred[:,-1:].numpy()
		
		all_gaps_pred_norm = utils.normalize_avg_given_param(all_gaps_pred,
												test_gap_in_bin_norm_a,
												test_gap_in_bin_norm_d)
		all_prev_gaps_pred = tf.concat([test_data_input_gaps_bin, all_gaps_pred_norm], axis=1)
		test_data_input_gaps_bin = all_prev_gaps_pred[:,-enc_len:].numpy()

	event_count_preds_nll = np.array(all_event_count_preds_nll).T
	return model_nll, event_count_preds_nll

def run_hierarchical(args, data, test_data):
	train_data_in_bin, train_data_out_bin = data
	test_data_in_bin, test_data_out_bin, test_mean_bin, test_std_bin = test_data
	batch_size = args.batch_size
	validation_split = 0.2
	num_epochs = args.epochs * 100
	model_cnt = models.hierarchical_model(args)
	model_cnt.summary()

	history_cnt = model_cnt.fit(train_data_in_bin, train_data_out_bin, batch_size=batch_size,
					epochs=num_epochs, validation_split=validation_split, verbose=0)

	hist = pd.DataFrame(history_cnt.history)
	hist['epoch'] = history_cnt.epoch
	print(hist)

	# plt.plot(hist['loss'])
	# plt.ylabel('Loss')
	# plt.xlabel('Epochs')

	# plt.plot(hist['mae'])
	# plt.ylabel('MAE')
	# plt.xlabel('Epochs')	

	test_data_out_norm = utils.normalize_data_given_param(test_data_out_bin, test_mean_bin, test_std_bin)
	loss, mae, mse = model_cnt.evaluate(test_data_in_bin, test_data_out_norm, verbose=0)
	print('Normalized loss, mae, mse', loss, mae, mse)

	test_predictions_norm_cnt = model_cnt.predict(test_data_in_bin)
	test_predictions_cnt = utils.denormalize_data(test_predictions_norm_cnt, 
											test_mean_bin, test_std_bin)
	event_count_preds_cnt = test_predictions_cnt
	return model_cnt, event_count_preds_cnt

def run_rmtpp_count_reinit(args, models, data, test_data):
	model_cnt, model_rmtpp = models
	[test_data_in_bin, test_data_out_bin, test_end_hr_bins,
	test_data_in_time_end_bin, test_data_in_gaps_bin, test_mean_bin, test_std_bin,
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] = test_data

	enc_len = args.enc_len
	dec_len = args.out_bin_sz
	bin_size = args.bin_size
	
	next_hidden_state = None
	scaled_rnn_hidden_state = None

	test_data_init_time = test_data_in_time_end_bin.astype(np.float32)
	test_data_input_gaps_bin = test_data_in_gaps_bin.astype(np.float32)
	all_events_in_bin_pred = list()

	test_predictions_norm_cnt = model_cnt.predict(test_data_in_bin)
	test_predictions_cnt = utils.denormalize_data(test_predictions_norm_cnt, test_mean_bin, test_std_bin)
	event_count_preds_cnt = np.round(test_predictions_cnt)
	event_count_preds_true = test_data_out_bin

	output_event_count_pred = tf.expand_dims(event_count_preds_cnt, axis=-1).numpy()

	for dec_idx in range(dec_len):
		all_gaps_pred, all_times_pred, _ = simulate_with_counter(model_rmtpp, 
												test_data_init_time, 
												test_data_input_gaps_bin,
												output_event_count_pred[:,dec_idx],
												(test_gap_in_bin_norm_a, 
												test_gap_in_bin_norm_d),
												prev_hidden_state=next_hidden_state)

		gaps_before_bin = all_times_pred[:,:1] - test_data_init_time
		gaps_before_bin = gaps_before_bin * np.random.uniform(size=gaps_before_bin.shape)
		bin_start = test_data_init_time + gaps_before_bin


		_, _, test_data_init_time, test_data_init_gaps = compute_event_in_bin(tf.expand_dims(all_times_pred, axis=-1), 
														 output_event_count_pred[:,dec_idx,0])
		test_data_init_time = np.expand_dims(test_data_init_time, axis=-1)
		test_data_init_gaps = np.expand_dims(test_data_init_gaps, axis=-1)
		gaps_after_bin = test_data_init_gaps
		gaps_after_bin = gaps_after_bin * np.random.uniform(size=gaps_after_bin.shape)
		bin_end = test_data_init_time + gaps_after_bin

		actual_bin_start = test_end_hr_bins[:,dec_idx]-bin_size
		actual_bin_end = test_end_hr_bins[:,dec_idx]

		all_times_pred, all_gaps_pred = scaled_points(actual_bin_start, actual_bin_end, bin_start, bin_end, all_times_pred)

		event_in_bin_preds, _, test_data_init_time, _ = compute_event_in_bin(tf.expand_dims(all_times_pred, axis=-1), 
														 output_event_count_pred[:,dec_idx,0])
		test_data_init_time = np.expand_dims(test_data_init_time, axis=-1)
		
		
		all_gaps_pred_norm = utils.normalize_avg_given_param(all_gaps_pred,
												test_gap_in_bin_norm_a,
												test_gap_in_bin_norm_d)
		
		_, test_data_input_gaps_bin_full, _, _ = compute_event_in_bin(all_gaps_pred_norm, 
															  output_event_count_pred[:,dec_idx,0],
															  test_data_input_gaps_bin, 
															  enc_len+1)
		
		all_events_in_bin_pred.append(event_in_bin_preds)
		
		test_data_input_gaps_bin = np.expand_dims(test_data_input_gaps_bin_full[:,1:], axis=-1)
		test_data_input_gaps_bin = test_data_in_gaps_bin.astype(np.float32)

		test_data_input_gaps_bin_scaled = np.expand_dims(test_data_input_gaps_bin_full[:,:-10], axis=-1)
		test_data_input_gaps_bin_scaled = test_data_in_gaps_bin.astype(np.float32)
		
		_, _, _, _, next_hidden_state \
					= model_rmtpp(test_data_input_gaps_bin_scaled, initial_state=scaled_rnn_hidden_state)

	all_events_in_bin_pred = np.array(all_events_in_bin_pred).T
	return None, all_events_in_bin_pred

def run_rmtpp_count_cont_rmtpp(args, models, data, test_data):
	model_cnt, model_rmtpp = models
	[test_data_in_bin, test_data_out_bin, test_end_hr_bins,
	test_data_in_time_end_bin, test_data_in_gaps_bin, test_mean_bin, test_std_bin,
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] = test_data

	enc_len = args.enc_len
	dec_len = args.out_bin_sz
	bin_size = args.bin_size
	
	next_hidden_state = None
	scaled_rnn_hidden_state = None

	test_data_init_time = test_data_in_time_end_bin.astype(np.float32)
	test_data_input_gaps_bin = test_data_in_gaps_bin.astype(np.float32)
	all_events_in_bin_pred = list()

	test_predictions_norm_cnt = model_cnt.predict(test_data_in_bin)
	test_predictions_cnt = utils.denormalize_data(test_predictions_norm_cnt, test_mean_bin, test_std_bin)
	event_count_preds_cnt = np.round(test_predictions_cnt)
	event_count_preds_true = test_data_out_bin

	output_event_count_pred = tf.expand_dims(event_count_preds_cnt, axis=-1).numpy()
	
	output_event_count_pred_cumm = tf.reduce_sum(event_count_preds_cnt, axis=-1).numpy()
	full_cnt_event_all_bins_pred = max(output_event_count_pred_cumm) * np.ones_like(output_event_count_pred_cumm)
	full_cnt_event_all_bins_pred = np.expand_dims(full_cnt_event_all_bins_pred, axis=-1)

	all_gaps_pred, all_times_pred, _ = simulate_with_counter(model_rmtpp, 
											test_data_init_time, 
											test_data_input_gaps_bin,
											full_cnt_event_all_bins_pred,
											(test_gap_in_bin_norm_a, 
											test_gap_in_bin_norm_d),
											prev_hidden_state=next_hidden_state)
	
	all_times_pred_lst = list()
	for batch_idx in range(len(all_gaps_pred)):
		event_past_cnt=0
		times_pred_all_bin_lst=list()

		for dec_idx in range(dec_len):
			times_pred_for_bin = all_times_pred[batch_idx,event_past_cnt:event_past_cnt+int(output_event_count_pred[batch_idx,dec_idx,0])]
			
			gaps_before_bin = all_times_pred[batch_idx,event_past_cnt:event_past_cnt+1] - test_data_init_time[batch_idx]
			event_past_cnt += int(output_event_count_pred[batch_idx,dec_idx,0])
			gaps_before_bin = gaps_before_bin * np.random.uniform()
			bin_start = test_data_init_time[batch_idx] + gaps_before_bin

			if event_past_cnt==0:
				test_data_init_time[batch_idx] = all_times_pred[batch_idx,0:1]
			else:
				test_data_init_time[batch_idx] = all_times_pred[batch_idx,event_past_cnt-1:event_past_cnt]

			# test_data_init_time[batch_idx] = all_times_pred[batch_idx,event_past_cnt-1:event_past_cnt]
			gaps_after_bin = all_times_pred[batch_idx,event_past_cnt:event_past_cnt+1] - test_data_init_time[batch_idx]
			gaps_after_bin = gaps_after_bin * np.random.uniform()
			bin_end = test_data_init_time[batch_idx] + gaps_after_bin
			
			actual_bin_start = test_end_hr_bins[batch_idx,dec_idx]-bin_size
			actual_bin_end = test_end_hr_bins[batch_idx,dec_idx]

			times_pred_for_bin_scaled = (((actual_bin_end - actual_bin_start)/(bin_end - bin_start)) * \
							 (times_pred_for_bin - bin_start)) + actual_bin_start
			
			times_pred_all_bin_lst.append(times_pred_for_bin_scaled.numpy())
			
			# if batch_idx <= 2:
			#	 print('gaps_before_bin', gaps_before_bin)
			#	 print('gaps_after_bin', gaps_after_bin)
				
			#	 print('bin_start', bin_start)
			#	 print('bin_end', bin_end)

			#	 print('actual_bin_start', actual_bin_start)
			#	 print('actual_bin_end', actual_bin_end)
			#	 print('times_pred_for_bin', times_pred_for_bin)
			#	 print('times_pred_for_bin_scaled', times_pred_for_bin_scaled)
		
		all_times_pred_lst.append(times_pred_all_bin_lst)
	all_times_pred = np.array(all_times_pred_lst)
	return None, all_times_pred

def run_rmtpp_for_count(args, models, data, test_data, query_data):
	_, model_rmtpp = models
	[test_data_in_bin, test_data_out_bin, test_end_hr_bins,
	test_data_in_time_end_bin, test_data_in_gaps_bin, test_mean_bin, test_std_bin,
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] = test_data
	[t_b_plus, t_e_plus, true_count] = query_data

	enc_len = args.enc_len
	dec_len = args.out_bin_sz
	bin_size = args.bin_size

	next_hidden_state = None
	scaled_rnn_hidden_state = None

	test_data_init_time = test_data_in_time_end_bin.astype(np.float32)
	test_data_input_gaps_bin = test_data_in_gaps_bin.astype(np.float32)

	t_b_plus = np.expand_dims(t_b_plus, axis=-1)
	t_e_plus = np.expand_dims(t_e_plus, axis=-1)

	_, all_times_pred, _ = simulate(model_rmtpp,
										test_data_init_time,
										test_data_input_gaps_bin,
										t_e_plus,
										(test_gap_in_bin_norm_a,
										test_gap_in_bin_norm_d),
										prev_hidden_state=next_hidden_state)
	test_event_count_pred = None
	if query_data is not None:
		[t_b_plus, t_e_plus, true_count] = query_data
		t_b_plus = np.expand_dims(t_b_plus, axis=-1)
		t_e_plus = np.expand_dims(t_e_plus, axis=-1)
		test_event_count_pred = count_events(all_times_pred, t_b_plus, t_e_plus)
		test_event_count_pred = np.array(test_event_count_pred)

		event_count_mse = tf.keras.losses.MSE(true_count, test_event_count_pred).numpy()
		event_count_mae = tf.keras.losses.MAE(true_count, test_event_count_pred).numpy()
		#print("MSE of event count in range:", event_count_mse)
		#print("MAE of event count in range:", event_count_mae)

	all_times_pred = np.expand_dims(all_times_pred.numpy(), axis=-1)
	return test_event_count_pred, all_times_pred

def run_rmtpp_count_query(args, models, data, test_data, all_events_in_bin_pred=None, query=1, query_data=None):
	test_event_count_pred = None
	if query == 1:
		[t_b_plus, t_e_plus, true_count] = query_data
		if all_events_in_bin_pred is None:
			_, all_events_in_bin_pred = run_rmtpp_count_cont_rmtpp(args, models, data, test_data)
		test_event_count_pred = compute_count_event_range(all_events_in_bin_pred, t_b_plus, t_e_plus)
		event_count_mse = tf.keras.losses.MSE(true_count, test_event_count_pred).numpy()
		event_count_mae = tf.keras.losses.MAE(true_count, test_event_count_pred).numpy()
		print("MSE of event count in range:", event_count_mse)
		print("MAE of event count in range:", event_count_mae)
	else:
		print('Invalid query')

	return test_event_count_pred

def compute_threshold_loss_with_plt(all_run_count_fun, all_run_count_fun_name, model_data, query_data, dataset_name, query_1_data):
	[arguments, models, data, test_data] = model_data
	[interval_range_count_less, interval_range_count_more, 
	less_threshold, more_threshold, interval_size] = query_data
	[test_time_out_tb_plus, test_time_out_te_plus, test_out_event_count_true] = query_1_data

	[test_data_in_bin, test_data_out_bin, test_end_hr_bins,
	test_data_in_time_end_bin, test_data_in_gaps_bin, test_mean_bin, test_std_bin,
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] = test_data

	interval_range_count_more_pred = [[list() for j in range(len(all_run_count_fun))] for i in range(len(more_threshold))]
	interval_range_count_less_pred = [[list() for j in range(len(all_run_count_fun))] for i in range(len(less_threshold))]

	sample_count = 10
	execute_multiprocessing = False

	global compute_range_sample
	def compute_range_sample(batch_idx):
		print('Times pred for ', batch_idx)
		test_data_in_bin_i = test_data_in_bin[batch_idx:batch_idx+1]
		test_data_out_bin_i = test_data_out_bin[batch_idx:batch_idx+1]
		test_end_hr_bins_i = test_end_hr_bins[batch_idx:batch_idx+1]
		test_data_in_time_end_bin_i = test_data_in_time_end_bin[batch_idx:batch_idx+1]
		test_data_in_gaps_bin_i = test_data_in_gaps_bin[batch_idx:batch_idx+1]

		test_data_b = [test_data_in_bin_i, test_data_out_bin_i, test_end_hr_bins_i,
		test_data_in_time_end_bin_i, test_data_in_gaps_bin_i, test_mean_bin, test_std_bin,
		test_gap_in_bin_norm_a, test_gap_in_bin_norm_d]

		loop_cnt = 0
		while loop_cnt<10 and \
		(len(interval_range_count_more_pred[batch_idx])<=sample_count or len(interval_range_count_less_pred[batch_idx])<=sample_count):
			_, all_event_pred_uncut = all_run_count_fun(arguments, models, data, test_data_b)

			lst = list()
			for each in all_event_pred_uncut[0]:
				lst = lst+each.tolist()
			all_times_pred = np.array(lst)
			all_event_pred = np.expand_dims(all_times_pred, axis=0)

			interval_range_count_more_pred_i = utils.get_interval_count_more_than_threshold(all_event_pred, interval_size, more_threshold[batch_idx:batch_idx+1])[0]
			if not(interval_range_count_more_pred_i==-1) and len(interval_range_count_more_pred[batch_idx])<=sample_count:
				interval_range_count_more_pred[batch_idx].append(interval_range_count_more_pred_i)
				loop_cnt = 0

			interval_range_count_less_pred_i = utils.get_interval_count_less_than_threshold(all_event_pred, interval_size, less_threshold[batch_idx:batch_idx+1])[0]
			if not(interval_range_count_less_pred_i==-1) and len(interval_range_count_less_pred[batch_idx])<=sample_count:
				interval_range_count_less_pred[batch_idx].append(interval_range_count_less_pred_i)
				loop_cnt = 0

			loop_cnt += 1
	
		print('Times pred for ', batch_idx, 'Done')

	if execute_multiprocessing:
		pool = Pool()
		pool.map(compute_range_sample, range(len(less_threshold))) 
	else:
		for batch_idx in range(len(less_threshold)):
			test_data_in_bin_i = test_data_in_bin[batch_idx:batch_idx+1]
			test_data_out_bin_i = test_data_out_bin[batch_idx:batch_idx+1]
			test_end_hr_bins_i = test_end_hr_bins[batch_idx:batch_idx+1]
			test_data_in_time_end_bin_i = test_data_in_time_end_bin[batch_idx:batch_idx+1]
			test_data_in_gaps_bin_i = test_data_in_gaps_bin[batch_idx:batch_idx+1]

			test_time_out_tb_plus_i = test_time_out_tb_plus[batch_idx:batch_idx+1]
			test_time_out_te_plus_i = test_time_out_te_plus[batch_idx:batch_idx+1]
			test_out_event_count_true_i = test_out_event_count_true[batch_idx:batch_idx+1]

			test_data_b = [test_data_in_bin_i, test_data_out_bin_i, test_end_hr_bins_i,
			test_data_in_time_end_bin_i, test_data_in_gaps_bin_i, test_mean_bin, test_std_bin,
			test_gap_in_bin_norm_a, test_gap_in_bin_norm_d]

			query_1_data_b = [test_time_out_tb_plus_i, test_time_out_te_plus_i, test_out_event_count_true_i]

			print('Times pred for ', batch_idx)
			x_range = [int(test_data_in_time_end_bin_i), int(test_data_in_time_end_bin_i+(arguments.bin_size*arguments.out_bin_sz))]

			fig_cnt_more = plt.figure(1)
			plt.xlabel('timeline')
			plt.ylabel('pdf_cnt_more')
			plt.axvline(x=interval_range_count_more[batch_idx], color='red', linestyle='--')
			
			fig_cnt_less = plt.figure(2)
			plt.xlabel('timeline')
			plt.ylabel('pdf_cnt_less')
			plt.axvline(x=interval_range_count_less[batch_idx], color='red', linestyle='--')
			
			fig_area_more = plt.figure(3)
			plt.xlabel('timeline')
			plt.ylabel('pdf_area_more')
			plt.axvline(x=interval_range_count_more[batch_idx], color='red', linestyle='--')

			fig_area_less = plt.figure(4)
			plt.xlabel('timeline')
			plt.ylabel('pdf_area_less')
			plt.axvline(x=interval_range_count_less[batch_idx], color='red', linestyle='--')

			fun_cntr = -1
			for run_count_fun in all_run_count_fun:
				fun_cntr += 1
				loop_cnt = 0
				while loop_cnt<10 and \
				(len(interval_range_count_more_pred[batch_idx][fun_cntr])<=sample_count or len(interval_range_count_less_pred[batch_idx][fun_cntr])<=sample_count):

					if all_run_count_fun_name[fun_cntr] == 'run_rmtpp_for_count':
						_, all_event_pred_uncut = run_count_fun(arguments, models, data, test_data_b, query_1_data_b)
					else:
						_, all_event_pred_uncut = run_count_fun(arguments, models, data, test_data_b)

					lst = list()
					for each in all_event_pred_uncut[0]:
						lst = lst+each.tolist()
					all_times_pred = np.array(lst)
					all_event_pred = np.expand_dims(all_times_pred, axis=0)

					interval_range_count_more_pred_i = utils.get_interval_count_more_than_threshold(all_event_pred, interval_size, more_threshold[batch_idx:batch_idx+1])[0]
					if not(interval_range_count_more_pred_i==-1) and len(interval_range_count_more_pred[batch_idx][fun_cntr])<=sample_count:
						interval_range_count_more_pred[batch_idx][fun_cntr].append(interval_range_count_more_pred_i)
						loop_cnt = 0

					interval_range_count_less_pred_i = utils.get_interval_count_less_than_threshold(all_event_pred, interval_size, less_threshold[batch_idx:batch_idx+1])[0]
					if not(interval_range_count_less_pred_i==-1) and len(interval_range_count_less_pred[batch_idx][fun_cntr])<=sample_count:
						interval_range_count_less_pred[batch_idx][fun_cntr].append(interval_range_count_less_pred_i)
						loop_cnt = 0

					loop_cnt += 1

				def plot_threshold(interval_range_count_begin, interval_size, fig_cnt, fig_area):
					interval_range_count_end = interval_range_count_begin + interval_size
					interval_range_count_begin = np.sort(interval_range_count_begin)
					interval_range_count_end = np.sort(interval_range_count_end)

					axis_points = list()
					begin_cnts = list()
					interval_area = list()
					for dx in range(x_range[0], x_range[1], 10):
						axis_points.append(dx)
						begin_cnts.append(bisect_right(interval_range_count_begin, dx)-bisect_right(interval_range_count_end, dx))

					fig_cnt_plt = plt.figure(fig_cnt)
					plt.plot(axis_points, begin_cnts, label=all_run_count_fun_name[fun_cntr])

					for dx in range(x_range[0], x_range[1], 10):
						area = 0.0
						for interval_idx in range(len(interval_range_count_begin)):
							begin = interval_range_count_begin[interval_idx]
							end = interval_range_count_end[interval_idx]
							if begin >= dx and begin < dx+interval_size:
								area += dx+interval_size-begin
							elif end > dx and end < dx+interval_size:
								area += end-dx
						interval_area.append(area)

					fig_area_plt = plt.figure(fig_area)
					plt.plot(axis_points, interval_area, label=all_run_count_fun_name[fun_cntr])

				interval_range_count_more_begin = np.array(interval_range_count_more_pred[batch_idx][fun_cntr])
				plot_threshold(interval_range_count_more_begin, interval_size, 1, 3)

				interval_range_count_less_begin = np.array(interval_range_count_less_pred[batch_idx][fun_cntr])
				plot_threshold(interval_range_count_less_begin, interval_size, 2, 4)

			os.makedirs('Outputs/'+dataset_name+'_cnt_more/', exist_ok=True)
			img_name_cnt = 'Outputs/'+dataset_name+'_cnt_more/'+dataset_name+'_threshold_more_'+str(batch_idx)+'.png'
			fig_cnt_more = plt.figure(1)
			plt.legend(loc='upper right')
			plt.savefig(img_name_cnt)
			plt.close()
			
			os.makedirs('Outputs/'+dataset_name+'_area_more/', exist_ok=True)
			img_name_area = 'Outputs/'+dataset_name+'_area_more/'+dataset_name+'_threshold_more_'+str(batch_idx)+'.png'
			fig_area_more = plt.figure(2)
			plt.legend(loc='upper right')
			plt.savefig(img_name_area)
			plt.close()

			os.makedirs('Outputs/'+dataset_name+'_cnt_less/', exist_ok=True)
			img_name_cnt = 'Outputs/'+dataset_name+'_cnt_less/'+dataset_name+'_threshold_less_'+str(batch_idx)+'.png'
			fig_cnt_less = plt.figure(3)
			plt.legend(loc='upper right')
			plt.savefig(img_name_cnt)
			plt.close()
			
			os.makedirs('Outputs/'+dataset_name+'_area_less/', exist_ok=True)
			img_name_area = 'Outputs/'+dataset_name+'_area_less/'+dataset_name+'_threshold_less_'+str(batch_idx)+'.png'
			fig_area_less = plt.figure(4)
			plt.legend(loc='upper right')
			plt.savefig(img_name_area)
			plt.close()

def compute_time_range_pdf(all_run_count_fun, all_run_count_fun_name, model_data, query_data, dataset_name, query_1_data):
	[arguments, models, data, test_data] = model_data
	[interval_range_count_less, interval_range_count_more, 
	less_threshold, more_threshold, interval_size] = query_data
	[test_time_out_tb_plus, test_time_out_te_plus, test_out_event_count_true] = query_1_data

	[test_data_in_bin, test_data_out_bin, test_end_hr_bins,
	test_data_in_time_end_bin, test_data_in_gaps_bin, test_mean_bin, test_std_bin,
	test_gap_in_bin_norm_a, test_gap_in_bin_norm_d] = test_data

	sample_count = 100
	no_points = 500

	x_range = np.round(np.array([(test_data_in_time_end_bin), (test_data_in_time_end_bin+(arguments.bin_size*arguments.out_bin_sz))]))[:,:,0].T.astype(int)

	more_results = list()
	less_results = list()
	more_results_rank = list()
	less_results_rank = list()

	for run_count_fun_idx in range(len(all_run_count_fun)):
		print('Running for model', all_run_count_fun_name[run_count_fun_idx])
		interval_counts_more = np.zeros((len(test_data_in_time_end_bin), no_points))
		interval_counts_less = np.zeros((len(test_data_in_time_end_bin), no_points))
		interval_counts_more_rank = np.zeros((len(test_data_in_time_end_bin), no_points))
		interval_counts_less_rank = np.zeros((len(test_data_in_time_end_bin), no_points))

		for each_sim_idx in range(sample_count):
			#print('Simulating sample number', each_sim_idx)

			if all_run_count_fun_name[run_count_fun_idx] == 'run_rmtpp_for_count':
				_, all_event_pred_uncut = all_run_count_fun[run_count_fun_idx](arguments, models, data, test_data, query_1_data)
			else:
				_, all_event_pred_uncut = all_run_count_fun[run_count_fun_idx](arguments, models, data, test_data)

			all_times_pred = list()
			for idx in range(len(all_event_pred_uncut)):
				lst = list()
				for each in all_event_pred_uncut[idx]:
					lst = lst+each.tolist()
				all_times_pred.append(lst)
			all_times_pred = np.array(all_times_pred)

			for batch_idx in range(len(all_times_pred)):
				all_begins = np.linspace(x_range[batch_idx][0], x_range[batch_idx][1], no_points)
				interval_position_more = 1.0
				interval_position_less = 1.0
				for begin_idx in range(len(all_begins)):
					interval_start_cand = np.array([all_begins[begin_idx]])
					interval_end_cand = np.array([all_begins[begin_idx] + interval_size])

					interval_count_in_range_pred = count_events(all_times_pred[batch_idx:batch_idx+1], interval_start_cand, interval_end_cand)
					if more_threshold[batch_idx] <= interval_count_in_range_pred:
						interval_counts_more[batch_idx][begin_idx]+=1.0
						interval_counts_more_rank[batch_idx][begin_idx]+=(1.0/interval_position_more)
						interval_position_more += 1.0

					if less_threshold[batch_idx] >= interval_count_in_range_pred:
						interval_counts_less[batch_idx][begin_idx]+=1
						interval_counts_less_rank[batch_idx][begin_idx]+=(1.0/interval_position_less)
						interval_position_less += 1.0

		more_results.append(interval_counts_more)
		less_results.append(interval_counts_less)
		more_results_rank.append(interval_counts_more_rank)
		less_results_rank.append(interval_counts_less_rank)

	more_results = np.array(more_results)
	less_results = np.array(less_results)
	more_results_rank = np.array(more_results_rank)
	less_results_rank = np.array(less_results_rank)

	all_begins = np.linspace(x_range[:,0], x_range[:,1], no_points).T
	crps_loss_more = -1*np.ones((len(all_run_count_fun)))
	crps_loss_less = -1*np.ones((len(all_run_count_fun)))
	for run_count_fun_idx in range(len(all_run_count_fun)):
		all_counts_sum_more = np.sum(more_results_rank[run_count_fun_idx], axis=1)
		all_counts_sum_less = np.sum(less_results_rank[run_count_fun_idx], axis=1)

		more_results_rank[run_count_fun_idx][(all_counts_sum_more == 0)] = np.ones_like(more_results_rank[run_count_fun_idx][(all_counts_sum_more == 0)])
		less_results_rank[run_count_fun_idx][(all_counts_sum_less == 0)] = np.ones_like(less_results_rank[run_count_fun_idx][(all_counts_sum_less == 0)])

		all_counts_sum_more = np.expand_dims(np.sum(more_results_rank[run_count_fun_idx], axis=1), axis=-1)
		all_counts_sum_less = np.expand_dims(np.sum(less_results_rank[run_count_fun_idx], axis=1), axis=-1)

		crps_loss_more[run_count_fun_idx] = np.mean(ps.crps_ensemble(interval_range_count_more, all_begins, weights=more_results_rank[run_count_fun_idx]/all_counts_sum_more))
		crps_loss_less[run_count_fun_idx] = np.mean(ps.crps_ensemble(interval_range_count_less, all_begins, weights=less_results_rank[run_count_fun_idx]/all_counts_sum_more))

	print("CRPS for More")
	for run_count_fun_idx in range(len(all_run_count_fun)):
		print("Model", all_run_count_fun_name[run_count_fun_idx], ": Score =", crps_loss_more[run_count_fun_idx])

	print("CRPS for Less")
	for run_count_fun_idx in range(len(all_run_count_fun)):
		print("Model", all_run_count_fun_name[run_count_fun_idx], ": Score =", crps_loss_less[run_count_fun_idx])

	# Plots
	os.makedirs('Outputs/'+dataset_name+'_threshold_less/', exist_ok=True)
	os.makedirs('Outputs/'+dataset_name+'_threshold_more/', exist_ok=True)
	os.makedirs('Outputs/'+dataset_name+'_threshold_less_rank/', exist_ok=True)
	os.makedirs('Outputs/'+dataset_name+'_threshold_more_rank/', exist_ok=True)
	for batch_idx in range(len(test_data_in_time_end_bin)):
		all_begins = np.linspace(x_range[batch_idx][0], x_range[batch_idx][1], no_points)
		for run_count_fun_idx in range(len(all_run_count_fun)):
			all_counts_sum = max(1, np.sum(more_results[run_count_fun_idx,batch_idx]))
			plt.plot(all_begins, (more_results[run_count_fun_idx,batch_idx] / all_counts_sum), 
					 label=all_run_count_fun_name[run_count_fun_idx])
		plt.xlabel('timeline')
		plt.ylabel('pdf_threshold_more')
		plt.axvline(x=interval_range_count_more[batch_idx], color='red', linestyle='--')
		img_name_cnt = 'Outputs/'+dataset_name+'_threshold_more/'+dataset_name+'_threshold_more_'+str(batch_idx)+'.png'
		plt.legend(loc='upper right')
		plt.savefig(img_name_cnt)
		plt.close()

		for run_count_fun_idx in range(len(all_run_count_fun)):
			all_counts_sum = max(1, np.sum(less_results[run_count_fun_idx,batch_idx]))
			plt.plot(all_begins, (less_results[run_count_fun_idx,batch_idx] / all_counts_sum), 
					 label=all_run_count_fun_name[run_count_fun_idx])
		plt.xlabel('timeline')
		plt.ylabel('pdf_threshold_less')
		plt.axvline(x=interval_range_count_less[batch_idx], color='red', linestyle='--')
		img_name_cnt = 'Outputs/'+dataset_name+'_threshold_less/'+dataset_name+'_threshold_less_'+str(batch_idx)+'.png'
		plt.legend(loc='upper right')
		plt.savefig(img_name_cnt)
		plt.close()

		for run_count_fun_idx in range(len(all_run_count_fun)):
			all_counts_sum = max(1, np.sum(more_results_rank[run_count_fun_idx,batch_idx]))
			plt.plot(all_begins, (more_results_rank[run_count_fun_idx,batch_idx] / all_counts_sum), 
					 label=all_run_count_fun_name[run_count_fun_idx])
		plt.xlabel('timeline')
		plt.ylabel('pdf_threshold_more_rank')
		plt.axvline(x=interval_range_count_more[batch_idx], color='red', linestyle='--')
		img_name_cnt = 'Outputs/'+dataset_name+'_threshold_more_rank/'+dataset_name+'_threshold_more_rank_'+str(batch_idx)+'.png'
		plt.legend(loc='upper right')
		plt.savefig(img_name_cnt)
		plt.close()

		for run_count_fun_idx in range(len(all_run_count_fun)):
			all_counts_sum = max(1, np.sum(less_results_rank[run_count_fun_idx,batch_idx]))
			plt.plot(all_begins, (less_results_rank[run_count_fun_idx,batch_idx] / all_counts_sum), 
					 label=all_run_count_fun_name[run_count_fun_idx])
		plt.xlabel('timeline')
		plt.ylabel('pdf_threshold_less_rank')
		plt.axvline(x=interval_range_count_less[batch_idx], color='red', linestyle='--')
		img_name_cnt = 'Outputs/'+dataset_name+'_threshold_less_rank/'+dataset_name+'_threshold_less_rank_'+str(batch_idx)+'.png'
		plt.legend(loc='upper right')
		plt.savefig(img_name_cnt)
		plt.close()

def run_model(dataset_name, model_name, dataset, args, prev_models=None):
	print("Running for model", model_name, "on dataset", dataset_name)

	tf.random.set_seed(args.seed)
	test_data_out_bin = dataset['test_data_out_bin']
	event_count_preds_true = test_data_out_bin
	batch_size = args.batch_size
	result=None

	if model_name is 'hierarchical':
		train_data_in_bin = dataset['train_data_in_bin']
		train_data_out_bin = dataset['train_data_out_bin']
		test_data_in_bin = dataset['test_data_in_bin']
		test_data_out_bin = dataset['test_data_out_bin']
		test_mean_bin = dataset['test_mean_bin']
		test_std_bin = dataset['test_std_bin']

		data = [train_data_in_bin, train_data_out_bin]
		test_data = [test_data_in_bin, test_data_out_bin, test_mean_bin, test_std_bin]
		event_count_preds_cnt = run_hierarchical(args, data, test_data)
		model, result = event_count_preds_cnt

	if model_name in ['rmtpp_mse', 'rmtpp_nll', 'rmtpp_count']:
		train_data_in_gaps = dataset['train_data_in_gaps']
		train_data_out_gaps = dataset['train_data_out_gaps']
		train_dataset_gaps = tf.data.Dataset.from_tensor_slices((train_data_in_gaps,
														train_data_out_gaps)).batch(batch_size,
														drop_remainder=True)
		dev_data_in_gaps = dataset['dev_data_in_gaps']
		dev_data_out_gaps = dataset['dev_data_out_gaps']
		train_norm_a_gaps = dataset['train_norm_a_gaps']
		train_norm_d_gaps = dataset['train_norm_d_gaps']

		test_data_in_gaps_bin = dataset['test_data_in_gaps_bin']
		test_end_hr_bins = dataset['test_end_hr_bins'] 
		test_data_in_time_end_bin = dataset['test_data_in_time_end_bin']
		test_gap_in_bin_norm_a = dataset['test_gap_in_bin_norm_a'] 
		test_gap_in_bin_norm_d = dataset['test_gap_in_bin_norm_d']

		test_data = [test_data_in_gaps_bin, test_end_hr_bins, test_data_in_time_end_bin, 
					test_gap_in_bin_norm_a, test_gap_in_bin_norm_d]
		train_norm_gaps = [train_norm_a_gaps ,train_norm_d_gaps]
		data = [train_dataset_gaps, dev_data_in_gaps, dev_data_out_gaps, train_norm_gaps]

		if model_name is 'rmtpp_mse':
			event_count_preds_mse = run_rmtpp_mse(args, data, test_data)
			model, result = event_count_preds_mse

		if model_name is 'rmtpp_nll':
			event_count_preds_nll = run_rmtpp_nll(args, data, test_data)
			model, result = event_count_preds_nll

		if model_name is 'rmtpp_count':
			test_data_in_bin = dataset['test_data_in_bin']
			test_data_out_bin = dataset['test_data_out_bin']
			test_mean_bin = dataset['test_mean_bin']
			test_std_bin = dataset['test_std_bin']

			test_time_out_tb_plus = dataset['test_time_out_tb_plus']
			test_time_out_te_plus = dataset['test_time_out_te_plus']
			test_out_event_count_true = dataset['test_out_event_count_true']
			test_out_all_event_true = dataset['test_out_all_event_true']

			interval_range_count_less = dataset['interval_range_count_less']
			interval_range_count_more = dataset['interval_range_count_more']
			less_threshold = dataset['less_threshold']
			more_threshold = dataset['more_threshold']
			interval_size = dataset['interval_size']



			model_cnt, model_rmtpp = prev_models['hierarchical'], prev_models['rmtpp_mse']
			models = model_cnt, model_rmtpp
			test_data = [test_data_in_bin, test_data_out_bin, test_end_hr_bins,
			test_data_in_time_end_bin, test_data_in_gaps_bin, test_mean_bin, test_std_bin,
			test_gap_in_bin_norm_a, test_gap_in_bin_norm_d]
			data = None
			compute_depth = 5

			query_1_data = [test_time_out_tb_plus, test_time_out_te_plus, test_out_event_count_true]
			query_2_data = [interval_range_count_less, interval_range_count_more, less_threshold, more_threshold, interval_size]

			old_stdout = sys.stdout
			sys.stdout=open("Outputs/count_model_"+dataset_name+".txt","w")
			print("True counts")
			print(test_out_event_count_true)

			print("____________________________________________________________________")
			print("")
			model_data = [args, models, data, test_data]
			all_run_count_fun = [run_rmtpp_count_cont_rmtpp, run_rmtpp_count_reinit, run_rmtpp_for_count]
			all_run_count_fun_name = ['run_rmtpp_count_cont_rmtpp', 'run_rmtpp_count_reinit', 'run_rmtpp_for_count']
			compute_time_range_pdf(all_run_count_fun, all_run_count_fun_name, model_data, query_2_data, dataset_name, query_1_data)

			print("____________________________________________________________________")
			print("")
			model, all_times_bin_pred = run_rmtpp_count_cont_rmtpp(args, models, data, test_data)
			deep_mae = compute_hierarchical_mae(all_times_bin_pred, query_1_data, test_out_all_event_true, compute_depth)
			threshold_mae = compute_threshold_loss(all_times_bin_pred, query_2_data)
			# result = run_rmtpp_count_query(args, models, data, test_data, all_times_bin_pred, 1, query_1_data)
			print("Prediction for rmtpp_count_with_cont_simu model")
			print("deep_mae", deep_mae)
			# print(result)

			print("____________________________________________________________________")
			print("")
			model, all_times_bin_pred = run_rmtpp_count_reinit(args, models, data, test_data)
			deep_mae = compute_hierarchical_mae(all_times_bin_pred, query_1_data, test_out_all_event_true, compute_depth)
			threshold_mae = compute_threshold_loss(all_times_bin_pred, query_2_data)
			# result = run_rmtpp_count_query(args, models, data, test_data, all_times_bin_pred, 1, query_1_data)
			print("Prediction for rmtpp_count_reinit model")
			print("deep_mae", deep_mae)
			# print(result)

			print("____________________________________________________________________")
			print("")
			result, all_times_bin_pred = run_rmtpp_for_count(args, models, data, test_data, query_1_data)
			deep_mae = compute_hierarchical_mae(all_times_bin_pred, query_1_data, test_out_all_event_true, compute_depth)
			threshold_mae = compute_threshold_loss(all_times_bin_pred, query_2_data)
			print("Prediction for plain_rmtpp_count model")
			print("deep_mae", deep_mae)

			print("____________________________________________________________________")
			print("")
			sys.stdout.close()
			sys.stdout = old_stdout
			
	return model, result

