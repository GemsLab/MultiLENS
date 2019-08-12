import sys
import datetime
from pathlib import Path
import numpy as np, scipy as sp, networkx as nx
import math, time, os, sys, random
from collections import deque
import pickle
import argparse

import scipy.sparse as sps
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import svds, eigs
import sparsesvd

from sklearn.decomposition import NMF, DictionaryLearning
from sklearn.manifold import TSNE

from collections import defaultdict

from util import *

def get_combined_feature_sequence(graph, rep_method, current_node, input_dense_matrix = None, feature_wid_ind = None):
	'''
	Get the combined degree/other feature sequence for a given node
	'''
	N, cur_P = input_dense_matrix.shape

	id_cat_dict = graph.id_cat_dict
	combined_feature_vector = []
	cur_neighbors = graph.neighbor_list[current_node][:]
	cur_neighbors.append(current_node)
	
	for cat in graph.cat_dict.keys():

		features = []
		for i in range(cur_P):
			features.append([0.0] * feature_wid_ind[i])

		for neighbor in cur_neighbors:

			if id_cat_dict[neighbor] != cat:
				continue			

			try:
				for i in range(cur_P):
					node_feature = input_dense_matrix[neighbor, i]

					if (rep_method.num_buckets is not None) and (node_feature != 0):
						bucket_index = int(math.log(node_feature, rep_method.num_buckets))
					else:
						bucket_index = int(node_feature)

					features[i][min(bucket_index, len(features[i]) - 1)] += 1#(rep_method.alpha ** layer) * weight
			except Exception as e:
				print "Exception:", e
				print("Node %d has %s value %d and will not contribute to feature distribution" % (khop_neighbor, feature, node_feature))
		cur_feature_vector = features[0]
		
		for feature_vector in features[1:]:
			cur_feature_vector += feature_vector

		combined_feature_vector += cur_feature_vector
	
	return combined_feature_vector

def get_features(graph, rep_method, input_dense_matrix = None, nodes_to_embed = None):

	feature_wid_sum, feature_wid_ind = get_feature_n_buckets(input_dense_matrix, num_buckets, rep_method.bucket_max_value)
	feature_matrix = np.zeros([graph.num_nodes, feature_wid_sum * len(graph.unique_cat)])

	for n in nodes_to_embed:
		if n % 50000 == 0:
			print "[Generate combined feature vetor] node: " + str(n)
		combined_feature_sequence = get_combined_feature_sequence(graph, rep_method, n, input_dense_matrix = input_dense_matrix, feature_wid_ind = feature_wid_ind)
		feature_matrix[n,:] = combined_feature_sequence

	return feature_matrix


def get_seq_features(graph, rep_method, input_dense_matrix = None, nodes_to_embed = None):
	
	if input_dense_matrix is None:
		sys.exit('get_seq_features: no input matrix.')

	if nodes_to_embed is None:
		nodes_to_embed = range(graph.num_nodes)
		num_nodes = graph.num_nodes
	else:
		num_nodes = len(nodes_to_embed)
	
	feature_matrix = get_features(graph, rep_method, input_dense_matrix, nodes_to_embed)

	if graph.directed:
		print "[Starting to obtain features from in-components]"

		neighbor_list_r = construct_neighbor_list(graph.adj_matrix.transpose(), nodes_to_embed)

		indegree_graph = Graph(graph.adj_matrix.transpose(),  max_id = graph.max_id, num_nodes = graph.num_nodes, 
			directed = graph.directed, base_features = graph.base_features, neighbor_list = neighbor_list_r,
			cat_dict = graph.cat_dict, id_cat_dict = graph.id_cat_dict, unique_cat = graph.unique_cat, check_eq = graph.check_eq)
		base_feature_matrix_in = get_features(indegree_graph, rep_method, input_dense_matrix, nodes_to_embed = nodes_to_embed)

		feature_matrix = np.hstack((feature_matrix, base_feature_matrix_in))

	return feature_matrix


def construct_cat(input_gt_path, delimiter):
	'''
	# Input: per line, 1) cat-id_init, id_end or 2) cat-id
	'''
	result = defaultdict(set)
	id_cat_dict = dict()

	fIn = open(input_gt_path, 'r')
	lines = fIn.readlines()
	for line in lines:

		parts = line.strip('\r\n').split(delimiter)
		if len(parts) == 3:
			cat = parts[0]
			node_id_start = parts[1]
			node_id_end = parts[2]

			for i in range( int(node_id_start), int(node_id_end)+1 ):
				result[ int(cat) ].add( i )
				id_cat_dict[i] = int(cat)

		elif len(parts) == 2:
			cat = parts[0]
			node_id = parts[1]

			result[int(cat)].add( int(node_id) )
			id_cat_dict[int(node_id)] = int(cat)

		else:
			sys.exit('Cat file format not supported')

	fIn.close()
	return result, result.keys(), id_cat_dict


