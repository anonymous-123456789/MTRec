# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

### import packages ###
from __future__ import absolute_import, division, print_function, unicode_literals

# miscellaneous
import time
import os
from os import path
import random
import csv

# numpy and scikit-learn
import numpy as np
from sklearn.metrics import roc_auc_score

# pytorch
import torch
import torch.nn as nn
from torch.autograd.profiler import record_function
import torch.nn.functional as Functional
from torch.nn.parameter import Parameter
#from torch.utils.tensorboard import SummaryWriter

# tbsm data
import tbsm_data_pytorch as tp

# set python, numpy and torch random seeds
def set_seed(seed, use_gpu):
	random.seed(seed)
	os.environ['PYTHONHASHSEED'] = str(seed)
	np.random.seed(seed)
	if use_gpu:
		torch.manual_seed(seed)
		torch.cuda.manual_seed(seed)
		torch.cuda.manual_seed_all(seed)   # if using multi-GPU.
		torch.backends.cudnn.benchmark = False
		torch.backends.cudnn.deterministic = True


### define time series layer (TSL) ###
class TSL_Net(nn.Module):
	def __init__(
			self,
			arch_interaction_op='dot',
			arch_attention_mechanism='mlp',
			ln=None,
			model_type="tsl",
			tsl_inner="def",
			mha_num_heads=8,
			tra_encoder_layers=1,
			tra_attention_heads=2,
			tra_feedforward_dim=512,
			tra_norm_first=False,
			tra_activation="relu",
			tra_dropout=0.1,
			ln_top=""
	):
		super(TSL_Net, self).__init__()

		# save arguments
		self.arch_interaction_op = arch_interaction_op
		self.arch_attention_mechanism = arch_attention_mechanism
		self.model_type = model_type
		self.tsl_inner = tsl_inner

		# setup for mechanism type
		if self.arch_attention_mechanism == 'mlp':
			self.mlp = dlrm.DLRM_Net().create_mlp(ln, len(ln) - 2)

		# setup extra parameters for some of the models
		if self.model_type == "tsl" and self.tsl_inner in ["def", "ind"]:
			m = ln_top[-1]  # dim of dlrm output
			mean = 0.0
			std_dev = np.sqrt(2 / (m + m))
			W = np.random.normal(mean, std_dev, size=(1, m, m)).astype(np.float32)
			self.A = Parameter(torch.tensor(W), requires_grad=True)
		elif self.model_type == "mha":
			m = ln_top[-1]  # dlrm output dim
			#print("m : ", m)
			self.nheads = mha_num_heads
			#print("nhead : ", self.nheads)
			self.emb_m = self.nheads * m  # mha emb dim
			#print("emb_m : ", self.emb_m)
			mean = 0.0
			std_dev = np.sqrt(2 / (m + m))  # np.sqrt(1 / m) # np.sqrt(1 / n)
			qm = np.random.normal(mean, std_dev, size=(1, m, self.emb_m)) \
				.astype(np.float32)
			self.Q = Parameter(torch.tensor(qm), requires_grad=True)
			#print("Q : ", self.Q.shape)
			km = np.random.normal(mean, std_dev, size=(1, m, self.emb_m))  \
				.astype(np.float32)
			self.K = Parameter(torch.tensor(km), requires_grad=True)
			#print("K : ", self.K.shape)
			vm = np.random.normal(mean, std_dev, size=(1, m, self.emb_m)) \
				.astype(np.float32)
			self.V = Parameter(torch.tensor(vm), requires_grad=True)
			#print("V : ", self.V.shape)
		elif self.model_type == "transformer":
			encoder_layer = nn.TransformerEncoderLayer(d_model=ln_top[-1], nhead=tra_attention_heads, batch_first=True, dim_feedforward=tra_feedforward_dim, norm_first=tra_norm_first, activation=tra_activation, dropout=tra_dropout)
			self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=tra_encoder_layers)

	def forward(self, x=None, H=None):
		# adjust input shape
		(batchSize, vector_dim) = x.shape
		#print("x : ", x.shape)
		#print("H : ", H.shape)
		x = torch.reshape(x, (batchSize, 1, -1))
		x = torch.transpose(x, 1, 2)
		# debug prints
		#print("shapes: ", self.A.shape, x.shape)

		# perform mode operation
		if self.model_type == "tsl":
			if self.tsl_inner == "def":
				ax = torch.matmul(self.A, x)
				x = torch.matmul(self.A.permute(0, 2, 1), ax)
				# debug prints
				# print("shapes: ", H.shape, ax.shape, x.shape)
			elif self.tsl_inner == "ind":
				x = torch.matmul(self.A, x)

			# perform interaction operation
			if self.arch_interaction_op == 'dot':
				if self.arch_attention_mechanism == 'mul':
					# coefficients
					a = torch.transpose(torch.bmm(H, x), 1, 2)
					# context
					c = torch.bmm(a, H)
				elif self.arch_attention_mechanism == 'mlp':
					# coefficients
					a = torch.transpose(torch.bmm(H, x), 1, 2)
					# MLP first/last layer dims are automatically adjusted to ts_length
					y = dlrm.DLRM_Net().apply_mlp(a, self.mlp)
					# context, y = mlp(a)
					c = torch.bmm(torch.reshape(y, (batchSize, 1, -1)), H)
				else:
					sys.exit('ERROR: --arch-attention-mechanism='
						+ self.arch_attention_mechanism + ' is not supported')

			else:
				sys.exit('ERROR: --arch-interaction-op=' + self.arch_interaction_op
					+ ' is not supported')

		elif self.model_type == "mha":
			x = torch.transpose(x, 1, 2)
			#print("x : ", x.shape)
			Qx = torch.transpose(torch.matmul(x, self.Q), 0, 1)
			#print("Qx : ", Qx.shape)
			HK = torch.transpose(torch.matmul(H, self.K), 0, 1)
			#print("HK : ", HK.shape)
			HV = torch.transpose(torch.matmul(H, self.V), 0, 1)
			#print("HV : ", HV.shape)
			# multi-head attention (mha)
			multihead_attn = nn.MultiheadAttention(self.emb_m, self.nheads).to(x.device)
			attn_output, _ = multihead_attn(Qx, HK, HV)
			#print("attn_output : ", attn_output.shape)
			# context
			c = torch.squeeze(attn_output, dim=0)
			#print("c : ", c.shape)
			# debug prints
			# print("shapes:", c.shape, Qx.shape)

		elif self.model_type == "transformer":
			x = torch.transpose(x, 1, 2)
			tra_input = torch.cat([x] + [H], dim=1)
			self.transformer = self.transformer.to(x.device)
			tra_output = self.transformer(tra_input)
			c = tra_output[:, 0, :]
			#print(next(self.transformer.parameters()).device)
		return c


