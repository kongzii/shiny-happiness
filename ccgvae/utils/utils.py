#!/usr/bin/env/python
import pickle
import queue
import threading
from collections import deque
from threading import Thread

from functools import lru_cache
import numpy as np
import tensorflow as tf
from rdkit import Chem
from rdkit.Chem import rdmolops

SMALL_NUMBER = 1e-7
LARGE_NUMBER = 1e10

geometry_numbers = [3, 4, 5, 6]  # triangle, square, pentagon, hexagon

# bond mapping
bond_dict = {"SINGLE": 0, "DOUBLE": 1, "TRIPLE": 2, "AROMATIC": 3}
number_to_bond = {
    0: Chem.rdchem.BondType.SINGLE,
    1: Chem.rdchem.BondType.DOUBLE,
    2: Chem.rdchem.BondType.TRIPLE,
    3: Chem.rdchem.BondType.AROMATIC,
}


def read_our_data(path):
    smiles_train = [line.strip() for line in open(path + "/train.smi") if line.strip()]
    smiles_valid = [line.strip() for line in open(path + "/valid.smi") if line.strip()]
    smiles_test = [line.strip() for line in open(path + "/test.smi") if line.strip()]
    return {
        "train": smiles_train,
        "valid": smiles_valid,
        "test": smiles_test,
    }


def build_dataset_info(data, size):
    size = int(size)
    atom_types = set()
    atom_to_maximum_valence = {}

    for split, split_smiles in data.items():
        for i, smiles in enumerate(split_smiles):
            mol = Chem.MolFromSmiles(smiles)
            atoms = mol.GetAtoms()
            for atom in atoms:
                valence = atom.GetTotalValence()
                symbol = atom.GetSymbol()
                charge = atom.GetFormalCharge()
                atom_str = "%s%i(%i)" % (symbol, valence, charge)
                atom_types.add(atom_str)
                atom_to_maximum_valence[atom_str] = max(
                    atom_to_maximum_valence.get(atom_str, 0), valence
                )

    atom_types = sorted(atom_types)
    number_to_atom = {
        i: atom[0] for i, atom in enumerate(atom_types)
    }  # [0] to test number_to_symbol
    max_n_atoms = len(number_to_atom)

    info = {
        "atom_types": atom_types,
        "number_to_atom": number_to_atom,
        "maximum_valence": {
            atom_types.index(atom): maxval
            for atom, maxval in atom_to_maximum_valence.items()
        },
        "max_valence_value": sum(atom_to_maximum_valence.values()) + 1,
        "max_n_atoms": max_n_atoms,
        "hist_dim": max_n_atoms + 1,
        "n_valence": max(atom_to_maximum_valence.values()),
        "bucket_sizes": np.array([max_n_atoms - 2, max_n_atoms - 1]),
    }
    print(info)
    return info


@lru_cache(maxsize=3)
def dataset_info(dataset):
    if "size_" in dataset:
        size = int(dataset.split("size_")[1].strip("/"))
        data = read_our_data(dataset)
        dataset_info = build_dataset_info(data, size)
        return dataset_info

    elif dataset == "qm9":
        return {
            "atom_types": ["H", "C", "N", "O", "F"],
            "maximum_valence": {0: 1, 1: 4, 2: 3, 3: 2, 4: 1},
            "hist_dim": 4,
            "max_valence_value": 9,
            "max_n_atoms": 30,
            "number_to_atom": {0: "H", 1: "C", 2: "N", 3: "O", 4: "F"},
            "bucket_sizes": np.array(list(range(4, 28, 2)) + [50]),
        }
    elif dataset == "zinc":
        return {
            "atom_types": [
                "Br1(0)",
                "C4(0)",
                "Cl1(0)",
                "F1(0)",
                "H1(0)",
                "I1(0)",
                "N2(-1)",
                "N3(0)",
                "N4(1)",
                "O1(-1)",
                "O2(0)",
                "S2(0)",
                "S4(0)",
                "S6(0)",
            ],
            "maximum_valence": {
                0: 1,
                1: 4,
                2: 1,
                3: 1,
                4: 1,
                5: 1,
                6: 2,
                7: 3,
                8: 4,
                9: 1,
                10: 2,
                11: 2,
                12: 4,
                13: 6,
            },
            "hist_dim": 6,
            "n_valence": 6,
            "max_valence_value": 34,
            "max_n_atoms": 85,
            "number_to_atom": {
                0: "Br",
                1: "C",
                2: "Cl",
                3: "F",
                4: "H",
                5: "I",
                6: "N",
                7: "N",
                8: "N",
                9: "O",
                10: "O",
                11: "S",
                12: "S",
                13: "S",
            },
            "bucket_sizes": np.array(
                [
                    28,
                    31,
                    33,
                    35,
                    37,
                    38,
                    39,
                    40,
                    41,
                    42,
                    43,
                    44,
                    45,
                    46,
                    47,
                    48,
                    49,
                    50,
                    51,
                    53,
                    55,
                    58,
                    84,
                ]
            ),
        }
    else:
        print(
            "Error: The datasets that you could use are QM9 or ZINC, not "
            + str(dataset)
        )
        exit(1)