def search_feature_layer(graph, rep_method, base_feature_matrix = None):

	n,p = base_feature_matrix.shape
	result = np.zeros([n, p*rep_method.use_total])
	ops = rep_method.operators

	for u in range(n):
		if u % 50000 == 0:
			print '[Current_node_id] ' + str(u)

		neighbors = graph.neighbor_list[u]

		for fid in range(p):

			mean_v = 0.0; sum_v = 0.0; var_v = 0.0; max_v = 0.0; min_v = 0.0; sum_sq_diff = 0.0; prod_v = 1.0; L1_v = 0.0; L2_v = 0.0

			for v in neighbors:

				L1_v += abs(base_feature_matrix[u][fid] - base_feature_matrix[v][fid])	# L1
				diff = base_feature_matrix[u][fid] - base_feature_matrix[v][fid]
				L2_v += diff*diff	# L2
				sum_sq_diff += base_feature_matrix[v][fid] * base_feature_matrix[v][fid]     # var
				sum_v += base_feature_matrix[v][fid]  # used in sum and mean
				if max_v < base_feature_matrix[v][fid]:	# max
					max_v = base_feature_matrix[v][fid]
				if min_v > base_feature_matrix[v][fid]: # min
					min_v = base_feature_matrix[v][fid]

			deg = len(neighbors)
			if deg == 0:
				mean_v = 0
				var_v = 0
			else:
				mean_v = sum_v / float(deg)
				var_v = (sum_sq_diff / float(deg)) - (mean_v * mean_v) 

			temp_vec = [0.0] * rep_method.use_total
			
			for idx, op in enumerate(ops):
				if op == 'mean':
					temp_vec[idx] = mean_v
				elif op == 'var':
					temp_vec[idx] = var_v
				elif op == 'sum':
					temp_vec[idx] = sum_v
				elif op == 'max':
					temp_vec[idx] = max_v
				elif op == 'min':
					temp_vec[idx] = min_v
				elif op == 'L1':
					temp_vec[idx] = L1_v
				elif op == 'L2':
					temp_vec[idx] = L2_v
				else:
					sys.exit('[Unsupported operation]')

			result[u, fid*rep_method.use_total:(fid+1)*rep_method.use_total] = temp_vec

	return result


def construct_neighbor_list(adj_matrix, nodes_to_embed):
	result = {}

	for i in nodes_to_embed:
		result[i] = list(adj_matrix.getrow(i).nonzero()[1])

	return result


def get_init_features(graph, base_features, nodes_to_embed):
	'''
	# set fb: sum as default.
	'''
	init_feature_matrix = np.zeros((len(nodes_to_embed), len(base_features)))
	adj = graph.adj_matrix

	if "row_col" in base_features:
		init_feature_matrix[:,base_features.index("row_col")] = (adj.sum(axis=0).transpose() +  adj.sum(axis=1)).ravel()

	if "col" in base_features:
		init_feature_matrix[:,base_features.index("col")] = adj.sum(axis=0).transpose().ravel()

	if "row" in base_features:
		init_feature_matrix[:,base_features.index("row")] = adj.sum(axis=1).ravel()

	print '[Initial_feature_all finished]'
	return init_feature_matrix


def get_feature_n_buckets(feature_matrix, num_buckets, bucket_max_value):

	result_sum = 0
	result_ind = []
	N, cur_P = feature_matrix.shape

	if num_buckets is not None:
		for i in range(cur_P):
			temp = max(bucket_max_value, int(math.log(max(max(feature_matrix[:,i]), 1), num_buckets) + 1))
			n_buckets = temp
			# print max(feature_matrix[:,i])
			result_sum += n_buckets
			result_ind.append(n_buckets)
	else:
		for i in range(cur_P):
			temp = max(bucket_max_value, int( max(feature_matrix[:,i]) ) + 1)
			n_buckets = temp
			result_sum += n_buckets
			result_ind.append(n_buckets)

	return result_sum, result_ind



def parse_args():
	'''
	Parses the arguments.
	'''
	parser = argparse.ArgumentParser(description="Multi-Lens: Bridging Network Embedding and Summarization.")

	parser.add_argument('--input', nargs='?', default='../graph/test.tsv', help='Input graph file path')

	parser.add_argument('--cat', nargs='?', default='../graph/test_cat.tsv', help='Input node category file path')

	parser.add_argument('--summary', nargs='?', default='./latent_summary.pkl', help='Summary file path')

	parser.add_argument('--output', nargs='?', default='../emb/test_emb_ind.tsv', help='Embedding file path')

	parser.add_argument('--test', nargs='?', default='../emb/test_emb.tsv', help='Embedding file (old) path. This file is just used for testing.')

	parser.add_argument('--dim', type=int, default=128, help='Embedding dimension')

	parser.add_argument('--L', type=int, default=2, help='Subgraph level')

	parser.add_argument('--base', type=int, default=4, help='Base constant of logarithm histograms')

	parser.add_argument('--operators', default=['mean', 'var', 'sum', 'max', 'min', 'L1', 'L2'], nargs="+", help='Relational operators to use.')

	return parser.parse_args()


def dist_cal(row1, row2):
	dist = np.linalg.norm(row1-row2)

	return dist