### define Time-based Sequence Model (TBSM) ###
class TBSM_Net(nn.Module):
	def __init__(
			self,
			m_spa,
			ln_emb,
			ln_bot,
			ln_top,
			intr_b1,
			intr_b2,
			arch_interaction_op,
			arch_interaction_itself,
			ln_mlp,
			ln_tsl,
			tsl_interaction_op,
			tsl_mechanism,
			ts_length,
			num_layers,
			nhead,
			dim_feedforward,
			norm_first,
			activation,
			dropout,
			mask_threshold,
			ndevices=-1,
			model_type="",
			tsl_seq=False,
			tsl_proj=True,
			tsl_inner="def",
			tsl_num_heads=1,
			mha_num_heads=8,
			rnn_num_layers=5,
			tra_encoder_layers=1,
			tra_attention_heads=2,
			tra_feedforward_dim=512,
			tra_norm_first=False,
			tra_activation="relu",
			tra_dropout=0.1,
			dcn_num_layers=2,
			dcn_low_rank_dim=16,
			mha_intr_num_heads=2,
			debug_mode=False,
	):
		super(TBSM_Net, self).__init__()

		# save arguments
		self.ndevices = ndevices
		self.debug_mode = debug_mode
		self.ln_bot = ln_bot
		self.ln_top = ln_top
		self.ln_tsl = ln_tsl
		self.ts_length = ts_length
		self.tsl_interaction_op = tsl_interaction_op
		self.tsl_mechanism = tsl_mechanism
		self.model_type = model_type
		self.tsl_seq = tsl_seq
		self.tsl_proj = tsl_proj
		self.tsl_inner = tsl_inner
		self.tsl_num_heads = tsl_num_heads
		self.mha_num_heads = mha_num_heads
		self.rnn_num_layers = rnn_num_layers
		self.ams = nn.ModuleList()
		self.mlps = nn.ModuleList()
		if self.model_type == "tsl":
			self.num_mlps = int(self.tsl_num_heads)   # number of tsl components
		else:
			self.num_mlps = 1
		#debug prints
		if self.debug_mode:
			print(self.model_type)
			print(ln_bot)
			print(ln_top)
			print(ln_emb)

		# embedding layer (implemented through dlrm tower, without last layer sigmoid)
		if "qr" in model_type:
			self.dlrm = dlrm.DLRM_Net(
				m_spa, ln_emb, ln_bot, ln_top,
				arch_interaction_op, dcn_num_layers, dcn_low_rank_dim, mha_intr_num_heads, arch_interaction_itself,
				qr_flag=True, qr_operation="add", qr_collisions=4, qr_threshold=100000
			)
			print("Using QR embedding method.")
		else:
			self.dlrm = dlrm.DLRM_Net(
				m_spa,
				ln_emb,
				ln_bot,
				ln_top,
				intr_b1,
				intr_b2,
				arch_interaction_op,
				dcn_num_layers,
				dcn_low_rank_dim,
				mha_intr_num_heads,
				arch_interaction_itself,
				num_layers=num_layers,
				nhead=nhead,
				dim_feedforward=dim_feedforward,
				norm_first=norm_first,
				activation=activation,
				dropout=dropout,
				mask_threshold=mask_threshold
			)

		# prepare data needed for tsl layer construction
		if self.model_type == "tsl":
			if not self.tsl_seq:
				self.ts_array = [self.ts_length] * self.num_mlps
			else:
				self.ts_array = []
				m = self.ts_length / self.tsl_num_heads
				for j in range(self.tsl_num_heads, 0, -1):
					t = min(self.ts_length, round(m * j))
					self.ts_array.append(t)
		elif self.model_type == "mha":
			self.ts_array = [self.ts_length]
			#print("ts_array : ", self.ts_array)
		elif self.model_type == "transformer":
			self.ts_array = [self.ts_length]
			#print("ts_array : ", self.ts_array)
		else:
			self.ts_array = []

		# construction of one or more tsl components
		for ts in self.ts_array:

			ln_tsl = np.concatenate((np.array([ts]), self.ln_tsl))
			ln_tsl = np.append(ln_tsl, ts)

			print("ln_tsl : ", ln_tsl)
			# create tsl mechanism
			am = TSL_Net(
				arch_interaction_op=self.tsl_interaction_op,
				arch_attention_mechanism=self.tsl_mechanism,
				ln=ln_tsl, model_type=self.model_type,
				tsl_inner=self.tsl_inner,
				mha_num_heads=self.mha_num_heads,
				tra_encoder_layers=tra_encoder_layers,
				tra_attention_heads=tra_attention_heads,
				tra_feedforward_dim=tra_feedforward_dim,
				tra_norm_first=tra_norm_first,
				tra_activation=tra_activation,
				tra_dropout=tra_dropout, ln_top=self.ln_top,
			)

			self.ams.append(am)

		# tsl MLPs (with sigmoid on last layer)
		for _ in range(self.num_mlps):
			mlp_tsl = dlrm.DLRM_Net().create_mlp(ln_mlp, ln_mlp.size - 2)
			self.mlps.append(mlp_tsl)

		# top mlp if needed
		if self.num_mlps > 1:
			f_mlp = np.array([self.num_mlps, self.num_mlps + 4, 1])
			self.final_mlp = dlrm.DLRM_Net().create_mlp(f_mlp, f_mlp.size - 2)

	def forward(self, x, lS_o, lS_i):
		# data point is history H and last entry w
		n = x[0].shape[0]  # batch_size
		ts = len(x)
		H = torch.zeros(n, self.ts_length, self.ln_top[-1]).to(x[0].device)
		
		# split point into first part (history)
		# and last item
		for j in range(ts - self.ts_length - 1, ts - 1):
			oj = j - (ts - self.ts_length - 1)
			v = self.dlrm(x[j], lS_o[j], lS_i[j])
			if self.model_type == "tsl" and self.tsl_proj:
				v = Functional.normalize(v, p=2, dim=1)
			H[:, oj, :] = v
		
		w = self.dlrm(x[-1], lS_o[-1], lS_i[-1])
		# project onto sphere
		if self.model_type == "tsl" and self.tsl_proj:
			w = Functional.normalize(w, p=2, dim=1)
		# print("data: ", x[-1], lS_o[-1], lS_i[-1])

		(mini_batch_size, _) = w.shape

		# for cases when model is tsl or mha
		if self.model_type != "rnn":

			# create MLP for each TSL component
			# each ams[] element is one component
			for j in range(self.num_mlps):

				ts = self.ts_length - self.ts_array[j]
				#print("ts : ", ts)
				c = self.ams[j](w, H[:, ts:, :])
				#print("c : ", c.shape)
				c = torch.reshape(c, (mini_batch_size, -1))
				#print("c : ", c.shape)
				# concat context and w
				z = torch.cat([c, w], dim=1)
				#print("z : ", z.shape)
				
				# obtain probability of a click as a result of MLP
				p = dlrm.DLRM_Net().apply_mlp(z, self.mlps[j])
				if j == 0:
					ps = p
				else:
					ps = torch.cat((ps, p), dim=1)

			if ps.shape[1] > 1:
				p_out = dlrm.DLRM_Net().apply_mlp(ps, self.final_mlp)
			else:
				p_out = ps

		# RNN based on LSTM cells case, context is final hidden state
		else:
			hidden_dim = w.shape[1]     # equal to dim(w) = dim(c)
			level = self.rnn_num_layers  # num stacks of rnns
			Ht = H.permute(1, 0, 2)
			rnn = nn.LSTM(int(self.ln_top[-1]), int(hidden_dim),
			int(level)).to(x[0].device)
			h0 = torch.randn(level, n, hidden_dim).to(x[0].device)
			c0 = torch.randn(level, n, hidden_dim).to(x[0].device)
			output, (hn, cn) = rnn(Ht, (h0, c0))
			hn, cn = torch.squeeze(hn[level - 1, :, :]), \
				torch.squeeze(cn[level - 1, :, :])
			if self.debug_mode:
				print(w.shape, output.shape, hn.shape)
			# concat context and w
			z = torch.cat([hn, w], dim=1)
			p_out = dlrm.DLRM_Net().apply_mlp(z, self.mlps[0])

		return p_out