# add one edge to adj matrix
def add_edge_mat(amat, src, dest, e, considering_edge_type=True):
    if considering_edge_type:
        amat[e, dest, src] = 1
        amat[e, src, dest] = 1
    else:
        amat[src, dest] = 1
        amat[dest, src] = 1


def graph_to_adj_mat(graph, max_n_vertices, num_edge_types, considering_edge_type=True):
    if considering_edge_type:
        amat = np.zeros((num_edge_types, max_n_vertices, max_n_vertices))
        for src, e, dest in graph:
            add_edge_mat(amat, src, dest, e)
    else:
        amat = np.zeros((max_n_vertices, max_n_vertices))
        for src, e, dest in graph:
            add_edge_mat(amat, src, dest, e, considering_edge_type=False)
    return amat


# generates one hot vector
def onehot(idx, len):
    z = [0 for _ in range(len)]
    z[idx] = 1
    return z


# standard normal with shape [a1, a2, a3]
def generate_std_normal(a1, a2, a3):
    return np.random.normal(0, 1, [a1, a2, a3])


# Get length for each graph based on node masks
def get_graph_length(all_node_mask):
    all_lengths = []
    for graph in all_node_mask:
        if 0 in graph:
            length = np.argmin(graph)
        else:
            length = len(graph)
        all_lengths.append(length)
    return all_lengths


# sample node symbols based on node predictions
def sample_node_symbol(all_node_symbol_prob, all_lengths, dataset):
    all_node_symbol = []
    for graph_idx, graph_prob in enumerate(all_node_symbol_prob):
        node_symbol = []
        for node_idx in range(all_lengths[graph_idx]):
            symbol = np.random.choice(
                np.arange(len(dataset_info(dataset)["atom_types"])),
                p=graph_prob[node_idx],
            )
            node_symbol.append(symbol)
        all_node_symbol.append(node_symbol)
    return all_node_symbol


# sample node symbols based on node predictions
def sample_argmax_node_symbol(all_node_symbol_prob, all_lengths, dataset):
    all_node_symbol = []
    for graph_idx, graph_prob in enumerate(all_node_symbol_prob):
        node_symbol = []
        for node_idx in range(all_lengths[graph_idx]):
            symbol = np.arange(len(dataset_info(dataset)["atom_types"]))[
                np.argmax(graph_prob[node_idx])
            ]
            node_symbol.append(symbol)
        all_node_symbol.append(node_symbol)
    return all_node_symbol


# generate a new feature on whether adding the edges will generate more than two overlapped edges for rings
def get_overlapped_edge_feature(edge_mask, color, new_mol):
    overlapped_edge_feature = []
    for node_in_focus, neighbor in edge_mask:
        if color[neighbor] == 1:
            # attempt to add the edge
            new_mol.AddBond(int(node_in_focus), int(neighbor), number_to_bond[0])
            # Check whether there are two cycles having more than two overlap edges
            try:
                ssr = Chem.GetSymmSSSR(new_mol)  # smallest set of smallest rings
            except:
                ssr = []
            overlap_flag = False
            for idx1 in range(len(ssr)):
                for idx2 in range(idx1 + 1, len(ssr)):
                    if len(set(ssr[idx1]) & set(ssr[idx2])) > 2:
                        overlap_flag = True
            # remove that edge
            new_mol.RemoveBond(int(node_in_focus), int(neighbor))
            if overlap_flag:
                overlapped_edge_feature.append((node_in_focus, neighbor))
    return overlapped_edge_feature