if __name__ == '__main__':

	# assume the graph is directed, weighted
	directed = True

	args = parse_args()

	######################################################
	# Base features to use
	######################################################

	emb_write = True
	check_difference = False

	base_features = ['row', 'col', 'row_col']

	###########################################
	# graph_2_file_path
	###########################################
	input_graph_file_path = args.input
	input_gt_path = args.cat
	input_summary_path = args.summary
	input_emb_file_path = args.test

	output_file_path = args.output

	dim = args.dim
	L = args.L
	num_buckets = args.base
	op = args.operators
	print '----------------------------------'
	print '[Input graph (new) file] ' + input_graph_file_path
	print '[Input category file] ' + input_gt_path
	print '[Input summary (existing) file]' + input_summary_path
	print '[Output embedding file] ' + output_file_path
	print '[Embedding dimension] ' + str(dim)
	print '[Number of levels] ' + str(L)
	print '[Base of logarithm binning] ' + str(num_buckets)
	print '[Relational operators] ' + str(op)
	print '----------------------------------'


	pkl_file = open(input_summary_path, 'rb')
	g_summs = pickle.load(pkl_file)
	pkl_file.close()


	delimiter = get_delimiter(input_graph_file_path)


	raw = np.genfromtxt(input_graph_file_path, dtype=int)
	COL = raw.shape[1]

	if COL < 2:
		sys.exit('[Input format error.]')
	elif COL == 2:
		print '[unweighted graph detected.]'
		rows = raw[:,0]
		cols = raw[:,1]
		weis = np.ones(len(rows))

	elif COL == 3:
		print '[weighted graph detected.]'
		rows = raw[:,0]
		cols = raw[:,1]
		weis = raw[:,2]


	check_eq = True
	max_id = int(max(max(rows), max(cols)))
	num_nodes = max_id + 1
	print '[max_node_id] ' + str(max_id)
	print '[num_nodes] ' + str(num_nodes)

	nodes_to_embed = range(int(max_id)+1)

	if max(rows) != max(cols):
		rows = np.append(rows,max(max(rows), max(cols)))
		cols = np.append(cols,max(max(rows), max(cols)))
		weis = np.append(weis, 0)
		check_eq = False


	adj_matrix = sps.lil_matrix( sps.csc_matrix((weis, (rows, cols))) )

	CAT_DICT, unique_cat, ID_CAT_DICT = construct_cat(input_gt_path, delimiter)	

	g_sums = []

	neighbor_list = construct_neighbor_list(adj_matrix, nodes_to_embed)

	graph = Graph(adj_matrix = adj_matrix, max_id = max_id, num_nodes = num_nodes, base_features = base_features, neighbor_list = neighbor_list,
		directed = directed, cat_dict = CAT_DICT, id_cat_dict = ID_CAT_DICT, unique_cat = unique_cat, check_eq = check_eq)
	
	init_feature_matrix = get_init_features(graph, base_features, nodes_to_embed)

	rep_method = RepMethod(method = "hetero", bucket_max_value = 30, num_buckets = num_buckets, operators = op, use_total = len(op))
	

	######################################################
	# Step 1: get node embeddings from the summary
	# The number of layers we want to explore - 
	# layer 0 is the base feature matrix
	# layer 1+: are the layers of higher order
	############################################
	init_feature_matrix_seq = get_seq_features(graph, rep_method, input_dense_matrix = init_feature_matrix, nodes_to_embed = nodes_to_embed)
	init_gs = g_summs[0]
	print 'init_gs shape: ' + str(init_gs.shape)
	U = np.dot( init_feature_matrix_seq, np.linalg.pinv(init_gs) )


	feature_matrix = init_feature_matrix

	for i in range(L):
		print '[Current layer] ' + str(i)

		feature_matrix_new = search_feature_layer(graph, rep_method, base_feature_matrix = feature_matrix)

		feature_matrix_new_seq = get_seq_features(graph, rep_method, input_dense_matrix = feature_matrix_new, nodes_to_embed = nodes_to_embed)

		cur_gs = g_summs[i+1]
		print '[Summary shape] ' + str(cur_gs.shape)
		cur_U = np.dot( feature_matrix_new_seq, np.linalg.pinv(cur_gs) )
		U = np.concatenate((U, cur_U), axis=1)

		feature_matrix = feature_matrix_new


	######################################################
	# Step 2: evaluate the difference (optional)
	######################################################


	if check_difference:
		orig = read_embedding(input_emb_file_path)
		modi = U

		rows = orig[:,0]
		cols = orig[:,1]

		max_id_1 = orig.shape[0]
		max_id_2 = U.shape[0]
		max_id = min(max_id_1, max_id_2)

		ID_DIST_DICT = {}
		dist_total = 0

		for i in range(max_id):
			if i % 10000 == 0:
				print '[Current_node_id] ' + str(i)
			cur_row_orig = orig[i,:]
			cur_row_modi = modi[i,:]
			dist = dist_cal(cur_row_orig, cur_row_modi)

			ID_DIST_DICT[i] = dist
			dist_total += dist

		print '[total dist] ' + str(dist_total)


	if emb_write:
		write_embedding(U, output_file_path)

	

	