# construct tbsm model or read it from the file specified
# by args.save_model
def get_tbsm(args, use_gpu):

	# train, test, or train-test
	modes = args.mode.split("-")
	model_file = args.save_model

	if args.debug_mode:
		print("model_file: ", model_file)
		print("model_type: ", args.model_type)

	if use_gpu:
		ngpus = torch.cuda.device_count()  # 1
		devicenum = "cuda:" + str(args.device_num % ngpus)
		print("device:", devicenum)
		device = torch.device(devicenum)
		print("Using {} GPU(s)...".format(ngpus))
	else:
		device = torch.device("cpu")
		print("Using CPU...")

	# prepare dlrm arch
	m_spa = args.arch_sparse_feature_size
	# this is an array of sizes of cat features
	ln_emb = np.fromstring(args.arch_embedding_size, dtype=int, sep="-")
	num_fea = ln_emb.size + 1  # user: num sparse + bot_mlp(all dense)
	ln_bot = np.fromstring(args.arch_mlp_bot, dtype=int, sep="-")
	intr_b1 = np.fromstring(args.interaction_branch1_layer_sizes, dtype=int, sep="-")
	intr_b2 = np.fromstring(args.interaction_branch2_layer_sizes, dtype=int, sep="-")
	#  m_den = ln_bot[0]
	ln_bot[ln_bot.size - 1] = m_spa  # enforcing
	m_den_out = ln_bot[ln_bot.size - 1]  # must be == m_spa (embed dim)

	if args.arch_interaction_op == "dot":
		# approach 1: all
		# num_int = num_fea * num_fea + m_den_out
		# approach 2: unique
		if args.arch_interaction_itself:
			num_int = (num_fea * (num_fea + 1)) // 2 + m_den_out
		else:
			num_int = (num_fea * (num_fea - 1)) // 2 + m_den_out
	elif args.arch_interaction_op == "cat":
		num_int = num_fea * m_den_out
	elif args.arch_interaction_op == "dcn":
		num_int = num_fea * m_den_out
	elif args.arch_interaction_op == "proj":
		num_int = num_fea * m_den_out
		intr_b1_adjusted = str(num_int) + "-" + args.interaction_branch1_layer_sizes
		intr_b2_adjusted = str(num_int) + "-" + args.interaction_branch2_layer_sizes
		intr_b1 = np.fromstring(intr_b1_adjusted, dtype=int, sep="-")
		intr_b2 = np.fromstring(intr_b2_adjusted, dtype=int, sep="-")
		intr_b1_out = intr_b1[intr_b1.size - 1]
		intr_b2_out = intr_b2[intr_b2.size - 1]
		if intr_b1_out % m_spa != 0:
			raise ValueError(
				"Final interaction branch1 layer size "
				"({}) is not a multiple of embedding size ({})".format(
					intr_b1_out, m_spa
				)
			)
		projected_b1 = intr_b1_out // m_spa
		if intr_b2_out % m_spa != 0:
			raise ValueError(
				"Final interaction branch2 layer size "
				"({}) is not a multiple of embedding size ({})".format(
					intr_b2_out, m_spa
				)
			)
		projected_b2 = intr_b2_out // m_spa
		num_int = m_den_out + projected_b1 * projected_b2
	elif args.arch_interaction_op == "mha":
		num_int = num_fea * m_den_out
	elif args.arch_interaction_op == "transformers":
		num_int = (num_fea * m_den_out) + m_den_out
	else:
		sys.exit(
			"ERROR: --arch-interaction-op="
			+ args.arch_interaction_op
			+ " is not supported"
		)
	arch_mlp_top_adjusted = str(num_int) + "-" + args.arch_mlp_top
	ln_top = np.fromstring(arch_mlp_top_adjusted, dtype=int, sep="-")
	# sigmoid_top = len(ln_top) - 2    # used only if length_ts == 1
	# attention mlp (will be automatically adjusted so that first and last
	# layer correspond to number of vectors (ts_length) used in attention)
	ln_atn = np.fromstring(args.tsl_mlp, dtype=int, sep="-")
	# context MLP (with automatically adjusted first layer)
	if args.model_type == "mha":
		num_cat = (int(args.mha_num_heads) + 1) * ln_top[-1]    # mha with k heads + w
	elif args.model_type == "transformer":
		num_cat = 2 * ln_top[-1]
	else:         # tsl or rnn
		num_cat = 2 * ln_top[-1]   # [c,w]
	arch_mlp_adjusted = str(num_cat) + "-" + args.arch_mlp
	print(arch_mlp_adjusted)
	ln_mlp = np.fromstring(arch_mlp_adjusted, dtype=int, sep="-")
	ndevices = min(ngpus, args.mini_batch_size) if use_gpu else -1

	# construct TBSM
	tbsm = TBSM_Net(
		m_spa,
		ln_emb,
		ln_bot,
		ln_top,
		intr_b1,
		intr_b2,
		args.arch_interaction_op,
		args.arch_interaction_itself,
		ln_mlp,
		ln_atn,
		args.tsl_interaction_op,
		args.tsl_mechanism,
		args.ts_length,
		args.num_encoder_layers,
		args.num_attention_heads,
		args.feedforward_dim,
		args.norm_first,
		args.activation,
		args.dropout,
		args.mask_threshold,
		ndevices,
		args.model_type,
		args.tsl_seq,
		args.tsl_proj,
		args.tsl_inner,
		args.tsl_num_heads,
		args.mha_num_heads,
		args.rnn_num_layers,
		args.tra_encoder_layers,
		args.tra_attention_heads,
		args.tra_feedforward_dim,
		args.tra_norm_first,
		args.tra_activation,
		args.tra_dropout,
		args.dcn_num_layers,
		args.dcn_low_rank_dim,
		args.mha_intr_num_heads,
		args.debug_mode,
	)
	# move model to gpu
	if use_gpu:
		tbsm = tbsm.to(device)  # .cuda()

	# load existing pre-trained model if needed
	
	if path.exists(model_file):
		if modes[0] == "test" or (len(modes) > 1 and modes[1] == "test"):
			if use_gpu:
				ld_model = torch.load(
					model_file,
					map_location=torch.device('cuda')
				)
			else:
				# when targeting inference on CPU
				ld_model = torch.load(model_file, map_location=torch.device('cpu'))

			tbsm.load_state_dict(ld_model['model_state_dict'])
	

	return tbsm, device