# adj_list [3, v, v] or defaultdict. bfs distance on a graph
def bfs_distance(start, adj_list, is_dense=False):
    distances = {}
    visited = set()
    queue = deque([(start, 0)])
    visited.add(start)
    while len(queue) != 0:
        current, d = queue.popleft()
        for neighbor, edge_type in adj_list[current]:
            if neighbor not in visited:
                distances[neighbor] = d + 1
                visited.add(neighbor)
                queue.append((neighbor, d + 1))
    return [(start, node, d) for node, d in distances.items()]


def get_initial_valence(node_symbol, dataset):
    return [dataset_info(dataset)["maximum_valence"][s] for s in node_symbol]


def add_atoms(new_mol, node_symbol, dataset):
    for number in node_symbol:
        if dataset == "qm9" or dataset == "cep":
            idx = new_mol.AddAtom(
                Chem.Atom(dataset_info(dataset)["number_to_atom"][number])
            )
        elif dataset == "zinc" or "size_" in dataset:
            new_atom = Chem.Atom(dataset_info(dataset)["number_to_atom"][number])
            charge_num = int(
                dataset_info(dataset)["atom_types"][number].split("(")[1].strip(")")
            )
            new_atom.SetFormalCharge(charge_num)
            new_mol.AddAtom(new_atom)


def get_idx_of_largest_frag(frags):
    return np.argmax([len(frag) for frag in frags])


def remove_extra_nodes(new_mol):
    frags = Chem.rdmolops.GetMolFrags(new_mol)
    while len(frags) > 1:
        # Get the idx of the frag with largest length
        largest_idx = get_idx_of_largest_frag(frags)
        for idx in range(len(frags)):
            if idx != largest_idx:
                # Remove one atom that is not in the largest frag
                new_mol.RemoveAtom(frags[idx][0])
                break
        frags = Chem.rdmolops.GetMolFrags(new_mol)


def need_kekulize(mol):
    for bond in mol.GetBonds():
        if bond_dict[str(bond.GetBondType())] >= 3:
            return True
    return False


def to_graph(smiles, dataset):
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return [], []
    # Kekulize it
    if need_kekulize(mol):
        rdmolops.Kekulize(mol)
        if mol is None:
            return None, None
    # remove stereo information, such as inward and outward edges
    Chem.RemoveStereochemistry(mol)

    edges = []
    nodes = []
    for bond in mol.GetBonds():
        edges.append(
            (
                bond.GetBeginAtomIdx(),
                bond_dict[str(bond.GetBondType())],
                bond.GetEndAtomIdx(),
            )
        )
        assert bond_dict[str(bond.GetBondType())] != 3
    for atom in mol.GetAtoms():
        if dataset == "qm9":
            nodes.append(
                onehot(
                    dataset_info(dataset)["atom_types"].index(atom.GetSymbol()),
                    len(dataset_info(dataset)["atom_types"]),
                )
            )
        else:  # transform using "<atom_symbol><valence>(<charge>)"  notation
            symbol = atom.GetSymbol()
            valence = atom.GetTotalValence()
            charge = atom.GetFormalCharge()
            atom_str = "%s%i(%i)" % (symbol, valence, charge)

            if atom_str not in dataset_info(dataset)["atom_types"]:
                print("Unrecognized atom type %s" % atom_str)
                return [], []

            nodes.append(
                onehot(
                    dataset_info(dataset)["atom_types"].index(atom_str),
                    len(dataset_info(dataset)["atom_types"]),
                )
            )

    return nodes, edges


def shape_count(dataset, remove_print=False, all_smiles=None):
    if all_smiles == None:
        with open("generated_smiles_%s.txt" % dataset, "rb") as f:
            all_smiles = set(pickle.load(f))

    geometry_counts = [0] * len(geometry_numbers)
    geometry_counts_per_molecule = []  # record the geometry counts for each molecule
    for smiles in all_smiles:
        nodes, edges = to_graph(smiles, dataset)
        if len(edges) <= 0:
            continue
        new_mol = Chem.MolFromSmiles(smiles)

        ssr = Chem.GetSymmSSSR(new_mol)
        counts_for_molecule = [0] * len(geometry_numbers)
        for idx in range(len(ssr)):
            ring_len = len(list(ssr[idx]))
            if ring_len in geometry_numbers:
                geometry_counts[geometry_numbers.index(ring_len)] += 1
                counts_for_molecule[geometry_numbers.index(ring_len)] += 1
        geometry_counts_per_molecule.append(counts_for_molecule)

    return len(all_smiles), geometry_counts, geometry_counts_per_molecule