def data_wrap(X, lS_o, lS_i, use_gpu, device):
	if use_gpu:  # .cuda()
		return ([xj.to(device) for xj in X],
				[[S_o.to(device) for S_o in row] for row in lS_o],
				[[S_i.to(device) for S_i in row] for row in lS_i])
	else:
		return X, lS_o, lS_i


def time_wrap(use_gpu):
	if use_gpu:
		torch.cuda.synchronize()
	return time.time()


def loss_fn_wrap(Z, T, use_gpu, device):
	if use_gpu:
		return loss_fn(Z, T.to(device))
	else:
		return loss_fn(Z, T)

loss_fn = torch.nn.BCELoss(reduction="mean")

# iterate through validation data, which can be used to determine the best seed and
# during main training for deciding to save the current model
def iterate_val_data(val_ld, tbsm, use_gpu, device):
	# NOTE: call to tbsm.eval() not needed here, see
	# https://discuss.pytorch.org/t/model-eval-vs-with-torch-no-grad/19615
	total_loss_val = 0
	total_accu_test = 0
	total_samp_test = 0

	for _, (X, lS_o, lS_i, T_test) in enumerate(val_ld):
		batchSize = X[0].shape[0]

		Z_test = tbsm(*data_wrap(X,
			lS_o,
			lS_i,
			use_gpu,
			device
		))

		# # compute loss and accuracy
		z = Z_test.detach().cpu().numpy()  # numpy array
		t = T_test.detach().cpu().numpy()  # numpy array
		A_test = np.sum((np.round(z, 0) == t).astype(np.uint8))
		total_accu_test += A_test
		total_samp_test += batchSize

		E_test = loss_fn_wrap(Z_test, T_test, use_gpu, device)
		L_test = E_test.detach().cpu().numpy()  # numpy array
		total_loss_val += (L_test * batchSize)

	return total_accu_test, total_samp_test, total_loss_val

# iterate through test data, to find AUC
def iterate_test_data(test_ld, n_test, tbsm, use_gpu, device):
	# NOTE: call to tbsm.eval() not needed here, see
	# https://discuss.pytorch.org/t/model-eval-vs-with-torch-no-grad/19615

	# setup initial values
	z_test = np.zeros((len(n_test), ), dtype=np.float)
	t_test = np.zeros((len(n_test), ), dtype=np.float)

	offset = 0
	for _, (X, lS_o, lS_i, T) in enumerate(test_ld):

		batchSize = X[0].shape[0]
		
		Z = tbsm(*data_wrap(X,
			lS_o,
			lS_i,
			use_gpu,
			device
		))
		
		z_test[offset: offset + batchSize] = np.squeeze(Z.detach().cpu().numpy(),
		axis=1)
		t_test[offset: offset + batchSize] = np.squeeze(T.detach().cpu().numpy(),
		axis=1)
		offset += batchSize

	if args.quality_metric == "auc":
		# compute AUC metric
		auc_score = 100.0 * roc_auc_score(t_test.astype(int), z_test)
	else:
		sys.exit("Metric not supported.")

	return auc_score

# iterate through training data, which is called once every epoch. It updates weights,
# computes loss, accuracy, saves model if needed and calls iterate_val_data() function.
# isMainTraining is True for main training and False for fast seed selection
def iterate_train_data(args, train_ld, val_ld, test_ld, n_test, tbsm, k, use_gpu, device, writer, losses, accuracies, isMainTraining):
	# select number of batches
	if isMainTraining:
		nbatches = len(train_ld) if args.num_batches == 0 else args.num_batches
	else:
		nbatches = len(train_ld)

	# specify the optimizer algorithm
	optimizer = torch.optim.Adagrad(tbsm.parameters(), lr=args.learning_rate)

	total_time = 0
	total_loss = 0
	total_accu = 0
	total_iter = 0
	total_samp = 0
	max_gA_test = 0
	fwd_itr = 0
	bwd_itr = 0
	opt_itr = 0
	forward_time = 0
	backward_time = 0
	optimizer_time = 0
	test_accuracy_numbers = []
	
	for j, (X, lS_o, lS_i, T) in enumerate(train_ld):
		if j >= nbatches:
			break
		t1 = time_wrap(use_gpu)
		batchSize = X[0].shape[0]
		# forward pass
		
		begin_forward = time_wrap(use_gpu)
		Z = tbsm(*data_wrap(X,
			lS_o,
			lS_i,
			use_gpu,
			device
		))

		end_forward = time_wrap(use_gpu)

		# loss
		E = loss_fn_wrap(Z, T, use_gpu, device)
		# compute loss and accuracy
		L = E.detach().cpu().numpy()  # numpy array
		z = Z.detach().cpu().numpy()  # numpy array
		t = T.detach().cpu().numpy()  # numpy array
		# rounding t
		A = np.sum((np.round(z, 0) == np.round(t, 0)).astype(np.uint8))

		begin_backward = time_wrap(use_gpu)
		# backward pass
		optimizer.zero_grad()
		E.backward(retain_graph=True)

		end_backward = time_wrap(use_gpu)

		# weights update
		optimizer.step()

		end_optimizing = time_wrap(use_gpu)

		t2 = time_wrap(use_gpu)
		total_time += t2 - t1
		total_loss += (L * batchSize)
		total_accu += A
		total_iter += 1
		total_samp += batchSize
		fwd_itr += end_forward - begin_forward
		bwd_itr += end_backward - begin_backward
		opt_itr += end_optimizing - end_backward
		forward_time += end_forward - begin_forward
		backward_time += end_backward - begin_backward
		optimizer_time += end_optimizing - end_backward

		print_tl = ((j + 1) % args.print_freq == 0) or (j + 1 == nbatches)
		# print time, loss and accuracy
		if print_tl and isMainTraining:

			gT = 1000.0 * total_time / total_iter if args.print_time else -1
			total_time = 0

			gL = total_loss / total_samp
			total_loss = 0

			gA = total_accu / total_samp
			total_accu = 0

			gForward = 1000 * fwd_itr / total_iter

			gBackward = 1000 * bwd_itr / total_iter

			gOptimizer = 1000 * opt_itr / total_iter

			str_run_type = "inference" if args.inference_only else "training"

			print("Forward ", gForward)
			print("Backward ", gBackward)
			print("Optimizer ", gOptimizer)
			print("Epoch ", k)
			print("Iteration ", (k * nbatches) + (j+1))
			print("Total_Iterations ", nbatches)
			print("Iteration_time ", gT)
			print("Loss ", gL)
			print("Accuracy ", gA*100)
			print("\n")

			total_iter = 0
			total_samp = 0
			fwd_itr = 0
			bwd_itr = 0
			opt_itr = 0

		if isMainTraining:
			should_test = (
				(args.test_freq > 0
				and (j + 1) % args.test_freq == 0) or j + 1 == nbatches
			)
		else:
			should_test = (j == min(int(0.05 * len(train_ld)), len(train_ld) - 1))

		#  validation run
		if should_test:

			total_accu_test, total_samp_test, total_loss_val = iterate_val_data(val_ld, tbsm, use_gpu, device)

			gA_test = total_accu_test / total_samp_test
			if not isMainTraining:
				break

			gL_test = total_loss_val / total_samp_test


			if args.enable_summary and isMainTraining:
				losses = np.append(losses, np.array([[j, gL, gL_test]]),
				axis=0)
				accuracies = np.append(accuracies, np.array([[j, gA * 100,
				gA_test * 100]]), axis=0)
			
			# save model if best so far
			if gA_test > max_gA_test and isMainTraining:
				print("Saving current model...")
				max_gA_test = gA_test
				model_ = tbsm
				torch.save(
					{
						"model_state_dict": model_.state_dict(),
						# "opt_state_dict": optimizer.state_dict(),
					},
					args.save_model,
				)
			
			print("Test_Iteration ", (k * nbatches) + (j+1))
			print("Total_Iterations ", nbatches)
			print("Test_Loss ", gL_test)
			print("Test_Accuracy ", gA_test * 100)
			print("Best_test_Accuracy ", max_gA_test * 100)
			test_auc = iterate_test_data(test_ld, n_test, tbsm, use_gpu, device)
			print("Test_AUC ", test_auc)
			print("\n")

			test_accuracy_numbers.append([(k * nbatches) + (j+1), gA_test, gL_test, test_auc])

	print("Total_Fwd_Time ", forward_time, " s")
	print("Total_Bwd_Time ", backward_time, " s")
	print("Total_Opt_Time ", optimizer_time, " s")

	csv_header = ['Test_Iteration', 'Test_Accuracy', 'Test_Loss', 'Test_AUC']

	with open(args.output_csv_file, 'w') as csvfile:
		csv_writer = csv.writer(csvfile)
		csv_writer.writerow(csv_header)
		for i in range(len(test_accuracy_numbers)):
			csv_writer.writerow(test_accuracy_numbers[i])

	if not isMainTraining:
		return gA_test