def check_adjacent_sparse(adj_list, node, neighbor_in_doubt):
    for neighbor, edge_type in adj_list[node]:
        if neighbor == neighbor_in_doubt:
            return True, edge_type
    return False, None


def glorot_init(shape):
    initialization_range = np.sqrt(6.0 / (shape[-2] + shape[-1]))
    return np.random.uniform(
        low=-initialization_range, high=initialization_range, size=shape
    ).astype(np.float32)


class ThreadedIterator:
    """An iterator object that computes its elements in a parallel thread to be ready to be consumed.
    The iterator should *not* return None"""

    def __init__(self, original_iterator, max_queue_size: int = 2):
        self.__queue = queue.Queue(maxsize=max_queue_size)
        self.__thread = threading.Thread(target=lambda: self.worker(original_iterator))
        self.__thread.start()

    def worker(self, original_iterator):
        for element in original_iterator:
            assert (
                element is not None
            ), "By convention, iterator elements must not be None"
            self.__queue.put(element, block=True)
        self.__queue.put(None, block=True)

    def __iter__(self):
        next_element = self.__queue.get(block=True)
        while next_element is not None:
            yield next_element
            next_element = self.__queue.get(block=True)
        self.__thread.join()


# Implements multilayer perceptron
class MLP(object):
    def __init__(self, in_size, out_size, hid_sizes, dropout_keep_prob):
        self.in_size = in_size
        self.out_size = out_size
        self.hid_sizes = hid_sizes
        self.dropout_keep_prob = dropout_keep_prob
        self.params = self.make_network_params()

    def make_network_params(self):
        dims = [self.in_size] + self.hid_sizes + [self.out_size]
        weight_sizes = list(zip(dims[:-1], dims[1:]))
        weights = [
            tf.Variable(self.init_weights(s), name="MLP_W_layer%i" % i)
            for (i, s) in enumerate(weight_sizes)
        ]
        biases = [
            tf.Variable(np.zeros(s[-1]).astype(np.float32), name="MLP_b_layer%i" % i)
            for (i, s) in enumerate(weight_sizes)
        ]

        network_params = {
            "weights": weights,
            "biases": biases,
        }

        return network_params

    def init_weights(self, shape):
        return np.sqrt(6.0 / (shape[-2] + shape[-1])) * (
            2 * np.random.rand(*shape).astype(np.float32) - 1
        )

    def __call__(self, inputs):
        acts = inputs
        for W, b in zip(self.params["weights"], self.params["biases"]):
            hid = tf.matmul(acts, tf.nn.dropout(W, self.dropout_keep_prob)) + b
            acts = tf.nn.relu(hid)
        last_hidden = hid
        return last_hidden


class Graph:
    def __init__(self, V, g):
        self.V = V
        self.graph = g

    def addEdge(self, v, w):
        # Add w to v ist.
        self.graph[v].append(w)
        # Add v to w list.
        self.graph[w].append(v)

        # A recursive function that uses visited[]

    # and parent to detect cycle in subgraph
    # reachable from vertex v.
    def isCyclicUtil(self, v, visited, parent):
        # Mark current node as visited
        visited[v] = True

        # Recur for all the vertices adjacent
        # for this vertex
        for i in self.graph[v]:
            # If an adjacent is not visited,
            # then recur for that adjacent
            if visited[i] == False:
                if self.isCyclicUtil(i, visited, v) == True:
                    return True

            # If an adjacent is visited and not
            # parent of current vertex, then there
            # is a cycle.
            elif i != parent:
                return True

        return False

    # Returns true if the graph is a tree,
    # else false.
    def isTree(self):
        # Mark all the vertices as not visited
        # and not part of recursion stack
        visited = [False] * self.V

        # The call to isCyclicUtil serves multiple
        # purposes. It returns true if graph reachable
        # from vertex 0 is cyclcic. It also marks
        # all vertices reachable from 0.
        if self.isCyclicUtil(0, visited, -1) == True:
            return False

        # If we find a vertex which is not reachable
        # from 0 (not marked by isCyclicUtil(),
        # then we return false
        for i in range(self.V):
            if visited[i] == False:
                return False

        return True


# select the best based on shapes and probs
def select_best(all_mol):
    extracted = []
    for i in range(len(all_mol)):
        extracted.append((all_mol[i][0], all_mol[i][1], i))

    extracted = sorted(extracted)
    return all_mol[extracted[-1][2]][2]