# selects best seed, and does main model training
def train_tbsm(args, use_gpu):
	# prepare the data
	train_ld, _ = tp.make_tbsm_data_and_loader(args, "train")
	val_ld, _ = tp.make_tbsm_data_and_loader(args, "val")
	test_ld, n_test = tp.make_tbsm_data_and_loader(args, "test")

	# setup initial values
	isMainTraining = False
	#writer = SummaryWriter()
	writer = 0
	losses = np.empty((0,3), np.float32)
	accuracies = np.empty((0,3), np.float32)

	# selects best seed out of 5. Sometimes Adagrad gets stuck early, this
	# seems to occur randomly depending on initial weight values and
	# is independent of chosen model: N-inner, dot etc.
	# this procedure is used to reduce the probability of this happening.
	def select(args):

		seeds = np.random.randint(2, 10000, size=5)
		if args.debug_mode:
			print(seeds)
		best_index = 0
		max_val_accuracy = 0.0
		testpoint = min(int(0.05 * len(train_ld)), len(train_ld) - 1)
		print("testpoint, total batches: ", testpoint, len(train_ld))

		for i, seed in enumerate(seeds):

			set_seed(seed, use_gpu)
			tbsm, device = get_tbsm(args, use_gpu)

			gA_test = iterate_train_data(args, train_ld, val_ld, tbsm, 0, use_gpu,
										 device, writer, losses, accuracies,
										 isMainTraining)

			if args.debug_mode:
				print("select: ", i, seed, gA_test, max_val_accuracy)
			if gA_test > max_val_accuracy:
				best_index = i
				max_val_accuracy = gA_test

		return seeds[best_index]

	# select best seed if needed
	seed = args.numpy_rand_seed

	# create or load TBSM
	tbsm, device = get_tbsm(args, use_gpu)
	if args.debug_mode:
		print("initial parameters (weights and bias):")
		for name, param in tbsm.named_parameters():
			print(name)
			print(param.detach().cpu().numpy())

	# main training loop
	isMainTraining = True
	print("time/loss/accuracy (if enabled):")
	for k in range(args.nepochs):
		iterate_train_data(args, train_ld, val_ld, test_ld, n_test, tbsm, k, use_gpu, device,
		writer, losses, accuracies, isMainTraining)

	# collect metrics and other statistics about the run
	if args.enable_summary:
		with open('summary.npy', 'wb') as acc_loss:
			np.save(acc_loss, losses)
			np.save(acc_loss, accuracies)
		writer.close()

	# debug prints
	if args.debug_mode:
		print("final parameters (weights and bias):")
		for name, param in tbsm.named_parameters():
			print(name)
			print(param.detach().cpu().numpy())

	return

# evaluates model on test data and computes AUC metric
def test_tbsm(args, use_gpu):
	# prepare data
	test_ld, N_test = tp.make_tbsm_data_and_loader(args, "test")

	# setup initial values
	z_test = np.zeros((len(N_test), ), dtype=np.float)
	t_test = np.zeros((len(N_test), ), dtype=np.float)

	# check saved model exists
	if not path.exists(args.save_model):
		sys.exit("Can't find saved model. Exiting...")

	# create or load TBSM
	tbsm, device = get_tbsm(args, use_gpu)
	print(args.save_model)
	
	offset = 0
	for _, (X, lS_o, lS_i, T) in enumerate(test_ld):

		batchSize = X[0].shape[0]
		
		Z = tbsm(*data_wrap(X,
			lS_o,
			lS_i,
			use_gpu,
			device
		))
		
		z_test[offset: offset + batchSize] = np.squeeze(Z.detach().cpu().numpy(),
		axis=1)
		t_test[offset: offset + batchSize] = np.squeeze(T.detach().cpu().numpy(),
		axis=1)
		offset += batchSize

	if args.quality_metric == "auc":
		# compute AUC metric
		auc_score = 100.0 * roc_auc_score(t_test.astype(int), z_test)
		print("auc score: ", auc_score)
	else:
		sys.exit("Metric not supported.")