# a series util function converting sparse matrix representation to dense
def incre_adj_mat_to_dense(incre_adj_mat, num_edge_types, maximum_vertice_num):
    new_incre_adj_mat = []
    for sparse_incre_adj_mat in incre_adj_mat:
        dense_incre_adj_mat = np.zeros(
            (num_edge_types, maximum_vertice_num, maximum_vertice_num)
        )
        for current, adj_list in sparse_incre_adj_mat.items():
            for neighbor, edge_type in adj_list:
                dense_incre_adj_mat[edge_type][current][neighbor] = 1
        new_incre_adj_mat.append(dense_incre_adj_mat)
    return new_incre_adj_mat  # [number_iteration,num_edge_types,maximum_vertice_num, maximum_vertice_num]


def distance_to_others_dense(distance_to_others, maximum_vertice_num):
    new_all_distance = []
    for sparse_distances in distance_to_others:
        dense_distances = np.zeros((maximum_vertice_num), dtype=int)
        for x, y, d in sparse_distances:
            dense_distances[y] = d
        new_all_distance.append(dense_distances)
    return new_all_distance  # [number_iteration, maximum_vertice_num]


def overlapped_edge_features_to_dense(overlapped_edge_features, maximum_vertice_num):
    new_overlapped_edge_features = []
    for sparse_overlapped_edge_features in overlapped_edge_features:
        dense_overlapped_edge_features = np.zeros((maximum_vertice_num), dtype=int)
        for node_in_focus, neighbor in sparse_overlapped_edge_features:
            dense_overlapped_edge_features[neighbor] = 1
        new_overlapped_edge_features.append(dense_overlapped_edge_features)
    return new_overlapped_edge_features  # [number_iteration, maximum_vertice_num]


def node_sequence_to_dense(node_sequence, maximum_vertice_num):
    new_node_sequence = []
    for node in node_sequence:
        s = [0] * maximum_vertice_num
        s[node] = 1
        new_node_sequence.append(s)
    return new_node_sequence  # [number_iteration, maximum_vertice_num]


def edge_type_masks_to_dense(edge_type_masks, maximum_vertice_num, num_edge_types):
    new_edge_type_masks = []
    for mask_sparse in edge_type_masks:
        mask_dense = np.zeros([num_edge_types, maximum_vertice_num])
        for node_in_focus, neighbor, bond in mask_sparse:
            mask_dense[bond][neighbor] = 1
        new_edge_type_masks.append(mask_dense)
    return new_edge_type_masks  # [number_iteration, 3, maximum_vertice_num]


def edge_type_labels_to_dense(edge_type_labels, maximum_vertice_num, num_edge_types):
    new_edge_type_labels = []
    for labels_sparse in edge_type_labels:
        labels_dense = np.zeros([num_edge_types, maximum_vertice_num])
        for node_in_focus, neighbor, bond in labels_sparse:
            labels_dense[bond][neighbor] = 1 / float(
                len(labels_sparse)
            )  # fix the probability bug here.
        new_edge_type_labels.append(labels_dense)
    return new_edge_type_labels  # [number_iteration, 3, maximum_vertice_num]


def edge_masks_to_dense(edge_masks, maximum_vertice_num):
    new_edge_masks = []
    for mask_sparse in edge_masks:
        mask_dense = [0] * maximum_vertice_num
        for node_in_focus, neighbor in mask_sparse:
            mask_dense[neighbor] = 1
        new_edge_masks.append(mask_dense)
    return new_edge_masks  # [number_iteration, maximum_vertice_num]


def edge_labels_to_dense(edge_labels, maximum_vertice_num):
    new_edge_labels = []
    for label_sparse in edge_labels:
        label_dense = [0] * maximum_vertice_num
        for node_in_focus, neighbor in label_sparse:
            label_dense[neighbor] = 1 / float(len(label_sparse))
        new_edge_labels.append(label_dense)
    return new_edge_labels  # [number_iteration, maximum_vertice_num]


class ThreadWithReturnValue(object):
    def __init__(self, target=None, args=(), **kwargs):
        self._que = queue.Queue()
        self._t = Thread(
            target=lambda q, arg1, kwargs1: q.put(target(*arg1, **kwargs1)),
            args=(self._que, args, kwargs),
        )
        self._t.start()

    def join(self):
        self._t.join()
        return self._que.get()