if __name__ == "__main__":
	### import packages ###

	import sys
	import argparse

	### parse arguments ###
	parser = argparse.ArgumentParser(description="Time Based Sequence Model (TBSM)")
	# path to dlrm
	parser.add_argument("--dlrm-path", type=str, default="")
	# data type: taobao or synthetic (generic)
	parser.add_argument("--datatype", type=str, default="synthetic")
	# mode: train or inference or both
	parser.add_argument("--mode", type=str, default="train")   # train, test, train-test
	# data locations
	parser.add_argument("--raw-train-file", type=str, default="./input/train.txt")
	parser.add_argument("--pro-train-file", type=str, default="./output/train.npz")
	parser.add_argument("--raw-test-file", type=str, default="./input/test.txt")
	parser.add_argument("--pro-test-file", type=str, default="./output/test.npz")
	parser.add_argument("--pro-val-file", type=str, default="./output/val.npz")
	parser.add_argument("--num-train-pts", type=int, default=100)
	parser.add_argument("--num-val-pts", type=int, default=20)
	# time series length for train/val and test
	parser.add_argument("--ts-length", type=int, default=20)
	# model_type = "tsl", "mha", "rnn", "transformer"
	parser.add_argument("--model-type", type=str, default="tsl")  # tsl, mha, rnn
	parser.add_argument("--tsl-seq", action="store_true", default=False)  # k-seq method
	parser.add_argument("--tsl-proj", action="store_true", default=True)  # sphere proj
	parser.add_argument("--tsl-inner", type=str, default="def")   # ind, def, dot
	parser.add_argument("--tsl-num-heads", type=int, default=1)   # num tsl components
	parser.add_argument("--mha-num-heads", type=int, default=8)   # num mha heads
	parser.add_argument("--rnn-num-layers", type=int, default=5)  # num rnn layers
	# ========================= Transformers Parameters ========================
	parser.add_argument("--tra-encoder-layers", type=int, default=1)
	parser.add_argument("--tra-attention-heads", type=int, default=2)
	parser.add_argument("--tra-feedforward-dim", type=int, default=512)
	parser.add_argument("--tra-norm-first", type=bool, default=False)
	parser.add_argument("--tra-activation", type=str, default="relu")
	parser.add_argument("--tra-dropout", type=float, default=0.1)
	# ==========================================================================

	# num positive (and negative) points per user
	parser.add_argument("--points-per-user", type=int, default=10)
	# model arch related parameters
	# embedding dim for all sparse features (same for all features)
	parser.add_argument("--arch-sparse-feature-size", type=int, default=4)  # emb_dim
	# number of distinct values for each sparse feature
	parser.add_argument("--arch-embedding-size", type=str, default="4-3-2")  # vectors
	# for taobao use "987994-4162024-9439")
	# MLP 1: num dense fea --> embedding dim for sparse fea (out_dim enforced)
	parser.add_argument("--arch-mlp-bot", type=str, default="1-4")
	# MLP 2: num_interactions + bot[-1] --> top[-1]
	# (in_dim adjusted, out_dim can be any)
	parser.add_argument("--arch-mlp-top", type=str, default="2-2")
	# MLP 3: attention: ts_length --> ts_length (both adjusted)
	parser.add_argument("--tsl-mlp", type=str, default="2-2")
	# MLP 4: final prob. of click: 2 * top[-1] --> [0,1] (in_dim adjusted)
	parser.add_argument("--arch-mlp", type=str, default="4-1")
	# interactions
	parser.add_argument("--arch-interaction-op", type=str, default="dot")
	# ========================= DCN_v2 specifications ========================
	parser.add_argument("--dcn_num_layers", type=int, default=2)
	parser.add_argument("--dcn_low_rank_dim", type=int, default=16)
	# ======================= Projection specifications ======================
	parser.add_argument("--interaction_branch1_layer_sizes", type=str, default="128-128")
	parser.add_argument("--interaction_branch2_layer_sizes", type=str, default="128-128")
	# ======================= AutoInt specifications ======================
	parser.add_argument("--mha_intr_num_heads", type=int, default=2)   # num mha heads
	# ========================= Transformers Parameters ========================
	parser.add_argument("--num-encoder-layers", type=int, default=1)
	parser.add_argument("--num-attention-heads", type=int, default=8)
	parser.add_argument("--feedforward-dim", type=int, default=2048)
	parser.add_argument("--norm-first", type=bool, default=False)
	parser.add_argument("--activation", type=str, default="relu")
	parser.add_argument("--dropout", type=float, default=0.1)
	parser.add_argument("--mask-threshold", type=float, default=0.01)
	# ==========================================================================
	parser.add_argument("--arch-interaction-itself", action="store_true", default=False)
	parser.add_argument("--tsl-interaction-op", type=str, default="dot")
	parser.add_argument("--tsl-mechanism", type=str, default="mlp")  # mul or MLP
	# data
	parser.add_argument("--num-batches", type=int, default=0)
	# training
	parser.add_argument("--mini-batch-size", type=int, default=1)
	parser.add_argument("--nepochs", type=int, default=1)
	parser.add_argument("--learning-rate", type=float, default=0.05)
	parser.add_argument("--print-precision", type=int, default=5)
	parser.add_argument("--numpy-rand-seed", type=int, default=123)
	parser.add_argument("--no-select-seed", action="store_true", default=False)
	# inference
	parser.add_argument("--quality-metric", type=str, default="auc")
	parser.add_argument("--test-freq", type=int, default=0)
	parser.add_argument("--inference-only", type=bool, default=False)
	# saving model
	parser.add_argument("--save-model", type=str, default="./output/model.pt")
	# gpu
	parser.add_argument("--use-gpu", action="store_true", default=False)
	parser.add_argument("--device-num", type=int, default=0)
	# debugging and profiling
	parser.add_argument("--debug-mode", action="store_true", default=False)
	parser.add_argument("--print-freq", type=int, default=1)
	parser.add_argument("--print-time", action="store_true", default=False)
	parser.add_argument("--enable-summary", action="store_true", default=False)
	parser.add_argument("--enable-profiling", action="store_true", default=False)
	parser.add_argument("--profiling-file", type=str, default="")
	parser.add_argument("--output-csv-file", type=str, default="./output.csv")
	args = parser.parse_args()

	# the code requires access to dlrm model
	if not path.exists(str(args.dlrm_path)):
		sys.exit("Please provide path to DLRM as --dlrm-path")
	sys.path.insert(1, args.dlrm_path)
	import MTRec_wo_LN_non_seq as dlrm

	if args.datatype == "taobao" and args.arch_embedding_size != "987994-4162024-9439":
		sys.exit(
			"ERROR: arch-embedding-size for taobao "
			+ " needs to be 987994-4162024-9439"
		)
	if args.tsl_inner not in ["def", "ind"] and int(args.tsl_num_heads) > 1:
		 sys.exit(
			"ERROR: dot product "
			+ " assumes one tsl component (due to redundancy)"
		)

	# model_type = "tsl", "mha", "rnn"
	print("dlrm path: ", args.dlrm_path)
	print("model_type: ", args.model_type)
	print("time series length: ", args.ts_length)
	print("seed: ", args.numpy_rand_seed)
	print("model_file:", args.save_model)

	### some basic setup ###
	use_gpu = args.use_gpu and torch.cuda.is_available()
	set_seed(args.numpy_rand_seed, use_gpu)
	np.set_printoptions(precision=args.print_precision)
	torch.set_printoptions(precision=args.print_precision)
	print("use-gpu:", use_gpu)

	# possible modes:
	# "train-test" for both training and metric computation on test data,
	# "train"      for training model
	# "test"       for metric computation on test data using saved trained model
	modes = args.mode.split("-")
	if modes[0] == "train":
		train_tbsm(args, use_gpu)
	if modes[0] == "test" or (len(modes) > 1 and modes[1] == "test"):
		test_tbsm(args, use_gpu)
